# TASK-13 — Type a/b synthetic case generator + grading oracle

**Depends on:** TASK-12 (`palimpsest/eval/` package exists).
**Builds:** the case generator for types a/b and the pure grading oracle. Both
are unit-tested **without** the scorer or FAISS — generation produces validated
row structures; the oracle is a pure function. End-to-end wiring is TASK-15.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §3.1.

## Context you need (restated)

The Type a scorer (`palimpsest/scorers/type_a.py`) recovers a redacted span by:
1. **Anchor route** — building a set `A` of entity norms that are near the
   redaction *or whose text appears in `context_before`/`context_after`*, then
   finding other-document pages sharing ≥2 of those norms; every entity on a
   matched page becomes a candidate.
2. **Embedding route** — embedding `context_before + " " + context_after`,
   searching the FAISS index, and pulling entities inside hit chunks.

Score = `0.5·cosine + 0.3·anchor + 0.2·kind`; kind is 1.0 when the redaction
label implies the candidate's kind (`(b)(6)`/`(b)(7)` → `person`), else 0.5;
threshold 0.65. **Therefore, for a positive case the answer must outrank the
shared anchors** — achieved by labeling the redaction `(b)(6)` for a person
answer, or (Type b) by the dosage proximity bonuses in the scorer.

Implications the generator must honor:
- Anchor entity **texts must appear in the redaction's context strings** (that
  is how they enter `A` when bboxes are null).
- The redacted document and the source document must share ≥2 anchor norms.
- A `negative_control`/`hard_negative` case must **not** contain the answer norm
  anywhere in the corpus (only the runner-level corpus matters; within a case,
  omit the source page that holds the answer).
- **Anchors are unique per case** (keyed on the case uid). All cases share one
  eval DB and the scorer matches across every document in it; if cases shared
  literal anchors like "Oak Ridge"/"1957", every case would cross-link to every
  other and corrupt grading. Do **not** simplify the `Site-{uid}`/`Ref-{uid}`
  anchors back to fixed strings — the per-case uniqueness is load-bearing.

## Files

- Create: `palimpsest/eval/generators.py`
- Create: `palimpsest/eval/oracle.py`
- Test: `tests/test_eval_generators_ab.py`, `tests/test_eval_oracle.py`

---

- [ ] **Step 1: Write the failing oracle test**

Create `tests/test_eval_oracle.py`:

```python
from palimpsest.eval.oracle import grade, Result


def test_positive_correct_is_tp():
    out = grade("john smith", [(0.9, "john smith")])
    assert out == [Result("TP", 0.9, "john smith")]


def test_positive_wrong_is_fp_plus_fn():
    out = grade("john smith", [(0.8, "oak ridge")])
    labels = sorted(r.label for r in out)
    assert labels == ["FN", "FP"]


def test_negative_with_hits_all_fp():
    out = grade(None, [(0.7, "x"), (0.66, "y")])
    assert [r.label for r in out] == ["FP", "FP"]


def test_negative_empty_is_tn():
    assert grade(None, []) == [Result("TN", None, None)]


def test_positive_empty_is_fn():
    assert grade("a", []) == [Result("FN", None, None)]
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_oracle.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.oracle`.

- [ ] **Step 3: Implement the oracle**

Create `palimpsest/eval/oracle.py`:

```python
"""Pure grading oracle for eval predictions.

`grade` is type-agnostic: given the known correct value (or None when no correct
answer exists) and a list of (score, value) predictions for one case, it returns
one Result per prediction (TP/FP), plus an FN when a positive case surfaced no
correct prediction, or a single TN when a negative case surfaced nothing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Result:
    label: str            # "TP" | "FP" | "FN" | "TN"
    raw_score: float | None
    predicted: str | None


def grade(answer: str | None, preds: list[tuple[float, str]]) -> list[Result]:
    out: list[Result] = []
    if answer is None:
        if not preds:
            out.append(Result("TN", None, None))
        else:
            out.extend(Result("FP", s, v) for s, v in preds)
        return out

    if not preds:
        out.append(Result("FN", None, None))
        return out

    matched = False
    for s, v in preds:
        if v == answer:
            out.append(Result("TP", s, v))
            matched = True
        else:
            out.append(Result("FP", s, v))
    if not matched:
        out.append(Result("FN", None, None))
    return out
```

- [ ] **Step 4: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_oracle.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Write the failing generator test**

Create `tests/test_eval_generators_ab.py`:

```python
from palimpsest.eval.generators import gen_type_a_cases, gen_type_b_cases


def _check_offsets(case):
    for page in case.pages:
        for e in page.entities:
            assert page.text[e["char_start"]:e["char_end"]] == e["text"]


def test_type_a_case_mix_and_truth():
    cases = gen_type_a_cases(n_per_kind=3, seed=1)
    kinds = sorted({c.case_kind for c in cases})
    assert kinds == ["hard_negative", "negative_control", "positive"]
    assert len(cases) == 9

    for c in cases:
        _check_offsets(c)
        assert c.type_key == "type_a"
        red_pages = [p for p in c.pages if p.redaction]
        assert len(red_pages) == 1            # exactly one redaction under test

    for c in cases:
        answer = c.truth["answer_norm"]
        norms = {e["norm"] for p in c.pages for e in p.entities}
        if c.case_kind == "positive":
            assert answer is not None and answer in norms   # source present
        else:
            assert answer is None                            # no correct answer exists


def test_type_a_positive_anchor_in_context():
    cases = gen_type_a_cases(n_per_kind=1, seed=2)
    pos = next(c for c in cases if c.case_kind == "positive")
    red = next(p for p in pos.pages if p.redaction)
    ctx = (red.redaction["context_before"] + " " + red.redaction["context_after"]).lower()
    # the case's two unique anchor norms appear in the redaction context (drives anchor route)
    assert f"site-{pos.case_uid}".lower() in ctx
    assert f"ref-{pos.case_uid}".lower() in ctx


def test_type_b_answer_is_dosage():
    cases = gen_type_b_cases(n_per_kind=2, seed=3)
    assert len(cases) == 6
    pos = [c for c in cases if c.case_kind == "positive"]
    for c in pos:
        ans = c.truth["answer_norm"]
        dosage_norms = {e["norm"] for p in c.pages for e in p.entities if e["kind"] == "dosage"}
        assert ans in dosage_norms
    for c in cases:
        _check_offsets(c)
```

- [ ] **Step 6: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_generators_ab.py -v`
Expected: FAIL — `ImportError` (no `gen_type_a_cases`).

- [ ] **Step 7: Implement the generators**

Create `palimpsest/eval/generators.py`:

```python
"""Synthetic case generators for the eval harness (types a, b, c).

A Case is a self-contained bundle of documents/pages/entities/redactions plus a
truth record. The runner (TASK-15) inserts these into the isolated eval DB,
embeds each page's chunk text, runs the real scorer, and grades the output.

Doc ids are namespaced by case_uid so many cases coexist in one eval DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Doc:
    doc_id: str
    year: int | None = None


@dataclass
class Page:
    doc_id: str
    page_no: int
    text: str
    entities: list[dict] = field(default_factory=list)  # kind,text,norm,char_start,char_end
    chunk_id: int | None = None
    redaction: dict | None = None  # kind,label,context_before,context_after


@dataclass
class Case:
    case_uid: str
    type_key: str
    case_kind: str          # positive | negative_control | hard_negative
    docs: list[Doc]
    pages: list[Page]
    truth: dict             # {"answer_norm": str | None} for a/b


def _segments_to_page(doc_id: str, page_no: int, segments: list) -> Page:
    """Assemble page text from segments, tracking entity offsets.

    A segment is either a plain str (literal text) or a dict
    {kind,text,norm} (an entity). Segments are joined with single spaces.
    """
    parts: list[str] = []
    entities: list[dict] = []
    cursor = 0
    for i, seg in enumerate(segments):
        if i > 0:
            parts.append(" ")
            cursor += 1
        if isinstance(seg, str):
            parts.append(seg)
            cursor += len(seg)
        else:
            text = seg["text"]
            start = cursor
            parts.append(text)
            cursor += len(text)
            entities.append({
                "kind": seg["kind"], "text": text, "norm": seg["norm"],
                "char_start": start, "char_end": cursor,
            })
    return Page(doc_id=doc_id, page_no=page_no, text="".join(parts), entities=entities)


_ANSWERS_A = ["john smith", "robert hale", "edward grant", "alice brenner", "frank doyle"]
_DOSES = ["15 rem", "200 rad", "3.5 sv", "50 rem", "120 rad"]


def _ab_case(uid, kind, answer_text, answer_kind):
    """Build one a/b case. answer_kind ∈ {'person','dosage'}."""
    answer_norm = answer_text.lower()
    anchor1 = {"kind": "location", "text": f"Site-{uid}", "norm": f"site-{uid}"}
    anchor2 = {"kind": "org", "text": f"Ref-{uid}", "norm": f"ref-{uid}"}
    label = "(b)(6)" if answer_kind == "person" else "DELETED"
    ctx_before = f"Report from Site-{uid} dated Ref-{uid} concerning subject"
    ctx_after = f"filed under medical records at Site-{uid} again Ref-{uid}."

    pages: list[Page] = []
    docs: list[Doc] = []

    # Redacted document (doc_A) — always present, holds the redaction under test
    a_doc = f"{uid}_A"
    a_page = _segments_to_page(a_doc, 1, [
        "Report from", anchor1, "dated", anchor2,
        "concerning subject [REDACTED] filed under medical records.",
    ])
    a_page.redaction = {
        "kind": "deleted_text", "label": label,
        "context_before": ctx_before, "context_after": ctx_after,
    }
    pages.append(a_page)
    docs.append(Doc(a_doc, year=1957))

    def source_page(doc_id, ans_text, ans_kind):
        segs = ["Report from", anchor1, "dated", anchor2, "concerning subject",
                {"kind": ans_kind, "text": ans_text, "norm": ans_text.lower()}]
        if ans_kind == "dosage":
            segs.append({"kind": "subject_ref", "text": "Subject 3", "norm": "subject 3"})
        segs.append("filed under medical records.")
        return _segments_to_page(doc_id, 1, segs)

    if kind == "positive":
        b_doc = f"{uid}_B"
        pages.append(source_page(b_doc, answer_text, answer_kind))
        docs.append(Doc(b_doc, year=1957))
        truth = {"answer_norm": answer_norm}
    elif kind == "negative_control":
        truth = {"answer_norm": None}      # no source anywhere
    else:  # hard_negative — a decoy of the same kind, true answer absent
        decoy = "robert decoy" if answer_kind == "person" else "999 rem"
        b_doc = f"{uid}_D"
        pages.append(source_page(b_doc, decoy, answer_kind))
        docs.append(Doc(b_doc, year=1957))
        truth = {"answer_norm": None}

    return Case(case_uid=uid, type_key=("type_b" if answer_kind == "dosage" else "type_a"),
                case_kind=kind, docs=docs, pages=pages, truth=truth)


def gen_type_a_cases(n_per_kind: int = 5, seed: int = 1337) -> list[Case]:
    cases: list[Case] = []
    for kind in ("positive", "negative_control", "hard_negative"):
        for i in range(n_per_kind):
            ans = _ANSWERS_A[i % len(_ANSWERS_A)]
            cases.append(_ab_case(f"a_{kind}_{i}", kind, ans, "person"))
    return cases


def gen_type_b_cases(n_per_kind: int = 5, seed: int = 1337) -> list[Case]:
    cases: list[Case] = []
    for kind in ("positive", "negative_control", "hard_negative"):
        for i in range(n_per_kind):
            dose = _DOSES[i % len(_DOSES)]
            cases.append(_ab_case(f"b_{kind}_{i}", kind, dose, "dosage"))
    return cases
```

- [ ] **Step 8: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_generators_ab.py -v`
Expected: PASS (3 tests).

- [ ] **Step 9: Full suite + lint + commit**

Run: `uv run pytest -q`
Run: `uv run ruff check palimpsest/eval/generators.py palimpsest/eval/oracle.py tests/test_eval_generators_ab.py tests/test_eval_oracle.py`

```bash
git add palimpsest/eval/generators.py palimpsest/eval/oracle.py \
        tests/test_eval_generators_ab.py tests/test_eval_oracle.py
git commit -m "feat(eval): type a/b synthetic case generator and pure grading oracle"
```

## Out of scope
- No FAISS, no scorer call, no DB insert. The generator returns data; the oracle
  grades lists. Wiring is TASK-15.
- Type c generation is TASK-14.

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Hard blocker → record in
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, move on.
