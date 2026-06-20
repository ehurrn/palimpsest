# tests/test_identity.py
"""Tests for Type-c anonymous identity linkage (TypeCScorer)."""
import sqlite3

import pytest

from palimpsest.db import migrate
from palimpsest.indexer import _edit_distance
from palimpsest.scorers.type_c import TypeCScorer

# ---------------------------------------------------------------------------
# Mock embed function — deterministic, injectable
# ---------------------------------------------------------------------------

def _mock_embed(texts: list[str]) -> list[list[float]]:
    """Unit vector based on first character of each string.

    Strings starting with the same character get cosine=1.0.
    Strings starting with different characters get cosine=0.0.
    """
    dim = 8
    vecs = []
    for t in texts:
        v = [0.0] * dim
        idx = ord(t[0]) % dim if t else 0
        v[idx] = 1.0
        vecs.append(v)
    return vecs


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
# Unit: basic linkage — same decade
# ---------------------------------------------------------------------------

def test_basic_linkage_same_decade(tmp_path):
    """Subject and person in same decade (1950s): cosine=1.0 > 0.65 → one row inserted."""
    cfg, conn = _setup_db(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1955)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_subj', 1, 'test')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_named', 1, 'test')")

        # Subject page: subject_ref + org (qualifies the EXISTS filter)
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Subject 3', 'subject 3')""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'org', 'Los Alamos National Lab', 'los alamos national lab')""")

        # Named page: person
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'doc_named', 1, 'person', 'John Smith', 'john smith')""")

    TypeCScorer(embed_fn=_mock_embed).run(conn, cfg)

    rows = conn.execute("SELECT * FROM identity_link_candidates").fetchall()
    assert len(rows) == 1
    row = rows[0]
    # Both profile strings start with 'E' (Entity type: ...) → cosine = 1.0
    assert pytest.approx(row["score"], abs=1e-6) == 1.0
    assert row["score"] >= 0.65


# ---------------------------------------------------------------------------
# Unit: decade filter excludes distant persons
# ---------------------------------------------------------------------------

def test_decade_filter_excludes_distant(tmp_path):
    """Person A is same decade (1950s) → compared and inserted.
    Person B is decade 1980 → not in [1940,1950,1960] → NOT compared → not inserted.
    """
    cfg, conn = _setup_db(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named_exact', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named_far', 1985)")
        for doc in ("doc_subj", "doc_named_exact", "doc_named_far"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        # Subject page: subject_ref + org (decade 1950)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Subject A', 'subject a')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'org', 'Nevada Test Site', 'nevada test site')""")

        # Named page exact year (decade 1950 → in window)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named_exact', 1, 'person', 'Jane Doe', 'jane doe')""")

        # Named page far year (decade 1980 → outside ±1 of 1950)
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (6, 'doc_named_far', 1, 'person', 'Bob Brown', 'bob brown')""")

    TypeCScorer(embed_fn=_mock_embed).run(conn, cfg)

    rows = conn.execute(
        "SELECT * FROM identity_link_candidates ORDER BY score DESC"
    ).fetchall()

    assert len(rows) == 1
    assert rows[0]["named_doc_id"] == "doc_named_exact"


# ---------------------------------------------------------------------------
# Unit: threshold gating
# ---------------------------------------------------------------------------

def test_threshold_gating(tmp_path):
    """With threshold=0.40, cosine=1.0 (same decade, same first char) → inserted."""
    cfg, conn = _setup_db(tmp_path)
    cfg.gapjoin = {"score_threshold": 0.40}

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1958)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1958)")
        for doc in ("doc_subj", "doc_named"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Subject 7', 'subject 7')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'dosage', '50 rem', '50 rem')""")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named', 1, 'person', 'Alice Grey', 'alice grey')""")

    TypeCScorer(embed_fn=_mock_embed).run(conn, cfg)

    rows = conn.execute("SELECT * FROM identity_link_candidates").fetchall()
    assert len(rows) == 1
    assert rows[0]["score"] > 0.4


# ---------------------------------------------------------------------------
# Unit: threshold higher than cosine — nothing inserted
# ---------------------------------------------------------------------------

def test_threshold_higher_than_cosine(tmp_path):
    """With threshold=2.0 (impossible), no row should be inserted regardless of cosine."""
    cfg, conn = _setup_db(tmp_path)
    cfg.gapjoin = {"score_threshold": 2.0}

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1958)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1958)")
        for doc in ("doc_subj", "doc_named"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Patient B', 'patient b')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'dosage', '50 rem', '50 rem')""")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'doc_named', 1, 'person', 'Tom Evans', 'tom evans')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (5, 'doc_named', 1, 'dosage', '50 rem', '50 rem')""")

    TypeCScorer(embed_fn=_mock_embed).run(conn, cfg)

    rows = conn.execute("SELECT * FROM identity_link_candidates").fetchall()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Unit: score below threshold — not inserted (decade-based exclusion)
# ---------------------------------------------------------------------------

def test_below_threshold_not_inserted(tmp_path):
    """Subject in decade 1950, person in decade 1980 (3 decades away, not in ±1).
    Not compared at all → len(rows)==0.
    """
    cfg, conn = _setup_db(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_subj', 1955)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_named', 1985)")
        for doc in ("doc_subj", "doc_named"):
            conn.execute(f"INSERT INTO pages (doc_id, page_no, text) VALUES ('{doc}', 1, 'test')")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_subj', 1, 'subject_ref', 'Case 9', 'case 9')""")
        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_subj', 1, 'date', '1955', '1955')""")

        conn.execute("""INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'doc_named', 1, 'person', 'Carl White', 'carl white')""")

    TypeCScorer(embed_fn=_mock_embed).run(conn, cfg)

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

        # Insert a candidate directly (org_match/date_proximity/dosage_bonus are nullable)
        conn.execute("""INSERT INTO identity_link_candidates
            (subject_doc_id, subject_page, subject_ref,
             named_doc_id, named_page, named_entity_id,
             org_match, date_proximity, dosage_bonus, score, status)
            VALUES ('doc_subj', 1, 'Subject X', 'doc_named', 1, 3, 1.0, 0.0, 0.0, 0.5, 'candidate')""")

    # Simulate user input: 'v' (verify) then 's' (skip) then 'q' (quit)
    inputs = iter(["TEST", "v", "s", "q"])
    monkeypatch.setattr("builtins.input", lambda _="": next(inputs))

    import io
    import sys
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
