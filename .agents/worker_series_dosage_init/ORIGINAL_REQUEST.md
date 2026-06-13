## 2026-06-13T03:44:48Z
You are a worker subagent (teamwork_preview_worker). Your working directory is `/Users/herren/dev/palimpsest/.agents/worker_series_dosage_init`.

Please perform the following tasks:
1. Update `/Users/herren/dev/palimpsest/WORK-LOG.md` by appending a new log entry noting that the Phase 2 Type f and Type b implementation has started.
2. Run pytest on the current test suite (e.g. `pytest`) to verify all existing 67 unit tests pass.
3. Investigate the codebase (`palimpsest/tasks/features.py`, `palimpsest/indexer.py`, `palimpsest/db.py`) to understand where and how regex-based entities (`seq_ref`, `subject_ref`) should be added, normalized, and how the `seriesjoin` subcommand and the Type b gapjoin proximity scoring logic should be structured.
4. Write a detailed strategy and verification plan in `.agents/worker_series_dosage_init/handoff.md` including any proposed schema additions, python implementation changes, and test design for `tests/test_series.py` and `tests/test_dosage.py`.
5. Run the existing tests again to verify everything is clean, and reply with the absolute path to your handoff.md file and a summary.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
