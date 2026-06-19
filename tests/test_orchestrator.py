# tests/test_orchestrator.py
"""Tests for palimpsest.orchestrator — heartbeat_cycle, investigate, _worker_alive."""
import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest

from palimpsest.db import migrate
from palimpsest.orchestrator import _worker_alive, heartbeat_cycle, investigate
from palimpsest.scorers.base import Candidate


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.broker = {"url": "http://localhost:9999"}
        self.orchestrator = {
            "heartbeat_interval_secs": 900,
            "low_water_mark": 3,
            "broker_timeout_secs": 2,
        }
        self.gapjoin = {
            "w_cosine": 0.5,
            "w_anchor": 0.3,
            "w_kind": 0.2,
            "score_threshold": 0.65,
            "topk_embedding_candidates": 50,
        }
        self.embed = {"dim": 768, "model": "nomic-embed"}
        self.models = {"keep_alive": "24h"}
        self.features = {"redaction_context_chars": 300, "redaction_context_lines": 2}


@pytest.fixture
def cfg(tmp_path):
    c = DummyConfig(tmp_path)
    migrate(c)
    return c


def _make_scorer_cls(candidates_table: str, run_return=None, top_return=None):
    """Return a mock scorer class whose instances have the right attributes."""
    instance = MagicMock()
    instance.candidates_table = candidates_table
    instance.run.return_value = run_return or []
    instance.top.return_value = top_return or []
    cls = MagicMock(return_value=instance)
    return cls, instance


# ---------------------------------------------------------------------------
# _worker_alive
# ---------------------------------------------------------------------------


def test_worker_alive_ok(cfg):
    with patch("httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=200)
        assert _worker_alive(cfg) is True


def test_worker_alive_503(cfg):
    with patch("httpx.get") as mock_get:
        mock_get.return_value = MagicMock(status_code=503)
        assert _worker_alive(cfg) is False


def test_worker_alive_connection_error(cfg):
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        assert _worker_alive(cfg) is False


def test_worker_alive_no_broker_url(cfg):
    """Missing broker.url → treat as alive."""
    cfg.broker = {}
    assert _worker_alive(cfg) is True


# ---------------------------------------------------------------------------
# heartbeat_cycle
# ---------------------------------------------------------------------------


def test_heartbeat_cycle_runs_scorer_below_low_water(cfg):
    """Scorer below low_water_mark should have .run() called."""
    cand = Candidate("type_e", 0.8, ["doc_a"], ["doc_a p.1"], "test", [])
    scorer_cls, instance = _make_scorer_cls("violation_candidates", run_return=[cand])

    with patch("palimpsest.orchestrator.SCORERS", {"type_e": scorer_cls}), \
         patch("palimpsest.orchestrator._worker_alive", return_value=True):
        results = heartbeat_cycle(cfg)

    assert results.get("type_e") == 1
    instance.run.assert_called_once()


def test_heartbeat_cycle_skips_scorer_above_low_water(cfg):
    """Scorer at or above low_water_mark should NOT have .run() called."""
    # low_water = 3; pre-populate gap_candidates with 5 rows
    conn = sqlite3.connect(cfg.db_path)
    with conn:
        conn.execute("INSERT INTO documents (doc_id) VALUES ('doc_x')")
        conn.execute("INSERT INTO pages (doc_id, page_no) VALUES ('doc_x', 1)")
        for i in range(5):
            conn.execute(
                "INSERT INTO redactions (doc_id, page_no, kind) VALUES ('doc_x', 1, 'deleted_text')"
            )
            conn.execute(
                "INSERT INTO entities (doc_id, page_no, kind, text, norm) "
                "VALUES ('doc_x', 1, 'person', 'X', 'x')"
            )
        redaction_ids = [
            r[0] for r in conn.execute("SELECT redaction_id FROM redactions").fetchall()
        ]
        entity_ids = [
            r[0] for r in conn.execute("SELECT entity_id FROM entities").fetchall()
        ]
        for rid, eid in zip(redaction_ids, entity_ids):
            conn.execute(
                "INSERT INTO gap_candidates (redaction_id, clear_entity_id, score, method) "
                "VALUES (?, ?, 0.75, 'anchor')",
                (rid, eid),
            )
    conn.close()

    scorer_cls, instance = _make_scorer_cls("gap_candidates")

    with patch("palimpsest.orchestrator.SCORERS", {"type_a": scorer_cls}), \
         patch("palimpsest.orchestrator._worker_alive", return_value=True):
        results = heartbeat_cycle(cfg)

    assert results.get("type_a") == 0
    instance.run.assert_not_called()


def test_heartbeat_cycle_logs_warning_when_broker_unreachable(cfg, caplog):
    """Dead broker → WARNING logged, cycle completes."""
    scorer_cls, instance = _make_scorer_cls("outcome_gap_candidates", run_return=[])

    with patch("palimpsest.orchestrator.SCORERS", {"type_d": scorer_cls}), \
         patch("palimpsest.orchestrator._worker_alive", return_value=False), \
         caplog.at_level("WARNING"):
        heartbeat_cycle(cfg)

    assert any("unreachable" in r.message for r in caplog.records)


def test_heartbeat_cycle_continues_after_scorer_exception(cfg):
    """Exception in one scorer must not abort the rest."""
    bad_cls, bad_instance = _make_scorer_cls("series_gap_candidates")
    bad_instance.run.side_effect = RuntimeError("FAISS index not found")

    cand = Candidate("type_d", 0.75, ["doc_a"], ["doc_a p.1"], "test", [])
    good_cls, good_instance = _make_scorer_cls("outcome_gap_candidates", run_return=[cand])

    with patch("palimpsest.orchestrator.SCORERS", {"type_f": bad_cls, "type_d": good_cls}), \
         patch("palimpsest.orchestrator._worker_alive", return_value=True):
        results = heartbeat_cycle(cfg)

    assert results["type_f"] == 0
    assert results["type_d"] == 1
    good_instance.run.assert_called_once()


# ---------------------------------------------------------------------------
# investigate
# ---------------------------------------------------------------------------


def test_investigate_creates_markdown_file(cfg):
    """investigate() writes a markdown file in investigations/."""
    conn = sqlite3.connect(cfg.db_path)
    with conn:
        conn.execute("INSERT INTO documents (doc_id) VALUES ('NV0014689_0001')")

    cand = Candidate(
        "type_e",
        0.82,
        ["NV0014689_0001"],
        ["NV0014689_0001 p.3"],
        "Regulatory violation: CFR 835.209 exceeded",
        [42],
    )
    scorer_cls, instance = _make_scorer_cls(
        "violation_candidates", run_return=[], top_return=[cand]
    )

    with patch("palimpsest.orchestrator.SCORERS", {"type_e": scorer_cls}):
        out = investigate("NV0014689", cfg)

    assert out.exists()
    content = out.read_text()
    assert "NV0014689" in content
    assert "CFR 835.209" in content


def test_investigate_accession_not_found(cfg):
    """Unknown accession → output file created but scorers not called."""
    scorer_cls, instance = _make_scorer_cls("violation_candidates")

    with patch("palimpsest.orchestrator.SCORERS", {"type_e": scorer_cls}):
        out = investigate("UNKNOWN99999", cfg)

    # File path is returned even when empty
    assert out.parent.name == "investigations"
    instance.run.assert_not_called()
    instance.top.assert_not_called()


def test_investigate_file_appends_on_second_call(cfg):
    """investigate() appends to an existing file rather than overwriting."""
    conn = sqlite3.connect(cfg.db_path)
    with conn:
        conn.execute("INSERT INTO documents (doc_id) VALUES ('NV0014689_0001')")

    scorer_cls, _ = _make_scorer_cls("violation_candidates", run_return=[], top_return=[])

    with patch("palimpsest.orchestrator.SCORERS", {"type_e": scorer_cls}):
        out1 = investigate("NV0014689", cfg)
        out2 = investigate("NV0014689", cfg)

    assert out1 == out2
    content = out1.read_text()
    # Two Investigation headers should appear
    assert content.count("## Investigation:") == 2
