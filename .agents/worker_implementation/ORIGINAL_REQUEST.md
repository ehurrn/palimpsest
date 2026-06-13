## 2026-06-13T03:46:28Z
You are a worker subagent (teamwork_preview_worker). Your working directory is `/Users/herren/dev/palimpsest/.agents/worker_implementation`.

Please implement the following Phase 2 requirements:
1. Database Schema Migration v4:
   Update `palimpsest/db.py` to create the `series_gap_candidates` table and increment the schema version to 4. The table should have the following fields:
   `gap_id` INTEGER PRIMARY KEY,
   `series_prefix` TEXT NOT NULL,
   `missing_number` INTEGER NOT NULL,
   `missing_accession` TEXT NOT NULL UNIQUE,
   `flanking_doc_id` TEXT REFERENCES documents(doc_id),
   `ref_entity_id` INTEGER REFERENCES entities(entity_id),
   `score` REAL NOT NULL,
   `status` TEXT DEFAULT 'candidate',
   `reviewed_by` TEXT,
   `reviewed_at` TEXT,
   `notes` TEXT

2. Features Extraction:
   Update `palimpsest/tasks/features.py` to:
   - Add regex patterns for sequence reference `seq_ref` (accession pattern matching `NV\d{7}`, `NV-\d+`, `Report No. \d+` case-insensitive) and subject reference `subject_ref` (`\b(Subject|Patient|Case|Individual)\s+[A-Z\d]+\b` case-insensitive).
   - In `process_features`, extract `seq_ref` and `subject_ref` matching spans and append them as entities of respective kind.
   - Standardize/normalize `seq_ref` (uppercase, space cleanup, standard prefix mapping e.g., NV-12345, NV0012345, REPORT-NO-3) and `subject_ref` (lowercase) in `normalize` and the corresponding helper functions.

3. Subcommand `seriesjoin`:
   Update `palimpsest/indexer.py` to:
   - Add `seriesjoin` command to the CLI parser and implement `run_series_join(cfg)` which parses accessions from the `documents` table, groups them by prefix, identifies sequence gaps (under gap ratio > 20%), corroborates them by checking if flanking documents (N-1 or N+1 sequence) contain a `seq_ref` entity pointing to the missing accession, scores the candidate (0.90 if referenced by both flanking documents, 0.70 if by one, 0.50 if neither), and inserts/upserts candidates with score >= 0.65 into `series_gap_candidates`.
   - Update `print_stats(cfg)` to display statistics for `series_gap_candidates`.

4. Dosage Proximity & Deduplication:
   Update the gapjoin scoring logic (`run_gapjoin`) in `palimpsest/indexer.py` to:
   - When joining candidates for a redaction: if the clear entity kind is `dosage`, check for the nearest `subject_ref` or `person` entity on the same page using character distance, and compute proximity score `proximity_score = exp(-distance / 500)`. Check if a subject reference with the same normalized name/value is present on both the candidate page and the redaction page, and apply appropriate scoring adjustments/boosts. Also verify if the dosage value matches any dosage on the redaction page or in the redaction context and boost/adjust accordingly.
   - For a given redaction, deduplicate candidate matches by normalized dosage value (group by `norm` for dosage entities), retaining only the highest-scoring candidate.

5. Unit Tests:
   Create `tests/test_series.py` and `tests/test_dosage.py` to thoroughly verify the new features, commands, normalization, scoring, and deduplication. Run the entire test suite (`pytest`) to verify 100% pass rates.

Ensure your work meets layout conventions, and output a detailed handoff report in `/Users/herren/dev/palimpsest/.agents/worker_implementation/handoff.md`.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
