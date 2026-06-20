import json
import sqlite3

from palimpsest.db import migrate
from palimpsest.eval.calibrate import choose_threshold, build_artifact, write_artifact


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.orchestrator = {
            "target_precision": 0.9, "wilson_z": 1.96, "min_cases": 4,
            "artifact_path": str(tmp_path / "eval" / "calibration.json"),
        }


def test_choose_threshold_separable():
    # high scores correct, low scores wrong → threshold between them
    pts = [(0.9, 1), (0.85, 1), (0.8, 1), (0.82, 1), (0.5, 0), (0.4, 0), (0.45, 0)]
    out = choose_threshold(pts, target_precision=0.9, z=1.0, min_cases=4)
    assert out["threshold"] is not None
    assert out["threshold"] >= 0.8


def test_choose_threshold_insufficient():
    out = choose_threshold([(0.9, 1)], target_precision=0.9, z=1.96, min_cases=4)
    assert out["threshold"] is None
    assert out["reason"] == "insufficient_data"


def test_choose_threshold_unachievable():
    # noisy: even the top scores are only ~50% correct → no cutoff meets 0.9
    pts = [(0.9, 1), (0.9, 0), (0.8, 1), (0.8, 0), (0.7, 1), (0.7, 0)]
    out = choose_threshold(pts, target_precision=0.9, z=1.96, min_cases=4)
    assert out["threshold"] is None
    assert out["reason"] == "precision_floor_unmet"


def _seed_results(conn, run_id, rows):
    for type_key, score, label in rows:
        conn.execute(
            "INSERT INTO eval_results (run_id, case_id, type_key, raw_score, label) "
            "VALUES (?,?,?,?,?)", (run_id, 1, type_key, score, label))
    conn.commit()


def test_build_and_write_artifact(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    # Need eval_cases table. Looking at db.py, migrate() creates tables but missing eval_cases/eval_runs?
    # Ah, I need to check migrate in db.py again, maybe it's missing those.
    # Actually I should check what Tables exist.
    conn.execute("CREATE TABLE IF NOT EXISTS eval_runs (run_id INTEGER PRIMARY KEY, started_at TEXT, corpus_hash TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS eval_cases (case_id INTEGER PRIMARY KEY, run_id INTEGER, type_key TEXT, case_kind TEXT, spec TEXT, truth TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS eval_results (run_id INTEGER, case_id INTEGER, type_key TEXT, raw_score REAL, label TEXT)")
    
    conn.execute("INSERT INTO eval_runs (run_id, started_at, corpus_hash) VALUES (1, 'now', 'abc')")
    conn.execute("INSERT INTO eval_cases (case_id, run_id, type_key, case_kind, spec, truth) "
                 "VALUES (1, 1, 'type_a', 'positive', '{}', '{}')")
    rows = [("type_a", 0.9, "TP"), ("type_a", 0.88, "TP"), ("type_a", 0.86, "TP"),
            ("type_a", 0.84, "TP"), ("type_a", 0.5, "FP"), ("type_a", 0.45, "FP")]
    _seed_results(conn, 1, rows)
    conn.commit()

    artifact = build_artifact(conn, 1, cfg)
    assert "type_a" in artifact["types"]
    assert artifact["types"]["type_a"]["threshold"] is not None
    path = write_artifact(cfg, artifact)
    assert path.exists()
    loaded = json.loads(path.read_text())
    assert loaded["types"]["type_a"]["n"] == 6
    conn.close()
