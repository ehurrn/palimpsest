## 2026-06-13T09:35:53Z
You are a worker agent assigned to implement Task 6 of the Phase 3 Scorer Refactoring plan.
Your task is to:
1. Update `/Users/herren/dev/palimpsest/WORK-LOG.md` under the current date (2026-06-13) to note that "Phase 3 Task 6 (Type a Scorer Extraction & Test Migration) has started."
2. Write `palimpsest/scorers/type_a.py` with `get_ollama_embedding()`, `get_slot_expectation()`, and `TypeAScorer` (Step 6.1 of docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-02.md).
3. Modify `palimpsest/indexer.py` (Step 6.2) to import and re-export `get_ollama_embedding` and `get_slot_expectation` from `palimpsest.scorers.type_a`.
4. Modify `palimpsest/scorers/__init__.py` (Step 6.3) to import and include `TypeAScorer` in the `SCORERS` registry.
5. Create `tests/test_scorer_type_a.py` (Step 6.4) with the migrated tests.
6. Run the new tests: `python -m pytest tests/test_scorer_type_a.py -v`
7. Run the existing gapjoin and dosage tests: `python -m pytest tests/test_gapjoin.py tests/test_dosage.py -v`
8. Verify they pass 100%. If any tests fail, debug and resolve them.
9. Update `/Users/herren/dev/palimpsest/WORK-LOG.md` to note that "Phase 3 Task 6 (Type a Scorer Extraction & Test Migration) is complete."
10. Write your handoff report to `.agents/worker_phase3_r6/handoff.md` and send a message back with your verification results.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
