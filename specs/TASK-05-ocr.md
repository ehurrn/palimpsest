# TASK-05 — OCR Task Handler

**Read `specs/00-ARCHITECTURE.md` §3 ([ocr]), §6 (page JSON + COORDINATE CONVENTION). The coordinate flip is the most likely silent bug in this task — read §6 twice.**

## Objective
`palimpsest/tasks/ocr.py`: the `@handler('ocr')` implementation. Given a doc_id, produce the page-array JSON (§6) using OSTI's embedded text layer where good, Apple Vision OCR where not, Tesseract as fallback.

## Depends on
TASK-04 (handler registry contract).

## Deliverables
```
palimpsest/tasks/ocr.py
tests/test_ocr.py
tests/fixtures/   # 2 tiny fixture PDFs you generate: one with embedded text, one image-only
```
New deps: `pymupdf` (fitz). macOS-only optional: `ocrmac`. Fallback: `pytesseract` + `Pillow` (requires `brew install tesseract` — if missing on the node, raise PermanentJobError with install instructions; also note it in HUMAN_DO_THIS.md).

## Spec

### Flow per document
1. Fetch PDF: `GET {broker}/file/{doc_id}.pdf` → tmp dir.
2. Open with PyMuPDF. For each page (1-based `page_no`):
   a. Try embedded text: `page.get_text("dict")` → lines with bboxes.
      If total text length ≥ `cfg.ocr["rerun_if_osti_text_shorter_than"]` chars,
      use it: `ocr_source = "osti"`.
   b. Else render page to PNG at 300 DPI (`page.get_pixmap(dpi=300)`) and OCR:
      - engines in `cfg.ocr["engine_preference"]` order;
      - `vision`: via `ocrmac.OCR(png_path, recognition_level="accurate")`.
        **Vision returns normalized bbox with BOTTOM-LEFT origin as
        (x, y, w, h). Convert: `x0=x, y0=1-y-h, x1=x+w, y1=1-y`.**
      - `tesseract`: `pytesseract.image_to_data` → pixel boxes; divide by
        rendered pixmap width/height to normalize (origin already top-left).
      - engine import failure ⇒ try next engine; all engines fail ⇒ raise
        (retryable — broker may reroute to a node that has Vision).
3. Drop lines with `conf < cfg.ocr["min_confidence"]` (keep them out of `text` too).
4. Assemble page object: `width`/`height` in PDF points from PyMuPDF; `lines`
   sorted top-to-bottom then left-to-right (sort key: `(round(y0, 2), x0)`);
   `text` = line texts joined with `\n`.
5. Return the full array as the job result (broker persists it).

### Edge cases (handle explicitly)
- Encrypted/corrupt PDF ⇒ `PermanentJobError("unreadable pdf: <detail>")`.
- 0-page PDF ⇒ PermanentJobError.
- Page with zero recognized lines ⇒ valid page object with empty `lines`, `text=""` (NOT an error — blank/black pages exist).
- Per-page OCR timing logged; > 5s/page on Metal means something is wrong (log a warning, continue).

## Acceptance (paste output)
```
python -m pytest tests/test_ocr.py -q
```
Tests must cover: embedded-text path chosen for the text fixture; image-only fixture goes to OCR (mock the engine if not on macOS, but run the REAL ocrmac path when `platform == darwin` and ocrmac importable); **a unit test for the Vision coordinate flip with hand-computed values** (e.g. vision (x=0.1, y=0.2, w=0.3, h=0.05) → bbox [0.1, 0.75, 0.4, 0.8]); confidence filter; line sort order; blank page; corrupt PDF → PermanentJobError.

Then an integration smoke (manual, on a Mac): handler against one real fixture PDF end-to-end through a local broker, verify `{root}/ocr/{doc_id}.json` matches §6 schema and pages rows exist.

## Out of scope
Redaction/entity detection (TASK-06), enqueueing follow-on jobs (the broker chains `ocr`→`features` — see TASK-06 note), embedding.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
