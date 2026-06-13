# TASK-19 — Phase-4 verification run

**Depends on:** TASK-11 … TASK-18 (all committed, tests green).
**Builds:** nothing new. This is the acceptance gate for the Evaluation & Trust
Gate — a checklist of commands with expected output that proves the stack works
end to end. Mirrors `specs/TASK-10-verification-run.md`.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` (§0 decisions, §7 Iron Rule #4).

## Procedure (run in order; every box must pass)

- [ ] **Full unit suite green.**
  Run: `uv run pytest -q`
  Expected: all pass — the prior baseline (≈150) plus the new
  `tests/test_eval_*.py`. No failures, no errors.

- [ ] **Lint clean.**
  Run: `uv run ruff check palimpsest tests`
  Expected: `All checks passed!`

- [ ] **Types clean (if `ty` is configured for the project).**
  Run: `uv run ty check palimpsest` (skip if the project doesn't use `ty`)
  Expected: no errors on new `palimpsest/eval/*` code.

- [ ] **Migration reaches v7.**
  Run: `uv run python -m palimpsest.db migrate`
  Expected: output lists `eval_runs, eval_cases, eval_results` among tables.
  Verify: `sqlite3 "$STORAGE/db/palimpsest.db" "SELECT MAX(version) FROM schema_version;"` → `7`.

- [ ] **Stub end-to-end (no Ollama required).**
  Run:
  ```
  uv run palimpsest-eval run --n-per-kind 30
  uv run palimpsest-eval calibrate
  uv run palimpsest-eval report
  ```
  Expected: `run` prints per-type label counts; `calibrate` prints per-type
  thresholds; `report` writes `reports/eval-report-<run>.md`. The report contains
  the phrase `upper bound` (mandatory validity disclosure) and `PLUMBING-ONLY`
  (stub embedding flag).

- [ ] **Safety signal — Type c gates low.**
  Inspect the calibration output / `calibration.json`. Expected: `type_c`
  `threshold` is `null` (`precision_floor_unmet`) or markedly higher than
  `type_a`'s, because the decoy and answer-absent cases inject false positives.
  This is the intended result: the gate withholds most identity links. If
  `type_c` shows high precision under the stub, something is wrong with the
  decoy/answer-absent generation (revisit TASK-14) — investigate, do not ship.

- [ ] **Gate enforces at the boundary.**
  With `calibration.json` present and `gate_enforcement = "enforce"`:
  `palimpsest_find_redaction_gaps()` returns only `surfaceable` rows by default;
  each row carries `confidence` and `gate_tier`; calling with
  `min_tier="tentative"` returns all rows, annotated. Verify via the server test
  suite: `uv run pytest tests/test_server.py -q` (extend it if it doesn't yet
  cover the tier filter).

- [ ] **Identity invariant holds (Iron Rule #3 unbroken).**
  Confirm a `person` entity that is not `deceased_historical` + approved is still
  masked to `PERSON-XXXX` regardless of `gate_tier`. The gate runs after masking
  and can only drop/annotate. (Covered by `tests/test_eval_gate.py`
  `test_apply_gate_enforce_drops_tentative` asserting masked text is untouched;
  spot-check in `test_server.py`.)

- [ ] **REAL calibration — BLOCKED, do not skip silently.**
  Producing valid (non-stub) numbers requires Ollama `nomic-embed-text` on a
  worker node. Ollama is down on the M4 (see `HUMAN_DO_THIS.md`). When restored:
  ```
  uv run palimpsest-eval run --real-embed --n-per-kind 50
  uv run palimpsest-eval calibrate
  uv run palimpsest-eval report
  ```
  Archive the resulting `calibration.json` and report under `reports/`. Until
  then, the gate runs on stub thresholds = PLUMBING-ONLY; do not surface real
  findings against them.

- [ ] **Log + commit.**
  Append a completion entry to `WORK-LOG.md`. Commit any test additions made
  during verification:
  ```bash
  git add tests/ WORK-LOG.md
  git commit -m "test(eval): Phase-4 verification run — stack green end to end"
  ```

## Definition of done (Phase 4)
All boxes above pass; TASK-11…18 committed; full suite green; `palimpsest-eval
run|calibrate|report` works on the stub; the trust gate enforces surfaceable-only
by default; REAL calibration is the only remaining item and is tracked in
`HUMAN_DO_THIS.md` pending Ollama.

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Any blocker →
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, surface it.
