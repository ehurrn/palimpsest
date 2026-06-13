# BRIEFING — 2026-06-13T03:49:20Z

## Mission
Implement Phase 2 requirements: DB v4 migration, seq_ref and subject_ref extraction, seriesjoin command, dosage proximity & deduplication logic, and unit tests.

## 🔒 My Identity
- Archetype: teamwork_preview_worker
- Roles: implementer, qa, specialist
- Working directory: /Users/herren/dev/palimpsest/.agents/worker_implementation
- Original parent: 25d1f556-dbf6-4cfb-824a-45b32243eccc
- Milestone: Phase 2 Core Implementation

## 🔒 Key Constraints
- Default to conversational prose over structured formatting for user messages.
- Optimize responses for token use.
- Always be direct, not performative.
- Do not work outside of `~/dev/`.
- No recursive syncing on entire GCS bucket.
- provenance invariant: No de-redaction claim is valid without an explicit citation to a Document ID and page number for both the redacted source and the clear corroborating source.
- identity gate: Any data matching potentially-living subjects must flag the person entity and require human approval before being written to any public output.
- work log invariant: Always write to `~/dev/palimpsest/WORK-LOG.md` when starting/completing actions.
- write any tasks which block you and require human intervention to `~/dev/palimpsest/HUMAN_DO_THIS.md`.

## Current Parent
- Conversation ID: 25d1f556-dbf6-4cfb-824a-45b32243eccc
- Updated: 2026-06-13T03:49:20Z

## Task Summary
- **What to build**: DB schema v4 migration (`series_gap_candidates` table), extraction of `seq_ref` & `subject_ref` in `features.py` (with normalization), `seriesjoin` subcommand in `indexer.py`, dosage proximity scoring & candidate deduplication by normalized value in `run_gapjoin`, and unit tests `tests/test_series.py` & `tests/test_dosage.py`.
- **Success criteria**: All new tables, regex extractors, CLI subcommand `seriesjoin`, and dosage proximity scoring/deduplication logic implemented correctly. 100% test pass rate with pytest.
- **Interface contracts**: `palimpsest/db.py`, `palimpsest/tasks/features.py`, `palimpsest/indexer.py`.
- **Code layout**: Source in `palimpsest/`, tests in `tests/`.

## Key Decisions Made
- Implemented sequence reference prefix parsing and sequence gap detection (under gap ratio > 20%) in `seriesjoin`.
- Added exponential character distance proximity scoring `proximity_score = exp(-distance / 500)` on candidate page for `dosage` entities in `run_gapjoin`.
- Added co-occurrence boost (+0.15) and dosage value match boost (+0.15) for dosage entities.
- Implemented grouping by normalized dosage value and retaining only the highest-scoring candidate to deduplicate matches.

## Artifact Index
- None

## Change Tracker
- **Files modified**:
  - `palimpsest/db.py` - Created `series_gap_candidates` table (Schema version 4).
  - `palimpsest/tasks/features.py` - Added regex patterns and normalization for `seq_ref` and `subject_ref`.
  - `palimpsest/indexer.py` - Implemented `seriesjoin` CLI command and updated `run_gapjoin` with dosage proximity, boosts, and deduplication logic.
  - `palimpsest/preflight.py` - Updated EXPECTED_VERSION check to 4.
  - `tests/test_db.py` - Added verification for `series_gap_candidates` table.
  - `tests/test_gapjoin.py` - Updated `test_gapjoin_algorithm` assertion to match new dosage-proximity scoring behavior.
- **Build status**: PASS
- **Pending issues**: None

## Quality Status
- **Build/test result**: PASS (80/80 tests passing)
- **Lint status**: 0 violations on all modified/added files
- **Tests added/modified**: Added `tests/test_series.py` and `tests/test_dosage.py`.

## Loaded Skills
- None
