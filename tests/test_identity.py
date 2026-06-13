# tests/test_identity.py
"""Tests for Type-c anonymous identity linkage (run_identity_link)."""
import sqlite3
import pytest
from palimpsest.db import migrate
from palimpsest.indexer import run_identity_link, _edit_distance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.gapjoin = {"score_threshold": 0.65}


def _setup_db(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return cfg, conn


# ---------------------------------------------------------------------------
# Unit: _edit_distance
# ---------------------------------------------------------------------------

def test_edit_distance_identical():
    assert _edit_distance("Los Alamos", "Los Alamos") == 0


def test_edit_distance_case_insensitive():
    assert _edit_distance("LOS ALAMOS", "los alamos") == 0


def test_edit_distance_one_typo():
    assert _edit_distance("Los Almos", "Los Alamos") == 1


def test_edit_distance_far_apart():
    assert _edit_distance("Hanford", "Los Alamos") > 2


# ---------------------------------------------------------------------------
# Unit: org_match score
# ---------------------------------------------------------------------------

def test_org_match_score(tmp_path):
    """Edit-distance ≤ 2 between any org pair should score 1.0, contributing 0.5 to total.
    Combined with exact year match (date_proximity=1.0, contributing 0.3), score = 0.8 >= 0.65.
    """
    cfg, conn = _setup_db(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1955)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_subj', 1, 'test')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_named', 1, 'test')")

        # Subject page: subject_ref + org + date 1955
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Subject 3', 'subject 3')""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'org', 'Los Alamos National Lab', 'los alamos national lab')""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (5, 'doc_subj', 1, 'date', '1955', '1955')""")

        # Named page: person + org with one-letter typo (edit distance = 1)
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'doc_named', 1, 'person', 'John Smith', 'john smith')""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named', 1, 'org', 'Los Almos National Lab', 'los almos national lab')""")

    run_identity_link(cfg)

    rows = conn.execute("SELECT * FROM identity_link_candidates").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert pytest.approx(row["org_match"], abs=1e-6) == 1.0
    # score = 1.0*0.5 + 1.0*0.3 + 0.0 = 0.8
    assert pytest.approx(row["score"], abs=1e-6) == 0.8
    assert row["score"] >= 0.65


# ---------------------------------------------------------------------------
# Unit: date_proximity score
# ---------------------------------------------------------------------------

def test_date_proximity_score(tmp_path):
    """Year gap of 0 → proximity 1.0; gap of 3 → proximity 0.0."""
    cfg, conn = _setup_db(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named_exact', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named_far', 1960)")
        for doc in ("doc_subj", "doc_named_exact", "doc_named_far"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        # Subject page: subject_ref + org + date 1955
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Subject A', 'subject a')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'org', 'Nevada Test Site', 'nevada test site')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'doc_subj', 1, 'date', '1955', '1955')""")

        # Named page exact year
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named_exact', 1, 'person', 'Jane Doe', 'jane doe')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (5, 'doc_named_exact', 1, 'org', 'Nevada Test Site', 'nevada test site')""")

        # Named page far year (gap 5 → proximity=0)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (6, 'doc_named_far', 1, 'person', 'Bob Brown', 'bob brown')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (7, 'doc_named_far', 1, 'org', 'Nevada Test Site', 'nevada test site')""")

    run_identity_link(cfg)

    rows = conn.execute(
        "SELECT * FROM identity_link_candidates ORDER BY score DESC"
    ).fetchall()

    # doc_named_exact should score higher (date proximity=1.0) than doc_named_far (proximity=0)
    assert len(rows) >= 1
    exact = next((r for r in rows if r["named_doc_id"] == "doc_named_exact"), None)
    assert exact is not None
    assert pytest.approx(exact["date_proximity"], abs=1e-6) == 1.0

    # doc_named_far: gap = 5, date_proximity = max(0, 1-5/3) = 0 → only org contributes
    # score = 1.0*0.5 + 0.0*0.3 = 0.5 < threshold 0.65, so NOT inserted
    far = next((r for r in rows if r["named_doc_id"] == "doc_named_far"), None)
    assert far is None


# ---------------------------------------------------------------------------
# Unit: dosage_bonus
# ---------------------------------------------------------------------------

def test_dosage_bonus(tmp_path):
    """Shared normalized dosage between subject and named page adds 0.2 bonus.

    Formula: score = org_match*0.5 + date_proximity*0.3 + dosage_bonus
    Here: no org, date_proximity=1.0 (exact year match), dosage_bonus=0.2
    score = 0.0 + 0.3 + 0.2 = 0.5 — below default threshold 0.65.
    Use lower threshold to confirm the bonus value itself is computed correctly.
    """
    cfg, conn = _setup_db(tmp_path)
    cfg.gapjoin = {"score_threshold": 0.40}  # lower to isolate dosage_bonus check

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1958)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1958)")
        for doc in ("doc_subj", "doc_named"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        # Subject page: subject_ref + date + dosage
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Subject 7', 'subject 7')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'date', '1958', '1958')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'doc_subj', 1, 'dosage', '50 rem', '50 rem')""")

        # Named page: person + same dosage (doc year = 1958 → date_proximity = 1.0)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named', 1, 'person', 'Alice Grey', 'alice grey')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (5, 'doc_named', 1, 'dosage', '50 rem', '50 rem')""")

    run_identity_link(cfg)

    rows = conn.execute("SELECT * FROM identity_link_candidates").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert pytest.approx(row["dosage_bonus"], abs=1e-6) == 0.2
    # score = 0.0*0.5 + 1.0*0.3 + 0.2 = 0.5
    assert pytest.approx(row["score"], abs=1e-6) == 0.5


def test_dosage_bonus_applied(tmp_path):
    """dosage_bonus combined with date_proximity pushes score above a lower threshold.

    Formula: score = 0.0*0.5 + 1.0*0.3 + 0.2 = 0.5 > threshold=0.40
    Confirms the bonus is incorporated into the final score.
    """
    cfg, conn = _setup_db(tmp_path)
    cfg.gapjoin = {"score_threshold": 0.40}

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1958)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1958)")
        for doc in ("doc_subj", "doc_named"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Patient B', 'patient b')""")
        # date entity so date_proximity = 1.0 (matches doc_named year=1958)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'date', '1958', '1958')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'doc_subj', 1, 'dosage', '50 rem', '50 rem')""")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named', 1, 'person', 'Tom Evans', 'tom evans')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (5, 'doc_named', 1, 'dosage', '50 rem', '50 rem')""")

    run_identity_link(cfg)

    rows = conn.execute("SELECT * FROM identity_link_candidates").fetchall()
    assert len(rows) == 1
    assert pytest.approx(rows[0]["dosage_bonus"], abs=1e-6) == 0.2
    # score = 0.0*0.5 + 1.0*0.3 + 0.2 = 0.5
    assert pytest.approx(rows[0]["score"], abs=1e-6) == 0.5


# ---------------------------------------------------------------------------
# Unit: score below threshold — not inserted
# ---------------------------------------------------------------------------

def test_below_threshold_not_inserted(tmp_path):
    """Pairs that score below threshold must not appear in identity_link_candidates."""
    cfg, conn = _setup_db(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1975)")
        for doc in ("doc_subj", "doc_named"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        # Subject: subject_ref + date 1955
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Case 9', 'case 9')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'date', '1955', '1955')""")

        # Named: different year (1975), no org overlap → low score
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'doc_named', 1, 'person', 'Carl White', 'carl white')""")

    run_identity_link(cfg)

    rows = conn.execute("SELECT * FROM identity_link_candidates").fetchall()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Integration: identity gate enforced in review
# ---------------------------------------------------------------------------

def test_identity_gate_locked_in_review(tmp_path, monkeypatch):
    """handle_links must not allow verifying a link when gate is locked."""
    from palimpsest.review import handle_links

    cfg, conn = _setup_db(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1955)")
        for doc in ("doc_subj", "doc_named"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Subject X', 'subject x')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'org', 'Sandia', 'sandia')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm,
            living_status) VALUES (3, 'doc_named', 1, 'person', 'Real Name', 'real name', 'unknown')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named', 1, 'org', 'Sandia', 'sandia')""")

        # Insert a candidate directly
        conn.execute("""INSERT INTO identity_link_candidates
            (subject_doc_id, subject_page, subject_ref,
             named_doc_id, named_page, named_entity_id,
             org_match, date_proximity, dosage_bonus, score, status)
            VALUES ('doc_subj', 1, 'Subject X', 'doc_named', 1, 3, 1.0, 0.0, 0.0, 0.5, 'candidate')""")

    # Simulate user input: 'v' (verify) then 's' (skip) then 'q' (quit)
    inputs = iter(["TEST", "v", "s", "q"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))

    import io, sys
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    handle_links(cfg)

    output = captured.getvalue()
    # Gate should be LOCKED (person not approved)
    assert "GATE LOCKED" in output
    assert "Cannot verify" in output
    # The linkage row must remain 'candidate' (not verified)
    row = conn.execute("SELECT status FROM identity_link_candidates WHERE ilc_id=1").fetchone()
    assert row["status"] == "candidate"
