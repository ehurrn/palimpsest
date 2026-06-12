# tests/test_gapjoin.py
import datetime
import os
import sqlite3
import pytest
import numpy as np
import faiss

from palimpsest.config import Config
from palimpsest.db import migrate
from palimpsest.indexer import run_gapjoin, get_slot_expectation

@pytest.fixture
def gapjoin_db(tmp_path):
    # Setup temp config
    class DummyConfig:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {
            "w_cosine": 0.5,
            "w_anchor": 0.3,
            "w_kind": 0.2,
            "score_threshold": 0.65,
            "topk_embedding_candidates": 50
        }
        embed = {
            "dim": 768,
            "model": "nomic-embed"
        }
        models = {
            "keep_alive": "24h"
        }
        
    cfg = DummyConfig()
    migrate(cfg)
    
    # Insert mock data
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    
    with conn:
        # 1. Documents
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_A', 'indexed');")
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_B', 'indexed');")
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_C', 'indexed');")
        
        # 2. Pages
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_A', 1, 'This has a [deleted] redaction here.');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_B', 1, 'Clear text with oak ridge and 1957-03-02.');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_C', 1, 'Unrelated page contents.');")
        
        # 3. Redaction in Doc A (context_before/after length >= 40)
        # Context combined length = 20 + 20 + 1 = 41 >= 40
        conn.execute("""
            INSERT INTO redactions (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after)
            VALUES (100, 'doc_A', 1, 'deleted_text', 'DELETED', 0.1, 0.1, 0.2, 0.2, 
                    'preceding context info here', 'succeeding context info here')
        """)
        
        # Page A anchors
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (1, 'doc_A', 1, 'location', 'Oak Ridge', 'oak ridge', 10, 20, 0.1, 0.1, 0.2, 0.15);")
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (2, 'doc_A', 1, 'date', '1957-03-02', '1957-03-02', 25, 35, 0.1, 0.16, 0.2, 0.22);")
        
        # Page B clear anchors + target entities (dosage and person)
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (3, 'doc_B', 1, 'location', 'Oak Ridge', 'oak ridge', 10, 20, 0.1, 0.1, 0.2, 0.15);")
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (4, 'doc_B', 1, 'date', '1957-03-02', '1957-03-02', 25, 35, 0.1, 0.16, 0.2, 0.22);")
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (5, 'doc_B', 1, 'dosage', '15 rem', '15 rem', 40, 50, 0.1, 0.23, 0.2, 0.28);")
        # Person target entity
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (6, 'doc_B', 1, 'person', 'John Smith', 'john smith', 55, 65, 0.1, 0.29, 0.2, 0.35);")
        
        # Page C clear entities (shares nothing)
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1) VALUES (7, 'doc_C', 1, 'person', 'Alice Johnson', 'alice johnson', 10, 20, 0.1, 0.1, 0.2, 0.15);")

        
        # Chunks for FAISS index (doc B chunk)
        conn.execute("INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) VALUES (500, 'doc_B', 1, 0, 100, 'Clear text with oak ridge and 1957-03-02.');")
        conn.execute("INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) VALUES (600, 'doc_C', 1, 0, 100, 'Unrelated page contents.');")
        
    conn.close()
    
    # Create mock FAISS index
    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(768))
    
    # Chunk 500 vector (doc B): let's make it have cosine similarity = 0.8 with query
    # E.g., query = [1, 0, 0, ...], chunk 500 = [0.8, 0.6, 0, ...]
    v_b = np.zeros(768, dtype=np.float32)
    v_b[0] = 0.8
    v_b[1] = 0.6
    # Chunk 600 vector (doc C): orthogonal
    v_c = np.zeros(768, dtype=np.float32)
    v_c[2] = 1.0
    
    # Add to index
    index.add_with_ids(np.array([v_b, v_c]), np.array([500, 600], dtype=np.int64))
    faiss.write_index(index, str(index_dir / "faiss.idx"))
    
    return cfg

def test_slot_expectation_heuristic():
    assert get_slot_expectation("exemption_stamp", "(b)(6)") == "person"
    assert get_slot_expectation("exemption_stamp", "(b)(7)") == "person"
    assert get_slot_expectation("exemption_stamp", "(b)(1)") is None
    assert get_slot_expectation("deleted_text", "DELETED") is None
    assert get_slot_expectation("black_box", None) is None

def test_gapjoin_algorithm(gapjoin_db):
    # Mock query embedding: query = [1, 0, 0, ...] (gives cosine similarity 0.8 with chunk 500)
    q_vec = [1.0] + [0.0] * 767
    
    def mock_embed(cfg, text):
        return q_vec
        
    run_gapjoin(gapjoin_db, mock_embed)
    
    # Assertions on database state
    conn = sqlite3.connect(gapjoin_db.db_path)
    conn.row_factory = sqlite3.Row
    
    # 1. Check gapjoin_runs table has redaction 100
    cur = conn.execute("SELECT * FROM gapjoin_runs WHERE redaction_id = 100")
    run = cur.fetchone()
    assert run is not None
    
    # 2. Check gap_candidates table
    # It should find candidate entities on doc_B page (entities 3, 4, 5, 6) but not doc_C (entity 7)
    cur = conn.execute("SELECT * FROM gap_candidates ORDER BY clear_entity_id")
    candidates = cur.fetchall()
    assert len(candidates) == 4
    
    # Entity 5 (15 rem):
    # - score_cosine = 0.8
    # - score_anchor = 1.0 (shares oak ridge and 1957-03-02, which is 2/2)
    # - score_kind = 0.5 (no slot expectation)
    # - score = 0.5*0.8 + 0.3*1.0 + 0.2*0.5 = 0.4 + 0.3 + 0.1 = 0.8
    cand5 = next(c for c in candidates if c["clear_entity_id"] == 5)
    assert pytest.approx(cand5["score_cosine"], abs=1e-6) == 0.8
    assert pytest.approx(cand5["score_anchor"], abs=1e-6) == 1.0
    assert pytest.approx(cand5["score_kind"], abs=1e-6) == 0.5
    assert pytest.approx(cand5["score"], abs=1e-6) == 0.8
    assert cand5["method"] == "both" # Found by both anchor and embedding route
    
    # Entity 6 (john smith, person):
    cand6 = next(c for c in candidates if c["clear_entity_id"] == 6)
    assert pytest.approx(cand6["score"], abs=1e-6) == 0.8
    assert cand6["method"] == "both"
    
    # 3. Check review_queue table has auto-flagged row for entity 6 (person) but not entity 5 (dosage)
    cur = conn.execute("SELECT * FROM review_queue")
    reviews = cur.fetchall()
    assert len(reviews) == 1
    assert reviews[0]["entity_id"] == 6
    assert "person in gap candidate #" in reviews[0]["reason"]
    assert reviews[0]["status"] == "pending"
    
    # 4. Rerun should not duplicate candidates or review_queue rows
    conn.execute("DELETE FROM gapjoin_runs")
    conn.commit()
    conn.close()
    
    # Run again
    run_gapjoin(gapjoin_db, mock_embed)
    
    conn = sqlite3.connect(gapjoin_db.db_path)
    conn.row_factory = sqlite3.Row
    
    cur = conn.execute("SELECT COUNT(*) FROM gap_candidates")
    assert cur.fetchone()[0] == 4
    
    cur = conn.execute("SELECT COUNT(*) FROM review_queue")
    assert cur.fetchone()[0] == 1
    
    conn.close()


def test_gapjoin_short_context_skip(tmp_path):
    # Setup database with a short context redaction (< 40 characters)
    class DummyConfig:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        gapjoin = {
            "w_cosine": 0.5, "w_anchor": 0.3, "w_kind": 0.2,
            "score_threshold": 0.65, "topk_embedding_candidates": 50
        }
        embed = {"dim": 768, "model": "nomic-embed"}
        models = {"keep_alive": "24h"}
        
    cfg = DummyConfig()
    migrate(cfg)
    
    conn = sqlite3.connect(cfg.db_path)
    # Insert short context redaction (combined len = 10 + 10 + 1 = 21 < 40)
    conn.execute("""
        INSERT INTO redactions (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after)
        VALUES (200, 'doc_D', 1, 'deleted_text', 'DELETED', 0.1, 0.1, 0.2, 0.2, 
                'short before', 'short after')
    """)
    conn.commit()
    conn.close()
    
    # Mock FAISS
    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(768))
    faiss.write_index(index, str(index_dir / "faiss.idx"))
    
    def mock_embed(cfg, text):
        return [1.0] + [0.0] * 767
        
    run_gapjoin(cfg, mock_embed)
    
    # Check that redaction 200 was recorded in gapjoin_runs as skipped, but generated 0 candidates
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    
    cur = conn.execute("SELECT COUNT(*) FROM gapjoin_runs WHERE redaction_id = 200")
    assert cur.fetchone()[0] == 1
    
    cur = conn.execute("SELECT COUNT(*) FROM gap_candidates")
    assert cur.fetchone()[0] == 0
    
    conn.close()
