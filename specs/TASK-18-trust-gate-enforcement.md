# TASK-18 — Trust gate + surfacing-boundary enforcement (Iron Rule #4)

**Depends on:** TASK-16 (calibration artifact + `predict_isotonic`).
**Builds:** `gate.py` (load artifact, score→confidence→tier, filter/annotate) and
its enforcement in `server.py::palimpsest_find_redaction_gaps`. Establishes Iron
Rule #4: no finding surfaced for publication below its calibrated precision bar.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §4.2, §7.

## Context you need (restated)

- The calibration artifact (`calibration.json`, TASK-16) has, per type:
  `threshold` (or `null` = gate disabled) and `isotonic` (a list of
  `[x_right, value]` pairs usable by `palimpsest.eval.stats.predict_isotonic`).
- `server.py::palimpsest_find_redaction_gaps` already queries `gap_candidates`
  joined to the redaction and clear entity, applies person masking via
  `mask_person`/`get_masked_text_for_page`/`mask_context_text`, builds a list of
  result dicts (each carries `score`, `status`, the entity `kind` as
  `row["e_kind"]`, masked text, citations), and returns `json.dumps(...)`.
- `gap_candidates` rows are Type a **or** Type b; distinguish by the clear
  entity kind: `dosage` ⇒ `type_b`, else `type_a`.
- **Ordering invariant:** masking happens while building the result dicts; the
  gate runs *after*, on already-masked dicts, and only drops or annotates rows.
  The gate must never unmask or relax Iron Rule #3.

## Files

- Create: `palimpsest/eval/gate.py`
- Modify: `palimpsest/server.py` (`palimpsest_find_redaction_gaps`)
- Modify: `specs/00-ARCHITECTURE.md` (add Iron Rule #4 to the list)
- Test: `tests/test_eval_gate.py`

---

- [ ] **Step 1: Write the failing gate test**

Create `tests/test_eval_gate.py`:

```python
from palimpsest.eval.gate import confidence_and_tier, apply_gate

ARTIFACT = {
    "types": {
        "type_a": {"threshold": 0.80, "isotonic": [[0.5, 0.2], [0.8, 0.9], [1.0, 0.98]]},
        "type_c": {"threshold": None, "isotonic": [[0.6, 0.3], [0.8, 0.5]]},
    }
}


def test_confidence_and_tier_above_threshold():
    conf, tier = confidence_and_tier("type_a", 0.9, ARTIFACT)
    assert tier == "surfaceable"
    assert 0.0 <= conf <= 1.0


def test_below_threshold_is_tentative():
    _, tier = confidence_and_tier("type_a", 0.7, ARTIFACT)
    assert tier == "tentative"


def test_disabled_gate_is_tentative():
    _, tier = confidence_and_tier("type_c", 0.99, ARTIFACT)
    assert tier == "tentative"          # threshold is null → never surfaceable


def test_unknown_type_is_tentative():
    conf, tier = confidence_and_tier("type_x", 0.99, ARTIFACT)
    assert (conf, tier) == (None, "tentative")


def test_apply_gate_enforce_drops_tentative():
    rows = [
        {"score": 0.9, "type_key": "type_a", "text": "PERSON-0042"},
        {"score": 0.7, "type_key": "type_a", "text": "PERSON-0043"},
    ]
    out = apply_gate(rows, ARTIFACT, min_tier="surfaceable", enforcement="enforce")
    assert len(out) == 1
    assert out[0]["gate_tier"] == "surfaceable"
    assert out[0]["text"] == "PERSON-0042"          # masking untouched


def test_apply_gate_annotate_keeps_all():
    rows = [{"score": 0.7, "type_key": "type_a"}]
    out = apply_gate(rows, ARTIFACT, enforcement="annotate")
    assert len(out) == 1
    assert out[0]["gate_tier"] == "tentative"
    assert "confidence" in out[0]
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.gate`.

- [ ] **Step 3: Implement the gate**

Create `palimpsest/eval/gate.py`:

```python
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
```

- [ ] **Step 4: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_gate.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Wire the gate into `palimpsest_find_redaction_gaps`**

Read `palimpsest/server.py::palimpsest_find_redaction_gaps`. Apply exactly these
changes:

1. Add a `min_tier: str = "surfaceable"` parameter to the function signature.
2. Near where `approved_ids` is loaded (once per call), add:
   ```python
   from palimpsest.eval.gate import load_artifact, apply_gate
   _artifact = load_artifact(cfg)
   _enforcement = (getattr(cfg, "eval", {}) or {}).get("gate_enforcement", "enforce")
   ```
3. When building each result dict (the dict that already holds `"score"` and the
   masked entity), add a key:
   ```python
   "type_key": "type_b" if row["e_kind"] == "dosage" else "type_a",
   ```
4. After the loop that builds the `results` list and **before** `json.dumps`,
   add:
   ```python
   results = apply_gate(results, _artifact, min_tier=min_tier, enforcement=_enforcement)
   ```

Result: by default only `surfaceable` findings are returned, each annotated with
`confidence`/`gate_tier`. Callers doing reviewer triage can pass
`min_tier="tentative"` to see everything, clearly flagged. When no artifact
exists, every row is `tentative`; with `gate_enforcement="enforce"` that means
the tool returns nothing until a calibration is produced — fail-closed, the
correct default for a precision gate. If that is too aggressive before first
calibration, set `gate_enforcement = "annotate"` in `config.toml`.

- [ ] **Step 6: Add Iron Rule #4 to the architecture contract**

In `specs/00-ARCHITECTURE.md`, in the "Iron rules" list, add:

```
5. **No finding surfaced for publication below its calibrated precision bar.**
   Enforced by the trust gate (gate_tier + the min_tier default in server.py;
   see specs/EVAL-TRUST-GATE.md). Strictly additive to rule 3 — the gate can
   only withhold or flag a finding, never unmask a person.
```

(Numbered 5 in the file because that list already contains 4 entries; it is
"Iron Rule #4" in prose since rule 4 there is "all tunables in config".)

- [ ] **Step 7: Regression + new tests + lint + commit**

Run: `uv run pytest tests/test_eval_gate.py tests/test_server.py -v`
Expected: gate tests PASS; `test_server.py` still PASS (no regression). If a
server test now returns fewer gap rows because enforcement is on and no artifact
exists, set `gate_enforcement="annotate"` in that test's config fixture, or have
the test write a permissive artifact — do **not** weaken the production default.
Run: `uv run pytest -q`
Run: `uv run ruff check palimpsest/eval/gate.py palimpsest/server.py tests/test_eval_gate.py`

```bash
git add palimpsest/eval/gate.py palimpsest/server.py specs/00-ARCHITECTURE.md tests/test_eval_gate.py
git commit -m "feat(eval): trust gate + server enforcement (Iron Rule #4)"
```

## Out of scope
- Caching `confidence`/`gate_tier` onto `gap_candidates` rows (schema columns
  exist from TASK-11). Surface-time computation is correct and sufficient;
  caching is a deferred optimization.
- Wiring the gate into Type c link surfacing (`review.py links`). Do it next:
  read that handler, tag rows `type_key="type_c"`, run `apply_gate` **after** the
  existing `deceased_historical`+approved filter, never before. Track as a
  follow-up if review.py is out of scope for this packet.

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Hard blocker →
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, move on.
