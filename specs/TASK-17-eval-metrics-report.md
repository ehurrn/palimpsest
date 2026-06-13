# TASK-17 — Metrics + report (`palimpsest-eval report`)

**Depends on:** TASK-15 (runner/results), TASK-16 (artifact, optional).
**Builds:** per-type metrics (precision/recall/specificity, reliability bins) and
a markdown report whose footer carries the mandatory synthetic-validity
disclosure. Adds the `report` CLI subcommand.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §6 (the disclosure is mandatory).

## Context you need (restated)

- `eval_results.label ∈ {TP, FP, FN, TN}`; precision = `TP/(TP+FP)`,
  recall = `TP/(TP+FN)`, specificity = `TN/(TN+FP)`. Any denominator can be 0.
- The report must state whether the run used the lexical stub (precision
  plumbing-only) or the real Ollama embedder. We record the embed identity in
  `eval_runs.notes` (added below).
- The validity disclosure (synthetic precision is an upper bound) is **required**
  in every report — not optional.

## Files

- Modify: `palimpsest/eval/runner.py` (record embed identity in `eval_runs.notes`)
- Create: `palimpsest/eval/metrics.py`
- Modify: `palimpsest/eval/cli.py` (add `report` subcommand)
- Test: `tests/test_eval_metrics.py`

---

- [ ] **Step 1: Record the embed identity in the run row**

In `palimpsest/eval/runner.py`, change the `eval_runs` INSERT to also store
`notes`. Replace the INSERT statement in `run_eval` with:

```python
        cur = conn.execute(
            "INSERT INTO eval_runs "
            "(started_at, scorer_git_sha, corpus_hash, seed, config_snapshot, notes) "
            "VALUES (?,?,?,?,?,?)",
            (now, _git_sha(), _corpus_hash(cases), seed, json.dumps(cfg.eval),
             f"embed={embed_fn.__module__}.{embed_fn.__name__}"),
        )
```

- [ ] **Step 2: Write the failing metrics test**

Create `tests/test_eval_metrics.py`:

```python
import sqlite3

from palimpsest.db import migrate
from palimpsest.eval.metrics import confusion, rates, render_report


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.eval = {"target_precision": 0.9}


def _seed(conn):
    conn.execute("INSERT INTO eval_runs (run_id, started_at, corpus_hash, notes) "
                 "VALUES (1,'now','abc','embed=palimpsest.eval.embedding.deterministic_embed')")
    conn.execute("INSERT INTO eval_cases (case_id, run_id, type_key, case_kind, spec, truth) "
                 "VALUES (1,1,'type_a','positive','{}','{}')")
    rows = [("type_a", 0.9, "TP"), ("type_a", 0.5, "FP"),
            ("type_a", None, "FN"), ("type_a", None, "TN")]
    for tk, s, l in rows:
        conn.execute("INSERT INTO eval_results (run_id, case_id, type_key, raw_score, label) "
                     "VALUES (1,1,?,?,?)", (tk, s, l))
    conn.commit()


def test_confusion_and_rates(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    _seed(conn)
    c = confusion(conn, 1, "type_a")
    assert c == {"TP": 1, "FP": 1, "FN": 1, "TN": 1}
    prec, rec, spec = rates(c)
    assert prec == 0.5 and rec == 0.5 and spec == 0.5
    conn.close()


def test_report_has_disclosure_and_stub_flag(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    _seed(conn)
    text = render_report(conn, 1, cfg, artifact=None)
    assert "type_a" in text
    assert "precision" in text.lower()
    assert "upper bound" in text.lower()            # mandatory disclosure present
    assert "PLUMBING-ONLY" in text                  # stub detected from notes
    conn.close()
```

- [ ] **Step 3: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.metrics`.

- [ ] **Step 4: Implement metrics + report**

Create `palimpsest/eval/metrics.py`:

```python
"""Per-type metrics and the markdown eval report (with mandatory disclosure)."""
from __future__ import annotations

import datetime
import sqlite3

from palimpsest.config import Config


def confusion(conn: sqlite3.Connection, run_id: int, type_key: str) -> dict:
    counts = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
    for label, n in conn.execute(
        "SELECT label, COUNT(*) FROM eval_results WHERE run_id=? AND type_key=? GROUP BY label",
        (run_id, type_key),
    ):
        counts[label] = n
    return counts


def rates(c: dict):
    tp, fp, fn, tn = c["TP"], c["FP"], c["FN"], c["TN"]
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    spec = tn / (tn + fp) if (tn + fp) else None
    return prec, rec, spec


def reliability_bins(conn, run_id, type_key, n_bins=5):
    pts = [(float(s), 1 if l == "TP" else 0) for s, l in conn.execute(
        "SELECT raw_score, label FROM eval_results "
        "WHERE run_id=? AND type_key=? AND raw_score IS NOT NULL AND label IN ('TP','FP')",
        (run_id, type_key))]
    if not pts:
        return []
    pts.sort()
    lo, hi = pts[0][0], pts[-1][0]
    if hi == lo:
        return [(lo, hi, sum(y for _, y in pts) / len(pts), len(pts))]
    width = (hi - lo) / n_bins
    out = []
    for b in range(n_bins):
        a = lo + b * width
        z = hi if b == n_bins - 1 else lo + (b + 1) * width
        sub = [y for s, y in pts if s >= a and (s < z or (b == n_bins - 1 and s <= z))]
        if sub:
            out.append((round(a, 3), round(z, 3), round(sum(sub) / len(sub), 3), len(sub)))
    return out


def _fmt(x):
    return "—" if x is None else f"{x:.3f}"


def render_report(conn, run_id: int, cfg: Config, artifact: dict | None = None) -> str:
    run = conn.execute(
        "SELECT started_at, scorer_git_sha, corpus_hash, notes FROM eval_runs WHERE run_id=?",
        (run_id,)).fetchone()
    notes = (run[3] if run else "") or ""
    stub = "deterministic_embed" in notes
    type_keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT type_key FROM eval_results WHERE run_id=? ORDER BY type_key", (run_id,))]

    lines = [
        f"# Palimpsest eval report — run {run_id}",
        "",
        f"Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        f"Embedding: {notes or 'unknown'}",
        f"Corpus hash: {run[2] if run else '—'}   Scorer SHA: {run[1] if run else '—'}",
        "",
        "## Per-type metrics",
        "",
        "| type | TP | FP | FN | TN | precision | recall | specificity | gate threshold |",
        "|------|----|----|----|----|-----------|--------|-------------|----------------|",
    ]
    for tk in type_keys:
        c = confusion(conn, run_id, tk)
        prec, rec, spec = rates(c)
        thr = None
        if artifact:
            thr = artifact.get("types", {}).get(tk, {}).get("threshold")
        lines.append(
            f"| {tk} | {c['TP']} | {c['FP']} | {c['FN']} | {c['TN']} | "
            f"{_fmt(prec)} | {_fmt(rec)} | {_fmt(spec)} | {thr} |")

    lines += ["", "## Reliability (score band → empirical correctness)", ""]
    for tk in type_keys:
        bins = reliability_bins(conn, run_id, tk)
        lines.append(f"- {tk}: " + (
            ", ".join(f"[{a}-{z}]→{p} (n={n})" for a, z, p, n in bins) if bins else "no scored predictions"))

    lines += [
        "",
        "## ⚠ Validity disclosure (required)",
        "",
        "Precision here is measured on SYNTHETIC cases whose answer is recoverable "
        "by construction. It is an UPPER BOUND on real-world precision, not an "
        "estimate of it. Real anchor cases included: 0.",
    ]
    if stub:
        lines += [
            "",
            "**PLUMBING-ONLY**: this run used the deterministic lexical embedding "
            "stub, not the production model. Treat all precision/recall numbers as "
            "a pipeline smoke test, NOT a measurement of detector quality.",
        ]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 5: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_metrics.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Add the `report` CLI subcommand**

In `palimpsest/eval/cli.py`, add a handler and register it:

```python
def _cmd_report(args):
    import json
    import sqlite3
    from pathlib import Path
    from palimpsest.eval.metrics import render_report
    cfg = load(args.config)
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    run_id = args.run if args.run is not None else conn.execute(
        "SELECT MAX(run_id) FROM eval_runs").fetchone()[0]
    if run_id is None:
        raise SystemExit("no eval runs found")
    artifact = None
    apath = Path(cfg.eval.get("artifact_path", ""))
    if apath and apath.exists():
        artifact = json.loads(apath.read_text())
    text = render_report(conn, run_id, cfg, artifact)
    conn.close()
    out = Path(args.out) if args.out else Path(f"reports/eval-report-{run_id}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out}")
```

Register in `main()`:

```python
    rep = sub.add_parser("report", help="render a markdown metrics report")
    rep.add_argument("--config", default="config.toml")
    rep.add_argument("--run", type=int, default=None)
    rep.add_argument("--out", default=None)
    rep.set_defaults(func=_cmd_report)
```

- [ ] **Step 7: Full suite + lint + smoke + commit**

Run: `uv run pytest -q`
Run: `uv run ruff check palimpsest/eval/metrics.py palimpsest/eval/runner.py palimpsest/eval/cli.py tests/test_eval_metrics.py`
Smoke:
Run: `uv run palimpsest-eval run --n-per-kind 20 && uv run palimpsest-eval calibrate && uv run palimpsest-eval report`
Expected: prints `wrote reports/eval-report-1.md`; the file contains the metrics
table and the validity disclosure (with PLUMBING-ONLY, since the smoke run uses
the stub).

```bash
git add palimpsest/eval/metrics.py palimpsest/eval/runner.py palimpsest/eval/cli.py tests/test_eval_metrics.py
git commit -m "feat(eval): per-type metrics + markdown report with mandatory validity disclosure"
```

## Out of scope
- No gate enforcement (TASK-18). The `gate threshold` column here is informational.

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Hard blocker →
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, move on.
