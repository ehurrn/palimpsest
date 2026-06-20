# palimpsest/results.py
"""Result processors for completed pipeline jobs.

Decouples per-job-type persistence (file writes, DB upserts) and pipeline
chaining from the broker. The broker owns the queue mechanics and the SQLite
connection/transaction — preserving the single-writer model that keeps remote
workers off the WAL — and simply hands each completed job's result to
:func:`process_result`, which knows how to store an OCR page set, feature
extraction, or embedding batch and how to enqueue the next pipeline stage.

Adding a new job type means registering a processor here; the broker stays
agnostic to bounding boxes, vectors, and the stage DAG.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from palimpsest.config import Config


def _enqueue_followon(
    conn: sqlite3.Connection, job_type: str, doc_id: str, now: str
) -> None:
    """Enqueue (or re-arm) the next pipeline job for a document, idempotently.

    Args:
        conn: Active connection (within the broker's transaction).
        job_type: The follow-on job type to enqueue (e.g. "features", "embed").
        doc_id: Document the job is for.
        now: ISO-8601 timestamp for created_at/updated_at.
    """
    try:
        conn.execute(
            "INSERT INTO jobs (type, doc_id, payload, state, priority, created_at, updated_at) "
            "VALUES (?, ?, '{}', 'pending', 5, ?, ?)",
            (job_type, doc_id, now, now),
        )
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE jobs SET state='pending', updated_at=? WHERE type=? AND doc_id=?",
            (now, job_type, doc_id),
        )


def process_ocr(
    conn: sqlite3.Connection, cfg: Config, doc_id: str, result: Any, now: str
) -> None:
    """Persist OCR pages, mark the doc ocr_done, and chain the features job."""
    ocr_dir = cfg.storage_root / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = ocr_dir / f"{doc_id}.tmp"
    dest_path = ocr_dir / f"{doc_id}.json"
    tmp_path.write_text(json.dumps(result))
    tmp_path.rename(dest_path)

    # Upsert pages rows
    for page in result:
        conn.execute(
            "INSERT OR REPLACE INTO pages (doc_id, page_no, width, height, ocr_source, text) VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, page["page_no"], page.get("width"), page.get("height"), page.get("ocr_source"), page["text"]),
        )

    conn.execute(
        "UPDATE documents SET status='ocr_done', ocr_at=?, page_count=? WHERE doc_id=?",
        (now, len(result), doc_id),
    )
    _enqueue_followon(conn, "features", doc_id, now)


def process_features(
    conn: sqlite3.Connection, cfg: Config, doc_id: str, result: Any, now: str
) -> None:
    """Persist redactions + entities, mark features_done, and chain the embed job."""
    feat_dir = cfg.storage_root / "features"
    feat_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = feat_dir / f"{doc_id}.tmp"
    dest_path = feat_dir / f"{doc_id}.json"
    tmp_path.write_text(json.dumps(result))
    tmp_path.rename(dest_path)

    # Replace any prior extraction for this document.
    # Child tables use varied FK columns — delete each via the right join.
    # gap_candidates/gapjoin_runs join through redactions; review_queue through entities.
    conn.execute(
        "DELETE FROM gap_candidates WHERE redaction_id IN (SELECT redaction_id FROM redactions WHERE doc_id=?)"
        " OR clear_entity_id IN (SELECT entity_id FROM entities WHERE doc_id=?)",
        (doc_id, doc_id),
    )
    conn.execute(
        "DELETE FROM gapjoin_runs WHERE redaction_id IN (SELECT redaction_id FROM redactions WHERE doc_id=?)",
        (doc_id,),
    )
    conn.execute(
        "DELETE FROM review_queue WHERE entity_id IN (SELECT entity_id FROM entities WHERE doc_id=?)",
        (doc_id,),
    )
    conn.execute("DELETE FROM violation_candidates WHERE doc_id=?", (doc_id,))
    conn.execute(
        "DELETE FROM series_gap_candidates WHERE flanking_doc_id=?"
        " OR ref_entity_id IN (SELECT entity_id FROM entities WHERE doc_id=?)",
        (doc_id, doc_id),
    )
    conn.execute(
        "DELETE FROM identity_link_candidates WHERE subject_doc_id=? OR named_doc_id=?"
        " OR named_entity_id IN (SELECT entity_id FROM entities WHERE doc_id=?)",
        (doc_id, doc_id, doc_id),
    )
    conn.execute(
        "DELETE FROM outcome_gap_candidates WHERE initiation_doc_id=?"
        " OR future_ref_entity_id IN (SELECT entity_id FROM entities WHERE doc_id=?)",
        (doc_id, doc_id),
    )
    conn.execute("DELETE FROM redactions WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM entities WHERE doc_id=?", (doc_id,))

    for red in result.get("redactions", []):
        bbox = red.get("bbox", [None, None, None, None])
        conn.execute(
            "INSERT INTO redactions (doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after, char_capacity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, red["page_no"], red["kind"], red.get("label"), bbox[0], bbox[1], bbox[2], bbox[3], red.get("context_before"), red.get("context_after"), red.get("char_capacity")),
        )

    for ent in result.get("entities", []):
        bbox = ent.get("bbox", [None, None, None, None])
        conn.execute(
            "INSERT INTO entities (doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, ent["page_no"], ent["kind"], ent["text"], ent["norm"], ent.get("char_start"), ent.get("char_end"), bbox[0], bbox[1], bbox[2], bbox[3]),
        )

    conn.execute(
        "UPDATE documents SET status='features_done', features_at=? WHERE doc_id=?",
        (now, doc_id),
    )
    year_row = conn.execute("SELECT year FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    doc_year = year_row["year"] if year_row else None
    payload_json = json.dumps({"year": doc_year}) if doc_year is not None else "{}"
    try:
        conn.execute(
            "INSERT INTO jobs (type, doc_id, payload, state, priority, created_at, updated_at) "
            "VALUES ('embed', ?, ?, 'pending', 5, ?, ?)",
            (doc_id, payload_json, now, now),
        )
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE jobs SET state='pending', payload=?, updated_at=? WHERE type='embed' AND doc_id=?",
            (payload_json, now, doc_id),
        )
    # Enqueue brief alongside embed (brief only needs OCR text; gating on
    # features_done means entities exist for later prompt enrichment and
    # avoids racing OCR).
    _enqueue_followon(conn, "brief", doc_id, now)


def process_embed(
    conn: sqlite3.Connection, cfg: Config, doc_id: str, result: Any, now: str
) -> None:
    """Persist chunks, append their embeddings to the pending index, mark indexed."""
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    doc_year: int | None = result.get("year")
    shard_by = cfg.embed.get("shard_by")

    for ch in result.get("chunks", []):
        cur_chunk = conn.execute(
            "INSERT INTO chunks (doc_id, page_no, char_start, char_end, text) VALUES (?, ?, ?, ?, ?) RETURNING chunk_id",
            (doc_id, ch["page_no"], ch["char_start"], ch["char_end"], ch["text"]),
        )
        chunk_id = cur_chunk.fetchone()["chunk_id"]

        if shard_by == "decade" and doc_year is not None:
            decade = (doc_year // 10) * 10
            index_dir = cfg.storage_root / "index" / "shards" / str(decade)
        else:
            index_dir = cfg.storage_root / "index"

        index_dir.mkdir(parents=True, exist_ok=True)
        with open(index_dir / "pending_embeddings.jsonl", "a") as f:
            f.write(json.dumps({"chunk_id": chunk_id, "embedding": ch["embedding"]}) + "\n")

    conn.execute(
        "UPDATE documents SET status='indexed', indexed_at=? WHERE doc_id=?",
        (now, doc_id),
    )


def process_extract(
    conn: sqlite3.Connection, cfg: Config, doc_id: str, result: Any, now: str
) -> None:
    """Stub persistence for ad-hoc extract jobs: dump the raw result to disk."""
    ext_dir = cfg.storage_root / "features"
    ext_dir.mkdir(parents=True, exist_ok=True)
    dest_path = ext_dir / f"{doc_id}.extract.json"
    dest_path.write_text(json.dumps(result))


def process_brief(
    conn: sqlite3.Connection, cfg: Config, doc_id: str, result: Any, now: str
) -> None:
    """Persist a completed brief: write JSON file and upsert one briefs row.

    Brief is terminal — it enqueues no follow-on jobs.
    Scores (interest_score, novelty_score) are left NULL; triage fills them.
    """
    briefs_dir = cfg.storage_root / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = briefs_dir / f"{doc_id}.tmp"
    dest_path = briefs_dir / f"{doc_id}.json"
    tmp_path.write_text(json.dumps(result))
    tmp_path.rename(dest_path)

    conn.execute(
        """
        INSERT OR REPLACE INTO briefs
          (doc_id, model, doc_type, summary,
           claims_json, events_json, redactions_json, flags_json,
           interest_score, novelty_score, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            doc_id,
            result.get("model"),
            result.get("doc_type"),
            result.get("summary"),
            json.dumps(result.get("claims", [])),
            json.dumps(result.get("events", [])),
            json.dumps(result.get("redaction_hypotheses", [])),
            json.dumps(result.get("flags", [])),
            now,
        ),
    )


ResultProcessor = Callable[[sqlite3.Connection, Config, str, Any, str], None]

RESULT_PROCESSORS: dict[str, ResultProcessor] = {
    "ocr": process_ocr,
    "features": process_features,
    "embed": process_embed,
    "extract": process_extract,
    "brief": process_brief,
}


def process_result(
    conn: sqlite3.Connection,
    cfg: Config,
    job_type: str,
    doc_id: str,
    result: Any,
    now: str,
) -> None:
    """Dispatch a completed job's result to its type-specific processor.

    Runs within the broker's open transaction. Unknown job types are a no-op
    (the broker still marks the job done), matching the prior /complete behavior
    where unrecognized types fell through with no persistence.

    Args:
        conn: Active connection inside the broker's transaction.
        cfg: Loaded configuration (for storage paths).
        job_type: The completed job's type.
        doc_id: Validated document id.
        result: The worker-reported result payload.
        now: ISO-8601 timestamp for status/timestamps.
    """
    processor = RESULT_PROCESSORS.get(job_type)
    if processor is not None:
        processor(conn, cfg, doc_id, result, now)
