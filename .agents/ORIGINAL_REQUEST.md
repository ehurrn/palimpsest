# Original User Request

## Initial Request — 2026-06-12T22:23:27-05:00

The project is to review the Palimpsest Phase 2 build plan, reinstate the identity safety gates, repair the M4 Ollama and worker node models, check OCR coverage across the cluster, and write the specifications for the six finding-types.

Working directory: /Users/herren/dev/palimpsest
Integrity mode: benchmark

## Requirements

### R1. Re-establish Identity Safety Gates
Revert all bulk-approved person entities and bulk-verified gap candidates from the Phase 1 database on gonktop. Design and implement a birth-year and document-date heuristic: if the document date is older than 75 years, or if the document date minus the subject's birth year exceeds 100 years, the person entity may be classified as `deceased_historical`; otherwise, they must remain flagged as `potentially_living` and masked per the identity gate.

### R2. Repair M4 Worker and OCR Infrastructure
Reinstall/repair the local Ollama installation on the M4 host to resolve the missing `llama-server` binary and the 500 embedding query errors. Verify that Tesseract is correctly installed on gonktop and all worker nodes to prevent dead OCR jobs. Re-enable the `embed` capability for the `m4` node in `config.toml` once local embedding is functional.

### R3. Finding-Types Specification
Write `specs/FINDING-TYPES.md` defining all six finding-types. For each of the six types, specify:
1. One detector (how the patterns are flagged in the `features` stage).
2. One corroboration rule (what counts as a clear-text match in another document).
3. The linear scoring formula and weights.

## Acceptance Criteria

### Safety Gates and Database Invariants
- [ ] Database contains no bulk-approved person entities or gap candidates that bypass individual checks.
- [ ] Person entities are only marked `deceased_historical` if they satisfy the birth-year/document-date safety heuristic.
- [ ] All other person entities default to `potentially_living` or `unknown` and are properly masked in MCP and review tool outputs.

### Node and Cluster Health
- [ ] Local M4 Ollama responds successfully to embedding queries with latency under 3 seconds.
- [ ] The `embed` capability is active for the `m4` node in `config.toml`.
- [ ] `python -m palimpsest.preflight` runs and passes 100% of its checks on both the local M4 machine and `gonktop`.

### Specifications
- [ ] The file `specs/FINDING-TYPES.md` exists and contains detailed sections for all six finding-types with specific detectors, corroboration rules, and scoring formulas.

## Follow-up — 2026-06-13T09:25:44Z

Implement Phase 3 (Scorer Registry & Lane A Orchestrator — Part 1) in the Palimpsest pipeline, extracting scorers for Types d, e, and f into a new `palimpsest/scorers` package per the Part 1 plan.

Working directory: /Users/herren/dev/palimpsest
Integrity mode: development

## Requirements

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

## Verification Resources
* Full implementation plan: [2026-06-13-scorer-registry-orchestrator-01.md](file:///Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-01.md)
* Design spec: [2026-06-13-scorer-registry-orchestrator-design.md](file:///Users/herren/dev/palimpsest/docs/superpowers/specs/2026-06-13-scorer-registry-orchestrator-design.md)

## Acceptance Criteria

### Test Suite Execution
- [ ] `./venv/bin/pytest tests/test_scorers_base.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_e.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_f.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_d.py` passes 100%
- [ ] Existing tests in `tests/test_violation.py`, `tests/test_series.py`, and `tests/test_outcome.py` continue to pass 100%

## Follow-up — 2026-06-13T09:30:35Z

Phase 3 Part 2 plan is now available at docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-02.md.

Please append these tasks to your briefing and instruct the Project Orchestrator to execute them sequentially immediately after Part 1 tasks are complete and verified:

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

## Phase 3 Part 2 Acceptance Criteria
- [ ] `./venv/bin/pytest tests/test_scorer_type_c.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_a.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_b.py` passes 100%
- [ ] Registry validation passes (all six scorers successfully registered).
- [ ] Full test suite (101+ tests) passes 100%.

## Follow-up — 2026-06-13T09:33:11Z

Implement Phase 3 (Scorer Registry & Lane A Orchestrator — Part 1 & Part 2) in the Palimpsest pipeline, extracting scorers for all six finding types into the new `palimpsest/scorers` package.

Working directory: /Users/herren/dev/palimpsest
Integrity mode: development

## Requirements

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

### R5. Type c Scorer (Anonymous Identity Linkage)
1. Extract `_edit_distance` and `run_identity_link` from `palimpsest/indexer.py` into `palimpsest/scorers/type_c.py` as `TypeCScorer`.
2. Update `palimpsest/indexer.py` to import `_edit_distance` from `palimpsest/scorers/type_c`.
3. Add unit tests in `tests/test_scorer_type_c.py` migrating existing tests from `tests/test_identity.py`.

### R6. Type a Scorer (Redacted-Text Corroboration)
1. Extract `run_gapjoin` logic from `palimpsest/indexer.py` into `palimpsest/scorers/type_a.py` as `TypeAScorer`.
2. Update `palimpsest/indexer.py` to import `get_ollama_embedding` and `get_slot_expectation` from `palimpsest/scorers/type_a`.
3. Add unit tests in `tests/test_scorer_type_a.py` migrating existing tests from `tests/test_gapjoin.py`.

### R7. Type b Scorer (Undisclosed Radiation Dosage)
1. Create `TypeBScorer` in `palimpsest/scorers/type_b.py` delegating to `TypeAScorer` and filtering results.
2. Ensure all six scorers are registered in `palimpsest/scorers/__init__.py`.
3. Add unit tests in `tests/test_scorer_type_b.py` migrating existing tests from `tests/test_dosage.py`.

## Verification Resources
* Full implementation plans: 
  * [2026-06-13-scorer-registry-orchestrator-01.md](file:///Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-01.md)
  * [2026-06-13-scorer-registry-orchestrator-02.md](file:///Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-02.md)
* Design spec: [2026-06-13-scorer-registry-orchestrator-design.md](file:///Users/herren/dev/palimpsest/docs/superpowers/specs/2026-06-13-scorer-registry-orchestrator-design.md)

## Acceptance Criteria

### Test Suite Execution
- [ ] `./venv/bin/pytest tests/test_scorers_base.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_e.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_f.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_d.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_c.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_a.py` passes 100%
- [ ] `./venv/bin/pytest tests/test_scorer_type_b.py` passes 100%
- [ ] Scorer registry is verified successfully.
- [ ] All unit tests pass 100%.

## Follow-up — 2026-06-13T09:34:14Z

The user has provided the plan for Part 3 of Phase 3 in `docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-03.md`.
Please expand the scope of work for the Project Orchestrator subagent to also include Part 3 (Tasks 8, 9, and 10), which covers:
- Thinning `palimpsest/indexer.py` down to a pure CLI shim.
- Adding the `orchestrator` section to `palimpsest/config.py` and `config.toml`.
- Adding CLI entry points in `pyproject.toml`.
- Running the full regression checkpoint.

Please coordinate this with your orchestrator subagent (or spawn a new one/update it) and update the briefing/handoff/progress tracking accordingly.

## Follow-up — 2026-06-13T09:35:54Z

Part 4 of the Phase 3 plan is now available at `docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-04.md`.

Please expand your scope to include Part 4 tasks:

- **Task 11**: Create `palimpsest/orchestrator.py` (heartbeat daemon + investigate command). Full code is in the plan.
- **Task 12**: Write `tests/test_orchestrator.py` with ~10 tests covering heartbeat_cycle, investigate, and _worker_alive. Full test code is in the plan.
- **Task 13**: Create `deploy/palimpsest.orchestrator.plist` (launchd plist for daemon). Create `logs/` directory with `.gitkeep`. Do NOT install the plist — document it only.
- **Task 14**: Update WORK-LOG.md and final commit, then tag `lane-a-orchestrator-complete`.
- **Acceptance checklist** (A through F): run all checks after Task 14.

Update the Orchestrator Gen 6 briefing, ORIGINAL_REQUEST.md, and progress.md to include these tasks. Then pass the expanded scope to the active worker subagent (or queue it for after Parts 2 and 3 are done).




