# Work Log

## 2026-06-12
- Started Task 1: Repo Scaffold, Config, and DB Schema.
- Completed Task 0b: OpenNet Mechanics Probe.
  - Fetched and analyzed `https://www.osti.gov/robots.txt`.
  - Probed the search endpoints (both GET and POST). Verified that GET requests support all required query parameters including pagination.
  - Discovered that the accession number search parameter must use the wildcard `NV*` instead of `NV` to return results.
  - Verified that pagination is controlled by the `start` parameter (0-based starting index) and the page size is controlled by the `length` parameter (which supports `length=100` to retrieve 100 entries per request).
  - Verified the PURL retrieval servlet pattern (`https://www.osti.gov/opennet/servlets/purl/{id}.pdf`) for document IDs.
  - Downloaded 2 sample PDFs (`16007515.pdf` and `16387497.pdf`) and verified they contain an embedded text layer (searchable PDF / OCR layer) using `pdftotext`.
  - Documented findings in `specs/CONFIRMED-OPENNET.md`.
  - Added bulk-download terms request task to `~/dev/HUMAN_DO_THIS.md`.
