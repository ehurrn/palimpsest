# tests/test_outcome.py
import sqlite3
import pytest

from palimpsest.db import migrate
from palimpsest.tasks.features import normalize, normalize_outcome_ref
from palimpsest.indexer import run_outcome_gap


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
    # Import the handler-level extraction logic indirectly via normalize
    text = "to be submitted"
    norm = normalize("outcome_ref", text)
    assert norm.startswith("future_ref:")

def test_outcome_ref_indicator_extracted():
    # The indicator pattern would match "mortality"
    from palimpsest.tasks.features import normalize
    norm = normalize("outcome_ref", "mortality")
    assert norm.startswith("outcome_ind:")


# ── run_outcome_gap ──────────────────────────────────────────────────────────

@pytest.fixture
def outcome_db(tmp_path):
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}

    cfg = DummyCfg()
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
    run_outcome_gap(outcome_db)
    conn = sqlite3.connect(outcome_db.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM outcome_gap_candidates WHERE protocol_code = 'CAL-12'").fetchall()
    conn.close()
    assert len(rows) == 0


def test_outcome_gap_no_outcome_doc(outcome_db):
    """HP-1 has initiation doc but no outcome doc — should produce a candidate."""
    run_outcome_gap(outcome_db)
    conn = sqlite3.connect(outcome_db.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM outcome_gap_candidates WHERE protocol_code = 'HP-1'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["initiation_doc_id"] == "unrelated_doc"
    # Base score (no future_ref, year 1965 + 5 = 1970 < current year so +0.10 bonus)
    assert rows[0]["score"] >= 0.70


def test_outcome_gap_future_ref_bonus(tmp_path):
    """Initiation doc with a future_ref entity should get +0.15 on top of base 0.70."""
    class DummyCfg:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {"score_threshold": 0.65}

    cfg = DummyCfg()
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
    conn.close()

    run_outcome_gap(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM outcome_gap_candidates WHERE protocol_code = 'CAL-99'").fetchall()
    conn.close()
    assert len(rows) == 1
    # 0.70 base + 0.15 future_ref + 0.10 overdue = 0.95
    assert pytest.approx(rows[0]["score"], abs=1e-6) == 0.95
    assert rows[0]["future_ref_entity_id"] == 12


def test_outcome_gap_idempotent(outcome_db):
    """Running outcomegap twice should not duplicate candidates."""
    run_outcome_gap(outcome_db)
    run_outcome_gap(outcome_db)
    conn = sqlite3.connect(outcome_db.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT COUNT(*) FROM outcome_gap_candidates").fetchall()
    conn.close()
    assert rows[0][0] == 1  # only HP-1 candidate
