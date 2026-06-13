# tests/test_series.py
import sqlite3
import pytest
from palimpsest.db import migrate
from palimpsest.tasks.features import normalize, process_features
from palimpsest.indexer import run_series_join

def test_seq_ref_normalization():
    # Verify prefix and padding normalization
    assert normalize("seq_ref", "nv0042452") == "NV0042452"
    assert normalize("seq_ref", "NV-12345") == "NV-12345"
    assert normalize("seq_ref", "Report No. 3") == "REPORT-NO-3"
    assert normalize("seq_ref", "Report Number 12") == "REPORT-NO-12"
    assert normalize("seq_ref", "nv- 999") == "NV-999"

def test_seq_ref_extraction():
    # Verify extraction from text
    class DummyConfig:
        features = {
            "redaction_context_chars": 300,
            "redaction_context_lines": 2,
        }
    cfg = DummyConfig()
    ocr_data = [{
        "page_no": 1,
        "lines": [
            {"text": "The details are in NV0042452 and Report No. 3.", "bbox": [0.1, 0.1, 0.9, 0.2]}
        ]
    }]
    res = process_features(None, ocr_data, cfg)
    entities = res["entities"]
    seq_refs = [e for e in entities if e["kind"] == "seq_ref"]
    assert len(seq_refs) == 2
    norms = {e["norm"] for e in seq_refs}
    assert "NV0042452" in norms
    assert "REPORT-NO-3" in norms

def test_series_join_suppression(tmp_path):
    # Setup test configuration and database
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
        # We need a sequence with > 20% gap ratio.
        # E.g. NV0000001, NV0000002, NV0000004.
        # Total range is 4 (1 to 4). Present are 3. Gap ratio = 1/4 = 25%.
        # (This is > 20%, so it will trigger the gap join)
        conn.execute("INSERT INTO documents (doc_id, accession) VALUES ('doc_1', 'NV0000001');")
        conn.execute("INSERT INTO documents (doc_id, accession) VALUES ('doc_2', 'NV0000002');")
        conn.execute("INSERT INTO documents (doc_id, accession) VALUES ('doc_4', 'NV0000004');")
        
        # Insert pages
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_1', 1, 'Page 1');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_2', 1, 'Page 2 referring to NV0000003');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_4', 1, 'Page 4');")
        
        # Insert entity for seq_ref in doc_2 (flanking document M-1) pointing to missing M (NV0000003)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (10, 'doc_2', 1, 'seq_ref', 'NV0000003', 'NV0000003', 18, 27);
        """)
        
    conn.close()
    
    # Run the series join subcommand logic
    run_series_join(cfg)
    
    # Check results in the database
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    candidates = conn.execute("SELECT * FROM series_gap_candidates").fetchall()
    assert len(candidates) == 1
    
    cand = candidates[0]
    assert cand["series_prefix"] == "NV"
    assert cand["missing_number"] == 3
    assert cand["missing_accession"] == "NV0000003"
    assert cand["flanking_doc_id"] == "doc_2"
    assert cand["ref_entity_id"] == 10
    assert pytest.approx(cand["score"]) == 0.70
    assert cand["status"] == "candidate"
    
    conn.close()
