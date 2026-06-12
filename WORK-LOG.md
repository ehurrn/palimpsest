# Work Log

## 2026-06-12
- Started Task 4: Worker Daemon.
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
