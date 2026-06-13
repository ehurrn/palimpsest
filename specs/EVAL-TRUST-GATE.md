# Palimpsest — Evaluation & Trust Gate (Phase 4)

*Source-of-truth design for the synthetic evaluation harness and the calibrated
trust gate. Implementation is decomposed into `specs/TASK-11` … `TASK-18`.
If a task packet ever contradicts this file, THIS FILE WINS.*

Relationship to existing specs:
- `specs/00-ARCHITECTURE.md` — system contract; Iron Rules. This work adds a
  fourth Iron Rule (see §7) and touches only `server.py` at the surfacing
  boundary plus a schema migration.
- `specs/FINDING-TYPES.md` — the six finding-types. This work evaluates and
  gates **types a, b, c**. Type e is deferred (see §8). Types d, f (absence
  findings) are out of scope.

---

## 0. Decisions locked (from brainstorm 2026-06-13)

| Decision | Value | Why |
|----------|-------|-----|
| Purpose | **Precision-first trust gate** | Outputs are factual claims about real people/institutions; a confident-but-wrong de-redaction is a false public assertion (defamation risk). False positives are worse than false negatives. |
| Ground truth | **Synthetic redaction/linkage pairs + negative controls** | No labeled corpus exists; synthetic gives volume, recall, and calibration data without hand-labeling. Negative controls close the precision blind-spot. |
| Build target | **Offline eval CLI + integrated gate** | The CLI measures and calibrates; the gate persists confidence and enforces it at the surfacing boundary. |
| Type scope | **a, b, c** (e deferred) | a/b/c produce a recovered/linked fact whose truth a synthetic oracle can check. Type e ("violation") has no synthetic ground truth and a detector that fires on any citation — separate track. |
| Calibration | **Per-type, hand-rolled PAV isotonic + Wilson lower bound** | Score semantics differ per type; pooling is invalid. No scikit-learn dependency for two small functions. |
| Calibration confidence | **Computed at surface time** from stored raw score + artifact; cached on candidate rows | Lets calibration be re-fit without re-running scorers or backfilling. |

---

## 1. Core principle

**The harness runs the real scorers, unchanged, against an isolated eval
database.** It never re-implements scoring. It manufactures synthetic documents
with known answers, loads them into a throwaway eval DB, calls
`TypeAScorer.run()` / `TypeCScorer.run()` exactly as production does, and grades
what comes back. Precision numbers therefore describe the actual system.

The scorers already accept an injected `embed_fn` (see `type_a.py` constructor),
so the harness needs no production code change to run with a deterministic
embedding in CI or the real `nomic-embed-text` for a real calibration run. This
matters because Ollama embed is currently broken on the M4 — the harness is
runnable today with the deterministic stub.

---

## 2. Package layout (Section A)

New package `palimpsest/eval/`, mirroring existing module conventions:

```
palimpsest/eval/
  __init__.py
  isolation.py     # build an isolated eval DB + synthetic FAISS index (TASK-12)
  embedding.py     # deterministic embedding stub for synthetic text (TASK-12)
  generators.py    # per-type synthetic case builders (TASK-13 a/b, TASK-14 c)
  oracle.py        # per-type truth grading → label TP/FP/FN/TN (TASK-13, 14)
  runner.py        # generate → eval DB → real scorer → oracle → eval_results (TASK-15)
  stats.py         # PAV isotonic + Wilson lower bound (TASK-16)
  calibrate.py     # per-type fit + threshold selection → calibration.json (TASK-16)
  metrics.py       # precision/recall, PR points, reliability bins, report (TASK-17)
  gate.py          # load artifact, score→confidence→tier (TASK-18)
  cli.py           # `palimpsest-eval generate|run|calibrate|report` (TASK-15+)
```

Data flow:

```
generate(seed) → eval.db (+ synthetic faiss.idx)
             → real scorer.run(conn, cfg)         [unchanged production code]
             → oracle grades each prediction
             → eval_results rows
             → calibrate(per type_key)            → eval/calibration.json
             → gate reads artifact at surface time (server.py)
```

---

## 3. Synthetic case generation (Section B)

Every type produces three case kinds. The negative kinds are the point of a
precision gate.

- **positive** — the true answer exists in the corpus; the scorer should find it.
- **negative_control** — the true answer is held out of the corpus; the scorer
  should surface nothing above threshold. Measures the false-positive rate.
- **hard_negative** — a tempting decoy is present but wrong; the scorer should
  not surface it confidently.

### 3.1 Types a / b (recover a redacted entity / dose) — TASK-13

A case builds two documents from a controlled template:
- **doc_B (source)**: a page containing a known clear entity `E` (kind ∈
  {person, org, location, date, dosage}) with known `norm`, plus chunk text and
  a synthetic embedding (via the deterministic stub).
- **doc_A (redacted)**: a page describing the same event with `E`'s span
  replaced by a redaction marker (`deleted_text` / `black_box`), context_before
  / context_after preserved, with **partial** (not identical) overlap of anchor
  entities so both the anchor and embedding routes must do real work.

Case kinds:
- positive → `E` present in doc_B. Expected: top candidate norm == `E.norm`.
- negative_control → doc_B omitted (or `E` removed). Expected: no candidate ≥
  `score_threshold`.
- hard_negative → a same-kind distractor `D` (different norm) present with
  partial overlap; doc_B with the true `E` omitted. Expected: `D` not surfaced
  above threshold.

Type b is type a restricted to `kind = 'dosage'`; the same generator emits
dosage cases (subject_ref/person near the dose, dosage value as the answer).

**Truth oracle (a/b):** for the redaction under test, read the highest-scoring
`gap_candidates` row. Grade:
- positive: TP if recovered `entities.norm` == masked `E.norm`; FN if no row;
  FP if a row with a different norm.
- negative_control / hard_negative: TN if no row ≥ threshold; FP if any row ≥
  threshold.

### 3.2 Type c (link anonymous subject → named person) — TASK-14

The safety-critical generator. A case constructs one subject page and one or
more named-person pages with **controlled attribute overlap**:
- subject page: `subject_ref` ("Subject 3"), `org = O`, `date = Y`, optional
  `dosage = d`.
- **true-link positive**: named person `P` with `org ≈ O` (edit distance ≤ 2),
  document `year` within ±1 of `Y`, optional `dosage = d`. By construction `P`
  *is* the subject. Expected: link subject→`P`.
- **decoy hard_negative (the crux)**: a different named person `P'` that *also*
  matches `org ≈ O` and `year ± 1` but is a distinct identity. Under the current
  rule (`org·0.5 + date·0.3 + dosage·0.2`, threshold 0.65) both `P` and `P'`
  clear the bar. Only subject→`P` is correct; subject→`P'` is a false
  de-anonymization.
- **answer_absent negative_control**: the subject's true identity is absent, but
  a same-org/same-year decoy `P'` exists. Expected: no link (or low confidence).
  This is the most dangerous real-world case.

**Truth oracle (c):** each case carries the constructed true `named_entity_id`
(or `None` for answer_absent). Read `identity_link_candidates` for the subject.
Grade: TP if the surfaced link's `named_entity_id` == true id; FP for any link to
a different id, or any link on an answer_absent case; FN if true id exists but no
link surfaced; TN if answer_absent and no link surfaced.

**Expected result, stated plainly:** on decoy and answer_absent cases Type c
will link both `P` and `P'`, so measured precision will be low. That is the
harness doing its job — it produces the number that proves Type c must not reach
publication without a stronger detector or a very high confidence bar, and it
still sits behind the identity HITL gate regardless (§7).

---

## 4. Calibration & trust gate (Section C)

### 4.1 Per-type calibration — TASK-16

For each `type_key`, collect `(raw_score, correct ∈ {0,1})` over all graded
cases (TP/FN count toward positives-at-score; FP/TN toward negatives).

- **PAV isotonic** (`stats.fit_isotonic`): pool-adjacent-violators producing a
  monotone non-decreasing step function `g: score → P(correct)`. Hand-rolled,
  ~25 lines, no dependency. Used to report a calibrated confidence per score.
- **Threshold selection** (`calibrate.choose_threshold`): over distinct scores
  descending, for each cutoff `c` compute precision over cases with `score ≥ c`
  as the **Wilson lower bound** at 95%. Pick the *lowest* `c` whose Wilson-LB ≥
  `target_precision` (maximizes recall subject to a precision floor). If no `c`
  qualifies → **gate disabled for that type** (artifact records
  `threshold = null`, reason `"precision_floor_unmet"`).

Using the Wilson **lower** bound (not the point estimate) is the honesty move:
with small N it refuses to certify precision the data can't statistically
support.

Artifact `eval/calibration.json` (versioned):
```json
{
  "schema": 1,
  "created_at": "ISO-8601",
  "scorer_git_sha": "…",
  "corpus_hash": "sha256 of generated case specs",
  "config": {"target_precision": 0.90, "wilson_z": 1.96, "min_cases": 40},
  "types": {
    "type_a": {"threshold": 0.83, "n": 120, "wilson_lb": 0.91,
               "isotonic": [[0.65,0.40],[0.72,0.55],[0.83,0.92],[0.95,0.99]]},
    "type_b": {"threshold": 0.88, "n": 60,  "wilson_lb": 0.90, "isotonic": [...]},
    "type_c": {"threshold": null, "n": 80,  "reason": "precision_floor_unmet",
               "isotonic": [...]}
  }
}
```

### 4.2 The gate — TASK-18

`gate.confidence_and_tier(type_key, raw_score, artifact) -> (confidence, tier)`:
- `confidence` = isotonic `g(raw_score)` (clipped to the fitted range).
- `tier`:
  - `surfaceable` if `threshold` is not null and `raw_score ≥ threshold`;
  - `tentative` otherwise (includes every type whose gate is disabled).

Enforcement at the surfacing boundary (`server.py`):
- `palimpsest_find_redaction_gaps` (types a/b) gains a `min_tier` parameter
  (default `"surfaceable"`). Each row is annotated with `confidence` and
  `gate_tier`; rows below `min_tier` are excluded by default. An explicit
  `min_tier="tentative"` opt-in returns everything, clearly flagged — for
  reviewer triage, never for publication.
- Type c links (surfaced via `review.py links` and any future MCP tool) gain the
  same tier filter, **stacked on top of** the existing identity gate. The trust
  gate can only *remove* a finding from view; it never relaxes the person
  masking or the `deceased_historical`+approved requirement.

Cached columns (`confidence`, `confidence_method`, `gate_tier`) are written
opportunistically when a candidate is surfaced and recomputed whenever
`confidence_method` (the artifact `corpus_hash`) differs from the current
artifact — so a re-fit never serves stale confidences.

---

## 5. Schema & config (Section D) — TASK-11

### 5.1 Schema migration v7 (`palimpsest/db.py`)

```sql
-- eval bookkeeping
CREATE TABLE IF NOT EXISTS eval_runs (
  run_id          INTEGER PRIMARY KEY,
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  scorer_git_sha  TEXT,
  corpus_hash     TEXT,
  seed            INTEGER,
  config_snapshot TEXT,                 -- JSON of [eval] section
  notes           TEXT
);
CREATE TABLE IF NOT EXISTS eval_cases (
  case_id     INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES eval_runs(run_id),
  type_key    TEXT NOT NULL,            -- type_a | type_b | type_c
  case_kind   TEXT NOT NULL,            -- positive | negative_control | hard_negative
  spec        TEXT NOT NULL,            -- JSON: how the case was built
  truth       TEXT NOT NULL             -- JSON: known answer (norm / entity_id / null)
);
CREATE TABLE IF NOT EXISTS eval_results (
  result_id      INTEGER PRIMARY KEY,
  run_id         INTEGER NOT NULL REFERENCES eval_runs(run_id),
  case_id        INTEGER NOT NULL REFERENCES eval_cases(case_id),
  type_key       TEXT NOT NULL,
  raw_score      REAL,                  -- null if nothing surfaced
  score_components TEXT,                -- JSON of persisted component columns
  predicted      TEXT,                  -- JSON: norm / entity_id surfaced (null if none)
  label          TEXT NOT NULL,         -- TP | FP | FN | TN
  confidence     REAL
);

-- cached gate output on production candidate tables
ALTER TABLE gap_candidates           ADD COLUMN confidence REAL;
ALTER TABLE gap_candidates           ADD COLUMN confidence_method TEXT;
ALTER TABLE gap_candidates           ADD COLUMN gate_tier TEXT;
ALTER TABLE identity_link_candidates ADD COLUMN confidence REAL;
ALTER TABLE identity_link_candidates ADD COLUMN confidence_method TEXT;
ALTER TABLE identity_link_candidates ADD COLUMN gate_tier TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (7);
```

`ALTER TABLE … ADD COLUMN` is idempotent-guarded in code (catch
`sqlite3.OperationalError: duplicate column name`) because SQLite has no
`ADD COLUMN IF NOT EXISTS`.

### 5.2 Config `[eval]` section (`config.toml`, `config.py`)

```toml
[eval]
target_precision   = 0.90      # Wilson lower-bound precision floor for surfaceable
wilson_z           = 1.96      # 95% one-sided ~ use 1.96 two-sided per house default
min_cases          = 40        # per type; below this the gate is disabled
default_seed       = 1337      # deterministic case generation
gate_enforcement   = "enforce" # off | annotate | enforce
artifact_path      = "{storage.root}/eval/calibration.json"
eval_db_path       = "{storage.root}/eval/eval.db"
```

`config.py`: add `eval: dict` field to `Config`, append `"eval"` handling in
`load()` (default to `{}` if absent so existing configs still load), and expand
`{storage.root}` in the two paths the same way `db.path` is expanded.

---

## 6. Error handling & honesty guards (Section E)

- **Ollama down:** `runner` accepts an `embed_fn`; `embedding.deterministic_embed`
  (seeded hash → unit vector) lets the whole pipeline run without Ollama. The
  report header states whether the run used the stub or the real model;
  stub-based precision numbers are labeled `PLUMBING-ONLY — NOT VALID PRECISION`.
- **Small N:** if `n < min_cases` for a type, the gate is disabled
  (`threshold = null`) and the report says `insufficient_data`, never a fake
  threshold.
- **Synthetic optimism disclosure:** every report ends with a fixed paragraph
  stating precision is measured on synthetic cases and is an *upper bound* on
  real-world precision; lists the count of real anchor cases included (0 in v1).
- **Isolation:** `runner` asserts `cfg.eval["eval_db_path"] != str(cfg.db_path)`
  before any write and refuses to run otherwise.
- **Determinism:** the RNG seed is recorded in `eval_runs.seed`; same seed ⇒
  identical cases ⇒ comparable runs.

### Testing strategy

- **stats.py** unit: PAV output is monotone non-decreasing; matches hand-worked
  examples; Wilson LB matches known values; `choose_threshold` picks the correct
  cutoff on separable / random / unachievable distributions.
- **generators / oracle** unit: generated rows satisfy schema FKs; truth
  annotations correct; negative_control cases genuinely omit the source; oracle
  labels TP/FP/FN/TN correctly on hand-built predictions.
- **runner** integration (deterministic stub): end-to-end on a tiny case set —
  generate → `TypeAScorer.run` → grade → assert positives recovered, controls
  not surfaced, labels written.
- **gate** unit: separable scores → τ at the gap, all-positives `surfaceable`;
  random scores → gate disabled, all `tentative`; Type c gate never reduces
  masking (assert a `potentially_living` person stays `PERSON-XXXX` regardless
  of tier).
- All new tests live in `tests/test_eval_*.py`; target ≥ the existing bar
  (project is at 150 green).

---

## 7. Iron Rule alignment

Adds **Iron Rule #4 — No finding surfaced for publication below its calibrated
precision bar.** Mechanism: `gate_tier` + the `min_tier` default in `server.py`.
Like Rule #2 (two citations) it is structural, not behavioral. The trust gate is
strictly *additive* to Rule #3 (person masking): for any person/identity output
both gates must pass; the trust gate never weakens masking.

---

## 8. Out of scope (v1)

- **Type e** — no synthetic ground truth; detector fires on any citation. Needs
  a tightened procedure-vs-rule detector and a small hand-labeled set. Tracked
  separately (candidate TASK-19, not in this plan).
- **Types d, f** — absence findings; a different evaluation regime ("is the gap
  real?"). Out of scope.
- **Adversarial redactor model** (strip corroborating context, vary redaction
  density). Natural follow-on once the gate exists; deferred.
- **Real labeled corpus / the 2 Phase-1 findings as anchors** — the schema
  supports it (`eval_cases.spec` can mark `source="real"`), but curation is
  deferred; v1 is synthetic-only with the disclosure in §6.

---

## 9. Build order

| Packet | Builds | Depends on |
|--------|--------|------------|
| TASK-11 | Schema v7 + `[eval]` config | — |
| TASK-12 | Eval DB isolation + deterministic embedding + synthetic index | 11 |
| TASK-13 | Type a/b generator + oracle | 12 |
| TASK-14 | Type c generator + oracle | 12 |
| TASK-15 | Runner + `generate`/`run` CLI | 13, 14 |
| TASK-16 | `stats.py` (PAV + Wilson) + calibrate + `calibrate` CLI | 15 |
| TASK-17 | Metrics + report + `report` CLI | 16 |
| TASK-18 | Gate + `server.py` enforcement | 16 |

Each packet ends in pytest commands with expected output and a commit. Prove the
packet's tests green before moving on (Phase 1 discipline).

---

## 10. Risks accepted

- **Synthetic ≠ real.** Calibrated precision is an upper bound; the disclosure
  (§6) is mandatory in every report. Real-anchor curation is the documented next
  step.
- **Type c may gate to near-zero.** Expected and acceptable — the gate correctly
  withholds weak de-anonymizations; the fix is a stronger detector, tracked
  separately.
- **Small corpus → wide Wilson intervals.** The gate errs toward `tentative`
  when data is thin. Correct failure direction for a precision-first gate.
