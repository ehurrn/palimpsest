import sqlite3
import textwrap
from palimpsest.db import migrate
from palimpsest.config import load


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_v7_tables_and_columns(tmp_path):
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


def test_migrate_is_idempotent(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    migrate(cfg)  # must not raise (ALTER TABLE re-run)
    conn = sqlite3.connect(cfg.db_path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 7
    conn.close()


_BASE_TOML = """
[storage]
root = "{root}"
[db]
path = "{{storage.root}}/db/palimpsest.db"
[broker]
host = "h"
port = 1
[mcp]
port = 2
[harvest]
base_url = "u"
[ocr]
engine_preference = ["tesseract"]
[features]
redaction_context_chars = 300
[embed]
model = "nomic-embed-text"
dim = 768
[gapjoin]
score_threshold = 0.65
[models]
keep_alive = "24h"
[nodes]
"""


def _write_cfg(tmp_path, extra=""):
    p = tmp_path / "config.toml"
    p.write_text(_BASE_TOML.format(root=str(tmp_path)) + extra)
    return p


def test_eval_section_loads_and_expands(tmp_path):
    extra = textwrap.dedent("""
    [eval]
    target_precision = 0.9
    min_cases = 40
    artifact_path = "{storage.root}/eval/calibration.json"
    eval_db_path = "{storage.root}/eval/eval.db"
    """)
    cfg = load(_write_cfg(tmp_path, extra))
    assert cfg.eval["target_precision"] == 0.9
    assert cfg.eval["artifact_path"] == f"{tmp_path}/eval/calibration.json"
    assert cfg.eval["eval_db_path"] == f"{tmp_path}/eval/eval.db"


def test_eval_section_optional(tmp_path):
    cfg = load(_write_cfg(tmp_path))  # no [eval]
    assert cfg.eval == {}
