- 2026-06-19 [claude] COMPLETED: Dead-job auto-retry in broker (c272a2c on main).
  - broker.py: revive_dead_jobs() runs in reaper loop every 60s, revives jobs dead >30min
  - New POST /jobs/revive endpoint for immediate bulk reset (filter by type optional)
  - enqueue dead→pending now resets attempts=0 so jobs get a clean slate
  - config.toml: dead_retry_minutes = 30 (update manually on gonktop)
  - Revived 478+36+1 dead jobs immediately; 0 dead across all types now
  - Gonktop broker restarted with new code

- 2026-06-19 [agy] COMPLETED: Code Review Fixes: Correlation Features & Orchestrator Bugs (fix/code-review-correlation-bugs).
  - Fix 1: Orchestrator `investigate()` truncation — push doc_id filtering into scorer SQL
  - Fix 2: Null bounding boxes on `outcome_ref` entities — use `line_offsets` lookup
  - Fix 3: `normalize_person()` suffix handling — handle 3+ comma parts
  - Fix 4: Document multi-line entity bbox behavior
  - 218 tests passing (1 pre-existing failure in test_eval_type_d.py excluded)

- 2026-06-19: Started TASK-20 Phase 2 Scaling & Safety.
  - Pre-flight: Ran pytest suite, noted pre-existing failure in tests/test_eval_type_d.py.
  - Starting Part A: Heuristic Auto-Approver Alignment.

- 2026-06-19: Started TASK-11 Phase 2 Scaling & Safety - Eval Schema v7 and Config.
