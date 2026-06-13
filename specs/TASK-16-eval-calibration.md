# TASK-16 — Calibration: PAV isotonic + Wilson threshold + artifact

**Depends on:** TASK-15 (eval_results populated by a run).
**Builds:** `stats.py` (dependency-free PAV isotonic regression + Wilson lower
bound) and `calibrate.py` (per-type threshold selection + `calibration.json`),
plus the `calibrate` CLI subcommand.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §4.1.

## Context you need (restated)

- A calibration point is `(raw_score, correct)` where `correct = 1` for a `TP`
  result and `0` for an `FP` result. `FN`/`TN` rows have a null `raw_score` and
  are excluded from the fit (they inform recall/specificity, reported in
  TASK-17, not calibration).
- Per-type, because score semantics differ (Type e excluded entirely; Type c
  caps at 0.8 without a dosage match; Type a/b range differently). **Never pool.**
- Threshold rule: the **lowest** score cutoff whose precision over
  `{score ≥ cutoff}`, measured by the Wilson **lower** bound at `wilson_z`, is ≥
  `target_precision`. None qualifying ⇒ gate disabled for that type. Fewer than
  `min_cases` points ⇒ gate disabled (`insufficient_data`).
- No scikit-learn. PAV and Wilson are ~30 lines total.

## Files

- Create: `palimpsest/eval/stats.py`
- Create: `palimpsest/eval/calibrate.py`
- Modify: `palimpsest/eval/cli.py` (add `calibrate` subcommand)
- Test: `tests/test_eval_stats.py`, `tests/test_eval_calibrate.py`

---

- [ ] **Step 1: Write the failing stats test**

Create `tests/test_eval_stats.py`:

```python
from palimpsest.eval.stats import fit_isotonic, predict_isotonic, wilson_lower_bound


def test_isotonic_is_monotone():
    pts = [(0.1, 0), (0.2, 1), (0.3, 0), (0.4, 1), (0.5, 1)]
    curve = fit_isotonic(pts)
    ys = [y for _, y in curve]
    assert ys == sorted(ys)              # non-decreasing
    assert all(0.0 <= y <= 1.0 for y in ys)


def test_isotonic_predict_clips_and_steps():
    pts = [(0.2, 0), (0.4, 0), (0.6, 1), (0.8, 1)]
    curve = fit_isotonic(pts)
    assert predict_isotonic(curve, 0.0) <= predict_isotonic(curve, 1.0)
    assert 0.0 <= predict_isotonic(curve, 0.5) <= 1.0


def test_wilson_known_values():
    # 80/100 successes, z=1.96 → lower bound ≈ 0.7106
    lb = wilson_lower_bound(80, 100, 1.96)
    assert 0.70 < lb < 0.72
    assert wilson_lower_bound(0, 0, 1.96) == 0.0
    # lower bound is below the point estimate
    assert wilson_lower_bound(9, 10, 1.96) < 0.9
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_stats.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.stats`.

- [ ] **Step 3: Implement stats**

Create `palimpsest/eval/stats.py`:

```python
"""Dependency-free calibration statistics: PAV isotonic regression + Wilson LB."""
from __future__ import annotations

import math


def fit_isotonic(points: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators isotonic regression.

    Input: (score, label in {0,1}). Output: a non-decreasing step function as a
    list of (x_right, value) blocks sorted by x_right. Use with predict_isotonic.
    """
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[0])
    # each block: [sum_y, count, value, x_right]
    blocks: list[list[float]] = []
    for x, y in pts:
        blocks.append([float(y), 1.0, float(y), float(x)])
        while len(blocks) >= 2 and blocks[-2][2] > blocks[-1][2]:
            s2, c2, _v2, xr2 = blocks.pop()
            s1, c1, _v1, _xr1 = blocks.pop()
            s, c = s1 + s2, c1 + c2
            blocks.append([s, c, s / c, xr2])
    return [(b[3], b[2]) for b in blocks]


def predict_isotonic(curve: list[tuple[float, float]], score: float) -> float:
    """Calibrated probability for *score* from a fitted curve (clipped at ends)."""
    if not curve:
        return 0.0
    for x_right, value in curve:
        if score <= x_right:
            return value
    return curve[-1][1]


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a binomial proportion."""
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)
```

- [ ] **Step 4: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_stats.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Write the failing calibrate test**

Create `tests/test_eval_calibrate.py`:

```python
import json
import sqlite3

from palimpsest.db import migrate
from palimpsest.eval.calibrate import choose_threshold, build_artifact, write_artifact


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.eval = {
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
```

- [ ] **Step 6: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_calibrate.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.calibrate`.

- [ ] **Step 7: Implement calibrate**

Create `palimpsest/eval/calibrate.py`:

```python
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
```

- [ ] **Step 8: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_calibrate.py -v`
Expected: PASS (4 tests).

- [ ] **Step 9: Add the `calibrate` CLI subcommand**

In `palimpsest/eval/cli.py`, add a handler and register it in `main()`:

```python
def _cmd_calibrate(args):
    import sqlite3
    from palimpsest.eval.calibrate import build_artifact, write_artifact
    cfg = load(args.config)
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    run_id = args.run if args.run is not None else conn.execute(
        "SELECT MAX(run_id) FROM eval_runs").fetchone()[0]
    if run_id is None:
        raise SystemExit("no eval runs found — run `palimpsest-eval run` first")
    artifact = build_artifact(conn, run_id, cfg)
    conn.close()
    path = write_artifact(cfg, artifact)
    print(f"calibrated run_id={run_id} → {path}")
    for tk, t in artifact["types"].items():
        print(f"  {tk:8} threshold={t['threshold']} n={t['n']} ({t['reason']})")
```

In `main()`, after the `run` parser block, add:

```python
    c = sub.add_parser("calibrate", help="fit per-type thresholds → calibration.json")
    c.add_argument("--config", default="config.toml")
    c.add_argument("--run", type=int, default=None, help="run_id (default: latest)")
    c.set_defaults(func=_cmd_calibrate)
```

- [ ] **Step 10: Full suite + lint + smoke + commit**

Run: `uv run pytest -q`
Run: `uv run ruff check palimpsest/eval/stats.py palimpsest/eval/calibrate.py palimpsest/eval/cli.py tests/test_eval_stats.py tests/test_eval_calibrate.py`
Smoke:
Run: `uv run palimpsest-eval run --n-per-kind 20 && uv run palimpsest-eval calibrate`
Expected: prints per-type thresholds. Type c will commonly show
`threshold=None (precision_floor_unmet)` under the lexical stub — that is the
expected safety signal, not a bug.

```bash
git add palimpsest/eval/stats.py palimpsest/eval/calibrate.py palimpsest/eval/cli.py \
        tests/test_eval_stats.py tests/test_eval_calibrate.py
git commit -m "feat(eval): PAV isotonic + Wilson threshold calibration and artifact writer"
```

## Out of scope
- No report rendering (TASK-17). No gate enforcement (TASK-18).

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Hard blocker →
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, move on.
