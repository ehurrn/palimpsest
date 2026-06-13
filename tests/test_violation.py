# tests/test_violation.py
import sqlite3
import pytest
from palimpsest.db import migrate
from palimpsest.indexer import run_violation_join

def test_violation_join(tmp_path):
    class DummyConfig:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {
            "score_threshold": 0.65
        }
    
    cfg = DummyConfig()
    migrate(cfg)
    
    conn = sqlite3.connect(cfg.db_path)
    with conn:
        # Insert test document with year 1990 (violates 1991 CFR)
        conn.execute("INSERT INTO documents (doc_id, year, status) VALUES ('doc_A', 1990, 'indexed');")
        # Insert test document with year 1995 (conforms/does not violate temporally)
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
    conn.close()
    
    # Run violation join
    run_violation_join(cfg)
    
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    
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
    
    # Clear and rerun
    with conn:
        conn.execute("DELETE FROM violation_candidates;")
    
    run_violation_join(cfg)
    
    candidates_after = conn.execute("SELECT * FROM violation_candidates WHERE doc_id = 'doc_A' ORDER BY reg_id").fetchall()
    assert len(candidates_after) == 2
    
    # Match for reg_id = 1 (45 CFR 46)
    cand_a_1 = next(c for c in candidates_after if c["reg_id"] == 1)
    assert pytest.approx(cand_a_1["score"], abs=1e-6) == 0.8
    
    conn.close()
