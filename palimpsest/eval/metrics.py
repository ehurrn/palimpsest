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
    pts = [(float(s), 1 if lbl == "TP" else 0) for s, lbl in conn.execute(
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
