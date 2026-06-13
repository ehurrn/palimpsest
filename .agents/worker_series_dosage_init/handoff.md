# Phase 2 Type f (Series Suppression) and Type b (Undisclosed Dosage) Implementation Plan

This handoff report outlines the detailed strategy, database schema changes, python code structure, and verification methods for implementing Type f (series suppression) and Type b (undisclosed radiation dosage) finding-types.

## 1. Observation
- Verified that all 74 existing tests pass successfully when executing `.venv/bin/pytest`:
  ```
  collected 74 items
  ...
  ======================== 74 passed, 8 warnings in 5.68s ========================
  ```
- Observed existing regex entity extraction and normalization pipeline in `palimpsest/tasks/features.py`. Entities are extracted from page text using pre-compiled regexes (`dosage_pattern`, `protocol_pattern`, `reg_cite_patterns`) and normalized in `normalize()`.
- Observed database schema structure in `palimpsest/db.py` up to migration v3 (supporting regulation citation metadata and violation candidates).
- Observed index construction, embedding generation, scoring formulas, and CLI subcommands in `palimpsest/indexer.py`.

## 2. Logic Chain
- **Inference 1 (features.py updates):** Since `features.py` already uses regular expressions to extract `dosage`, `protocol_code`, and `reg_cite`, we can add `seq_ref` and `subject_ref` patterns directly to `process_features` and normalize them using custom logic in `normalize()`.
- **Inference 2 (Type f database schema):** To track missing documents in sequence gaps without polluting other candidate tables, we should create a dedicated `series_gap_candidates` table in a new migration v4 in `db.py`.
- **Inference 3 (Type f analysis):** The `seriesjoin` command in `indexer.py` can parse cataloged accessions (e.g. `NV\d{7}`, `NV-\d+`, `REPORT-NO-\d+`), sort them to find missing integers, identify gaps within a window of 5 consecutive present accessions, search flanking documents (`M-1` and `M+1`) for `seq_ref` entities matching the missing number `M`, score the candidate, and persist it to `series_gap_candidates`.
- **Inference 4 (Type b logic):** For Type b, the co-occurrence of a `dosage` entity, a `person`/`subject_ref` entity, and a redaction on the same page will mark a candidate. During gapjoin, we will query other pages for the same dosage value. The score will incorporate the cosine similarity and a continuous proximity score: `proximity_score = exp(-distance / 500)` based on character distance on the candidate page. Scoring must also apply cross-document dosage deduplication by grouping candidate matches for the same triple and keeping the one with the maximum score.

## 3. Caveats
- We assume that `accession` values in the database are formatted as `NV\d{7}`, `NV-\d+`, or `Report No. \d+`. If new, arbitrary formats exist, they may not be recognized by the sequence parser.
- Proximity scoring based on character distance assumes that the text layout matches the reading order parsed from the PDF.

## 4. Conclusion & Strategy

### A. Python Implementation Changes

#### 1. `palimpsest/tasks/features.py`
- Add regex patterns:
  ```python
  seq_ref_patterns = [
      re.compile(r'\bNV\d{7}\b', re.IGNORECASE),
      re.compile(r'\bNV-\d+\b', re.IGNORECASE),
      re.compile(r'\bReport\s+No\.?\s*\d+\b', re.IGNORECASE),
      re.compile(r'\bReport\s+Number\s*\d+\b', re.IGNORECASE)
  ]
  subject_ref_pattern = re.compile(r'\b(?:Subject|Patient|Case|Individual)\s+[A-Z\d]+\b', re.IGNORECASE)
  ```
- Inside `process_features`, extract `seq_ref` and `subject_ref` matching spans, compute their line-based bounding boxes, and add them to `regex_entities`.
- Add normalization functions:
  ```python
  def normalize_seq_ref(text: str) -> str:
      text = " ".join(text.split()).strip().upper()
      m = re.match(r'^NV[-\s]?(\d+)$', text)
      if m:
          num_str = m.group(1)
          if len(num_str) == 7:
              return f"NV{num_str}"
          return f"NV-{int(num_str)}"
      m = re.match(r'^REPORT\s+(?:NO\.?|NUMBER)\s*(\d+)$', text)
      if m:
          return f"REPORT-NO-{int(m.group(1))}"
      return text

  def normalize_subject_ref(text: str) -> str:
      return " ".join(text.split()).strip().lower()
  ```
- Register the new normalization routines inside `normalize(kind, text)` under `"seq_ref"` and `"subject_ref"`.

#### 2. `palimpsest/db.py`
- Define migration v4 schema change to create the `series_gap_candidates` table:
  ```python
  conn.execute("""
  CREATE TABLE IF NOT EXISTS series_gap_candidates (
    gap_id         INTEGER PRIMARY KEY,
    series_prefix  TEXT NOT NULL,
    missing_number INTEGER NOT NULL,
    missing_accession TEXT NOT NULL UNIQUE,
    flanking_doc_id TEXT REFERENCES documents(doc_id),
    ref_entity_id  INTEGER REFERENCES entities(entity_id),
    score          REAL NOT NULL,
    status         TEXT DEFAULT 'candidate',
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    notes          TEXT
  );""")
  conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4);")
  ```

#### 3. `palimpsest/indexer.py`
- Add `seriesjoin` command to CLI and implement `run_series_join(cfg)`:
  - Query all documents from `documents` table to retrieve `doc_id` and `accession`.
  - Group and sort accessions per series prefix (`NV`, `NV-`, `REPORT-NO-`).
  - Scan for gaps. Under the sliding window rule, for every 5 consecutive present integers `window`, identify any missing integers `M` in range `[window[0], window[4]]`.
  - For each missing number `M`, determine flanking accessions `M-1` and `M+1`.
  - Check the `entities` table: search if either flanking document has a `seq_ref` entity referencing the normalized accession of `M`.
  - Score the gap candidate: `0.70` if one flanking document references `M`, `0.90` if both flanking documents reference `M`, and `0.50` if no references are found (below 0.65 threshold).
  - Persist candidates with score `>= 0.65` to `series_gap_candidates`.
- Update `run_gapjoin` scoring for Type b:
  - When candidate is dosage/subject, calculate proximity score: `proximity_score = exp(-distance / 500)` based on character distance between dosage and event on the candidate page.
  - Implement cross-document dosage dosage deduplication: group candidate matches by their normalized dosage value, keeping only the highest-scoring candidate.

### B. Test Design

#### `tests/test_series.py`
1. **Normalization Tests:** Assert that `normalize("seq_ref", text)` properly canonicalizes values:
   - `nv0042452` -> `NV0042452`
   - `NV-12345` -> `NV-12345`
   - `Report No. 3` -> `REPORT-NO-3`
2. **Extraction Tests:** Assert that `process_features` correctly extracts sequence references from text and does not overlap NER.
3. **Join Tests:** Insert mock documents into the DB representing a series (e.g. `NV0000001`, `NV0000002`, `NV0000004` (missing `NV0000003`)). Add a reference to `NV0000003` inside `NV0000002`. Run `run_series_join(cfg)` and verify that `series_gap_candidates` contains a candidate for `NV0000003` with a score of `0.70`.

#### `tests/test_dosage.py`
1. **Normalization Tests:** Assert that `normalize("subject_ref", text)` lowercases and cleans subject references (e.g., `Subject 3` -> `subject 3`).
2. **Extraction Tests:** Assert that `subject_ref` is correctly extracted without overlapping.
3. **Proximity & Deduplication Tests:** Create a mock join scenario where the same dosage value is present on multiple candidate pages with different proximity distances. Run gapjoin and verify that only the highest-scoring candidate (closest proximity) is kept.

## 5. Verification Method
1. Run target test files:
   - `.venv/bin/pytest tests/test_series.py`
   - `.venv/bin/pytest tests/test_dosage.py`
2. Run database migration command:
   - `python -m palimpsest.db migrate`
   - Inspect SQLite database tables to confirm `series_gap_candidates` table exists.
3. Run CLI command:
   - `python -m palimpsest.indexer seriesjoin`
   - Confirm it runs cleanly and displays stats.
