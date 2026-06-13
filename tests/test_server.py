# tests/test_server.py
import json
import os
import sqlite3
import pytest

from palimpsest.db import migrate
from palimpsest.server import (
    get_ro_connection,
    mask_person,
    get_masked_text_for_page,
    mask_context_text,
    palimpsest_find_redaction_gaps,
    palimpsest_get_document,
    palimpsest_get_entity,
    palimpsest_review_queue
)

@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("server_test_root")
    
    # Setup temp config
    class DummyConfig:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        broker = {"host": "localhost", "port": 8077}
        mcp = {"port": 8078}
        
    cfg = DummyConfig()
    migrate(cfg)
    
    # Save config to env var so server's load() reads it
    config_content = f"""
    [storage]
    root = "{tmp_path}"
    [db]
    path = "{{storage.root}}/db/palimpsest.db"
    [broker]
    host = "localhost"
    port = 8077
    [mcp]
    port = 8078
    [harvest]
    base_url = ""
    rate_limit_rps = 1.0
    backoff_initial_s = 5
    backoff_max_s = 300
    user_agent = ""
    accession_prefix = "NV"
    [ocr]
    engine_preference = ["vision"]
    min_confidence = 0.5
    rerun_if_osti_text_shorter_than = 200
    [features]
    redaction_context_chars = 300
    redaction_context_lines = 2
    [embed]
    model = "nomic-embed"
    dim = 768
    chunk_chars = 800
    chunk_overlap = 150
    [gapjoin]
    score_threshold = 0.65
    w_cosine = 0.5
    w_anchor = 0.3
    w_kind = 0.2
    topk_embedding_candidates = 50
    [models]
    extract = "llama"
    classify = "qwen"
    keep_alive = "24h"
    [nodes]
    gonktop = []
    """
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_content)
    os.environ["PALIMPSEST_CONFIG"] = str(cfg_file)
    
    # Seed DB
    conn = sqlite3.connect(cfg.db_path)
    with conn:
        conn.execute("INSERT INTO documents (doc_id, title, accession, source_url) VALUES ('doc1', 'Doc One', 'NV001', 'http://doc1.pdf');")
        conn.execute("INSERT INTO documents (doc_id, title, accession, source_url) VALUES ('doc2', 'Doc Two', 'NV002', 'http://doc2.pdf');")
        
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc1', 1, 'This is page one text containing John Smith.');")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc2', 1, 'This is page one text containing Jane Doe.');")
        
        # Redaction
        conn.execute("""
            INSERT INTO redactions (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after)
            VALUES (10, 'doc1', 1, 'exemption_stamp', '(b)(6)', 0.1, 0.1, 0.2, 0.2, 'text containing', 'John Smith.')
        """)
        
        # Entities: John Smith is unapproved person, Jane Doe is approved deceased_historical person
        # John Smith entity_id = 1
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1, living_status)
            VALUES (1, 'doc1', 1, 'person', 'John Smith', 'john smith', 33, 43, 0.1, 0.1, 0.2, 0.2, 'unknown')
        """)
        # Jane Doe entity_id = 2
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1, living_status)
            VALUES (2, 'doc2', 1, 'person', 'Jane Doe', 'jane doe', 33, 41, 0.1, 0.1, 0.2, 0.2, 'deceased_historical')
        """)

        
        # Dosage entity (should not mask)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (3, 'doc1', 1, 'dosage', '15 rem', '15 rem', 0, 8, 0.1, 0.1, 0.2, 0.2)
        """)
        
        # Gap candidate linking redaction 10 to clear entity 2 (Jane Doe)
        conn.execute("""
            INSERT INTO gap_candidates (gap_id, redaction_id, clear_entity_id, score, score_cosine, score_anchor, score_kind, method, status)
            VALUES (100, 10, 2, 0.85, 0.9, 0.8, 1.0, 'both', 'candidate')
        """)
        # Gap candidate linking redaction 10 to clear entity 1 (John Smith)
        conn.execute("""
            INSERT INTO gap_candidates (gap_id, redaction_id, clear_entity_id, score, score_cosine, score_anchor, score_kind, method, status)
            VALUES (101, 10, 1, 0.80, 0.9, 0.8, 1.0, 'both', 'candidate')
        """)
        
        # Review queue: Jane Doe approved, John Smith pending
        conn.execute("INSERT INTO review_queue (review_id, entity_id, reason, status) VALUES (1, 2, 'Jane Doe test', 'approved');")
        conn.execute("INSERT INTO review_queue (review_id, entity_id, reason, status) VALUES (2, 1, 'person in gap candidate #101', 'pending');")
        
    conn.close()
    return cfg

def test_readonly_db_connection(seeded_db):
    conn = get_ro_connection(seeded_db.db_path)
    
    # Reads should succeed
    cur = conn.execute("SELECT COUNT(*) FROM documents")
    assert cur.fetchone()[0] == 2
    
    # Writes should fail with sqlite3.OperationalError: attempt to write a readonly database
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        conn.execute("INSERT INTO documents (doc_id) VALUES ('fail_doc')")
    assert "readonly" in str(excinfo.value)
    
    conn.close()

def test_mask_person_both_branches(seeded_db):
    conn = get_ro_connection(seeded_db.db_path)
    
    # John Smith (unapproved, living_status = 'unknown'): should mask to PERSON-0001
    ent_smith = {"entity_id": 1, "kind": "person", "text": "John Smith", "living_status": "unknown"}
    assert mask_person(ent_smith, conn) == "PERSON-0001"
    
    # Jane Doe (approved deceased_historical): should NOT mask
    ent_doe = {"entity_id": 2, "kind": "person", "text": "Jane Doe", "living_status": "deceased_historical"}
    assert mask_person(ent_doe, conn) == "Jane Doe"
    
    # Non-person (dosage): should NOT mask
    ent_dose = {"entity_id": 3, "kind": "dosage", "text": "15 rem"}
    assert mask_person(ent_dose, conn) == "15 rem"
    
    conn.close()

def test_masking_applied_in_snippet_text(seeded_db):
    conn = get_ro_connection(seeded_db.db_path)
    
    # Page 1 text: "This is page one text containing John Smith."
    # Since John Smith is unapproved person at offset 30 to 40:
    # "This is page one text containing " (len 30) + "PERSON-0001" + "."
    expected = "This is page one text containing PERSON-0001."
    
    text = "This is page one text containing John Smith."
    res = get_masked_text_for_page("doc1", 1, text, conn)
    assert res == expected
    
    # Jane Doe on Page 2 is approved deceased_historical, so should NOT be masked
    text_doe = "This is page one text containing Jane Doe."
    res_doe = get_masked_text_for_page("doc2", 1, text_doe, conn)
    assert res_doe == "This is page one text containing Jane Doe."
    
    conn.close()

def test_mask_context_substring(seeded_db):
    conn = get_ro_connection(seeded_db.db_path)
    
    # Unapproved John Smith context should be replaced
    ctx = "The subject was John Smith."
    res = mask_context_text("doc1", 1, ctx, conn)
    assert res == "The subject was PERSON-0001."
    
    # Approved Jane Doe should not
    ctx_doe = "The subject was Jane Doe."
    res_doe = mask_context_text("doc2", 1, ctx_doe, conn)
    assert res_doe == "The subject was Jane Doe."
    
    conn.close()

def test_tool_find_redaction_gaps(seeded_db):
    # Retrieve candidates
    # gap 100 (Jane Doe, score 0.85)
    # gap 101 (John Smith, score 0.80)
    
    res_str = palimpsest_find_redaction_gaps(min_score=0.82)
    res = json.loads(res_str)
    # Should only return gap 100 because of min_score = 0.82
    assert len(res) == 1
    assert res[0]["gap_id"] == 100
    assert res[0]["clear_entity"]["text"] == "Jane Doe" # Not masked because approved
    
    # Check that both citations are present
    assert "citation" in res[0]["redaction"]
    assert "citation" in res[0]["clear_entity"]
    
    # Now check all candidates (min_score = 0.65)
    res_all_str = palimpsest_find_redaction_gaps(min_score=0.65)
    res_all = json.loads(res_all_str)
    assert len(res_all) == 2
    
    # Find John Smith gap (101)
    gap101 = next(g for g in res_all if g["gap_id"] == 101)
    assert gap101["clear_entity"]["text"] == "PERSON-0001" # Masked!
    assert gap101["requires_review"] is True

def test_tool_get_document(seeded_db):
    res_str = palimpsest_get_document("doc1")
    res = json.loads(res_str)
    
    assert "metadata" in res
    assert res["metadata"]["title"] == "Doc One"
    assert len(res["pages"]) == 1
    assert "PERSON-0001" in res["pages"][0]["text"]
    assert "John Smith" not in res["pages"][0]["text"]
    
    # Check entities in doc1
    ents = res["entities"]
    smith_ent = next(e for e in ents if e["entity_id"] == 1)
    assert smith_ent["text"] == "PERSON-0001"
    
    dose_ent = next(e for e in ents if e["entity_id"] == 3)
    assert dose_ent["text"] == "15 rem" # Dosage not masked

def test_tool_get_entity(seeded_db):
    # Query john smith
    res_str = palimpsest_get_entity("john smith")
    res = json.loads(res_str)
    assert len(res) == 1
    assert res[0]["text"] == "PERSON-0001" # Masked
    
    # Query dosage (15 rem)
    res_dose_str = palimpsest_get_entity("15 rem")
    res_dose = json.loads(res_dose_str)
    assert len(res_dose) == 1
    assert res_dose[0]["text"] == "15 rem" # Not masked

def test_tool_review_queue(seeded_db):
    res_str = palimpsest_review_queue()
    res = json.loads(res_str)
    
    # Should list both reviews (approved/pending)
    assert len(res) == 2
    
    smith_rev = next(r for r in res if r["entity_id"] == 1)
    assert smith_rev["pseudonym"] == "PERSON-0001"
    assert smith_rev["gap_id"] == 101 # parsed from "person in gap candidate #101"
