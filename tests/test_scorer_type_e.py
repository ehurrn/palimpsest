# tests/test_scorer_type_e.py
"""Tests for TypeEScorer (Type e — regulatory violation citation)."""
import sqlite3
import pytest
from palimpsest.db import migrate
from palimpsest.scorers.type_e import TypeEScorer
from palimpsest.scorers.base import Candidate, Scorer


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.gapjoin = {"score_threshold": 0.65}


def _setup(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("DELETE FROM regulation_citations;")
    return cfg, conn


def test_type_e_no_regulations_returns_empty(tmp_path):
    """With no rows in regulation_citations, run() returns []."""
    cfg, conn = _setup(tmp_path)
    scorer = TypeEScorer()
    result = scorer.run(conn, cfg)
    assert result == []


def test_type_e_pre_regulation_violation(tmp_path):
    """A reg_cite entity whose doc_year < reg effective_year scores 0.70 base."""
    cfg, conn = _setup(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('NV0001', 1948)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('NV0001', 1, 'test')")
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'NV0001', 1, 'reg_cite', '45 CFR 46', '45 cfr 46')
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (1, '45 CFR 46', '1991-06-18', 'Common Rule basic protections')
        """)

    scorer = TypeEScorer()
    results = scorer.run(conn, cfg)

    assert len(results) == 1
    assert results[0].type_key == "type_e"
    assert results[0].score == pytest.approx(0.70, abs=1e-6)
    assert results[0].doc_ids == ["NV0001"]
    assert "pre_regulation" in results[0].summary

    # Verify DB row was written
    row = conn.execute("SELECT * FROM violation_candidates").fetchone()
    assert row is not None
    assert row["violation_type"] == "pre_regulation"
    assert row["doc_year"] == 1948


def test_type_e_possible_violation_when_no_temporal_breach(tmp_path):
    """A reg_cite entity where doc_year >= reg effective_year scores 0.65 base."""
    cfg, conn = _setup(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('NV0002', 1995)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('NV0002', 1, 'test')")
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'NV0002', 1, 'reg_cite', '45 CFR 46', '45 cfr 46')
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (1, '45 CFR 46', '1991-06-18', 'Common Rule basic protections')
        """)

    scorer = TypeEScorer()
    results = scorer.run(conn, cfg)

    assert len(results) == 1
    assert results[0].score == pytest.approx(0.65, abs=1e-6)
    assert "possible_violation" in results[0].summary


def test_type_e_corroboration_bonus(tmp_path):
    """Each additional reg_cite on the same page adds 0.10 to the score (capped 0.95)."""
    cfg, conn = _setup(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('NV0003', 1948)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('NV0003', 1, 'test')")
        # Two reg_cite entities on the same page
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (3, 'NV0003', 1, 'reg_cite', '45 CFR 46', '45 cfr 46')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (4, 'NV0003', 1, 'reg_cite', 'Common Rule', 'common rule')
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (1, '45 CFR 46', '1991-06-18', 'Common Rule basic protections')
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (2, 'Common Rule', '1991-06-18', 'Common Rule policy')
        """)

    scorer = TypeEScorer()
    results = scorer.run(conn, cfg)

    # Each entity scored with 1 corroborator → 0.70 + 0.10 = 0.80
    assert len(results) == 2
    for r in results:
        assert r.score == pytest.approx(0.80, abs=1e-6)


def test_type_e_idempotent(tmp_path):
    """Calling run() twice does not create duplicate rows."""
    cfg, conn = _setup(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('NV0001', 1948)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('NV0001', 1, 'test')")
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'NV0001', 1, 'reg_cite', '45 CFR 46', '45 cfr 46')
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (1, '45 CFR 46', '1991-06-18', 'Common Rule basic protections')
        """)

    scorer = TypeEScorer()
    scorer.run(conn, cfg)
    scorer.run(conn, cfg)

    count = conn.execute("SELECT COUNT(*) FROM violation_candidates").fetchone()[0]
    assert count == 1


def test_type_e_top_returns_candidates_ordered_by_score(tmp_path):
    """top() returns Candidate objects ordered by score DESC."""
    cfg, conn = _setup(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('NV0001', 1948)")
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('NV0002', 1948)")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('NV0001', 1, 'test')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('NV0002', 1, 'test')")
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'NV0001', 1, 'reg_cite', '45 CFR 46', '45 cfr 46')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'NV0002', 1, 'reg_cite', '45 CFR 46', '45 cfr 46')
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (1, '45 CFR 46', '1991-06-18', 'Common Rule')
        """)
        # Insert two rows manually with known scores
        conn.execute("""
            INSERT INTO violation_candidates
              (doc_id, page_no, reg_id, reg_cite_entity_id, doc_year, violation_type, score, status)
            VALUES ('NV0001', 1, 1, 1, 1948, 'pre_regulation', 0.80, 'candidate')
        """)
        conn.execute("""
            INSERT INTO violation_candidates
              (doc_id, page_no, reg_id, reg_cite_entity_id, doc_year, violation_type, score, status)
            VALUES ('NV0002', 1, 1, 2, 1948, 'pre_regulation', 0.70, 'candidate')
        """)

    scorer = TypeEScorer()
    results = scorer.top(conn, limit=10)

    assert len(results) == 2
    assert all(isinstance(r, Candidate) for r in results)
    assert results[0].score == pytest.approx(0.80, abs=1e-6)
    assert results[1].score == pytest.approx(0.70, abs=1e-6)


def test_type_e_top_respects_limit(tmp_path):
    cfg, conn = _setup(tmp_path)

    with conn:
        for i in range(5):
            conn.execute(f"""
                INSERT INTO violation_candidates
                  (doc_id, page_no, reg_id, reg_cite_entity_id, doc_year,
                   violation_type, score, status)
                VALUES ('NV{i:04d}', 1, 1, {i+1}, 1948, 'pre_regulation', {0.7 + i*0.01}, 'candidate')
            """)

    scorer = TypeEScorer()
    results = scorer.top(conn, limit=3)
    assert len(results) == 3


def test_violation_join_migrated(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)

    conn = sqlite3.connect(cfg.db_path)
    with conn:
        # Clear regulation_citations so we control the IDs
        conn.execute("DELETE FROM regulation_citations;")
        conn.execute("INSERT INTO documents (doc_id, year, status) VALUES ('doc_A', 1990, 'indexed');")
        conn.execute("INSERT INTO documents (doc_id, year, status) VALUES ('doc_B', 1995, 'indexed');")

        # Insert pages
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_A', 1, 'References 45 CFR 46.');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_B', 1, 'References 45 CFR 46.');")

        # Insert reg_cite entities
        # matched_reg_id will be mapped to "45 CFR 46"
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (1, 'doc_A', 1, 'reg_cite', '45 CFR 46', '45 cfr 46', 11, 20);
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (2, 'doc_B', 1, 'reg_cite', '45 CFR 46', '45 cfr 46', 11, 20);
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (1, '45 CFR 46', '1991-06-18', 'Common Rule basic protections')
        """)
    conn.close()

    # Run violation join via TypeEScorer
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    scorer = TypeEScorer()
    results = scorer.run(conn, cfg)

    # Verify violation_candidates
    candidates = conn.execute("SELECT * FROM violation_candidates ORDER BY doc_id").fetchall()
    assert len(candidates) == 2

    cand_a = candidates[0]
    assert cand_a["doc_id"] == "doc_A"
    assert cand_a["doc_year"] == 1990
    assert cand_a["violation_type"] == "pre_regulation"
    assert cand_a["score"] == 0.70

    cand_b = candidates[1]
    assert cand_b["doc_id"] == "doc_B"
    assert cand_b["doc_year"] == 1995
    assert cand_b["violation_type"] == "possible_violation"
    assert cand_b["score"] == 0.65

    # Add a corroborating reg_cite entity to doc_A page 1
    with conn:
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (3, 'doc_A', 1, 'reg_cite', 'Belmont Report', 'belmont report', 25, 39);
        """)
        conn.execute("""
            INSERT INTO regulation_citations (reg_id, citation, effective_date, text_snippet)
            VALUES (2, 'Belmont Report', '1979-04-18', 'Belmont Report')
        """)

    # Clear and rerun
    with conn:
        conn.execute("DELETE FROM violation_candidates;")

    results_after = scorer.run(conn, cfg)

    candidates_after = conn.execute("SELECT * FROM violation_candidates WHERE doc_id = 'doc_A' ORDER BY reg_id").fetchall()
    assert len(candidates_after) == 2

    # Match for reg_id = 1 (45 CFR 46)
    cand_a_1 = next(c for c in candidates_after if c["reg_id"] == 1)
    assert pytest.approx(cand_a_1["score"], abs=1e-6) == 0.8

    conn.close()


def test_type_e_conforms_to_scorer_protocol():
    scorer = TypeEScorer()
    assert isinstance(scorer, Scorer)
    assert scorer.type_key == "type_e"
    assert scorer.candidates_table == "violation_candidates"


