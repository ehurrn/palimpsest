## 2026-06-13T09:26:38Z
You are a worker agent executing Task 1 (R1) of Phase 3 (Scorer Registry Base and Protocol).
Your working directory is: `/Users/herren/dev/palimpsest/.agents/worker_phase3_r1`
Your identity: `worker_phase3_r1`

Instructions:
1. First, write a starting entry to `/Users/herren/dev/palimpsest/WORK-LOG.md` indicating that Phase 3 Task 1 (Scorer Registry Base & Protocol) has started.
2. Implement R1 as per /Users/herren/dev/palimpsest/docs/superpowers/plans/2026-06-13-scorer-registry-orchestrator-01.md:
   - Create `palimpsest/scorers/base.py` with the `Candidate` dataclass and the `Scorer` protocol interface.
   - Create `palimpsest/scorers/__init__.py`.
   - Write tests in `tests/test_scorers_base.py`.
3. Run the tests using pytest. Make sure they pass.
4. Update `/Users/herren/dev/palimpsest/WORK-LOG.md` indicating Phase 3 Task 1 is complete.
5. Write your handoff/completion report to `/Users/herren/dev/palimpsest/.agents/worker_phase3_r1/handoff.md` with observed outputs, test results, verification details, and changes made.
6. Send a message back to the parent orchestrator with the status and the absolute path to your handoff.md.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
