# Work Log

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

