## 2026-06-13T09:38:31Z

You are a worker agent assigned to implement Task 8 of the Phase 3 Scorer Refactoring plan.
Your task is to:
1. Update `/Users/herren/dev/palimpsest/WORK-LOG.md` under the current date (2026-06-13) to note that "Phase 3 Task 8 (Thin indexer.py to CLI shim) has started."
2. Modify `palimpsest/indexer.py` to:
   - Replace the bodies of `run_gapjoin()`, `run_violation_join()`, `run_series_join()`, `run_outcome_gap()`, and `run_identity_link()` with thin delegate calls to their respective scorers in the `SCORERS` registry or direct instantiations (Steps 8.2–8.6 of docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-03.md).
   - Clean up the imports at the top of the file, removing unused imports (Step 8.7).
3. Verify the line count of `palimpsest/indexer.py` is between 250 and 350 lines (using wc -l).
4. Run the full test suite: `python -m pytest tests/ -q`
5. Verify that all tests pass 100%. If any test fails, debug and fix it.
6. Update `/Users/herren/dev/palimpsest/WORK-LOG.md` to note that "Phase 3 Task 8 (Thin indexer.py to CLI shim) is complete."
7. Write your handoff report to `.agents/worker_phase3_r8/handoff.md` and send a message back with your verification results.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
