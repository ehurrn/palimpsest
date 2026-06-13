# tests/test_review.py
import hashlib
import json
import os
import sqlite3
import pytest
from pathlib import Path

from palimpsest.config import load
from palimpsest.db import migrate, connect
from palimpsest.review import (
    handle_people,
    handle_gaps,
    handle_audit,
    get_clear_context,
    log_decision_to_audit
)

@pytest.fixture
def temp_cfg(tmp_path):
    # Setup temp config
    class DummyConfig:
        storage_root = tmp_path
        db_path = tmp_path / "db" / "palimpsest.db"
        broker = {"host": "localhost", "port": 8077}
        mcp = {"port": 8078}
        
    cfg = DummyConfig()
    migrate(cfg)
    
    # Save config to env var
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
    
    # Load Config object
    loaded_cfg = load(cfg_file)
    return loaded_cfg

def seed_test_data(cfg):
    conn = sqlite3.connect(cfg.db_path)
    with conn:
        # Docs
        conn.execute("INSERT INTO documents (doc_id, title, accession) VALUES ('doc1', 'Doc One', 'NV001')")
        conn.execute("INSERT INTO documents (doc_id, title, accession) VALUES ('doc2', 'Doc Two', 'NV002')")
        
        # Pages
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc1', 1, 'John Smith was a researcher on this project.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc1', 2, 'We also saw John Smith later in the day.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc2', 1, 'Jane Doe was co-author.')")
        
        # Entities
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, living_status)
            VALUES (1, 'doc1', 1, 'person', 'John Smith', 'john smith', 0, 10, 'unknown')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, living_status)
            VALUES (2, 'doc1', 2, 'person', 'John Smith', 'john smith', 12, 22, 'unknown')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, living_status)
            VALUES (3, 'doc2', 1, 'person', 'Jane Doe', 'jane doe', 0, 8, 'unknown')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, living_status)
            VALUES (4, 'doc2', 1, 'date', '1965-08-12', '1965-08-12', 0, 10, 'unknown')
        """)
        
        # Redactions
        conn.execute("""
            INSERT INTO redactions (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after)
            VALUES (10, 'doc1', 1, 'exemption_stamp', '(b)(6)', 0.1, 0.1, 0.2, 0.2, 'researcher on', 'project.')
        """)
        
        # Gap Candidates
        conn.execute("""
            INSERT INTO gap_candidates (gap_id, redaction_id, clear_entity_id, score, score_cosine, score_anchor, score_kind, method, status)
            VALUES (100, 10, 1, 0.85, 0.9, 0.8, 0.8, 'both', 'candidate')
        """)
        conn.execute("""
            INSERT INTO gap_candidates (gap_id, redaction_id, clear_entity_id, score, score_cosine, score_anchor, score_kind, method, status)
            VALUES (200, 10, 3, 0.70, 0.75, 0.6, 0.7, 'both', 'candidate')
        """)
        
        # Review Queue
        conn.execute("""
            INSERT INTO review_queue (review_id, entity_id, reason, status)
            VALUES (1000, 1, 'person in gap candidate #100', 'pending')
        """)
        conn.execute("""
            INSERT INTO review_queue (review_id, entity_id, reason, status)
            VALUES (2000, 3, 'person in gap candidate #200', 'pending')
        """)
    conn.close()

def test_people_approve(temp_cfg, monkeypatch):
    seed_test_data(temp_cfg)
    
    # Mock inputs: initials, then approve, then quit
    inputs = ["JEH", "a", "q"]
    def mock_input(prompt=""):
        return inputs.pop(0)
    monkeypatch.setattr("builtins.input", mock_input)
    
    # Run handle_people for the first item
    # Since we approved, it updates norm "john smith" and queue for "john smith"
    handle_people(temp_cfg, list_only=False)
    
    # Verify DB state
    conn = connect(temp_cfg)
    cur = conn.execute("SELECT living_status FROM entities WHERE norm = 'john smith'")
    statuses = [r[0] for r in cur.fetchall()]
    assert len(statuses) == 2
    assert all(s == "deceased_historical" for s in statuses)
    
    cur_rq = conn.execute("SELECT status, decided_by FROM review_queue WHERE review_id = 1000")
    rq = cur_rq.fetchone()
    assert rq["status"] == "approved"
    assert rq["decided_by"] == "JEH"
    conn.close()
    
    # Verify Audit file
    audit_file = temp_cfg.storage_root / "db" / "review_audit.jsonl"
    assert audit_file.exists()
    
    with open(audit_file, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["review_id"] == 1000
    assert record["decision"] == "approved"
    assert record["decided_by"] == "JEH"
    
    norm_hash = hashlib.sha256("john smith".encode("utf-8")).hexdigest()
    assert record["norm_hash"] == norm_hash
    
    # Plaintext name must NOT be in audit file
    with open(audit_file, "r") as f:
        content = f.read()
    assert "john smith" not in content
    assert "John Smith" not in content

def test_people_deny(temp_cfg, monkeypatch):
    seed_test_data(temp_cfg)
    
    # Mock inputs: initials, then deny, then quit
    inputs = ["JEH", "d", "q"]
    def mock_input(prompt=""):
        return inputs.pop(0)
    monkeypatch.setattr("builtins.input", mock_input)
    
    handle_people(temp_cfg, list_only=False)
    
    conn = connect(temp_cfg)
    cur = conn.execute("SELECT living_status FROM entities WHERE norm = 'john smith'")
    statuses = [r[0] for r in cur.fetchall()]
    assert len(statuses) == 2
    assert all(s == "potentially_living" for s in statuses)
    
    cur_rq = conn.execute("SELECT status, decided_by FROM review_queue WHERE review_id = 1000")
    rq = cur_rq.fetchone()
    assert rq["status"] == "denied"
    assert rq["decided_by"] == "JEH"
    conn.close()

def test_people_skip_and_quit(temp_cfg, monkeypatch):
    seed_test_data(temp_cfg)
    
    # Mock inputs: initials, then skip, then quit
    inputs = ["JEH", "s", "q"]
    def mock_input(prompt=""):
        return inputs.pop(0)
    monkeypatch.setattr("builtins.input", mock_input)
    
    handle_people(temp_cfg, list_only=False)
    
    conn = connect(temp_cfg)
    # The first item (John Smith) should remain pending because it was skipped
    cur_rq1 = conn.execute("SELECT status FROM review_queue WHERE review_id = 1000")
    assert cur_rq1.fetchone()["status"] == "pending"
    
    # The second item (Jane Doe) should remain pending because we quit
    cur_rq2 = conn.execute("SELECT status FROM review_queue WHERE review_id = 2000")
    assert cur_rq2.fetchone()["status"] == "pending"
    conn.close()

def test_people_list(temp_cfg, capsys):
    seed_test_data(temp_cfg)
    handle_people(temp_cfg, list_only=True)
    captured = capsys.readouterr()
    assert "john smith" in captured.out or "John Smith" in captured.out
    assert "jane doe" in captured.out or "Jane Doe" in captured.out

def test_gaps_verify(temp_cfg, monkeypatch):
    seed_test_data(temp_cfg)
    
    # Mock inputs: initials, verify, optional note, then quit
    inputs = ["JEH", "v", "looks like a match", "q"]
    def mock_input(prompt=""):
        return inputs.pop(0)
    monkeypatch.setattr("builtins.input", mock_input)
    
    handle_gaps(temp_cfg)
    
    conn = connect(temp_cfg)
    cur = conn.execute("SELECT status, reviewed_by, notes FROM gap_candidates WHERE gap_id = 100")
    row = cur.fetchone()
    assert row["status"] == "verified"
    assert row["reviewed_by"] == "JEH"
    assert row["notes"] == "looks like a match"
    conn.close()

def test_gaps_reject_and_quit(temp_cfg, monkeypatch):
    seed_test_data(temp_cfg)
    
    # Mock inputs: initials, reject, optional note, then quit
    inputs = ["JEH", "r", "not a match", "q"]
    def mock_input(prompt=""):
        return inputs.pop(0)
    monkeypatch.setattr("builtins.input", mock_input)
    
    handle_gaps(temp_cfg)
    
    conn = connect(temp_cfg)
    # The first one (gap_id=100) is rejected
    cur1 = conn.execute("SELECT status, reviewed_by, notes FROM gap_candidates WHERE gap_id = 100")
    row1 = cur1.fetchone()
    assert row1["status"] == "rejected"
    assert row1["reviewed_by"] == "JEH"
    assert row1["notes"] == "not a match"
    
    # The second one (gap_id=200) remains candidate because we quit
    cur2 = conn.execute("SELECT status FROM gap_candidates WHERE gap_id = 200")
    assert cur2.fetchone()["status"] == "candidate"
    conn.close()

def test_audit_logs(temp_cfg, capsys):
    # Log some dummy decisions manually
    log_decision_to_audit(temp_cfg, 1000, "john smith", "approved", "JEH", "2026-06-12T12:00:00Z")
    log_decision_to_audit(temp_cfg, 2000, "jane doe", "denied", "JEH", "2026-06-12T12:05:00Z")
    
    handle_audit(temp_cfg)
    captured = capsys.readouterr()
    assert "Review 1000 by JEH: APPROVED" in captured.out
    assert "Review 2000 by JEH: DENIED" in captured.out

def test_audit_logs_empty(temp_cfg, capsys):
    # No decisions written yet
    handle_audit(temp_cfg)
    captured = capsys.readouterr()
    assert "No audit records found." in captured.out
