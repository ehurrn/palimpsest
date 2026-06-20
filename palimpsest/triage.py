# palimpsest/triage.py
"""Triage CLI — rank document briefs by novelty and interest.

Usage:
    python -m palimpsest.triage [--limit N] [--config PATH]
    python -m palimpsest.triage --doc <doc_id> [--config PATH]

Two scores, both written back to `briefs`:

  novelty_score
      Cheap, unsupervised.  Embed each brief's summary, compute mean cosine
      distance to its k nearest brief-neighbours.  High distance = anomalous
      document = worth a human glance.

  interest_score  (optional, written when [triage] rubric is configured)
      One classify-model pass scoring 0–1 against a fixed rubric.

Ranking: max(interest_score, novelty_score).  NULL scores sort last.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from typing import Any

import httpx

from palimpsest.config import Config, load
from palimpsest.db import connect

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
)

# ── Embedding helpers ─────────────────────────────────────────────────────────

def _embed_text(cfg: Config, text: str) -> list[float]:
    """Embed a single text string using the configured embed model."""
    model = cfg.embed.get("model", "nomic-embed-text")
    keep_alive = cfg.models.get("keep_alive", "24h")
    resp = httpx.post(
        "http://localhost:11434/api/embeddings",
        json={"model": model, "prompt": text, "keep_alive": keep_alive},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Return 1 - cosine_similarity(a, b).  Range [0, 2]."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


# ── Novelty scoring ───────────────────────────────────────────────────────────

def _compute_novelty(cfg: Config, rows: list[dict[str, Any]], k: int = 5) -> dict[str, float]:
    """Compute novelty_score for each brief row.

    Each brief's summary is embedded and compared to all other briefs.
    novelty_score = mean cosine distance to k nearest neighbours.
    High score = anomalous = more interesting.

    Args:
        cfg: Loaded configuration.
        rows: List of dicts with at least 'doc_id' and 'summary'.
        k: Number of nearest neighbours.

    Returns:
        Dict mapping doc_id -> novelty_score.
    """
    if not rows:
        return {}

    logger.info("triage: embedding %d summaries for novelty scoring ...", len(rows))
    embeddings: list[list[float]] = []
    for i, row in enumerate(rows):
        summary = row.get("summary") or ""
        vec = _embed_text(cfg, summary or f"Document {row['doc_id']}")
        embeddings.append(vec)
        if (i + 1) % 10 == 0:
            logger.info("triage: embedded %d/%d", i + 1, len(rows))

    scores: dict[str, float] = {}
    n = len(rows)
    effective_k = min(k, n - 1)

    for i, row in enumerate(rows):
        if n == 1:
            scores[row["doc_id"]] = 0.0
            continue
        distances = sorted(
            _cosine_distance(embeddings[i], embeddings[j])
            for j in range(n) if j != i
        )
        knn_distances = distances[:effective_k]
        scores[row["doc_id"]] = sum(knn_distances) / len(knn_distances)

    return scores


# ── Interest scoring ──────────────────────────────────────────────────────────

_INTEREST_SYSTEM = """\
You are an investigative analyst scoring a document brief for investigative interest.
Score 0.0 (no interest) to 1.0 (highly interesting) based on the rubric.

Rubric: {rubric}

Reply with a single JSON object: {{"score": <float 0.0-1.0>, "reason": "<one sentence>"}}
No prose, no markdown."""


def _compute_interest(cfg: Config, rows: list[dict[str, Any]]) -> dict[str, float]:
    """Compute interest_score for each brief using the classify model and triage rubric.

    Skipped if no [triage] rubric is configured; returns empty dict.
    """
    triage_cfg = cfg.raw.get("triage", {})
    rubric = triage_cfg.get("rubric", "").strip()
    if not rubric:
        logger.info("triage: no [triage].rubric configured — skipping interest scoring.")
        return {}

    model = cfg.models.get("classify", "qwen2.5:3b")
    keep_alive = cfg.models.get("keep_alive", "24h")
    system = _INTEREST_SYSTEM.format(rubric=rubric)

    scores: dict[str, float] = {}
    for row in rows:
        doc_id = row["doc_id"]
        summary = row.get("summary") or ""
        flags = row.get("flags_json") or "[]"
        prompt = f"doc_id: {doc_id}\nsummary: {summary}\nflags: {flags}"
        try:
            resp = httpx.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": model,
                    "system": system,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0},
                    "keep_alive": keep_alive,
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            parsed = json.loads(raw)
            score = float(parsed.get("score", 0.0))
            scores[doc_id] = max(0.0, min(1.0, score))
        except Exception as exc:
            logger.warning("triage: interest scoring failed for %s: %s", doc_id, exc)
            scores[doc_id] = 0.0

    return scores


# ── Main triage functions ─────────────────────────────────────────────────────

def run_triage(cfg: Config, limit: int = 30, k_novelty: int = 5) -> list[dict[str, Any]]:
    """Score all briefs, write scores back to DB, return top-N ranked rows.

    Args:
        cfg: Loaded configuration.
        limit: Number of top briefs to return.
        k_novelty: Number of nearest neighbours for novelty scoring.

    Returns:
        List of dicts (ordered by score desc) with keys:
        doc_id, year, doc_type, score, summary, flags.
    """
    conn = connect(cfg)
    conn.row_factory = sqlite3.Row

    rows_raw = conn.execute(
        """
        SELECT b.doc_id, b.summary, b.doc_type, b.flags_json,
               b.interest_score, b.novelty_score,
               d.year
        FROM briefs b
        LEFT JOIN documents d ON b.doc_id = d.doc_id
        """
    ).fetchall()

    if not rows_raw:
        logger.info("triage: no briefs found in database.")
        conn.close()
        return []

    rows = [dict(r) for r in rows_raw]
    logger.info("triage: scoring %d briefs.", len(rows))

    # Novelty
    novelty_scores = _compute_novelty(cfg, rows, k=k_novelty)

    # Interest
    interest_scores = _compute_interest(cfg, rows)

    # Write back to DB
    with conn:
        for row in rows:
            doc_id = row["doc_id"]
            ns = novelty_scores.get(doc_id)
            is_ = interest_scores.get(doc_id)
            conn.execute(
                "UPDATE briefs SET novelty_score=?, interest_score=? WHERE doc_id=?",
                (ns, is_, doc_id),
            )
            row["novelty_score"] = ns
            row["interest_score"] = is_

    conn.close()

    # Rank by max(interest, novelty), NULLs last
    def rank_key(row: dict[str, Any]) -> float:
        ns = row.get("novelty_score") or 0.0
        is_ = row.get("interest_score") or 0.0
        return max(ns, is_)

    ranked = sorted(rows, key=rank_key, reverse=True)
    return ranked[:limit]


def get_brief(cfg: Config, doc_id: str) -> dict[str, Any] | None:
    """Return the full brief record for a single document, or None."""
    conn = connect(cfg)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT b.*, d.year
        FROM briefs b
        LEFT JOIN documents d ON b.doc_id = d.doc_id
        WHERE b.doc_id = ?
        """,
        (doc_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _print_table(rows: list[dict[str, Any]]) -> None:
    """Print a compact triage table to stdout."""
    header = f"{'#':>3}  {'doc_id':<14}  {'year':>4}  {'type':<14}  {'score':>6}  {'flags':<30}  summary"
    print(header)
    print("-" * len(header))
    for i, row in enumerate(rows, 1):
        ns = row.get("novelty_score")
        is_ = row.get("interest_score")
        score = max(v for v in (ns, is_) if v is not None) if (ns is not None or is_ is not None) else 0.0
        flags = json.loads(row.get("flags_json") or "[]")
        flag_str = ",".join(flags)[:30] if flags else ""
        summary_first = (row.get("summary") or "").split(".")[0][:80]
        year = row.get("year") or "????"
        doc_type = (row.get("doc_type") or "other")[:14]
        print(f"{i:>3}.  {row['doc_id']:<14}  {str(year):>4}  {doc_type:<14}  {score:>6.3f}  {flag_str:<30}  {summary_first}")


def _print_full_brief(brief: dict[str, Any]) -> None:
    """Pretty-print a full brief to stdout."""
    print(f"\n=== Brief: {brief['doc_id']} ===")
    print(f"Year:          {brief.get('year', '?')}")
    print(f"Type:          {brief.get('doc_type', '?')}")
    print(f"Model:         {brief.get('model', '?')}")
    print(f"Novelty:       {brief.get('novelty_score')}")
    print(f"Interest:      {brief.get('interest_score')}")
    print(f"\nSummary:\n{brief.get('summary', '')}")

    claims = json.loads(brief.get("claims_json") or "[]")
    if claims:
        print(f"\nClaims ({len(claims)}):")
        for c in claims:
            print(f"  [p{c.get('page_no','?')}] {c.get('text','')}")

    events = json.loads(brief.get("events_json") or "[]")
    if events:
        print(f"\nEvents ({len(events)}):")
        for e in events:
            print(
                f"  [p{e.get('page_no','?')}] {e.get('actor','')} {e.get('action','')} "
                f"{e.get('object','')} @ {e.get('date','')} ({e.get('place','')})"
            )

    reds = json.loads(brief.get("redactions_json") or "[]")
    if reds:
        print(f"\nRedaction hypotheses ({len(reds)}):")
        for r in reds:
            print(
                f"  [p{r.get('page_no','?')}] label={r.get('label','')}  "
                f"likely={r.get('likely_hidden','')}  {r.get('rationale','')}"
            )

    flags = json.loads(brief.get("flags_json") or "[]")
    if flags:
        print(f"\nFlags: {', '.join(flags)}")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="palimpsest-triage",
        description="Rank document briefs by investigative interest.",
    )
    parser.add_argument("--config", default=None, metavar="PATH")
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        metavar="N",
        help="Number of top briefs to show (default: 30).",
    )
    parser.add_argument(
        "--doc",
        default=None,
        metavar="DOC_ID",
        help="Dump full brief for a single document.",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        metavar="K",
        help="Nearest-neighbour count for novelty scoring (default: 5).",
    )
    args = parser.parse_args()
    cfg = load(args.config)

    if args.doc:
        brief = get_brief(cfg, args.doc)
        if brief is None:
            print(f"No brief found for doc_id '{args.doc}'.", file=sys.stderr)
            sys.exit(1)
        _print_full_brief(brief)
    else:
        ranked = run_triage(cfg, limit=args.limit, k_novelty=args.k)
        if not ranked:
            print("No briefs in database.")
            sys.exit(0)
        _print_table(ranked)


if __name__ == "__main__":
    main()
