"""Trust gate: calibrated confidence + surfacing tier (specs/EVAL-TRUST-GATE.md §4.2).

The gate is additive to the identity gate (Iron Rule #3): it operates on
already-masked rows and can only drop or annotate them, never unmask.
"""
from __future__ import annotations

import json
from pathlib import Path

from palimpsest.config import Config
from palimpsest.eval.stats import predict_isotonic

_TIER_ORDER = {"tentative": 0, "surfaceable": 1}


def load_artifact(cfg: Config) -> dict | None:
    path = (getattr(cfg, "eval", {}) or {}).get("artifact_path")
    if not path:
        return None
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


def confidence_and_tier(type_key: str, raw_score: float, artifact: dict | None):
    t = (artifact or {}).get("types", {}).get(type_key) if artifact else None
    if not t:
        return (None, "tentative")
    curve = [tuple(pt) for pt in t.get("isotonic", [])]
    conf = predict_isotonic(curve, raw_score) if curve else None
    thr = t.get("threshold")
    tier = "surfaceable" if (thr is not None and raw_score >= thr) else "tentative"
    return (conf, tier)


def apply_gate(rows, artifact, min_tier: str = "surfaceable", enforcement: str = "enforce"):
    """Annotate each row with confidence + gate_tier; drop sub-tier rows when enforcing.

    rows: list of dicts each carrying 'score' and 'type_key'. Other keys
    (including masked text) are preserved untouched.
    """
    out = []
    floor = _TIER_ORDER.get(min_tier, 1)
    for r in rows:
        conf, tier = confidence_and_tier(r.get("type_key", ""), float(r.get("score", 0.0)), artifact)
        r = dict(r)
        r["confidence"] = conf
        r["gate_tier"] = tier
        if enforcement == "enforce" and _TIER_ORDER[tier] < floor:
            continue
        out.append(r)
    return out
