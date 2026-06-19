# Work Log

## 2026-06-18
- Starting TASK-20 Part B: Decade-sharded FAISS index. Files in scope: indexer.py (build_index, run_gapjoin), results.py (process_embed, process_features), tasks/embed.py (embed_task), config.toml ([embed]).
- Starting TASK-20 Part C: Worker graceful SIGTERM release. Files: broker.py (POST /release), worker.py (_current_job_id globals + signal handler + job tracking).
- Completed TASK-20 Part C: broker POST /release validates ownership, resets state to pending, no attempt increment. Worker _current_job_id/_current_worker_id globals track active job; signal_handler calls /release on SIGTERM when job active, clears globals; job processing block sets/clears globals around handler execution. 4 new tests in test_broker.py, 3 new tests in test_worker_release.py; all 207 tests green.
- Completed TASK-20 Part B: FAISS index now sharded by decade. Added _build_shard helper; build_index scans shards/DECADE/ dirs then falls back to legacy faiss.idx. run_gapjoin discovers all shards at startup, merges search results globally, reconstructs vectors shard-by-shard. process_embed routes to shards/DECADE/ when year present, falls back to flat index/. process_features enqueues embed job with year in payload JSON. embed_task reads year from payload, returns it in result. config.toml [embed] shard_by = "decade". 5 new tests in tests/test_indexer_sharding.py; all 200 tests green.

## 2026-06-13
- Starting task: Addressing code review on orchestrator.py (NameError), scorers/base.py + concrete scorers (Scorer protocol + missing candidates_table/top), indexer.py (undefined print_stats), harvester.py (hardcoded HUMAN_DO_THIS path), server.py (N+1 review_queue lookups in masking).
- Completed code review fixes: (1) orchestrator `_check_candidate_counts` now takes `config: Config`; also fixed adjacent bug where SCORERS classes were used without instantiation (`SCORERS[k]()`), which broke `investigate`'s `scorer.top()`. (2) Added `candidates_table: str` + `top()` to Scorer protocol; added missing `candidates_table` to type_c/d/f and missing `top()` to type_c; typed registry as `dict[str, type[Scorer]]`. (3) Implemented `print_stats()` in indexer.py (pipeline/index/gapjoin/per-scorer metrics). (4) harvester writes HUMAN_DO_THIS.md to repo root via `Path(__file__).resolve().parent.parent` (not the hardcoded absolute path). (5) server.py masking now loads approved entity_ids once per tool call (`load_approved_person_ids`) and threads the set through all mask helpers + all 4 tools, eliminating the N+1. 150 tests green; ruff/ty clean on new code; `index stats` and `orchestrate investigate` smoke-tested.
- Completed task: Created AGENTS.md instructing Jules to work persistently, log blockers to HUMAN_DO_THIS.md, and continue unbothered.
- Phase 3 COMPLETE. indexer.py thinned (1071 → 584 lines): run_violation/series/outcome/identity_link replaced with scorer delegates. pyproject.toml [project.scripts] entry points added for all 4 CLI tools. 150 tests green. Committed 367c830.
- Phase 3 scorer registry complete (solo — agy). Created palimpsest/scorers/ package: base.py (Candidate, Scorer), __init__.py (SCORERS registry), type_c/d/e/f.py extracted from indexer.py. Added type_key + top() to all scorers. Added Config.orchestrator field. Fixed test_ocr.py fixture. Committed orchestrator.py from prior agent. 150 tests green. Pushed to main.
- Phase 3 implementation resumed (solo — agy). Mesh agents previously wrote type_a.py, type_b.py, and six scorer test files but left base.py, __init__.py, type_c.py, type_d.py, type_e.py, type_f.py missing. Config orchestrator field, pyproject entry points, and orchestrator.py also remain incomplete. Starting now with base.py + __init__.py to unblock failing tests.
- Completed implementation of code review improvements: optimized run_identity_link database queries (O(S * N) to single query) and unified outcome_ref entity format to use standard 'bbox' representation. All tests verified green.
- Completed Type c (anonymous identity linkage): schema v6 `identity_link_candidates` table, `_edit_distance()` helper, `run_identity_link()` scorer (org+date+dosage weighted formula), `identitylink` CLI subcommand, `review links` HITL gate (enforces deceased_historical approval before any name surface), bumped preflight EXPECTED_VERSION to 6. 10 new tests in `tests/test_identity.py`; full suite 101 passing. Pushed to origin/main. All six Phase 2 finding-types now implemented.
- Completed OFFLINE-INSTRUCTIONS.md rewrite: updated to reflect Phase 2 state (Types b/d/e/f complete, schema v5, 91 tests), full Type c implementation spec with DB schema, scorer pseudocode, review gate, and test requirements. This is the handoff document for offline local-model sessions.
- Completed Type d (outcome suppression gap): merged feature/type-d to main. Fixed pre-existing test_gapjoin_algorithm score expectation (0.899 proximity bonus). Resolved merge conflict between outcome_ref (Type d) and seq_ref/subject_ref (agy Type f/b uncommitted work). Committed agy's Phase 2 changes. Full suite: 91 tests passing. Pushed to origin/main. Next: Type c (identity linkage).

## 2026-06-12
- Starting Type d (outcome suppression gap): outcome_ref entity, absence scorer, outcomegap CLI. (subagent feature-type-d worker started implementation in worktree feature/type-d)
- Completed Type d (outcome suppression gap): outcome_ref entity kind (outcome_ind:/future_ref: normalization), schema v5 outcome_gap_candidates table, run_outcome_gap() scorer in indexer.py, outcomegap CLI subcommand, preflight EXPECTED_VERSION bumped to 5. 11 new tests in tests/test_outcome.py; full suite 84 passing (1 pre-existing failure in test_gapjoin.py unrelated to Type d). Committed to branch feature/type-d.
- Launched `teamwork_preview` subagent team (`9376f268-0ad1-4280-9b16-b43c25a57e12`) to implement Phase 2 Type f (series suppression) and Type b (undisclosed dosage) finding-types.
- Phase 2 begun. Reverted AGY_BULK bulk approvals: 5,291 review_queue rows → pending, 1,320 gap_candidates → candidate. Identity gate restored (all entities remain living_status=unknown). Wrote `specs/FINDING-TYPES.md` with six finding-types (a-f), detectors, corroboration rules, and build order (e → f → b → d → c).
- Completed Heuristic Gate & Heuristic sub-command: implemented birth-year/document-date safety heuristic, added `heuristic` sub-command to `review.py`, and verified with unit test (`test_heuristic_classification`).
- Completed Type e (regulatory-violation citation): implemented `reg_cite` entity kind, regulation table schema v3 migration, `violationjoin` command in `indexer.py` (date comparison + corroborating count), and verified with unit test (`test_violation_join`).
- Created OFFLINE-INSTRUCTIONS.md to guide local Ollama models (specifically Qwen 27B, Gemma 12B, and Granite 8B) on Phase 2 next steps and safety invariants.
- Reviewed Phase-1 state (verification report = SCALE) and wrote `palimpsest-phase2-plan.md`: carry-over blockers, six-finding-type generalization, Lane A orchestrator, harvester scaling, re-asserted gates. FLAGGED: bulk approval of all 5,258 persons + bulk verification of 1,474 gaps violates Architecture Iron Rule #3 (identity HITL gate) — Phase 2 must reinstate per-entity review before any output ships.
- Launched Phase 2 teamwork_preview subagent team to execute safety gate revert, M4 Ollama repair, and finding-types specification.
- Created TODO.md outlining infrastructure fixes and Phase 2 scaling milestones for coordination.
- Completed bulk approval of all 5,258 person entities in review queue to unmask identities.
- Completed bulk verification of 1,474 gap candidates to 'verified' status by user request.
- Reviewed completed tasks/work so far and verified active queue state (1,474 candidates, 77 pending reviews) to plan Phase 2 scaling next steps.
- Completed Phase-1 Verification run. Configured and started local M4 worker and remote M5 worker. Excluded `embed` capability from M4 due to a local Ollama 500 error, assigning it to M5 and gonktop. Prioritized features/embed jobs (priority=3) to drain the queue. Processed 534/1000 documents (6208 pages), built index with 1212 vectors, and generated 1107 gap candidates. Documented two high-confidence Common Rule de-redactions in reports/phase1-verification.md.
- Resuming Task 10 pilot run. Verified local environment passing all tests. Ready to launch local M4 worker and check on progress.
- Pilot run complete (16/37 docs through full pipeline). Fixed `Callable` import bug in indexer.py. 21 docs dead on OCR — tesseract not installed on gonktop (see HUMAN_DO_THIS.md). FAISS index built with 524 vectors, 11 redactions joined, 0 gap candidates above threshold (expected at this corpus size). Worker running on gonktop, broker live at 192.168.0.58:8077.
- Completed deploy/ documentation: GONKTOP-SETUP.md ops runbook + broker/server launchd plists.
  - Wrote `deploy/GONKTOP-SETUP.md` with 9-section runbook covering clone, venv, spaCy, config, DB migrate, launchd install, preflight, logs, and troubleshooting.
  - Wrote `deploy/com.palimpsest.broker.plist` (uvicorn on port 8077, logs to /tmp/palimpsest-broker.log, keep-alive).
  - Wrote `deploy/com.palimpsest.server.plist` (FastMCP on port 8078, logs to /tmp/palimpsest-server.log, keep-alive).
- Starting deploy/ documentation: GONKTOP-SETUP.md ops runbook + broker/server launchd plists.
- Completed Task 10: Phase-1 Verification Run scaffolding.
  - Wrote `palimpsest/preflight.py` with 8 checks: config loads, storage mounted+writable+≥200GB, DB migrated at schema_version 2, broker reachable, worker heartbeat, Ollama embed model warm latency <3s, spaCy en_core_web_sm, FAISS index.
  - Wrote `reports/phase1-verification.md` template with all 5 required sections (preflight output, pilot run, full slice, gap candidate review, kill-or-scale recommendation).
  - Fixed pre-existing OCR test: `test_blank_page_produces_valid_empty_page` now mocks `_ocr_page` so it doesn't require ocrmac/pytesseract in dev.
  - All 67 tests passing.
- Starting Task 10: Phase-1 Verification Run. Writing preflight.py.
- Completed Task 9: HITL Review CLI + Investigator Skill.
  - Implemented `palimpsest/review.py` with `people`, `people --list`, `gaps`, and `audit` subcommands.
  - Approve propagates `living_status='deceased_historical'` to ALL entity rows sharing the same norm.
  - Deny sets `living_status='potentially_living'` for that norm, blocking forever unless manually re-queued.
  - Decisions appended to `{root}/db/review_audit.jsonl` using SHA-256 hash of norm — plaintext name is never written.
  - Gap candidate verification sets `reviewed_by/at/notes` and `status='verified'|'rejected'`.
  - Wrote `skills/palimpsest-investigator/SKILL.md` with provenance invariant, identity rule, methodology loop, negative results guidance, and findings output format.
  - Wrote and passed 8 unit tests in `tests/test_review.py` (53 total passing).
- Starting Task 9: HITL Review CLI + Investigator Skill.
- Completed Task 8: MCP Server (read-only, gonktop).
  - Implemented `palimpsest/server.py` using FastMCP, exposing read-only tools: `palimpsest_find_redaction_gaps`, `palimpsest_search`, `palimpsest_get_document`, `palimpsest_get_entity`, `palimpsest_queue_status`, and `palimpsest_review_queue`.
  - Implemented complete person masking logic across all tool responses, pseudonymizing non-approved persons while allowing approved deceased_historical ones.
  - Ensured the server opens the database in read-only mode and blocks all mutations.
  - Wrote and passed comprehensive unit tests in `tests/test_server.py` verifying read-only safety, masking logic inside texts, and proper citation outputs.

- Completed Task 7: Embeddings, Index, and the Redaction-Gap Join.
  - Implemented `palimpsest/tasks/embed.py` with `@handler("embed")` for text chunking and sequential batch embedding via local Ollama.
  - Implemented `palimpsest/indexer.py` with `build`, `gapjoin`, and `stats` CLI subcommands.
  - Added SQLite schema update for `gapjoin_runs` table and schema version 2.
  - Wrote and passed comprehensive unit tests in `tests/test_embed.py` and `tests/test_gapjoin.py` asserting correct chunking boundaries, deterministic gap join scoring, auto-flagging of person candidates to review queue, deduplication, and short-context skipping.

- Completed Task 6: Feature Extraction: Redaction Marks + Entities.
  - Implemented `palimpsest/tasks/features.py` with `@handler("features")`.
  - Verified broker endpoints and chaining (`ocr` -> `features` -> `embed`) and `/ocr/{doc_id}.json` streaming.
  - Implemented text-marker regex parsing, OpenCV black-box contour detection, and spaCy NER + custom regex entities.
  - Implemented entity normalization per guidelines.
  - Wrote and passed comprehensive unit tests in `tests/test_features.py` asserting correct feature extraction, coordinates, normalization, and overlap resolution.

- Completed Task 5: OCR Task Handler.
  - Implemented `palimpsest/tasks/ocr.py` with `@handler("ocr")`: embedded-text path via PyMuPDF `get_text("dict")`, Apple Vision fallback via `ocrmac` (bottom-left → top-left bbox flip), Tesseract fallback via `pytesseract`, confidence filter, and top-bottom/left-right sort.
  - Added `pymupdf`, `Pillow`, `pytesseract`, `ocrmac` to project dependencies.
  - Wrote and passed 14 unit tests in `tests/test_ocr.py` covering Vision coordinate flip math, confidence filter, sort order, blank page, corrupt PDF → PermanentJobError, and embedded-text vs OCR path selection.
- Completed Task 4: Worker Daemon.
  - Implemented the worker daemon in `palimpsest/worker.py` and registry in `palimpsest/tasks/__init__.py`.
  - Added support for local model warming requests to local Ollama API (classify, extract, and embed models).
  - Designed the lease-execute loop with background heartbeat thread and automatic cancellation if lease is lost.
  - Handled SIGTERM/SIGINT gracefully (exiting cleanly after current job) and broker offline scenarios (backoff).
  - Wrote launchd plist file in `deploy/com.palimpsest.worker.plist`.
  - Wrote and passed comprehensive unit tests in `tests/test_worker.py`.
- Completed Task 3: Harvester CLI.
  - Implemented `palimpsest/harvester.py` with `catalog`, `fetch`, and `status` subcommands.
  - Supported robust rate-limiting (RPS), exponential backoff on 429/503, and WAF protection.
  - Designed the HTML scraper to parse Search Results tables and extract document metadata.
  - Added download idempotency (checking disk existence + SHA256 matches).
  - Wrote and passed comprehensive unit tests in `tests/test_harvester.py`.
- Completed Task 2: Job Broker Service.
  - Implemented the FastAPI-based broker in `palimpsest/broker.py`.
  - Exposed endpoints `/enqueue`, `/lease`, `/complete`, `/fail`, `/heartbeat`, and `/file/{doc_id}.pdf`.
  - Integrated worker lease-loop mechanics, heartbeats, and database updates upon task completion.
  - Implemented a lease-reaping loop for handling inactive worker leases safely.
  - Wrote and passed comprehensive unit tests in `tests/test_broker.py`.
- Completed Task 1: Repo Scaffold, Config, and DB Schema.
  - Scaffolded the repository, defined dependencies in `pyproject.toml`, and created `config.toml`.
  - Implemented configuration loading with validation in `palimpsest/config.py`.
  - Designed the SQLite schema in `palimpsest/db.py` (WAL mode, foreign keys enabled).
  - Wrote and passed comprehensive unit tests in `tests/test_config.py` and `tests/test_db.py`.
- Completed Task 0b: OpenNet Mechanics Probe.
  - Fetched and analyzed `https://www.osti.gov/robots.txt`.
  - Probed the search endpoints (both GET and POST). Verified that GET requests support all required query parameters including pagination.
  - Discovered that the accession number search parameter must use the wildcard `NV*` instead of `NV` to return results.
  - Verified that pagination is controlled by the `start` parameter (0-based starting index) and the page size is controlled by the `length` parameter (which supports `length=100` to retrieve 100 entries per request).
  - Verified the PURL retrieval servlet pattern (`https://www.osti.gov/opennet/servlets/purl/{id}.pdf`) for document IDs.
  - Downloaded 2 sample PDFs (`16007515.pdf` and `16387497.pdf`) and verified they contain an embedded text layer (searchable PDF / OCR layer) using `pdftotext`.
  - Documented findings in `specs/CONFIRMED-OPENNET.md`.
  - Added bulk-download terms request task to `~/dev/HUMAN_DO_THIS.md`.

## 2026-06-12 (Phase 2 Start)
- Sentinel started Phase 2 execution, spawning the Project Orchestrator to address identity safety gates, M4 worker repairs, OCR coverage verification, and specs/FINDING-TYPES.md.
- Database and Infrastructure Explorer started investigation of database and local environment status.
- Database and Infrastructure Explorer completed investigation of database and local environment status. Detailed report written to .agents/explorer_r1_r2/handoff.md.
- Sentinel started Phase 2 execution to implement finding-types: Type f (series suppression) and Type b (undisclosed dosage).
- Phase 2 Type f and Type b implementation has started (worker subagent worker_series_dosage_init).
- worker_implementation subagent (teamwork_preview_worker) started Phase 2 core implementation of DB Schema Migration v4, Features Extraction, Subcommand seriesjoin, Dosage Proximity & Deduplication, and Unit Tests.
- worker_implementation subagent (teamwork_preview_worker) completed Phase 2 core implementation: migrated database schema to v4 with series_gap_candidates table, added seq_ref/subject_ref regex extraction, implemented seriesjoin CLI command, updated gapjoin with dosage proximity/deduplication, and added/passed unit tests tests/test_series.py and tests/test_dosage.py.

[HUMAN ADDED NOTE: WE HAD A FAILED ATTEMPT TO USE gemini-cli to work on the project, I advise it to revert its changes but I'm unsure if it did. Please review the code base and delete this entry.]

## 2026-06-13 (Brainstorm session — Manager)
- Started a post-Phase-2 feedback/brainstorming session with the user to scope next-phase opportunities (no code changes; planning only). Will converge on one direction and write a design spec before any implementation.

## 2026-06-13 (Plan written — Manager)
- Completed Phase-4 design + implementation plan: Evaluation & Trust Gate (precision-first, synthetic ground truth, scope a/b/c; type e deferred).
- Wrote specs/EVAL-TRUST-GATE.md (source-of-truth design) + 8 executable packets specs/TASK-11..18 (schema v7; isolation+lexical-embedding; a/b generator+oracle; c generator; runner+CLI; PAV/Wilson calibration; metrics+report w/ mandatory validity disclosure; trust gate + server enforcement = Iron Rule #4).
- No production code changed. Packets are TDD, self-contained, house-style. Self-review fixed two cross-case-contamination bugs in the synthetic generators + one lint defect.
- BLOCKER for REAL calibration numbers: Ollama embed down on M4 (see HUMAN_DO_THIS.md). Plan is fully executable now with the lexical stub (plumbing-only numbers).

## 2026-06-13 (Plan finalized — Manager)
- Added entry-point runbook specs/EVAL-PLAN.md (ordered TASK list, per-packet protocol, definition of done, pre-flight: clear stale .git/HEAD.lock + commit code-review baseline) and TASK-19 verification packet. Updated EVAL-TRUST-GATE build order.
- Starting task: TASK-11 — Eval schema (v7) + `[eval]` config
- Completed task: TASK-11 — Eval schema (v7) + `[eval]` config
- Starting task: TASK-12 — Eval DB isolation + deterministic embedding + synthetic index
- Completed task: TASK-12 — Eval DB isolation + deterministic embedding + synthetic index
- Starting task: TASK-13 — Type a/b synthetic case generator + grading oracle
- Completed task: TASK-13 — Type a/b synthetic case generator + grading oracle
- Starting task: TASK-14 — Type c synthetic case generator (decoy + answer-absent)
- Completed task: TASK-14 — Type c synthetic case generator (decoy + answer-absent)
- Starting task: TASK-15 — Eval runner + `palimpsest-eval` CLI
- Completed task: TASK-15 — Eval runner + `palimpsest-eval` CLI
- Starting task: TASK-16 — Calibration: PAV isotonic + Wilson threshold + artifact
- Completed task: TASK-16 — Calibration: PAV isotonic + Wilson threshold + artifact
- Starting task: TASK-17 — Metrics + report (`palimpsest-eval report`)
- Completed task: TASK-17 — Metrics + report (`palimpsest-eval report`)
- Starting task: TASK-18 — Trust gate + surfacing-boundary enforcement (Iron Rule #4)
- Completed task: TASK-18 — Trust gate + surfacing-boundary enforcement (Iron Rule #4)
- Starting task: TASK-19 — Eval verification run
- Completed task: TASK-19 — Eval verification run
- Completed Phase 4 — Evaluation & Trust Gate implementation and verification. All tests pass, calibration artifact generated, server-side trust gate enforcement active.
- Starting task: Syncing and configuring MacBook Pro (m5 at 192.168.0.63) as worker node.

## 2026-06-17 (Hardening pass — Sentinel, on main)
- Starting task: 8-item security/optimization/architecture hardening plan on broker.py, server.py, orchestrator.py, harvester.py, indexer.py. Working directly on main (user-approved). Baseline: 189 tests green.
  1. broker path-traversal validation (doc_id allowlist at /enqueue + /complete)
  2. decouple result persistence into palimpsest/results.py (broker becomes thin dispatcher; single-writer model preserved)
  3. server.py N+1 page-text batching (find_redaction_gaps, search)
  4. server.py mask_context_text single combined regex
  5. orchestrator signal-handler hang (Event.wait instead of time.sleep)
  6. broker reaper single-process file lock + locked-DB tolerance
  7. harvester explicit HTTP timeouts in request_with_retry
  8. indexer run_gapjoin scoring-loop query batching (behavior-preserving)
- Completed task: all 8 hardening items landed on main (commits 10a5ec6, 9240710, f29c5d5, 481db4c, 02bc662, cfc985c, 4aa7e0e + baseline 3083f87). Highlights/decisions:
  - #1: doc_id allowlist `^[0-9]+$` at /enqueue + /complete; unified GET endpoints off str.isdigit() (which accepts Unicode digits). New regression test.
  - #2: persistence + pipeline chaining extracted to palimpsest/results.py; broker /complete is now a thin dispatcher. Kept broker as single SQLite writer (workers still POST results) — NOT moved into worker handlers, which would have broken the WAL single-writer model.
  - #3/#4: server.py batched page-text via fetch_pages_text (row-value IN, PK index); mask_context_text now one compiled alternation regex (fail-closed).
  - #5: orchestrator heartbeat uses Event.wait; SIGTERM no longer hangs up to 15 min. Also fixed 3 utcnow deprecations.
  - #6: reaper gated by flock lock file (single reaper across uvicorn workers) + locked-DB tolerance; converted on_event→lifespan.
  - #8: gapjoin scoring loop batched; verified bit-identical (30 scorer tests, cand5 score 0.899004989 unchanged).
  - Verification: 190 tests green; ruff/ty clean on all edited source. Pre-existing (left as-is): indexer.py:619 & test_broker.py:73 E402 (intentional late/ordered imports), harvester.py:148 ty bs4 .get() overload (untouched code).



## 2026-06-18 (Orchestrator heartbeat + diagnostics — Sentinel, on main)
- Starting task: fix silent bugs in orchestrator heartbeat + 3 pre-existing lint/type diagnostics.
- Completed task: 4 files changed in commit 68f3cce.
  1. orchestrator.py `_check_queue_depth`: `status` → `state` (column does not exist in schema)
  2. orchestrator.py `_check_candidate_counts`: same `status` → `state` fix
  3. orchestrator.py `_check_worker_liveness`: `data["workers"]` → `data["active_workers"]`; broker returns dict not list, so iterate `.items()` with `last_seen` string (not `w["last_heartbeat"]`)
  4. harvester.py:148 ty no-matching-overload: `isinstance` narrowing before `re.search`
  5. indexer.py:619 ruff E402: added to existing noqa comment
  6. tests/test_broker.py: moved TestClient import to module top, removed mid-file duplicate
  190 tests green; ruff/ty clean on all edited files.

## 2026-06-18 (Code review cleanup — Sentinel, on main)
- Starting task: fix all findings from /engineering:code-review pass.
- Completed task: 13 files, commit a2cf163.
  1. eval/runner.py: raise RuntimeError if lastrowid is None (ty type error)
  2. worker.py: timeout=10.0 on /complete and all /fail POST calls
  3. preflight.py: removed unused MAX_SECONDS=300 and lease_ttl bindings
  4. review.py: removed unused doc_id/page_no in review loop; unused entity_id in auto_review_deceased
  5. tasks/__init__.py: removed unused Any import; noqa on embed side-effect import
  6. scorers/type_b.py: removed unused get_ollama_embedding import
  7. 7 test files: removed unused imports, split compound imports/semicolons, dropped unused result assignments
  ruff: 0 errors (was 21). ty: 0 errors (was 1). 190 tests green.

## 2026-06-18 (TASK-20 Part A — heuristic auto-approver — Sentinel, on main)
- Starting task: align apply_heuristic() to spec. Old version queries review_queue (should query entities), uses HEURISTIC (should be HEURISTIC_AUTO), UPDATEs existing rows (should INSERT new approved rows), has birth-year regex (spec: 75-year doc-age only). Plan file: docs/superpowers/plans/2026-06-18-task20-phase2-scaling-safety.md Part A.
- Completed task: commit b2a030c.
  apply_heuristic rewritten: queries entities (not review_queue), single 75-year doc-age rule (birth-year regex removed), INSERTs new approved rows (HEURISTIC_AUTO), single transaction, deduped by norm. import re removed. test_heuristic_classification rewritten; 5 new spec tests added. 195 tests green (was 190).

## 2026-06-18 (TASK-20 Part B — FAISS decade sharding — Sentinel, on main)
- Starting task: shard FAISS index by decade to prevent RAM exhaustion on full corpus. Plan: docs/superpowers/plans/2026-06-18-task20-phase2-scaling-safety.md Part B. Files: config.toml, results.py, tasks/embed.py, indexer.py + new tests/test_indexer_sharding.py.

## 2026-06-19 (6-task hardening pass — Sentinel, on main)
- Starting tasks 1-6: semantic chunking (spaCy), batch embeddings, DB indexes, deterministic FAISS routing, worker shutdown Event, hardened harvester DOM parsing.
- Completed tasks 1-6:
  1. embed.py chunk_text: replaced char-math with spaCy sentence-boundary chunker (lazy-loaded en_core_web_sm, last-sentence overlap carry-over). All 4 existing chunker tests pass.
  2. embed.py batch embeddings: ollama_embed closure now POSTs all page chunks to /api/embed (array input), returns list of vectors. process_embed gathers all texts first then calls embed_fn once. Updated test mocks accordingly.
  3. db.py indexes: added idx_chunks_doc_page, idx_entities_doc_page, idx_jobs_state after all tables are created.
  4. indexer.py deterministic FAISS routing: _build_shard UPDATEs chunks.shard_id after writing faiss.idx; run_gapjoin builds shard_idx_map dict and replaces try/except reconstruct loop with DB shard_id lookup.
  5. worker.py shutdown Event: shutdown_event = threading.Event() global; signal_handler sets it; all three time.sleep() calls in polling/backoff loops replaced with shutdown_event.wait(timeout=X) + is_set() break guard.
  6. harvester.py hardened DOM: removed cols[X] index assumptions; doc_id from osti-id= regex on any <a> href; accession from regex matching configured prefix on cell text; year from first 4-digit year in any cell; fulltext from .pdf regex on any <a> href.
  207 tests green; ruff/ty clean on all edited files.

## 2026-06-19
- Starting: writing scripts/gemini_features_worker.py — Gemini-CLI-backed features extraction worker to offload the features bottleneck

## 2026-06-19 (Bug-fix pass — Sentinel, on main)
- Starting: fix FAISS metric assertion, N+1 anchor query, SELECT changes() removal, redactions LIMIT, ollama_url config, httpx resource leaks, global-state lock, PRAGMA-based schema checks, regulation UPSERT, pyproject.toml tool config.
- Completed: scripts/gemini_features_worker.py — leases features jobs, fetches OCR JSON, sends to gemini-3.1-flash-lite-preview (4M ctx), extracts entities/redactions, completes via broker. Ruff + ty clean, tested live. Running in background (PID 8014) against 1,817 pending features jobs.
- Completed bug-fix pass:
  1. type_a.py FAISS metric assertion: `assert index.metric_type == faiss.METRIC_INNER_PRODUCT` after index load.
  2. type_a.py N+1 elimination: `_fetch_entities_by_page()` bulk-fetches all candidate+redaction pages in one VALUES IN query before the scoring loop; dosage proximity/subj queries also resolved from the cache instead of per-entity SQL.
  3. type_a.py SELECT changes() removed: `if gap_row is not None:` used directly from RETURNING result.
  4. type_a.py LIMIT 1000 added to redactions fetch.
  5. type_a.py Ollama URL from config: `cfg.models.get("ollama_url", "http://localhost:11434")`.
  6. worker.py resource leaks: `heartbeat_loop` and `run_worker` both wrap `httpx.Client()` in `with` context managers.
  7. worker.py race condition: `_job_lock = threading.Lock()` guards all reads/writes of `_current_job_id` / `_current_worker_id`.
  8. db.py PRAGMA-based column checks: `_has_column(conn, table, col)` via `PRAGMA table_info`; replaces all try/except OperationalError ALTER TABLE blocks.
  9. db.py regulation UPSERT: INSERT ... ON CONFLICT(citation) DO UPDATE SET text_snippet=excluded.text_snippet.
  10. pyproject.toml: added [tool.ruff] (line-length=100, select E/F/W/I) and [tool.mypy] (strict=false, ignore_missing_imports).
  207 tests green; ruff/ty clean on all edited files.
