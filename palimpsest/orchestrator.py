"""Lane A Orchestrator.

Two entry points:
  palimpsest-orchestrate heartbeat   — 15-min daemon loop (run via launchd)
  palimpsest-orchestrate investigate — on-demand candidate pull for investigation
"""
from __future__ import annotations
import argparse
import datetime
import logging
import threading
from pathlib import Path

import httpx

from palimpsest.config import load, Config
from palimpsest.db import connect
from palimpsest.scorers import SCORERS
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ---------------------------------------------------------------------------
# Heartbeat helpers
# ---------------------------------------------------------------------------

def _check_queue_depth(conn, config: Config) -> int:
    """Return count of pending jobs. Logs a warning if below low-water mark."""
    low_water = int(config.orchestrator.get("queue_low_water_mark", 100))
    count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
    ).fetchone()[0]
    if count < low_water:
        logger.warning(
            "Queue depth %d is below low-water mark %d — "
            "consider running: palimpsest harvest fetch",
            count, low_water
        )
    else:
        logger.info("Queue depth: %d pending jobs", count)
    return count


def _check_candidate_counts(
    conn,
    config: Config,
    last_counts: dict[str, int],
) -> dict[str, int]:
    """Count candidates per scorer type. Log a flag if count grew significantly."""
    threshold = int(config.orchestrator.get("candidate_investigate_threshold", 50))
    current: dict[str, int] = {}
    for key, scorer_cls in SCORERS.items():
        scorer = scorer_cls()
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {scorer.candidates_table} "
                f"WHERE status = 'candidate'"
            ).fetchone()[0]
            current[key] = count
            prev = last_counts.get(key, 0)
            delta = count - prev
            if delta >= threshold:
                logger.info(
                    "INVESTIGATE FLAG: %s has %d new candidates (total=%d) — "
                    "run: palimpsest-orchestrate investigate --type %s",
                    key, delta, count, key
                )
            else:
                logger.info("%s: %d candidates (%+d since last tick)", key, count, delta)
        except Exception as e:
            logger.error("Candidate count failed for %s: %s", key, e)
            current[key] = last_counts.get(key, 0)
    return current


def _check_worker_liveness(config: Config) -> None:
    """Call broker /status and warn if no worker heartbeat in 10 minutes."""
    broker_url = config.broker.get("url", "http://localhost:8077")
    try:
        resp = httpx.get(f"{broker_url}/status", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        workers = data.get("workers", [])
        now = datetime.datetime.now(datetime.timezone.utc)
        alive = [
            w for w in workers
            if w.get("last_heartbeat") and
            (now - datetime.datetime.fromisoformat(w["last_heartbeat"])).total_seconds() < 600
        ]
        if not alive:
            logger.warning(
                "No worker heartbeat in the last 10 minutes. "
                "Workers registered: %d. Check M4/M5/gonktop workers.",
                len(workers)
            )
        else:
            logger.info("Workers alive: %d / %d", len(alive), len(workers))
    except Exception as e:
        logger.error("Broker liveness check failed: %s", e)


# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------

def run_heartbeat(config: Config) -> None:
    """Run the heartbeat daemon loop."""
    interval = int(config.orchestrator.get("heartbeat_interval_secs", 900))
    logger.info("Orchestrator heartbeat starting (interval=%ds)", interval)
    conn = connect(config)
    last_counts: dict[str, int] = {}

    import signal

    # An Event lets the inter-tick wait wake instantly on SIGTERM/SIGINT
    # instead of blocking inside time.sleep() for up to `interval` seconds.
    stop_event = threading.Event()

    def _stop(sig, frame):
        logger.info("Orchestrator received signal %s — stopping", sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop_event.is_set():
        logger.info(
            "--- Heartbeat tick at %s ---",
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        try:
            _check_queue_depth(conn, config)
        except Exception as e:
            logger.error("Queue depth check failed: %s", e)
        try:
            last_counts = _check_candidate_counts(conn, config, last_counts)
        except Exception as e:
            logger.error("Candidate sweep failed: %s", e)
        try:
            _check_worker_liveness(config)
        except Exception as e:
            logger.error("Worker liveness check failed: %s", e)

        # Interruptible sleep: returns immediately once _stop sets the event.
        stop_event.wait(timeout=interval)

    logger.info("Orchestrator heartbeat stopped.")


# ---------------------------------------------------------------------------
# Investigate command
# ---------------------------------------------------------------------------

def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def run_investigate(config: Config, type_key: str, limit: int, output: Path | None) -> None:
    """Pull top-N candidates and write a Markdown citation block."""
    if type_key not in SCORERS:
        raise SystemExit(
            f"Unknown type_key '{type_key}'. "
            f"Valid keys: {', '.join(sorted(SCORERS.keys()))}"
        )

    conn = connect(config)
    scorer = SCORERS[type_key]()
    candidates: list[Candidate] = scorer.top(conn, limit=limit)

    if not candidates:
        msg = f"No candidates found for {type_key} (table: {scorer.candidates_table}).\n"
        if output:
            _append(output, msg)
        else:
            print(msg, end="")
        return

    lines: list[str] = [
        f"\n\n<!-- investigate run: {datetime.datetime.now(datetime.timezone.utc).isoformat()} "
        f"type={type_key} limit={limit} -->\n"
    ]
    for i, cand in enumerate(candidates, start=1):
        lines.append("---\n")
        lines.append(f"## Candidate {i} — {cand.type_key} (score={cand.score:.3f})\n")
        lines.append(f"**Summary:** {cand.summary}\n")
        lines.append(f"**Source documents:** {', '.join(cand.doc_ids)}\n")
        lines.append(f"**Page references:** {', '.join(cand.page_refs)}\n")
        lines.append(
            f"**Entity IDs (for MCP lookup):** "
            f"{', '.join(str(e) for e in cand.entity_ids) or 'none'}\n"
        )

    result = "".join(lines)

    if output:
        _append(output, result)
        logger.info("Wrote %d candidates to %s", len(candidates), output)
    else:
        print(result)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="palimpsest-orchestrate",
        description="Lane A orchestrator: heartbeat daemon and investigation sessions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # heartbeat subcommand
    hb = sub.add_parser("heartbeat", help="Run the 15-minute heartbeat daemon loop.")
    hb.add_argument("--config", default="config.toml", help="Path to config.toml")

    # investigate subcommand
    inv = sub.add_parser(
        "investigate",
        help="Pull top-N candidates for a finding type and write a citation block.",
    )
    inv.add_argument("--config", default="config.toml", help="Path to config.toml")
    inv.add_argument(
        "--type",
        dest="type_key",
        required=True,
        choices=list(SCORERS.keys()),
        help="Finding type to investigate (type_a through type_f).",
    )
    inv.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of candidates to return (default: 20).",
    )
    inv.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to append output to. If omitted, writes to stdout.",
    )

    args = parser.parse_args()
    cfg = load(args.config)

    if args.command == "heartbeat":
        run_heartbeat(cfg)
    elif args.command == "investigate":
        run_investigate(cfg, args.type_key, args.limit, args.output)


if __name__ == "__main__":
    main()
