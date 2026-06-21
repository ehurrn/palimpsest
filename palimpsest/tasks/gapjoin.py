# palimpsest/tasks/gapjoin.py
"""Worker task handler for gap-join jobs."""
from __future__ import annotations

import logging
import threading
from typing import Any

from palimpsest.config import Config
from palimpsest.db import connect
from palimpsest.indexer import run_gapjoin_for_doc
from palimpsest.tasks import handler

logger = logging.getLogger(__name__)


@handler("gap_join")
def handle_gap_join(
    cfg: Config,
    job: dict[str, Any],
    *,
    lost_evt: threading.Event | None = None,
    shutdown_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run gap join for all pending redactions in a document.

    Args:
        cfg: Loaded configuration.
        job: Job dict with doc_id.
        lost_evt: Set by heartbeat thread if broker revokes the lease.
        shutdown_event: Set on SIGTERM. Accepted for interface consistency.

    Returns:
        Result dict with count of redactions processed.
    """
    doc_id = job["doc_id"]
    logger.info("gap_join: processing doc %s", doc_id)
    conn = connect(cfg)
    try:
        count = run_gapjoin_for_doc(cfg, conn, doc_id)
    finally:
        conn.close()
    logger.info("gap_join: processed %d redaction(s) for doc %s", count, doc_id)
    return {"doc_id": doc_id, "redactions_processed": count}
