# Orchestrator Plan — Phase 2 Finding-Types Implementation

## Goal
Implement and verify Type f (series suppression) and Type b (undisclosed dosage) finding-types in the Palimpsest pipeline.

## Execution Plan

1. **Decompose & Initialize**:
   - Reset `plan.md` and `progress.md` in `.agents/orchestrator/`.
   - Update `WORK-LOG.md` (via a subagent) to log starting the task.
   - Establish working directories for the subagents.

2. **Milestone 1: Type f (Series Suppression)**:
   - **Detector**: Add sequence-number regex patterns as `seq_ref` in `features.py`. Target patterns: `NV\d{7}`, `NV-\d+`, `Report No. \d+` (case-insensitive).
   - **Normalization**: Clean spacing, convert to uppercase (e.g., `"NV-123"`).
   - **Joiner**: Implement `seriesjoin` CLI subcommand in `indexer.py` invoking `run_series_join(cfg)`.
     - Read all `seq_ref` entities.
     - Extract numeric sequence ranges for each series prefix (e.g., `NV`, `Report No.`).
     - Detect gaps (missing sequence numbers) in local sequences.
     - A gap is a candidate if the gap ratio in the surrounding range is > 20% (e.g. at least 1 missing per 5 present).
     - Corroborate by checking if a flanking document (sequence N-1 or N+1) contains a reference to the missing document (either by number or title in its `seq_ref` or text).
     - Insert candidates into a new `series_gap_candidates` table in SQLite.
   - **Testing**: Create `tests/test_series.py` and verify it passes 100%.

3. **Milestone 2: Type b (Undisclosed Dosage)**:
   - **Detector**: Add subject-reference regex patterns (e.g. `\b(Subject|Patient|Case|Individual)\s+[A-Z\d]+\b`) in `features.py` as entity kind `subject_ref`.
   - **Normalization**: Clean spacing, convert to lowercase.
   - **Scoring**: Update `run_gapjoin` logic in `indexer.py` to support dosage value match and subject proximity scoring.
     - When the clear entity is a `dosage`, look for matching dosage references and calculate proximity score (e.g., distance between a `subject_ref` / `person` and the `dosage` or redaction on the page).
     - Score based on whether both pages have matching normalized dosage values and nearby subject references.
   - **Testing**: Create `tests/test_dosage.py` and verify it passes 100%.

4. **Milestone 3: Final Verification & Reporting**:
   - Re-run all tests to assert 100% pass rates.
   - Update `WORK-LOG.md` via a worker.
   - Send completion report back to parent.
