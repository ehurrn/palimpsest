# tests/test_brief.py
"""Acceptance tests for the brief intelligence layer (TASK-20).

All Ollama and broker HTTP calls are mocked — no real model or network required.

Tests:
1. handle_brief: returns §1 contract shape; every claim/event carries a
   page_no that exists in the input OCR; malformed model JSON raises
   PermanentJobError.
2. process_brief: writes JSON file + upserts exactly one briefs row;
   re-running (brief, doc_id) overwrites, never duplicates.
3. Migration v4 is idempotent: migrate() twice on a v3 DB → briefs table
   exists, no error.
4. triage.run_triage orders by max(interest, novelty) and honours --limit.
"""
from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from palimpsest.db import connect, migrate
from palimpsest.results import process_brief
from palimpsest.tasks import PermanentJobError
from palimpsest.tasks.brief import handle_brief
from palimpsest.triage import run_triage


# ── Shared fixtures ───────────────────────────────────────────────────────────

class _DummyConfig:
    """Minimal config stub that satisfies brief, triage, embed, and broker access."""

    def __init__(self, tmp_path: Path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.broker = {"host": "localhost", "port": 8077}
        self.embed = {"model": "nomic-embed-text", "dim": 768, "chunk_chars": 800, "chunk_overlap": 150}
        self.models = {"extract": "llama3.1:8b", "classify": "qwen2.5:3b", "keep_alive": "24h"}
        self.brief = {
            "model": "llama3.1:8b",
            "window_tokens": 6000,
            "max_claims": 25,
            "max_events": 25,
            "temperature": 0.1,
        }
        self.raw = {}  # no [triage] rubric → skip interest scoring

    # Config is a frozen dataclass in production; attribute access is enough here.


@pytest.fixture()
def cfg(tmp_path):
    return _DummyConfig(tmp_path)


@pytest.fixture()
def migrated_db(cfg):
    migrate(cfg)
    conn = connect(cfg)
    conn.row_factory = sqlite3.Row
    # Insert a document row so the briefs FK resolves.
    conn.execute(
        "INSERT INTO documents (doc_id, status) VALUES ('doc001', 'indexed')"
    )
    conn.commit()
    yield conn
    conn.close()


# ── Sample data ───────────────────────────────────────────────────────────────

_SAMPLE_OCR = [
    {
        "doc_id": "doc001",
        "page_no": 1,
        "text": "Field Report NV-001. Subjects at the test site received whole-body exposure.",
    },
    {
        "doc_id": "doc001",
        "page_no": 2,
        "text": "(b)(1) [redacted] administered dose 15 rem to Subject 3 in 1953-07 at NTS.",
    },
]

_VALID_BRIEF_JSON = {
    "doc_type": "field_report",
    "summary": "A 1953 field report from the Nevada Test Site describing radiation exposure of human subjects.",
    "claims": [
        {"text": "Subjects received whole-body exposure.", "page_no": 1, "confidence": 0.0},
    ],
    "events": [
        {
            "actor": "unknown",
            "action": "administered dose",
            "object": "15 rem",
            "subject_ref": "Subject 3",
            "date": "1953-07",
            "place": "NTS",
            "page_no": 2,
        }
    ],
    "redaction_hypotheses": [
        {
            "page_no": 2,
            "label": "(b)(1)",
            "likely_hidden": "person_name",
            "rationale": "follows 'subject' and precedes a dose value",
            "confidence": 0.0,
        }
    ],
    "flags": ["human_subjects", "consent_language_absent"],
}


# ── Test 1: handle_brief output contract ─────────────────────────────────────

def _mock_ocr_response(ocr_data):
    """Build a mock httpx.get response returning ocr_data as JSON."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = ocr_data
    mock_resp.raise_for_status = lambda: None
    return mock_resp


def _mock_ollama_response(result_dict):
    """Build a mock httpx.post response returning result_dict JSON."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"response": json.dumps(result_dict)}
    mock_resp.raise_for_status = lambda: None
    return mock_resp


@patch("palimpsest.tasks.brief.httpx.post")
@patch("palimpsest.tasks.brief.httpx.get")
def test_handle_brief_contract_shape(mock_get, mock_post, cfg):
    """handle_brief returns the §1 contract shape with correct top-level keys."""
    mock_get.return_value = _mock_ocr_response(_SAMPLE_OCR)
    mock_post.return_value = _mock_ollama_response(_VALID_BRIEF_JSON)

    job = {"doc_id": "doc001"}
    result = handle_brief(cfg, job)

    # Top-level required keys
    for key in ("doc_id", "model", "schema", "doc_type", "summary", "claims", "events",
                 "redaction_hypotheses", "flags"):
        assert key in result, f"Missing key: {key}"

    assert result["doc_id"] == "doc001"
    assert result["schema"] == 1
    assert isinstance(result["summary"], str) and result["summary"]
    assert isinstance(result["claims"], list)
    assert isinstance(result["events"], list)
    assert isinstance(result["redaction_hypotheses"], list)
    assert isinstance(result["flags"], list)


@patch("palimpsest.tasks.brief.httpx.post")
@patch("palimpsest.tasks.brief.httpx.get")
def test_handle_brief_page_nos_valid(mock_get, mock_post, cfg):
    """Every claim and event page_no must exist in the input OCR pages."""
    mock_get.return_value = _mock_ocr_response(_SAMPLE_OCR)
    mock_post.return_value = _mock_ollama_response(_VALID_BRIEF_JSON)

    result = handle_brief(cfg, {"doc_id": "doc001"})

    valid_pages = {p["page_no"] for p in _SAMPLE_OCR}
    for claim in result["claims"]:
        assert claim["page_no"] in valid_pages, (
            f"Claim page_no {claim['page_no']} not in OCR pages {valid_pages}"
        )
    for event in result["events"]:
        assert event["page_no"] in valid_pages, (
            f"Event page_no {event['page_no']} not in OCR pages {valid_pages}"
        )


@patch("palimpsest.tasks.brief.httpx.post")
@patch("palimpsest.tasks.brief.httpx.get")
def test_handle_brief_confidence_always_zero(mock_get, mock_post, cfg):
    """confidence is always overridden to 0.0 regardless of model output."""
    # Model tries to return non-zero confidence
    bad_conf = dict(_VALID_BRIEF_JSON)
    bad_conf["claims"] = [{"text": "test", "page_no": 1, "confidence": 0.99}]
    bad_conf["redaction_hypotheses"] = [
        {"page_no": 2, "label": "(b)(1)", "likely_hidden": "dose",
         "rationale": "test", "confidence": 0.87}
    ]
    mock_get.return_value = _mock_ocr_response(_SAMPLE_OCR)
    mock_post.return_value = _mock_ollama_response(bad_conf)

    result = handle_brief(cfg, {"doc_id": "doc001"})
    for claim in result["claims"]:
        assert claim["confidence"] == 0.0
    for rh in result["redaction_hypotheses"]:
        assert rh["confidence"] == 0.0


@patch("palimpsest.tasks.brief.httpx.post")
@patch("palimpsest.tasks.brief.httpx.get")
def test_handle_brief_malformed_json_raises_permanent_error(mock_get, mock_post, cfg):
    """Malformed JSON from model raises PermanentJobError (don't burn retries)."""
    mock_get.return_value = _mock_ocr_response(_SAMPLE_OCR)
    # Model returns garbage
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.json.return_value = {"response": "This is not JSON at all, sorry!"}
    bad_resp.raise_for_status = lambda: None
    mock_post.return_value = bad_resp

    with pytest.raises(PermanentJobError, match="malformed JSON"):
        handle_brief(cfg, {"doc_id": "doc001"})


# ── Test 2: process_brief persistence ────────────────────────────────────────

def _make_brief_result(doc_id: str = "doc001") -> dict:
    return {
        "doc_id": doc_id,
        "model": "llama3.1:8b",
        "schema": 1,
        "doc_type": "field_report",
        "summary": "A test summary.",
        "claims": [{"text": "Test claim.", "page_no": 1, "confidence": 0.0}],
        "events": [],
        "redaction_hypotheses": [],
        "flags": ["human_subjects"],
    }


def test_process_brief_writes_json_file(cfg, migrated_db):
    """process_brief writes {root}/briefs/{doc_id}.json."""
    result = _make_brief_result()
    process_brief(migrated_db, cfg, "doc001", result, "2026-06-20T00:00:00Z")

    dest = cfg.storage_root / "briefs" / "doc001.json"
    assert dest.exists(), "JSON file not written"
    written = json.loads(dest.read_text())
    assert written["doc_id"] == "doc001"
    assert written["summary"] == "A test summary."


def test_process_brief_upserts_one_row(cfg, migrated_db):
    """process_brief upserts exactly one briefs row; re-running never duplicates."""
    result = _make_brief_result()
    process_brief(migrated_db, cfg, "doc001", result, "2026-06-20T00:00:00Z")
    migrated_db.commit()

    row_count = migrated_db.execute("SELECT COUNT(*) FROM briefs WHERE doc_id='doc001'").fetchone()[0]
    assert row_count == 1

    # Run again with updated summary — should overwrite, not duplicate
    result["summary"] = "Updated summary."
    process_brief(migrated_db, cfg, "doc001", result, "2026-06-20T01:00:00Z")
    migrated_db.commit()

    row_count = migrated_db.execute("SELECT COUNT(*) FROM briefs WHERE doc_id='doc001'").fetchone()[0]
    assert row_count == 1

    summary_in_db = migrated_db.execute(
        "SELECT summary FROM briefs WHERE doc_id='doc001'"
    ).fetchone()["summary"]
    assert summary_in_db == "Updated summary."


# ── Test 3: migration v4 idempotent ──────────────────────────────────────────

def test_migration_v4_idempotent(tmp_path):
    """migrate() twice on a v3 DB → briefs table exists, no error."""

    class _Cfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"

    cfg = _Cfg()

    # First migrate (goes all the way to v4)
    migrate(cfg)

    # Second migrate should be a no-op, not raise
    migrate(cfg)

    conn = connect(cfg)
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='briefs'"
        ).fetchall()
    ]
    conn.close()
    assert "briefs" in tables, "briefs table missing after double migrate"


# ── Test 4: triage ordering ───────────────────────────────────────────────────

def test_triage_orders_by_max_interest_novelty_and_honours_limit(cfg, tmp_path):
    """run_triage returns rows ordered by max(interest, novelty) and respects limit."""
    migrate(cfg)
    conn = connect(cfg)
    conn.row_factory = sqlite3.Row

    # Insert 3 documents and briefs with known scores
    docs = [
        ("docA", 0.9, 0.2),   # max = 0.9
        ("docB", 0.1, 0.8),   # max = 0.8
        ("docC", 0.3, 0.3),   # max = 0.3
    ]
    now = "2026-06-20T00:00:00Z"
    with conn:
        for doc_id, interest, novelty in docs:
            conn.execute(
                "INSERT INTO documents (doc_id, status) VALUES (?, 'indexed')", (doc_id,)
            )
            conn.execute(
                """INSERT INTO briefs
                   (doc_id, model, doc_type, summary, claims_json, events_json,
                    redactions_json, flags_json, interest_score, novelty_score, created_at)
                   VALUES (?, 'llama3.1:8b', 'memo', ?, '[]', '[]', '[]', '[]', ?, ?, ?)""",
                (doc_id, f"Summary for {doc_id}.", interest, novelty, now),
            )

    # Patch _compute_novelty and _compute_interest to return the pre-seeded values
    # so we don't hit Ollama.
    novelty_by_id  = {d[0]: d[2] for d in docs}   # doc_id -> novelty score
    interest_by_id = {d[0]: d[1] for d in docs}   # doc_id -> interest score

    def _fake_novelty(cfg_, rows, k=5):
        return {r["doc_id"]: novelty_by_id[r["doc_id"]] for r in rows
                if r["doc_id"] in novelty_by_id}

    def _fake_interest(cfg_, rows):
        # Return interest scores so run_triage can rank by max(interest, novelty)
        return {r["doc_id"]: interest_by_id[r["doc_id"]] for r in rows
                if r["doc_id"] in interest_by_id}

    # run_triage re-embeds and rewrites scores — we'll patch those calls.
    with (
        patch("palimpsest.triage._compute_novelty", side_effect=_fake_novelty),
        patch("palimpsest.triage._compute_interest", side_effect=_fake_interest),
    ):
        ranked = run_triage(cfg, limit=2)

    assert len(ranked) == 2, f"Expected 2 rows, got {len(ranked)}"
    assert ranked[0]["doc_id"] == "docA", f"Expected docA first, got {ranked[0]['doc_id']}"
    assert ranked[1]["doc_id"] == "docB", f"Expected docB second, got {ranked[1]['doc_id']}"
