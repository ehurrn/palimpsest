# tests/test_review.py
import hashlib
import json
import sqlite3
import pytest

from palimpsest.config import load
from palimpsest.db import migrate, connect
from palimpsest.review import (
    handle_people,
    handle_gaps,
    handle_audit,
    log_decision_to_audit,
    apply_heuristic
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
[orchestrator]
    heartbeat_interval_secs = 900
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

def test_heuristic_classification(temp_cfg):
    """Spec-compliant heuristic: 75-year doc-age rule only, queries entities directly."""
    conn = sqlite3.connect(temp_cfg.db_path)
    with conn:
        # 1. Doc older than 75 years → entity approved
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_old', 1950)")
        # 2. Doc within 75 years (1980: 2026-1980=46) → potentially_living (birth-year regex removed)
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_born', 1980)")
        # 3. Doc within 75 years → potentially_living
        conn.execute("INSERT INTO documents (doc_id, year) VALUES ('doc_young', 2010)")

        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, living_status)
            VALUES (10, 'doc_old', 1, 'person', 'John Smith', 'john smith', 0, 10, 'unknown')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, living_status)
            VALUES (20, 'doc_born', 1, 'person', 'Jane Doe', 'jane doe', 0, 8, 'unknown')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, living_status)
            VALUES (30, 'doc_young', 1, 'person', 'Bob Johnson', 'bob johnson', 0, 11, 'unknown')
        """)
    conn.close()

    apply_heuristic(temp_cfg)

    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row

    # John Smith (1950 → 76 years ago) → deceased_historical, new review_queue row inserted
    ent1 = conn.execute("SELECT living_status FROM entities WHERE entity_id = 10").fetchone()
    assert ent1["living_status"] == "deceased_historical"
    rq1 = conn.execute(
        "SELECT status, decided_by FROM review_queue WHERE entity_id = 10"
    ).fetchone()
    assert rq1 is not None
    assert rq1["status"] == "approved"
    assert rq1["decided_by"] == "HEURISTIC_AUTO"

    # Jane Doe (1980 → 46 years ago, ≤75) → potentially_living, no review_queue row
    ent2 = conn.execute("SELECT living_status FROM entities WHERE entity_id = 20").fetchone()
    assert ent2["living_status"] == "potentially_living"
    rq2_count = conn.execute(
        "SELECT COUNT(*) as c FROM review_queue WHERE entity_id = 20"
    ).fetchone()["c"]
    assert rq2_count == 0

    # Bob Johnson (2010 → 16 years ago) → potentially_living
    ent3 = conn.execute("SELECT living_status FROM entities WHERE entity_id = 30").fetchone()
    assert ent3["living_status"] == "potentially_living"

    conn.close()


def test_heuristic_approves_old_document(temp_cfg):
    """Doc from 1940 (>75 years) → entity approved, review_queue row inserted."""
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("100", "NV-100", "Old Doc", "indexed", 1940),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('100', 1, 'person', 'Jane Smith', 'jane smith', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT living_status FROM entities WHERE doc_id='100' AND kind='person'"
    ).fetchone()
    assert row["living_status"] == "deceased_historical"

    rq = conn2.execute(
        "SELECT status, decided_by, reason FROM review_queue "
        "WHERE entity_id = (SELECT entity_id FROM entities WHERE doc_id='100' AND kind='person')"
    ).fetchone()
    assert rq is not None
    assert rq["status"] == "approved"
    assert rq["decided_by"] == "HEURISTIC_AUTO"
    assert rq["reason"] == "Auto-approved via 75-year document age heuristic"
    conn2.close()


def test_heuristic_flags_recent_document_as_potentially_living(temp_cfg):
    """Doc from 2010 (<75 years) → entity marked potentially_living, no review_queue insert."""
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("200", "NV-200", "Recent Doc", "indexed", 2010),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('200', 1, 'person', 'Bob Jones', 'bob jones', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT living_status FROM entities WHERE doc_id='200' AND kind='person'"
    ).fetchone()
    assert row["living_status"] == "potentially_living"

    rq_count = conn2.execute(
        "SELECT COUNT(*) as c FROM review_queue "
        "WHERE entity_id = (SELECT entity_id FROM entities WHERE doc_id='200' AND kind='person')"
    ).fetchone()["c"]
    assert rq_count == 0
    conn2.close()


def test_heuristic_flags_null_year_as_potentially_living(temp_cfg):
    """Doc with NULL year → entity marked potentially_living (can't confirm age)."""
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("300", "NV-300", "Undated Doc", "indexed", None),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('300', 1, 'person', 'Alice Li', 'alice li', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT living_status FROM entities WHERE doc_id='300' AND kind='person'"
    ).fetchone()
    assert row["living_status"] == "potentially_living"
    conn2.close()


def test_heuristic_skips_already_classified_entities(temp_cfg):
    """Entities with living_status != 'unknown' are not touched."""
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("400", "NV-400", "Old Doc 2", "indexed", 1930),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('400', 1, 'person', 'Pre-approved Person', 'pre-approved person', 'deceased_historical')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    rq_count = conn2.execute(
        "SELECT COUNT(*) as c FROM review_queue "
        "WHERE entity_id = (SELECT entity_id FROM entities WHERE doc_id='400' AND kind='person')"
    ).fetchone()["c"]
    assert rq_count == 0
    conn2.close()


def test_heuristic_prints_summary(temp_cfg, capsys):
    """apply_heuristic prints evaluated, historical, and potentially_living counts."""
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("500", "NV-500", "Old Doc 3", "indexed", 1940),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('500', 1, 'person', 'Frank Test', 'frank test', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    captured = capsys.readouterr()
    assert "1" in captured.out
    assert "deceased_historical" in captured.out or "historical" in captured.out.lower()
