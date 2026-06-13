# tests/test_dosage.py
import sqlite3
import numpy as np
import faiss
from palimpsest.db import migrate
from palimpsest.tasks.features import normalize, process_features
from palimpsest.indexer import run_gapjoin

def test_subject_ref_normalization():
    # subject_ref should lowercase and clean up spaces
    assert normalize("subject_ref", "Subject 5") == "subject 5"
    assert normalize("subject_ref", "Patient X12") == "patient x12"
    assert normalize("subject_ref", "Case  9") == "case 9"

def test_subject_ref_extraction():
    class DummyConfig:
        features = {
            "redaction_context_chars": 300,
            "redaction_context_lines": 2,
        }
    cfg = DummyConfig()
    ocr_data = [{
        "page_no": 1,
        "lines": [
            {"text": "Subject 5 had a high dose.", "bbox": [0.1, 0.1, 0.9, 0.2]}
        ]
    }]
    res = process_features(None, ocr_data, cfg)
    entities = res["entities"]
    subjects = [e for e in entities if e["kind"] == "subject_ref"]
    assert len(subjects) == 1
    assert subjects[0]["text"] == "Subject 5"
    assert subjects[0]["norm"] == "subject 5"

def test_dosage_proximity_and_deduplication(tmp_path):
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
    
    conn = sqlite3.connect(cfg.db_path)
    with conn:
        # doc_A has a redaction with some context
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_A', 'indexed');")
        # doc_B has the same dosage value (15 rem) close to a subject
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_B', 'indexed');")
        # doc_C has the same dosage value (15 rem) but far from a subject
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_C', 'indexed');")
        
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_A', 1, 'Redacted context page with some info.');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_B', 1, 'Subject X received 15 rem.');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_C', 1, 'A page talking about 15 rem with no subject details.');")
        
        # Redaction in doc_A
        conn.execute("""
            INSERT INTO redactions (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after)
            VALUES (200, 'doc_A', 1, 'deleted_text', 'DELETED', 0.1, 0.1, 0.2, 0.2, 
                    'preceding context info here', 'succeeding context info here')
        """)
        
        # Add matching subject_ref and dosage to the redaction page (doc_A) to trigger co-occurrence and match boosts
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (301, 'doc_A', 1, 'subject_ref', 'Subject X', 'subject x', 5, 14);
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (302, 'doc_A', 1, 'dosage', '15 rem', '15 rem', 16, 22);
        """)
        
        # Dosage entities on candidate pages (same norm '15 rem')
        # doc_B: dosage at 19-25, subject at 0-9. Distance = 19 - 9 = 10. Proximity score = exp(-10/500) = 0.98.
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (101, 'doc_B', 1, 'dosage', '15 rem', '15 rem', 19, 25);
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (102, 'doc_B', 1, 'subject_ref', 'Subject X', 'subject x', 0, 9);
        """)
        
        # doc_C: dosage at 19-25, subject at 100-109. Distance = 100 - 25 = 75. Proximity score = exp(-75/500) = 0.86.
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (201, 'doc_C', 1, 'dosage', '15 rem', '15 rem', 19, 25);
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (202, 'doc_C', 1, 'subject_ref', 'Subject Y', 'subject y', 100, 109);
        """)
        
        # Chunks for doc_B and doc_C
        conn.execute("INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) VALUES (1001, 'doc_B', 1, 0, 50, 'Subject X received 15 rem.');")
        conn.execute("INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) VALUES (2001, 'doc_C', 1, 0, 50, 'A page talking about 15 rem.');")
        
    conn.close()
    
    # Create mock FAISS index
    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(768))
    
    # Simple query embedding query = [1, 0, ...]
    v_b = np.zeros(768, dtype=np.float32)
    v_b[0] = 0.8
    v_c = np.zeros(768, dtype=np.float32)
    v_c[0] = 0.8
    index.add_with_ids(np.array([v_b, v_c]), np.array([1001, 2001], dtype=np.int64))
    faiss.write_index(index, str(index_dir / "faiss.idx"))
    
    def mock_embed(cfg, text):
        return [1.0] + [0.0] * 767
        
    run_gapjoin(cfg, mock_embed)
    
    # Verify candidate deduplication: we should only keep the highest scoring candidate (doc_B, since distance is smaller)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    candidates = conn.execute("SELECT * FROM gap_candidates WHERE redaction_id = 200").fetchall()
    
    # There should only be 1 candidate because of deduplication by norm '15 rem'
    assert len(candidates) == 1
    assert candidates[0]["clear_entity_id"] == 101 # From doc_B (nearer, higher score)
    
    conn.close()
