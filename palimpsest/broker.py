# palimpsest/broker.py
import datetime
import fcntl
import json
import re
import shutil
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from palimpsest.config import load
from palimpsest.db import connect
from palimpsest.results import process_result

cfg = load()

# doc_id is interpolated into filesystem paths (raw/, ocr/, features/ dirs), so
# it must be a bare OSTI numeric id. Anything else (separators, "..", NUL, or
# Unicode "digits" that str.isdigit() would wrongly accept) is rejected before
# it can escape storage_root.
_DOC_ID_RE = re.compile(r"^[0-9]+$")


def validate_doc_id(doc_id: str) -> str:
    """Return doc_id if it is a safe bare numeric id, else raise HTTP 400.

    Args:
        doc_id: Candidate document identifier from a request or job row.

    Returns:
        The validated doc_id (unchanged).

    Raises:
        HTTPException: 400 if doc_id is not strictly ``[0-9]+``.
    """
    if not isinstance(doc_id, str) or not _DOC_ID_RE.match(doc_id):
        raise HTTPException(status_code=400, detail="Invalid document ID")
    return doc_id


# Body request models
class EnqueuePayload(BaseModel):
    type: str
    doc_id: str
    priority: int = 5
    payload: dict = {}


class LeasePayload(BaseModel):
    worker_id: str
    capabilities: List[str]
    max_jobs: int = 1


class HeartbeatPayload(BaseModel):
    worker_id: str
    job_ids: List[int]


class CompletePayload(BaseModel):
    worker_id: str
    job_id: int
    result: Any


class FailPayload(BaseModel):
    worker_id: str
    job_id: int
    error: str
    retryable: bool


class ReleasePayload(BaseModel):
    worker_id: str
    job_id: int


# Helper for UTC ISO-8601 timestamps
def utc_now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def reap_leases():
    """Find leased jobs whose leases have expired and return them to pending or dead.

    Tolerates a transiently locked database (SQLite WAL contention) by skipping
    the sweep instead of raising; the next tick retries.
    """
    conn = connect(cfg)
    now = utc_now_str()
    max_attempts = cfg.broker["max_attempts"]
    try:
        with conn:
            # Bulk set-based UPDATEs (no SELECT + per-row Python loop): minimises
            # the write-lock hold time and WAL contention. The two WHERE clauses
            # are disjoint on `attempts`, so order is irrelevant.
            conn.execute(
                "UPDATE jobs SET state='dead', error='Lease expired and max attempts exceeded', updated_at=? "
                "WHERE state='leased' AND lease_expires_at < ? AND attempts >= ?",
                (now, now, max_attempts),
            )
            conn.execute(
                "UPDATE jobs SET state='pending', updated_at=? "
                "WHERE state='leased' AND lease_expires_at < ? AND attempts < ?",
                (now, now, max_attempts),
            )
    except sqlite3.OperationalError as e:
        # "database is locked" / "database is busy" — transient; retry next tick.
        if "lock" in str(e).lower() or "busy" in str(e).lower():
            return
        raise
    finally:
        conn.close()


def revive_dead_jobs() -> int:
    """Reset jobs that have been dead longer than dead_retry_minutes back to pending.

    Returns count of revived jobs.
    """
    retry_minutes = cfg.broker.get("dead_retry_minutes", 30)
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=retry_minutes)
    ).isoformat()
    now = utc_now_str()
    conn = connect(cfg)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET state='pending', attempts=0, error=NULL, updated_at=? "
                "WHERE state='dead' AND updated_at < ?",
                (now, cutoff),
            )
            return cur.rowcount
    except sqlite3.OperationalError as e:
        if "lock" in str(e).lower() or "busy" in str(e).lower():
            return 0
        raise
    finally:
        conn.close()


def reaper_loop():
    while True:
        try:
            reap_leases()
            revived = revive_dead_jobs()
            if revived:
                print(f"[reaper] revived {revived} dead jobs", flush=True)
        except Exception:
            pass
        time.sleep(60)


# Held open for the process lifetime to keep the cross-process reaper lock.
_reaper_lock_handle = None


def _acquire_reaper_lock() -> bool:
    """Take an exclusive cross-process lock so only one process runs the reaper.

    Under a multi-worker deployment (uvicorn/gunicorn --workers N) every process
    would otherwise spawn its own reaper thread and contend on the SQLite WAL.
    A non-blocking flock on a file under storage_root elects a single reaper.

    Returns:
        True if this process won the lock and should run the reaper; False if
        another process already holds it.
    """
    global _reaper_lock_handle
    lock_path = cfg.storage_root / "broker-reaper.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w")
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _reaper_lock_handle = handle  # keep the fd open to hold the lock
    return True


# Start the reaper daemon thread on startup — but only on the single process
# that wins the reaper lock.
@asynccontextmanager
async def lifespan(app: FastAPI):
    if _acquire_reaper_lock():
        thread = threading.Thread(target=reaper_loop, daemon=True)
        thread.start()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.post("/enqueue")
def enqueue(job: EnqueuePayload):
    validate_doc_id(job.doc_id)
    conn = connect(cfg)
    now = utc_now_str()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO jobs (type, doc_id, payload, state, priority, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (job.type, job.doc_id, json.dumps(job.payload), job.priority, now, now),
            )
            return {"job_id": cur.lastrowid, "state": "pending"}
    except sqlite3.IntegrityError:
        # Job exists; query it
        cur = conn.execute(
            "SELECT job_id, state FROM jobs WHERE type=? AND doc_id=?", (job.type, job.doc_id)
        )
        row = cur.fetchone()
        job_id = row["job_id"]
        state = row["state"]

        # If existing state is failed or dead, reset to pending and clear attempts
        if state in ("failed", "dead"):
            with conn:
                conn.execute(
                    "UPDATE jobs SET state='pending', attempts=0, error=NULL, updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
            state = "pending"

        return {"job_id": job_id, "state": state, "deduped": True}


@app.post("/lease")
def lease(req: LeasePayload):
    if not req.capabilities:
        return {"jobs": []}

    conn = connect(cfg)
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = (now + datetime.timedelta(seconds=cfg.broker["lease_ttl_seconds"])).isoformat()
    now_str = now.isoformat()

    # Single atomic UPDATE ... RETURNING. SQLite serialises writers, so the inner
    # SELECT is evaluated while this statement holds the write lock; a concurrent
    # /lease blocks (busy_timeout) and then re-reads — it cannot select the same
    # pending rows and double-lease them. Replaces the explicit BEGIN IMMEDIATE +
    # SELECT + per-row UPDATE loop.
    caps_placeholders = ",".join("?" for _ in req.capabilities)
    query = f"""
        UPDATE jobs
        SET state='leased', lease_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=?
        WHERE job_id IN (
            SELECT job_id FROM jobs
            WHERE state='pending' AND type IN ({caps_placeholders})
            ORDER BY priority ASC, job_id ASC
            LIMIT ?
        )
        RETURNING job_id, type, doc_id, payload
    """
    params: list[Any] = [req.worker_id, expires, now_str, *req.capabilities, req.max_jobs]

    try:
        with conn:
            rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    leased_jobs = [
        {
            "job_id": r["job_id"],
            "type": r["type"],
            "doc_id": r["doc_id"],
            "payload": json.loads(r["payload"]),
            "lease_expires_at": expires,
        }
        for r in rows
    ]
    return {"jobs": leased_jobs}


@app.post("/heartbeat")
def heartbeat(req: HeartbeatPayload):
    if not req.job_ids:
        return {"extended": [], "lost": []}

    conn = connect(cfg)
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = (now + datetime.timedelta(seconds=cfg.broker["lease_ttl_seconds"])).isoformat()

    # One batched UPDATE ... RETURNING extends every lease this worker still owns;
    # any requested id not returned was lost (reassigned, completed, or expired).
    placeholders = ",".join("?" for _ in req.job_ids)
    params: list[Any] = [expires, now.isoformat(), req.worker_id, *req.job_ids]

    try:
        with conn:
            rows = conn.execute(
                f"UPDATE jobs SET lease_expires_at=?, updated_at=? "
                f"WHERE lease_owner=? AND state='leased' AND job_id IN ({placeholders}) "
                f"RETURNING job_id",
                params,
            ).fetchall()
    finally:
        conn.close()

    extended = [r["job_id"] for r in rows]
    lost = list(set(req.job_ids) - set(extended))
    return {"extended": extended, "lost": lost}


@app.post("/complete")
def complete(req: CompletePayload):
    conn = connect(cfg)
    now = utc_now_str()
    # BEGIN IMMEDIATE acquires the write lock before the SELECT, preventing
    # concurrent /complete calls from racing to upgrade a read lock to a write
    # lock and producing "database is locked" errors.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "SELECT type, doc_id, state, lease_owner FROM jobs WHERE job_id=?",
            (req.job_id,),
        )
        job = cur.fetchone()
        if not job or job["lease_owner"] != req.worker_id or job["state"] != "leased":
            raise HTTPException(status_code=409, detail="Ownership mismatch or job not leased")

        doc_id = job["doc_id"]
        # Defense-in-depth: never build a storage path from an unsafe doc_id,
        # even if a row predates /enqueue validation.
        validate_doc_id(doc_id)

        # Delegate type-specific persistence and pipeline chaining to the result
        # processor; the broker only owns queue state and the transaction.
        process_result(conn, cfg, job["type"], doc_id, req.result, now)

        conn.execute("UPDATE jobs SET state='done', updated_at=? WHERE job_id=?", (now, req.job_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {"ok": True}


@app.post("/fail")
def fail(req: FailPayload):
    conn = connect(cfg)
    now = utc_now_str()
    max_attempts = cfg.broker["max_attempts"]
    # BEGIN IMMEDIATE prevents concurrent /fail calls from racing to upgrade a
    # read lock to a write lock, which causes "database is locked" errors.
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "SELECT attempts, state, lease_owner FROM jobs WHERE job_id=?",
            (req.job_id,),
        )
        job = cur.fetchone()
        if not job or job["lease_owner"] != req.worker_id or job["state"] != "leased":
            raise HTTPException(status_code=409, detail="Ownership mismatch or job not leased")

        attempts = job["attempts"]
        if req.retryable and attempts < max_attempts:
            conn.execute(
                "UPDATE jobs SET state='pending', error=?, updated_at=? WHERE job_id=?",
                (req.error, now, req.job_id),
            )
        else:
            state = "dead" if attempts >= max_attempts else "failed"
            conn.execute(
                "UPDATE jobs SET state=?, error=?, updated_at=? WHERE job_id=?",
                (state, req.error, now, req.job_id),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {"status": "recorded"}


@app.post("/release")
def release(req: ReleasePayload):
    """Return a leased job to pending without penalising attempts.

    Called by workers on graceful SIGTERM. Only the current lease_owner may
    call release. Resets state to 'pending' and clears lease fields without
    incrementing attempts (infrastructure shutdown is not a job failure).
    """
    conn = connect(cfg)
    now = utc_now_str()

    with conn:
        cur = conn.execute("SELECT state, lease_owner FROM jobs WHERE job_id=?", (req.job_id,))
        job = cur.fetchone()
        if not job or job["state"] != "leased" or job["lease_owner"] != req.worker_id:
            raise HTTPException(status_code=409, detail="Ownership mismatch or job not leased")

        conn.execute(
            "UPDATE jobs SET state='pending', lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE job_id=?",
            (now, req.job_id),
        )

    return {"ok": True}


@app.get("/status")
def status():
    conn = connect(cfg)

    # Counts by (type, state)
    cur = conn.execute("SELECT type, state, COUNT(*) as count FROM jobs GROUP BY type, state")
    counts = {}
    for row in cur.fetchall():
        t, s, c = row["type"], row["state"], row["count"]
        if t not in counts:
            counts[t] = {}
        counts[t][s] = c

    # Active leases / workers
    cur = conn.execute(
        "SELECT lease_owner, MAX(updated_at) as last_seen FROM jobs WHERE state='leased' GROUP BY lease_owner"
    )
    workers = {row["lease_owner"]: row["last_seen"] for row in cur.fetchall()}

    # 5 most recent dead jobs with errors
    cur = conn.execute(
        "SELECT type, doc_id, lease_owner, error, updated_at FROM jobs WHERE state='dead' ORDER BY updated_at DESC LIMIT 5"
    )
    dead_jobs = [
        {
            "type": row["type"],
            "doc_id": row["doc_id"],
            "worker": row["lease_owner"],
            "error": row["error"],
            "updated_at": row["updated_at"],
        }
        for row in cur.fetchall()
    ]

    return {"job_counts": counts, "active_workers": workers, "recent_dead_jobs": dead_jobs}


@app.get("/file/{doc_id}.pdf")
def get_file(doc_id: str):
    validate_doc_id(doc_id)
    path = cfg.storage_root / "raw" / f"{doc_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@app.get("/jobs/dead")
def list_dead_jobs(type: str = "ocr"):
    """Return all dead job doc_ids for a given job type."""
    conn = connect(cfg)
    rows = conn.execute(
        "SELECT DISTINCT doc_id FROM jobs WHERE state='dead' AND type=?", (type,)
    ).fetchall()
    return {"type": type, "doc_ids": [r["doc_id"] for r in rows], "count": len(rows)}


@app.post("/jobs/revive")
def revive_jobs(type: str | None = None):
    """Immediately reset all dead jobs (optionally filtered by type) to pending."""
    now = utc_now_str()
    conn = connect(cfg)
    try:
        with conn:
            if type:
                cur = conn.execute(
                    "UPDATE jobs SET state='pending', attempts=0, error=NULL, updated_at=? "
                    "WHERE state='dead' AND type=?",
                    (now, type),
                )
            else:
                cur = conn.execute(
                    "UPDATE jobs SET state='pending', attempts=0, error=NULL, updated_at=? "
                    "WHERE state='dead'",
                    (now,),
                )
            return {"revived": cur.rowcount}
    finally:
        conn.close()


@app.get("/ocr/{doc_id}.json")
def get_ocr(doc_id: str):
    validate_doc_id(doc_id)
    path = cfg.storage_root / "ocr" / f"{doc_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@app.get("/disk")
def disk_usage():
    usage = shutil.disk_usage(cfg.storage_root)
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "pct_used": round(usage.used / usage.total * 100, 1),
    }


@app.get("/harvest/stats")
def harvest_stats():
    conn = connect(cfg)
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                           AS total,
                SUM(CASE WHEN status='cataloged' THEN 1 ELSE 0 END)  AS cataloged,
                SUM(CASE WHEN status='indexed'   THEN 1 ELSE 0 END)  AS indexed,
                SUM(CASE WHEN has_fulltext=1     THEN 1 ELSE 0 END)  AS has_pdf,
                SUM(CASE WHEN has_fulltext=1 AND status='cataloged' THEN 1 ELSE 0 END) AS pdf_unfetched,
                SUM(CASE WHEN has_fulltext=1 AND status='indexed'   THEN 1 ELSE 0 END) AS pdf_fetched
            FROM documents
            """
        ).fetchone()
        return {
            "total": row[0],
            "cataloged": row[1],
            "indexed": row[2],
            "has_pdf": row[3],
            "pdf_unfetched": row[4],
            "pdf_fetched": row[5],
        }
    finally:
        conn.close()
