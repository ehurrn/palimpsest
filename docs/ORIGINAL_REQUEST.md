# Original User Request

## Initial Request — 2026-06-12T22:43:38-05:00

Implement Phase 2 finding-types: Type f (series suppression) and Type b (undisclosed dosage) in the Palimpsest pipeline, and run validation.

Working directory: /Users/herren/dev/palimpsest
Integrity mode: development

## Requirements

### R1. Type f — Document-Series Suppression
1. Add sequence-number regex detector (`seq_ref` entity kind) in `palimpsest/tasks/features.py` to match accession number or sequence patterns (e.g., `NV\d{7}`, `NV-\d+`, `Report No. \d+`).
2. Add a new subcommand `seriesjoin` (running `run_series_join(cfg)`) in `palimpsest/indexer.py` to identify missing sequence gaps (gap ratio > 20%) and generate gap candidates.

### R2. Type b — Undisclosed Radiation Dosage
1. Add subject-reference regex patterns (e.g. `\b(Subject|Patient|Case|Individual)\s+[A-Z\d]+\b`) in `features.py` as entity kind `subject_ref`.
2. Update the gapjoin scoring logic in `palimpsest/indexer.py` to support dosage value match and subject proximity scoring.

## Acceptance Criteria

### Type f Series Suppression
- [ ] `seq_ref` entities are correctly extracted and normalized in `features.py`.
- [ ] Subcommand `seriesjoin` exists and generates candidates in the database.
- [ ] Unit tests in `tests/test_series.py` assert series gap calculation correctness and pass 100%.

### Type b Undisclosed Dosage
- [ ] `subject_ref` entities are correctly extracted and normalized.
- [ ] Proximity dosage scorer matches subject-dosage pairs across documents.
- [ ] Unit tests in `tests/test_dosage.py` verify dosage matching and pass 100%.
