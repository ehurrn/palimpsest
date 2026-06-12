import sqlite3
import pytest
from pathlib import Path
from palimpsest.config import load
from palimpsest.db import connect, migrate

def test_db_migration_and_foreign_keys(tmp_path):
    config_content = """
    [storage]
    root = "{tmp_dir}"
    [db]
    path = "{storage.root}/db/palimpsest.db"
    [broker]
    host = "localhost"
    port = 8077
    lease_ttl_seconds = 900
    heartbeat_seconds = 120
    max_attempts = 3
    [mcp]
    port = 8078
    [harvest]
    base_url = "https://www.osti.gov/opennet"
    rate_limit_rps = 1.0
    backoff_initial_s = 5
    backoff_max_s = 300
    user_agent = "test-agent"
    accession_prefix = "NV"
    [ocr]
    engine_preference = ["vision"]
    min_confidence = 0.5
    rerun_if_osti_text_shorter_than = 200
    [features]
    redaction_context_chars = 300
    redaction_context_lines = 2
    blackbox_min_area_frac = 0.001
    blackbox_max_area_frac = 0.25
    blackbox_darkness_threshold = 60
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
    """.replace("{tmp_dir}", str(tmp_path))
    
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_content)
    
    cfg = load(cfg_file)
    migrate(cfg)
    
    conn = connect(cfg)
    
    # Assert journal_mode is WAL
    cur = conn.execute("PRAGMA journal_mode;")
    assert cur.fetchone()[0].lower() == "wal"
    
    # Assert foreign_keys is ON
    cur = conn.execute("PRAGMA foreign_keys;")
    assert cur.fetchone()[0] == 1
    
    # Verify tables created
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cur.fetchall()}
    assert "documents" in tables
    assert "pages" in tables
    assert "redactions" in tables
    assert "entities" in tables
    assert "jobs" in tables
    
    # Try inserting page without document: should fail due to foreign key
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('nonexistent', 1, 'text');")
