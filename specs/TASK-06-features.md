# TASK-06 — Feature Extraction: Redaction Marks + Entities

**Read `specs/00-ARCHITECTURE.md` §3 ([features]), §7 (features JSON, normalization rules §7.3), §6 (coordinate convention).**

## Objective
`palimpsest/tasks/features.py`: the `@handler('features')` implementation. Input: a doc whose OCR is done. Output: the features JSON (§7) — redaction markers (3 kinds) and entities (6 kinds).

## Depends on
TASK-04, TASK-05 (consumes its output shape).

## Broker chaining (small TASK-02 amendment, implement here as a broker patch)
When the broker completes an `ocr` job it must `enqueue('features', doc_id)`; when it completes a `features` job it must `enqueue('embed', doc_id)`. Add this (~6 lines) to broker.py result handling + one test in test_broker.py.

## Deliverables
```
palimpsest/tasks/features.py
tests/test_features.py
```
New deps: `spacy` (+ `en_core_web_sm` via `python -m spacy download`), `opencv-python-headless`, `numpy`, `pymupdf`.

## Spec

### Input
Handler fetches `GET {broker}/file/{doc_id}.pdf` AND needs the OCR JSON. Add to broker (same patch as chaining): `GET /ocr/{doc_id}.json` streaming the stored OCR artifact. Features works from OCR lines + rendered page images.

### A. Redaction markers — text kinds (regex over OCR lines)
Case-insensitive patterns (compile once, module level):
- `exemption_stamp`: `\(\s*b\s*\)\s*\(\s*[1-9]\s*\)` and bare `b\([1-9]\)` ; label = canonicalized `(b)(N)`.
- `deleted_text`: `\[\s*deleted\s*\]` , `\bDELETED\b` (this one case-SENSITIVE all-caps only, to avoid prose hits), `\[\s*redacted\s*\]`, `\bSANITIZED\b` (all-caps).
The marker's bbox = the containing OCR line's bbox. `context_before`/`context_after` = up to `cfg.features["redaction_context_chars"]` (300) chars of reading-order page text before/after the marker line, clipped at `redaction_context_lines` (2) lines in each direction — whichever is smaller.

### B. Redaction markers — `black_box` (image analysis)
Per page: render via PyMuPDF at 150 DPI, grayscale (cv2):
1. Threshold: pixels < `blackbox_darkness_threshold` (60) → mask.
2. `cv2.findContours` on mask; keep contours where:
   - `cv2.boundingRect` area fraction of page ∈ [`blackbox_min_area_frac`, `blackbox_max_area_frac`],
   - rectangularity: contour_area / bounding_rect_area > 0.85,
   - aspect: width/height between 0.2 and 50 (lines/rules are thinner — exclude h or w < 0.5% of page dimension).
3. **Disambiguator:** discard a box if > 10% of its area intersects any OCR line bbox (it's a figure/table cell containing text, not a redaction).
4. Survivors → `kind='black_box'`, bbox normalized to 0–1 top-left (divide pixel coords by rendered w/h — origin already top-left in cv2). Context = nearest OCR lines above and below within 1.5× median line height.
Expect false positives; that's acceptable (DESIGN-REVIEW F7) — they die quietly in the gap join.

### C. Entities
Run spaCy `en_core_web_sm` over each page's `text`:
- `PERSON`→person, `DATE`→date, `GPE`/`LOC`→location, `ORG`→org.
Then regex layer (these OVERRIDE spaCy on overlap — more precise):
- dosage: `\b\d+(?:\.\d+)?\s*(r|rad|rads|rem|mr|mrem|roentgen|uCi|μCi|mCi|curies?)\b` (case-insensitive except unit kept as matched).
- protocol_code: `\b(CAL|CHI|HP)[-\s]?(\d{1,4})\b` (case-sensitive uppercase).
For each entity: `char_start/char_end` into page text; bbox = bbox of the OCR line containing `char_start` (good enough for Phase 1); `norm` per §7.3 — implement `normalize(kind, text) -> str` as a pure function with its own tests.
Persons: emit `living_status` field set to `"unknown"` always (classification is TASK-09's human's job, not yours).

### Output
Assemble §7 features JSON; return as job result (broker persists file + rows).

## Acceptance (paste output)
```
python -m pytest tests/test_features.py -q
```
Tests: each text-marker regex (positive + near-miss negatives: "(b)(1)" hits, "b(12)" doesn't, prose "deleted the file" doesn't); context window clipping at both char and line limits; black-box detection on a generated fixture image (draw 2 black rects + 1 text-filled rect with PIL → exactly 2 detected); the >10%-text-overlap discard; every `normalize()` rule in §7.3 (table-driven test: ≥ 12 cases including "Dr. John SMITH"→"john smith", "Smith, John"→"john smith", "15 REM"→"15 rem", "cal 12"→no match, "CAL 12"→"CAL-12"); dosage regex vs "15 reminders" (must NOT match); spaCy+regex overlap resolution.

## Out of scope
Embedding, indexing, gap join, living-status classification, any LLM call.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
