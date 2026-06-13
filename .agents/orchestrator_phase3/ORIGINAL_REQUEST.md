# Original User Request

## Initial Request — 2026-06-13T04:26:14-05:00

You are the Project Orchestrator. Your mission is to implement Phase 3 (Scorer Registry & Lane A Orchestrator — Part 1) in the Palimpsest pipeline, extracting scorers for Types d, e, and f into a new `palimpsest/scorers` package.

Your working directory is: `/Users/herren/dev/palimpsest/.agents/orchestrator_phase3`
Your identity: `orchestrator`

Requirements to implement (see ORIGINAL_REQUEST.md and /Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-01.md):
1. R1. Scorer Registry Base and Protocol: Create `palimpsest/scorers/base.py` with `Candidate` dataclass and `Scorer` protocol interface. Create `palimpsest/scorers/__init__.py`. Write base tests in `tests/test_scorers_base.py`.
2. R2. Type e Scorer (Regulatory Violation): Extract `run_violation_join` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_e.py` as `TypeEScorer`. Add unit tests in `tests/test_scorer_type_e.py` migrating existing tests from `tests/test_violation.py`.
3. R3. Type f Scorer (Series Suppression Gap): Extract `run_series_join` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_f.py` as `TypeFScorer`. Add unit tests in `tests/test_scorer_type_f.py` migrating existing tests from `tests/test_series.py`.
4. R4. Type d Scorer (Outcome Suppression Gap): Extract `run_outcome_gap` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_d.py` as `TypeDScorer`. Add unit tests in `tests/test_scorer_type_d.py` migrating existing tests from `tests/test_outcome.py`.

Please make sure to write entries to WORK-LOG.md when starting/completing tasks and to ensure that all tests run and pass 100%. Write a final handoff/completion report to `.agents/orchestrator_phase3/handoff.md` when done.
