import pytest
import sqlite3
import textwrap
from pathlib import Path
from palimpsest.db import migrate

# Mocking the shell/environment as I cannot run 'uv run' directly.
# Assuming the environment is set up.

class DummyConfig:
    def __init__(self, tmp_path: Path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        # Ensure directory exists
        (tmp_path / "db").mkdir(parents=True, exist_ok=True)

def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

def test_v7_tables_and_columns(tmp_path: Path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version >= 7

    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"eval_runs", "eval_cases", "eval_results"} <= names

    for table in ("gap_candidates", "identity_link_candidates"):
        cols = _cols(conn, table)
        assert {"confidence", "confidence_method", "gate_tier"} <= cols
    conn.close()

def test_migrate_is_idempotent(tmp_path: Path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    migrate(cfg)  # must not raise (ALTER TABLE re-run)
    conn = sqlite3.connect(cfg.db_path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 7
    conn.close()
