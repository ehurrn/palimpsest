import pytest
import sqlite3
from palimpsest.db import connect, migrate
from palimpsest.config import Config, load
from pathlib import Path

@pytest.fixture
def temp_cfg(tmp_path):
    conf_file = tmp_path / "config.toml"
    conf_file.write_text("""
[storage]
root = "/tmp/pal"
[db]
path = "{storage.root}/pal.db"
[broker]
port = 8080
[mcp]
port = 8081
[harvest]
base_url = "http://test"
[ocr]
engine_preference = ["t"]
[features]
redaction_context_chars = 100
blackbox_min_area_frac = 0.01
blackbox_max_area_frac = 0.1
blackbox_darkness_threshold = 50
[embed]
model = "m"
dim = 100
chunk_chars = 100
chunk_overlap = 10
[gapjoin]
score_threshold = 0.5
[models]
extract = "m"
classify = "m"
keep_alive = "1h"
[nodes]
n = []
[orchestrator]
port = 8079
daemon_interval = 300
""")
    return load(conf_file)

def test_migrate_idempotent(temp_cfg):
    migrate(temp_cfg)
    migrate(temp_cfg) # should not error
    conn = connect(temp_cfg)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "documents" in tables
    assert "redactions" in tables
    assert "entities" in tables
    conn.close()

def test_gap_candidates_integrity(temp_cfg):
    migrate(temp_cfg)
    conn = connect(temp_cfg)
    with pytest.raises(sqlite3.IntegrityError):
        # redaction_id and clear_entity_id are NOT NULL
        conn.execute("INSERT INTO gap_candidates (redaction_id, clear_entity_id, score, method) VALUES (?, ?, ?, ?)", (None, None, 0.5, 'test'))
    conn.close()
