# palimpsest/orchestrator.py
"""Lane A Orchestrator for the Palimpsest pipeline.

Two modes:

  palimpsest-orchestrator daemon
      Long-running heartbeat loop. Every `heartbeat_interval_secs` (default 900):
        1. Check each scorer type's candidate count vs. low_water_mark.
        2. Run any scorer whose count is below the mark.
        3. Check worker liveness via broker /status endpoint.
      Exits on SIGTERM or KeyboardInterrupt.

  palimpsest-orchestrator investigate <accession>
      On-demand: immediately runs all scorers that touch docs with the given
      accession prefix, then appends a Markdown citation block to:
          <storage_root>/investigations/<accession>.md

Usage:
    palimpsest-orchestrator daemon [--config PATH]
    palimpsest-orchestrator investigate <accession> [--config PATH]
"""
from __future__ import annotations

import argparse
import datetime
import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path

import httpx

from palimpsest.config import Config, load
from palimpsest.db import connect
from palimpsest.scorers import SCORERS

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
)

# ---------------------------------------------------------------------------
# Heartbeat helpers
# ---------------------------------------------------------------------------


def _candidate_count(conn: sqlite3.Connection, scorer) -> int:
    """Return current row count in the scorer's candidates table."""
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {scorer.candidates_table}"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _enqueue_pending_gap_joins(conn: sqlite3.Connection, now: str) -> int:
    """Enqueue gap_join jobs for docs with unprocessed redactions.

    Args:
        conn: Active DB connection.
        now: ISO-format timestamp string for created_at/updated_at.

    Returns:
        Number of jobs newly enqueued.
    """
    rows = conn.execute("""
        SELECT DISTINCT r.doc_id
        FROM redactions r
        LEFT JOIN gapjoin_runs g ON r.redaction_id = g.redaction_id
        WHERE g.redaction_id IS NULL
    """).fetchall()

    enqueued = 0
    for row in rows:
        doc_id = row["doc_id"]
        try:
            conn.execute(
                "INSERT INTO jobs (type, doc_id, payload, state, priority, created_at, updated_at) "
                "VALUES ('gap_join', ?, '{}', 'pending', 5, ?, ?)",
                (doc_id, now, now),
            )
            enqueued += 1
        except sqlite3.IntegrityError:
            pass
    if enqueued:
        conn.commit()
    return enqueued


def _worker_alive(config: Config) -> bool:
    """Return True if the broker /status endpoint responds with HTTP 200."""
    broker_url = config.broker.get("url", "")
    if not broker_url:
        logger.warning("No broker.url configured — skipping worker liveness check.")
        return True
    timeout = float(config.orchestrator.get("broker_timeout_secs", 5))
    try:
        resp = httpx.get(f"{broker_url}/status", timeout=timeout)
        return resp.status_code == 200
    except Exception as exc:
        logger.error("Broker liveness check failed: %s", exc)
        return False


def heartbeat_cycle(config: Config) -> dict[str, int]:
    """Run one heartbeat cycle.

    For each scorer in SCORERS:
      - Count existing candidates.
      - If count < low_water_mark, call scorer.run().
      - Log the delta.

    Also checks worker liveness and logs a warning if the broker is unreachable.

    Returns a dict mapping type_key -> new candidates inserted this cycle.
    """
    low_water = int(config.orchestrator.get("low_water_mark", 10))
    conn = connect(config)
    conn.row_factory = sqlite3.Row
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    results: dict[str, int] = {}

    for type_key, scorer_cls in SCORERS.items():
        scorer = scorer_cls()
        before = _candidate_count(conn, scorer)
        if before >= low_water:
            logger.info(
                "Heartbeat: %s has %d candidates (>= %d low-water mark) — skipping.",
                type_key, before, low_water,
            )
            results[type_key] = 0
            continue

        logger.info(
            "Heartbeat: %s has %d candidates (< %d low-water mark) — running scorer.",
            type_key, before, low_water,
        )
        try:
            inserted = scorer.run(conn, config)
            delta = len(inserted)
        except Exception as exc:
            logger.error("Heartbeat: scorer %s failed: %s", type_key, exc, exc_info=True)
            delta = 0

        after = _candidate_count(conn, scorer)
        logger.info(
            "Heartbeat: %s inserted %d new candidates (total now %d).",
            type_key, delta, after,
        )
        results[type_key] = delta

    enqueued = _enqueue_pending_gap_joins(conn, now)
    if enqueued:
        logger.info("Heartbeat: enqueued %d gap_join job(s).", enqueued)

    if not _worker_alive(config):
        logger.warning(
            "Heartbeat: worker broker at %s is unreachable. "
            "Check broker process and network connectivity.",
            config.broker.get("url", "<not configured>"),
        )

    conn.close()
    return results


# ---------------------------------------------------------------------------
# Investigate command
# ---------------------------------------------------------------------------


def enqueue_brief(config: Config, status_filter: str = "indexed") -> int:
    """Insert pending brief jobs for all docs at or past the given status.

    Idempotent: docs that already have a brief job (any state) are skipped
    via the UNIQUE(type, doc_id) constraint.

    Args:
        config: Loaded configuration.
        status_filter: Only enqueue briefs for docs whose status matches this
            value.  Defaults to 'indexed' (past OCR and features).

    Returns:
        Number of new jobs enqueued.
    """
    conn = connect(config)
    conn.row_factory = sqlite3.Row
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT doc_id FROM documents WHERE status = ?",
        (status_filter,),
    ).fetchall()

    enqueued = 0
    with conn:
        for row in rows:
            doc_id = row["doc_id"]
            try:
                conn.execute(
                    "INSERT INTO jobs (type, doc_id, payload, state, priority, created_at, updated_at) "
                    "VALUES ('brief', ?, '{}', 'pending', 5, ?, ?)",
                    (doc_id, now, now),
                )
                enqueued += 1
            except sqlite3.IntegrityError:
                pass  # job already exists for this doc

    conn.close()
    logger.info("enqueue-brief: enqueued %d job(s) for status='%s'.", enqueued, status_filter)
    return enqueued


def investigate(accession: str, config: Config) -> Path:
    """Run all scorers against docs with the given accession prefix.

    Writes a Markdown citation block to:
        <storage_root>/investigations/<accession>.md

    Appends if the file already exists (investigations are cumulative).

    Returns the path of the output file.
    """
    inv_dir = config.storage_root / "investigations"
    inv_dir.mkdir(parents=True, exist_ok=True)
    out_path = inv_dir / f"{accession}.md"

    conn = connect(config)
    conn.row_factory = sqlite3.Row

    matching_docs = conn.execute(
        "SELECT doc_id FROM documents WHERE doc_id LIKE ?",
        (f"{accession}%",),
    ).fetchall()
    doc_ids = [r["doc_id"] for r in matching_docs]

    if not doc_ids:
        logger.warning(
            "investigate: no documents found with accession prefix '%s'.", accession
        )
        conn.close()
        return out_path

    logger.info(
        "investigate: found %d document(s) for accession '%s'.",
        len(doc_ids), accession,
    )

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        f"## Investigation: `{accession}` ({now})\n",
        f"Documents matched: {', '.join(doc_ids)}\n",
        "",
    ]

    for type_key, scorer_cls in SCORERS.items():
        scorer = scorer_cls()
        logger.info("investigate: running %s ...", type_key)
        try:
            scorer.run(conn, config)
            relevant = scorer.top(conn, limit=5, doc_ids=doc_ids)
        except Exception as exc:
            logger.error("investigate: scorer %s failed: %s", type_key, exc, exc_info=True)
            continue

        if not relevant:
            continue

        lines.append(f"### {type_key.upper()} findings\n")
        for i, cand in enumerate(relevant, 1):
            lines.append(f"**{i}.** {cand.summary}")
            lines.append(f"   - Score: `{cand.score:.4f}`")
            lines.append(f"   - Documents: {', '.join(cand.doc_ids)}")
            lines.append(f"   - Pages: {', '.join(cand.page_refs)}")
            lines.append("")

    conn.close()

    with open(out_path, "a") as f:
        f.write("\n".join(lines))
        f.write("\n---\n")

    logger.info("investigate: wrote findings to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

_SHUTDOWN = False


def _handle_sigterm(signum, frame):
    global _SHUTDOWN
    logger.info("Received SIGTERM — shutting down after current cycle.")
    _SHUTDOWN = True


def daemon_loop(config: Config) -> None:
    """Run the heartbeat daemon until SIGTERM or KeyboardInterrupt."""
    signal.signal(signal.SIGTERM, _handle_sigterm)
    interval = int(config.orchestrator.get("heartbeat_interval_secs", 900))
    logger.info("Orchestrator daemon started. Heartbeat interval: %ds.", interval)

    while not _SHUTDOWN:
        logger.info("Heartbeat cycle starting.")
        try:
            results = heartbeat_cycle(config)
            total_new = sum(results.values())
            logger.info("Heartbeat cycle complete. Total new candidates: %d.", total_new)
        except Exception as exc:
            logger.error("Heartbeat cycle error: %s", exc, exc_info=True)

        if _SHUTDOWN:
            break

        logger.info("Sleeping %ds until next heartbeat.", interval)
        for _ in range(interval):
            if _SHUTDOWN:
                break
            time.sleep(1)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="palimpsest-orchestrator",
        description="Lane A Orchestrator for the Palimpsest pipeline.",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to config.toml (default: $PALIMPSEST_CONFIG or ./config.toml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daemon", help="Run heartbeat daemon loop.")

    inv_parser = sub.add_parser("investigate", help="Run on-demand investigation.")
    inv_parser.add_argument(
        "accession",
        help="Accession prefix to investigate (e.g. 'NV0014689')",
    )

    eb_parser = sub.add_parser(
        "enqueue-brief",
        help="Enqueue pending brief jobs for all docs at a given pipeline status.",
    )
    eb_parser.add_argument(
        "--status",
        default="indexed",
        metavar="STATUS",
        help="Document status to filter on (default: indexed).",
    )

    args = parser.parse_args()
    config = load(args.config)

    if args.command == "daemon":
        try:
            daemon_loop(config)
        except KeyboardInterrupt:
            logger.info("Orchestrator daemon interrupted by user.")
            sys.exit(0)

    elif args.command == "investigate":
        out = investigate(args.accession, config)
        print(f"Findings written to: {out}")

    elif args.command == "enqueue-brief":
        count = enqueue_brief(config, status_filter=args.status)
        print(f"Enqueued {count} brief job(s) for status='{args.status}'.")


if __name__ == "__main__":
    main()
