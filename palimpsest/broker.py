# palimpsest/broker.py
import datetime
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from palimpsest.config import load
from palimpsest.db import connect

app = FastAPI()
cfg = load()

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

# Helper for UTC ISO-8601 timestamps
def utc_now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def reap_leases():
    """Find leased jobs whose leases have expired and return them to pending or dead."""
    conn = connect(cfg)
    now = utc_now_str()
    max_attempts = cfg.broker["max_attempts"]
    with conn:
        # Get expired leases
        cur = conn.execute(
            "SELECT job_id, attempts FROM jobs WHERE state='leased' AND lease_expires_at < ?",
            (now,)
        )
        expired = cur.fetchall()
        for row in expired:
            job_id = row["job_id"]
            attempts = row["attempts"]
            if attempts >= max_attempts:
                conn.execute(
                    "UPDATE jobs SET state='dead', error='Lease expired and max attempts exceeded', updated_at=? WHERE job_id=?",
                    (now, job_id)
                )
            else:
                conn.execute(
                    "UPDATE jobs SET state='pending', updated_at=? WHERE job_id=?",
                    (now, job_id)
                )

def reaper_loop():
    while True:
        try:
            reap_leases()
        except Exception as e:
            pass
        time.sleep(60)

# Start reaper daemon thread on startup
@app.on_event("startup")
def startup_event():
    thread = threading.Thread(target=reaper_loop, daemon=True)
    thread.start()

@app.post("/enqueue")
def enqueue(job: EnqueuePayload):
    conn = connect(cfg)
    now = utc_now_str()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO jobs (type, doc_id, payload, state, priority, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (job.type, job.doc_id, json.dumps(job.payload), job.priority, now, now)
            )
            return {"job_id": cur.lastrowid, "state": "pending"}
    except sqlite3.IntegrityError:
        # Job exists; query it
        cur = conn.execute("SELECT job_id, state FROM jobs WHERE type=? AND doc_id=?", (job.type, job.doc_id))
        row = cur.fetchone()
        job_id = row["job_id"]
        state = row["state"]
        
        # If existing state is failed or dead, reset to pending
        if state in ("failed", "dead"):
            with conn:
                conn.execute(
                    "UPDATE jobs SET state='pending', updated_at=? WHERE job_id=?",
                    (now, job_id)
                )
            state = "pending"
            
        return {"job_id": job_id, "state": state, "deduped": True}

@app.post("/lease")
def lease(req: LeasePayload):
    conn = connect(cfg)
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = (now + datetime.timedelta(seconds=cfg.broker["lease_ttl_seconds"])).isoformat()
    now_str = now.isoformat()
    
    if not req.capabilities:
        return {"jobs": []}
        
    caps_placeholders = ",".join("?" for _ in req.capabilities)
    query = f"""
    SELECT job_id, type, doc_id, payload FROM jobs 
    WHERE state='pending' AND type IN ({caps_placeholders}) 
    ORDER BY priority ASC, job_id ASC LIMIT ?
    """
    params = req.capabilities + [req.max_jobs]
    
    # We run in a transaction to avoid lease races
    with conn:
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        leased_jobs = []
        for row in rows:
            job_id = row["job_id"]
            conn.execute(
                "UPDATE jobs SET state='leased', lease_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=? WHERE job_id=?",
                (req.worker_id, expires, now_str, job_id)
            )
            leased_jobs.append({
                "job_id": job_id,
                "type": row["type"],
                "doc_id": row["doc_id"],
                "payload": json.loads(row["payload"]),
                "lease_expires_at": expires
            })
        return {"jobs": leased_jobs}

@app.post("/heartbeat")
def heartbeat(req: HeartbeatPayload):
    conn = connect(cfg)
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = (now + datetime.timedelta(seconds=cfg.broker["lease_ttl_seconds"])).isoformat()
    extended = []
    lost = []
    
    with conn:
        for job_id in req.job_ids:
            cur = conn.execute(
                "SELECT lease_owner, state FROM jobs WHERE job_id=?",
                (job_id,)
            )
            row = cur.fetchone()
            if row and row["lease_owner"] == req.worker_id and row["state"] == "leased":
                conn.execute(
                    "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE job_id=?",
                    (expires, now.isoformat(), job_id)
                )
                extended.append(job_id)
            else:
                lost.append(job_id)
                
    return {"extended": extended, "lost": lost}

@app.post("/complete")
def complete(req: CompletePayload):
    conn = connect(cfg)
    now = utc_now_str()
    
    with conn:
        cur = conn.execute("SELECT type, doc_id, state, lease_owner FROM jobs WHERE job_id=?", (req.job_id,))
        job = cur.fetchone()
        if not job or job["lease_owner"] != req.worker_id or job["state"] != "leased":
            raise HTTPException(status_code=409, detail="Ownership mismatch or job not leased")
        
        job_type = job["type"]
        doc_id = job["doc_id"]
        
        # Result handling (broker-side persistence)
        if job_type == "ocr":
            # Write ocr json file atomically
            ocr_dir = cfg.storage_root / "ocr"
            ocr_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = ocr_dir / f"{doc_id}.tmp"
            dest_path = ocr_dir / f"{doc_id}.json"
            tmp_path.write_text(json.dumps(req.result))
            tmp_path.rename(dest_path)
            
            # Upsert pages rows
            for page in req.result:
                conn.execute(
                    "INSERT OR REPLACE INTO pages (doc_id, page_no, width, height, ocr_source, text) VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, page["page_no"], page.get("width"), page.get("height"), page.get("ocr_source"), page["text"])
                )
            
            # Update documents table
            conn.execute(
                "UPDATE documents SET status='ocr_done', ocr_at=?, page_count=? WHERE doc_id=?",
                (now, len(req.result), doc_id)
            )
            
        elif job_type == "features":
            # Write features json file atomically
            feat_dir = cfg.storage_root / "features"
            feat_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = feat_dir / f"{doc_id}.tmp"
            dest_path = feat_dir / f"{doc_id}.json"
            tmp_path.write_text(json.dumps(req.result))
            tmp_path.rename(dest_path)
            
            # Delete old redactions & entities for doc
            # First need to find all page_nos to avoid FK issues, or just delete directly since documents table has pages
            conn.execute("DELETE FROM redactions WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM entities WHERE doc_id=?", (doc_id,))
            
            # Insert redactions
            for red in req.result.get("redactions", []):
                # bbox = [x0, y0, x1, y1]
                bbox = red.get("bbox", [None, None, None, None])
                conn.execute(
                    "INSERT INTO redactions (doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, red["page_no"], red["kind"], red.get("label"), bbox[0], bbox[1], bbox[2], bbox[3], red.get("context_before"), red.get("context_after"))
                )
                
            # Insert entities
            for ent in req.result.get("entities", []):
                bbox = ent.get("bbox", [None, None, None, None])
                conn.execute(
                    "INSERT INTO entities (doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, ent["page_no"], ent["kind"], ent["text"], ent["norm"], ent.get("char_start"), ent.get("char_end"), bbox[0], bbox[1], bbox[2], bbox[3])
                )
                
            # Update documents table
            conn.execute(
                "UPDATE documents SET status='features_done', features_at=? WHERE doc_id=?",
                (now, doc_id)
            )
            
        elif job_type == "embed":
            # Delete old chunks
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            
            # Insert chunks
            chunks_data = req.result.get("chunks", [])
            for ch in chunks_data:
                # Upsert chunk
                cur_chunk = conn.execute(
                    "INSERT INTO chunks (doc_id, page_no, char_start, char_end, text) VALUES (?, ?, ?, ?, ?) RETURNING chunk_id",
                    (doc_id, ch["page_no"], ch["char_start"], ch["char_end"], ch["text"])
                )
                chunk_id = cur_chunk.fetchone()["chunk_id"]
                
                # Append embedding to pending_embeddings.jsonl
                index_dir = cfg.storage_root / "index"
                index_dir.mkdir(parents=True, exist_ok=True)
                with open(index_dir / "pending_embeddings.jsonl", "a") as f:
                    f.write(json.dumps({"chunk_id": chunk_id, "embedding": ch["embedding"]}) + "\n")
                    
            # Update documents table
            conn.execute(
                "UPDATE documents SET status='indexed', indexed_at=? WHERE doc_id=?",
                (now, doc_id)
            )
            
        elif job_type == "extract":
            # Stub persistence
            ext_dir = cfg.storage_root / "features"
            ext_dir.mkdir(parents=True, exist_ok=True)
            dest_path = ext_dir / f"{doc_id}.extract.json"
            dest_path.write_text(json.dumps(req.result))
            
        conn.execute("UPDATE jobs SET state='done', updated_at=? WHERE job_id=?", (now, req.job_id))
        
    return {"ok": True}

@app.post("/fail")
def fail(req: FailPayload):
    conn = connect(cfg)
    now = utc_now_str()
    max_attempts = cfg.broker["max_attempts"]
    
    with conn:
        cur = conn.execute("SELECT attempts, state, lease_owner FROM jobs WHERE job_id=?", (req.job_id,))
        job = cur.fetchone()
        if not job or job["lease_owner"] != req.worker_id or job["state"] != "leased":
            raise HTTPException(status_code=409, detail="Ownership mismatch or job not leased")
        
        attempts = job["attempts"]
        if req.retryable and attempts < max_attempts:
            conn.execute(
                "UPDATE jobs SET state='pending', error=?, updated_at=? WHERE job_id=?",
                (req.error, now, req.job_id)
            )
        else:
            state = "dead" if attempts >= max_attempts else "failed"
            conn.execute(
                "UPDATE jobs SET state=?, error=?, updated_at=? WHERE job_id=?",
                (state, req.error, now, req.job_id)
            )
            
    return {"status": "recorded"}

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
    cur = conn.execute("SELECT lease_owner, MAX(updated_at) as last_seen FROM jobs WHERE state='leased' GROUP BY lease_owner")
    workers = {row["lease_owner"]: row["last_seen"] for row in cur.fetchall()}
    
    # 10 most recent dead jobs with errors
    cur = conn.execute("SELECT type, doc_id, error, updated_at FROM jobs WHERE state='dead' ORDER BY updated_at DESC LIMIT 10")
    dead_jobs = [
        {"type": row["type"], "doc_id": row["doc_id"], "error": row["error"], "updated_at": row["updated_at"]}
        for row in cur.fetchall()
    ]
    
    return {
        "job_counts": counts,
        "active_workers": workers,
        "recent_dead_jobs": dead_jobs
    }

@app.get("/file/{doc_id}.pdf")
def get_file(doc_id: str):
    if not doc_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid document ID")
    path = cfg.storage_root / "raw" / f"{doc_id}.pdf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)
