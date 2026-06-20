## WORK-LOG
- 2026-06-19 [agy] COMPLETED: Named-entity extraction task for OpenNet docs (907534, 907535).
- 2026-06-19 [agy] STARTED: Named-entity extraction task for OpenNet docs (907534, 907535).
- Started Task-20 Scaling & Safety Implementation: 2026-06-19
- Started verifying tests/test_eval_gate.py: 2026-06-19
- Phase 3 Orchestrator Integration & Acceptance Validation has started. 2026-06-19
- 2026-06-19 [agy] STARTED: Named-entity extraction task for OpenNet docs (16366201-16366210).

- 2026-06-19 [agy] COMPLETED: Code Review Fixes: Correlation Features & Orchestrator Bugs (fix/code-review-correlation-bugs).
  - Fix 1: Orchestrator `investigate()` truncation — push doc_id filtering into scorer SQL
  - Fix 2: Null bounding boxes on `outcome_ref` entities — use `line_offsets` lookup
  - Fix 3: `normalize_person()` suffix handling — handle 3+ comma parts
  - Fix 4: Document multi-line entity bbox behavior
  - Fix 5 (Reviewer feedback): Handled empty list filter (`doc_ids=[]`) safely in all Scorers.
  - Fix 6 (Reviewer feedback): Chunked `doc_ids` to mitigate SQLite maximum variable limits.
  - Fix 7 (Reviewer feedback): Replaced linear bbox scanning with binary search and spatial union.
  - 212 tests passing (1 pre-existing failure in test_eval_type_d.py excluded): 2026-06-19
- 2026-06-19 04:29 [claude] MONITORING CHECK: Features pipeline unblocked after two cascading bugs fixed:
  1. Cascade-delete used wrong FK join for child tables (gap_candidates/gapjoin_runs/review_queue have no doc_id column) — fixed in e0fbb51
  2. Watchdog remote_age used OS-specific stat which failed silently in nohup, causing M5 to be constantly restarted — fixed with python3 mtime in c458ac1
  - Features: 5297 done (+204 since baseline=5093), 84 leased, 3104 pending, 0 dead
  - OCR: 8485 done (+135), 9 leased, 3189 pending
  - All 3 workers (m4, gonktop, m5) active and healthy
- 2026-06-19 09:23 [claude] MONITORING: OCR 10066 done (1612 pending, ~5 hrs remain at 5.3/min). Features still queued behind OCR (job_id ordering). 4 PyMuPDF-corrupt OCR jobs revived. M4 zombie process SIGKILLed at ~06:28 (had broken socket, 75min CPU, was preventing M4 from doing useful work). All workers healthy.
- 2026-06-19 15:09 [claude] MONITORING: OCR COMPLETE (11680 done, 0 pending). Features 6011 done at 14.5/min (5669 pending). Embed 2952 done at 24.4/min (2984 pending). M4 rebooted at some point overnight — watchdog+worker restarted 15:09. 4 permanently broken OCR docs (PyMuPDF corruption) left dead. Priority-starvation issue in HUMAN_DO_THIS is now moot.
- 2026-06-19 17:08 [claude] MONITORING: M5 zombie detected (heartbeat-only since ~16:19 with no job leases — same pattern as M4 zombie at 06:28). Kill -9 ran at ~16:58 but new process also failed to log. Watchdog detected STALE at 17:03 and restarted M5 cleanly. Killed duplicate PID 8484. M5 now active on embed. Dead jobs: 0. Status: Features 8790 done (+2779 vs 15:09), 2893 pending ~4-7 hrs. Embed 4796 done (41% fully indexed), 3915 pending ~90-100 min. Workers: M4=features, M5+gonktop=embed (both using Ollama).
- 2026-06-19 17:30 [claude] CATALOG ANALYSIS + IMPROVEMENTS: Discovered catalog only 28% complete (111,500 of 401,996 NV* docs). PDF detection correct; low PDF rate (10.5%) is accurate for the RELV-sorted top bucket. Remaining 290K docs have ~5-23% PDF rate — estimated 15,000-25,000 more PDFs to find. Fixed harvester: (1) sort-by=accessionNo for deterministic pagination, (2) catalog_rate_limit_rps=2.0 separate from PDF rate, (3) --start override flag. Started catalog from 0 on gonktop at 2 RPS (~35 min to complete). Started 1 Gemini features worker (PID 7429) — features rate jumped from ~7/min to ~94/min with 21 concurrent leases. Features now: 9733 done, 1932 pending (~21 min to completion). watchdog updated GEMINI_WORKERS=1.
- 2026-06-20 00:20 [claude] DASHBOARD: Added CORS middleware + /disk + /harvest/stats endpoints to broker. Created monitor.html (auto-refreshes every 10s) showing pipeline queues, throughput, worker heartbeats, harvest corpus totals, disk usage, and recent dead jobs. NOTE: 476 dead embed jobs (Ollama Connection refused on m5) — reviving.
