# TASK-14 — Type c synthetic case generator (decoy + answer-absent)

**Depends on:** TASK-13 (`generators.py` with `Case/Doc/Page/_segments_to_page`,
`oracle.grade`).
**Builds:** the Type c (anonymous identity linkage) generator — the
safety-critical one. Reuses the `grade` oracle from TASK-13 unchanged.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §3.2.

## Context you need (restated)

`TypeCScorer` (`palimpsest/scorers/type_c.py`) links a `subject_ref` page to a
named `person` page using page-level attributes (no char offsets):
- A subject page qualifies only if it has a `subject_ref` entity **and** ≥1 of
  `org`/`date`/`dosage` on the same page.
- `org_match` = 1.0 if min Levenshtein(subject_org, named_org) ≤ 2, else 0.0.
- `date_proximity` = `max(0, 1 − |subj_year − named_doc_year| / 3)`, where
  `subj_year` is parsed by regex `\b(\d{4})\b` from a subject **date entity**, and
  `named_doc_year` is the named page's **`documents.year`** column.
- `dosage_bonus` = 0.2 if the subject and named pages share a normalized dosage.
- `score = org·0.5 + date·0.3 + dosage·0.2`; threshold 0.65.

So a same-org / same-year named person scores 0.8 (1.0 with a shared dose) — well
above threshold. The generator exploits this to build a decoy that the current
scorer cannot distinguish from the true subject. That is the point: the harness
will record those decoy links as FPs.

Case kinds (note: differs from a/b — here the decoy cases keep the true answer
present where stated):
- `positive` — true person `P` only. truth = `P.norm`.
- `hard_negative` — true `P` **and** a decoy `P'` (same org+year, distinct
  identity). truth = `P.norm`; the scorer will link both → one TP, one FP.
- `negative_control` — true identity absent, decoy `P'` present. truth = None;
  any link is an FP.

Cross-case isolation: cases share one eval DB and `TypeCScorer` matches every
subject against every named person in it. Each case gets a **unique year spaced
≥3 apart** (`1950 + 3*gi`) so cross-case `date_proximity` is 0 and cases cannot
link to one another. Keep that spacing — it is load-bearing isolation.

## Files

- Modify: `palimpsest/eval/generators.py` (append Type c functions + constants)
- Test: `tests/test_eval_generators_c.py`

---

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_generators_c.py`:

```python
from palimpsest.eval.generators import gen_type_c_cases
from palimpsest.eval.oracle import grade


def _check_offsets(case):
    for page in case.pages:
        for e in page.entities:
            assert page.text[e["char_start"]:e["char_end"]] == e["text"]


def test_type_c_mix_and_structure():
    cases = gen_type_c_cases(n_per_kind=3, seed=1)
    assert len(cases) == 9
    assert sorted({c.case_kind for c in cases}) == ["hard_negative", "negative_control", "positive"]

    for c in cases:
        _check_offsets(c)
        assert c.type_key == "type_c"
        subj_pages = [p for p in c.pages for e in p.entities if e["kind"] == "subject_ref"]
        assert len(subj_pages) == 1
        subj_page = subj_pages[0]
        kinds = {e["kind"] for e in subj_page.entities}
        assert "org" in kinds and "date" in kinds  # subject qualifies for the scorer


def test_type_c_truth_per_kind():
    cases = gen_type_c_cases(n_per_kind=2, seed=2)
    for c in cases:
        person_norms = {e["norm"] for p in c.pages for e in p.entities if e["kind"] == "person"}
        if c.case_kind == "positive":
            assert c.truth["true_named_norm"] in person_norms
            assert len(person_norms) == 1                 # only the true person
        elif c.case_kind == "hard_negative":
            assert c.truth["true_named_norm"] in person_norms
            assert len(person_norms) == 2                 # true + decoy
        else:  # negative_control
            assert c.truth["true_named_norm"] is None
            assert len(person_norms) == 1                 # decoy only


def test_grade_reused_for_c():
    # the decoy case: true + decoy both linked → one TP, one FP
    out = grade("john smith", [(0.8, "john smith"), (0.8, "carl reed")])
    assert sorted(r.label for r in out) == ["FP", "TP"]
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_generators_c.py -v`
Expected: FAIL — `ImportError: cannot import name 'gen_type_c_cases'`.

- [ ] **Step 3: Append the Type c generator to `generators.py`**

Add at the end of `palimpsest/eval/generators.py`:

```python
_ORGS = ["los alamos", "oak ridge", "hanford", "sandia", "argonne"]
_PERSONS_C = ["john smith", "robert hale", "edward grant", "alice brenner", "frank doyle"]


def _named_page(doc_id, person_norm, org_norm, year):
    page = _segments_to_page(doc_id, 1, [
        {"kind": "person", "text": person_norm.title(), "norm": person_norm},
        "of", {"kind": "org", "text": org_norm.title(), "norm": org_norm},
        "examined; dose", {"kind": "dosage", "text": "15 rem", "norm": "15 rem"}, "recorded.",
    ])
    return page, Doc(doc_id, year=year)


def _c_case(uid, kind, org, year, person_true, person_decoy):
    s_doc = f"{uid}_S"
    s_page = _segments_to_page(s_doc, 1, [
        "Examination of", {"kind": "subject_ref", "text": "Subject 3", "norm": "subject 3"},
        "at", {"kind": "org", "text": org.title(), "norm": org},
        "in", {"kind": "date", "text": str(year), "norm": str(year)},
        "received", {"kind": "dosage", "text": "15 rem", "norm": "15 rem"}, "total.",
    ])
    pages = [s_page]
    docs = [Doc(s_doc, year=year)]
    truth = {"true_named_norm": None}

    if kind in ("positive", "hard_negative"):
        pg, dc = _named_page(f"{uid}_P", person_true, org, year)
        pages.append(pg)
        docs.append(dc)
        truth = {"true_named_norm": person_true}
    if kind in ("hard_negative", "negative_control"):
        pg, dc = _named_page(f"{uid}_D", person_decoy, org, year)
        pages.append(pg)
        docs.append(dc)

    return Case(case_uid=uid, type_key="type_c", case_kind=kind,
                docs=docs, pages=pages, truth=truth)


def gen_type_c_cases(n_per_kind: int = 5, seed: int = 1337) -> list[Case]:
    cases: list[Case] = []
    gi = 0
    for kind in ("positive", "negative_control", "hard_negative"):
        for i in range(n_per_kind):
            org = _ORGS[i % len(_ORGS)]
            year = 1950 + 3 * gi          # unique per case, ≥3 apart → no cross-case linking
            person_true = _PERSONS_C[i % len(_PERSONS_C)]
            person_decoy = _PERSONS_C[(i + 1) % len(_PERSONS_C)]
            cases.append(_c_case(f"c_{kind}_{i}", kind, org, year, person_true, person_decoy))
            gi += 1
    return cases
```

- [ ] **Step 4: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_generators_c.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Full suite + lint + commit**

Run: `uv run pytest -q`
Run: `uv run ruff check palimpsest/eval/generators.py tests/test_eval_generators_c.py`

```bash
git add palimpsest/eval/generators.py tests/test_eval_generators_c.py
git commit -m "feat(eval): type c identity-linkage generator with decoy + answer-absent cases"
```

## Out of scope
- No scorer invocation; wiring is TASK-15. The oracle is reused unchanged.
- Fuzzy-org spelling variants (edit distance 1–2) are left identical in v1; the
  decoy already exercises the false-link path. A fuzzy variant is a later
  enhancement, not required here.

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Hard blocker →
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, move on.
