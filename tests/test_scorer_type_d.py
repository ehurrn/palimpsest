# tests/test_scorer_type_d.py
import sqlite3
import pytest
from typing import cast
from unittest.mock import patch
import datetime

from palimpsest.config import Config
from palimpsest.db import migrate
from palimpsest.tasks.features import normalize, normalize_outcome_ref
from palimpsest.scorers.type_d import TypeDScorer
from palimpsest.scorers.base import Candidate


# ── normalize_outcome_ref ────────────────────────────────────────────────────

def test_normalize_outcome_ref_future_ref():
    assert normalize_outcome_ref("to be submitted") == "future_ref:to be submitted"

def test_normalize_outcome_ref_annual_report():
    assert normalize_outcome_ref("annual report due") == "future_ref:annual report due"

def test_normalize_outcome_ref_outcome_indicator():
    assert normalize_outcome_ref("mortality rates").startswith("outcome_ind:")

def test_normalize_outcome_ref_survival():
    assert normalize_outcome_ref("survival rates").startswith("outcome_ind:")

def test_normalize_dispatches_outcome_ref():
    assert normalize("outcome_ref", "to be submitted").startswith("future_ref:")
    assert normalize("outcome_ref", "mortality rates").startswith("outcome_ind:")


# ── outcome_ref entity extraction ────────────────────────────────────────────

def test_outcome_ref_future_ref_extracted():
    """features.py should extract 'to be submitted' as outcome_ref kind."""
    text = "to be submitted"
    norm = normalize("outcome_ref", text)
    assert norm.startswith("future_ref:")

def test_outcome_ref_indicator_extracted():
    norm = normalize("outcome_ref", "mortality")
    assert norm.startswith("outcome_ind:")


# ── TypeDScorer Tests ────────────────────────────────────────────────────────

@pytest.fixture
def outcome_db(tmp_path):
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}

    cfg = cast(Config, DummyCfg())
    migrate(cfg)

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    with conn:
        # Two documents
        conn.execute("INSERT INTO documents (doc_id, year, status) VALUES ('init_doc', 1960, 'indexed')")
        conn.execute("INSERT INTO documents (doc_id, year, status) VALUES ('outcome_doc', 1963, 'indexed')")
        conn.execute("INSERT INTO documents (doc_id, year, status) VALUES ('unrelated_doc', 1965, 'indexed')")

        # Pages
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('init_doc', 1, 'CAL-12 protocol initiated.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('outcome_doc', 1, 'CAL-12 mortality results.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('unrelated_doc', 1, 'HP-1 protocol initiated.')")

        # Protocol code entities
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (1, 'init_doc', 1, 'protocol_code', 'CAL-12', 'CAL-12', 0, 6)""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (2, 'outcome_doc', 1, 'protocol_code', 'CAL-12', 'CAL-12', 0, 6)""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (3, 'unrelated_doc', 1, 'protocol_code', 'HP-1', 'HP-1', 0, 4)""")

        # Date entity on init_doc page 1 (marks it as initiation doc)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (4, 'init_doc', 1, 'date', '1960-01-01', '1960-01-01', 10, 20)""")
        # Date entity on unrelated_doc page 1
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (5, 'unrelated_doc', 1, 'date', '1965-05-01', '1965-05-01', 8, 18)""")

        # outcome_ref on outcome_doc (marks it as outcome doc for CAL-12)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (6, 'outcome_doc', 1, 'outcome_ref', 'mortality', 'outcome_ind:mortality', 10, 19)""")

    conn.close()
    return cfg


def test_outcome_gap_with_outcome_doc(outcome_db):
    """CAL-12 has an outcome doc, so no candidate should be created."""
    conn = sqlite3.connect(outcome_db.db_path)
    conn.row_factory = sqlite3.Row
    scorer = TypeDScorer()
    candidates = scorer.run(conn, outcome_db)
    assert not any("CAL-12" in c.summary for c in candidates)
    rows = conn.execute("SELECT * FROM outcome_gap_candidates WHERE protocol_code = 'CAL-12'").fetchall()
    conn.close()
    assert len(rows) == 0


def test_outcome_gap_no_outcome_doc(outcome_db):
    """HP-1 has initiation doc but no outcome doc — should produce a candidate."""
    conn = sqlite3.connect(outcome_db.db_path)
    conn.row_factory = sqlite3.Row
    scorer = TypeDScorer()
    candidates = scorer.run(conn, outcome_db)
    assert len(candidates) == 1
    assert candidates[0].type_key == "type_d"
    assert candidates[0].score >= 0.70
    
    rows = conn.execute("SELECT * FROM outcome_gap_candidates WHERE protocol_code = 'HP-1'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["initiation_doc_id"] == "unrelated_doc"
    assert rows[0]["score"] >= 0.70


def test_outcome_gap_future_ref_bonus(tmp_path):
    """Initiation doc with a future_ref entity should get +0.15 on top of base 0.70."""
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}

    cfg = cast(Config, DummyCfg())
    migrate(cfg)

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("INSERT INTO documents (doc_id, year, status) VALUES ('doc_x', 1955, 'indexed')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_x', 1, 'CAL-99 study, to be submitted.')")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (10, 'doc_x', 1, 'protocol_code', 'CAL-99', 'CAL-99', 0, 6)""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (11, 'doc_x', 1, 'date', '1955-03-01', '1955-03-01', 10, 20)""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
                        VALUES (12, 'doc_x', 1, 'outcome_ref', 'to be submitted', 'future_ref:to be submitted', 25, 40)""")
    
    scorer = TypeDScorer()
    candidates = scorer.run(conn, cfg)
    assert len(candidates) == 1
    assert pytest.approx(candidates[0].score, abs=1e-6) == 0.95
    
    rows = conn.execute("SELECT * FROM outcome_gap_candidates WHERE protocol_code = 'CAL-99'").fetchall()
    conn.close()
    assert len(rows) == 1
    # 0.70 base + 0.15 future_ref + 0.10 overdue = 0.95
    assert pytest.approx(rows[0]["score"], abs=1e-6) == 0.95
    assert rows[0]["future_ref_entity_id"] == 12


def test_outcome_gap_idempotent(outcome_db):
    """Running outcomegap twice should not duplicate candidates."""
    conn = sqlite3.connect(outcome_db.db_path)
    conn.row_factory = sqlite3.Row
    scorer = TypeDScorer()
    scorer.run(conn, outcome_db)
    scorer.run(conn, outcome_db)
    rows = conn.execute("SELECT COUNT(*) FROM outcome_gap_candidates").fetchall()
    conn.close()
    assert rows[0][0] == 1  # only HP-1 candidate


def test_type_d_no_protocol_codes_returns_empty(tmp_path):
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}
    cfg = cast(Config, DummyCfg())
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    scorer = TypeDScorer()
    assert scorer.run(conn, cfg) == []
    conn.close()


def test_type_d_base_score_0_70(tmp_path):
    """Initiation doc with no outcome doc and no future_ref: score = 0.70."""
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}
    cfg = cast(Config, DummyCfg())
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_init', 2000)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_init', 1, 'x')")
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_init', 1, 'protocol_code', 'CAL-12', 'CAL-12')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_init', 1, 'date', '2000', '2000')
        """)

    scorer = TypeDScorer()
    # Patch current year to 2003 to get base only (overdue needs > 5 years, i.e. 2000 + 5 = 2005)
    with patch("palimpsest.scorers.type_d.datetime") as mock_dt:
        mock_dt.datetime.now.return_value.year = 2003
        results = scorer.run(conn, cfg)

    assert len(results) == 1
    assert results[0].score == pytest.approx(0.70, abs=1e-6)
    assert results[0].type_key == "type_d"
    assert "CAL-12" in results[0].summary
    conn.close()


def test_type_d_overdue_bonus(tmp_path):
    """current_year > start_year + 5 adds +0.10."""
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}
    cfg = cast(Config, DummyCfg())
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_init', 2000)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_init', 1, 'x')")
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_init', 1, 'protocol_code', 'CAL-12', 'CAL-12')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_init', 1, 'date', '2000', '2000')
        """)

    scorer = TypeDScorer()
    with patch("palimpsest.scorers.type_d.datetime") as mock_dt:
        mock_dt.datetime.now.return_value.year = 2010  # > 2000 + 5
        results = scorer.run(conn, cfg)

    assert len(results) == 1
    assert results[0].score == pytest.approx(0.80, abs=1e-6)
    conn.close()


def test_type_d_top_returns_ordered_candidates(tmp_path):
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}
    cfg = cast(Config, DummyCfg())
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    with conn:
        conn.execute("""
            INSERT INTO outcome_gap_candidates
              (protocol_code, initiation_doc_id, start_year, future_ref_entity_id, score)
            VALUES ('CAL-12', 'doc_1', 2000, NULL, 0.85)
        """)
        conn.execute("""
            INSERT INTO outcome_gap_candidates
              (protocol_code, initiation_doc_id, start_year, future_ref_entity_id, score)
            VALUES ('HP-7', 'doc_2', 1998, NULL, 0.70)
        """)

    scorer = TypeDScorer()
    results = scorer.top(conn, limit=10)

    assert len(results) == 2
    assert all(isinstance(r, Candidate) for r in results)
    assert results[0].score == pytest.approx(0.85, abs=1e-6)
    assert results[1].score == pytest.approx(0.70, abs=1e-6)
    conn.close()
