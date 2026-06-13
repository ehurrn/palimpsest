# Original User Request

## Initial Request — 2026-06-13T04:31:51-05:00

You are the Project Orchestrator. Your mission is to implement Phase 3 (Scorer Registry & Lane A Orchestrator — Parts 1 and 2) in the Palimpsest pipeline, extracting all six scorers (Types a through f) into the `palimpsest/scorers` package.

Your working directory is: `/Users/herren/dev/palimpsest/.agents/orchestrator_phase3_gen4`
Your identity: `orchestrator`

Requirements to implement (see ORIGINAL_REQUEST.md):

## Phase 3 Part 1 Requirements

### R1. Scorer Registry Base and Protocol
1. Create `palimpsest/scorers/base.py` with `Candidate` dataclass and `Scorer` protocol interface.
2. Create skeleton `palimpsest/scorers/__init__.py` exposing the registry `SCORERS` dict.
3. Write base tests in `tests/test_scorers_base.py`.

### R2. Type e Scorer (Regulatory Violation)
1. Extract `run_violation_join` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_e.py` as `TypeEScorer` implementing the `Scorer` protocol.
2. Add `TypeEScorer` to the `SCORERS` registry in `palimpsest/scorers/__init__.py`.
3. Add unit tests in `tests/test_scorer_type_e.py` migrating existing tests from `tests/test_violation.py`.

### R3. Type f Scorer (Series Suppression Gap)
1. Extract `run_series_join` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_f.py` as `TypeFScorer` implementing the `Scorer` protocol.
2. Add `TypeFScorer` to the `SCORERS` registry in `palimpsest/scorers/__init__.py`.
3. Add unit tests in `tests/test_scorer_type_f.py` migrating existing tests from `tests/test_series.py`.

### R4. Type d Scorer (Outcome Suppression Gap)
1. Extract `run_outcome_gap` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_d.py` as `TypeDScorer` implementing the `Scorer` protocol.
2. Add `TypeDScorer` to the `SCORERS` registry in `palimpsest/scorers/__init__.py`.
3. Add unit tests in `tests/test_scorer_type_d.py` migrating existing tests from `tests/test_outcome.py`.

## Phase 3 Part 2 Requirements

### R5. Type c Scorer (Anonymous Identity Linkage)
1. Extract `_edit_distance` and `run_identity_link` from `palimpsest/indexer.py` into `palimpsest/scorers/type_c.py` as `TypeCScorer`.
2. Update `palimpsest/indexer.py` to import `_edit_distance` from `palimpsest/scorers/type_c`.
3. Add unit tests in `tests/test_scorer_type_c.py` migrating existing tests from `tests/test_identity.py`.

### R6. Type a Scorer (Redacted-Text Corroboration)
1. Extract `run_gapjoin` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_a.py` as `TypeAScorer`.
2. Update `palimpsest/indexer.py` to import `get_ollama_embedding` and `get_slot_expectation` from `palimpsest/scorers/type_a`.
3. Add unit tests in `tests/test_scorer_type_a.py` migrating existing tests from `tests/test_gapjoin.py`.

### R7. Type b Scorer (Undisclosed Radiation Dosage)
1. Create `TypeBScorer` in `palimpsest/scorers/type_b.py` delegating to `TypeAScorer` and filtering results to dosage-kind.
2. Ensure all six scorers are registered in `palimpsest/scorers/__init__.py`.
3. Add unit tests in `tests/test_scorer_type_b.py` migrating existing tests from `tests/test_dosage.py`.

## Verification Resources
* Part 1 plan: `/Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-01.md`
* Part 2 plan: `/Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-02.md`
* Design spec: `/Users/herren/dev/palimpsest/docs/superpowers/specs/2026-06-13-scorer-registry-orchestrator-design.md`

Please make sure to write entries to WORK-LOG.md when starting/completing tasks and to ensure that all tests run and pass 100%. Write a final handoff/completion report to `.agents/orchestrator_phase3_gen4/handoff.md` when done.
