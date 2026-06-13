## 2026-06-13T09:29:20Z

You are a worker agent executing Task 3 (R3) of Phase 3 (Type f Scorer - Series Suppression Gap).
Your working directory is: `/Users/herren/dev/palimpsest/.agents/worker_phase3_r3`
Your identity: `worker_phase3_r3`

Instructions:
1. First, write a starting entry to `/Users/herren/dev/palimpsest/WORK-LOG.md` indicating that Phase 3 Task 3 (Type f Scorer - Series Suppression Gap) has started.
2. Implement R3 as per /Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-01.md:
   - Create `palimpsest/scorers/type_f.py` with `TypeFScorer` implementation.
   - Update `palimpsest/scorers/__init__.py` to import and register `TypeFScorer` in `SCORERS`.
   - Write tests in `tests/test_scorer_type_f.py` migrating the existing tests from `tests/test_series.py`.
3. Run the tests using pytest to verify they pass successfully.
4. Update `/Users/herren/dev/palimpsest/WORK-LOG.md` indicating Phase 3 Task 3 is complete.
5. Write your handoff/completion report to `/Users/herren/dev/palimpsest/.agents/worker_phase3_r3/handoff.md` with observed outputs, test results, verification details, and changes made.
6. Send a message back to the parent orchestrator with the status and the absolute path to your handoff.md.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
