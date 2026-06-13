"""Per-type calibration → calibration.json (specs/EVAL-TRUST-GATE.md §4.1)."""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

from palimpsest.config import Config
from palimpsest.eval.stats import fit_isotonic, wilson_lower_bound


def collect_points(conn: sqlite3.Connection, run_id: int, type_key: str) -> list[tuple[float, int]]:
    rows = conn.execute(
        "SELECT raw_score, label FROM eval_results "
        "WHERE run_id=? AND type_key=? AND raw_score IS NOT NULL AND label IN ('TP','FP')",
        (run_id, type_key),
    ).fetchall()
    return [(float(s), 1 if lbl == "TP" else 0) for s, lbl in rows]


def choose_threshold(points, target_precision: float, z: float, min_cases: int) -> dict:
    n = len(points)
    if n < min_cases:
        return {"threshold": None, "n": n, "wilson_lb": None, "reason": "insufficient_data"}
    cutoffs = sorted({s for s, _ in points})
    best = None
    for c in cutoffs:                       # ascending → first qualifying is the lowest
        subset = [(s, y) for s, y in points if s >= c]
        succ = sum(y for _, y in subset)
        lb = wilson_lower_bound(succ, len(subset), z)
        if lb >= target_precision:
            best = {"threshold": c, "n": n, "wilson_lb": lb, "reason": "ok"}
            break
    if best is None:
        return {"threshold": None, "n": n, "wilson_lb": None, "reason": "precision_floor_unmet"}
    return best


def fit_type(conn, run_id, type_key, cfg: Config) -> dict:
    points = collect_points(conn, run_id, type_key)
    target = float(cfg.eval.get("target_precision", 0.90))
    z = float(cfg.eval.get("wilson_z", 1.96))
    min_cases = int(cfg.eval.get("min_cases", 40))
    out = choose_threshold(points, target, z, min_cases)
    out["isotonic"] = fit_isotonic(points)
    return out


def build_artifact(conn, run_id, cfg: Config) -> dict:
    run = conn.execute(
        "SELECT scorer_git_sha, corpus_hash FROM eval_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    type_keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT type_key FROM eval_results WHERE run_id=? ORDER BY type_key", (run_id,))]
    return {
        "schema": 1,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "run_id": run_id,
        "scorer_git_sha": run[0] if run else None,
        "corpus_hash": run[1] if run else None,
        "config": {
            "target_precision": float(cfg.eval.get("target_precision", 0.90)),
            "wilson_z": float(cfg.eval.get("wilson_z", 1.96)),
            "min_cases": int(cfg.eval.get("min_cases", 40)),
        },
        "types": {tk: fit_type(conn, run_id, tk, cfg) for tk in type_keys},
    }


def write_artifact(cfg: Config, artifact: dict) -> Path:
    path = Path(cfg.eval["artifact_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2))
    return path
