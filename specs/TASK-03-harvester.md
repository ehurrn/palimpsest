# TASK-03 — OpenNet Harvester

**BLOCKED until `specs/CONFIRMED-OPENNET.md` exists (TASK-00b). Where this packet says `<per CONFIRMED>`, substitute the verified value from that file — never guess URLs or params.**

## Objective
`palimpsest/harvester.py`: query OpenNet for the configured NV slice, persist catalog rows, download PDFs politely, enqueue `ocr` jobs via the broker.

## Depends on
TASK-00b, TASK-01, TASK-02 (running broker).

## Deliverables
```
palimpsest/harvester.py
tests/test_harvester.py
```
New deps: `httpx`, `beautifulsoup4` (only if CONFIRMED says HTML scraping; if an API/JSON route was confirmed, use it and skip bs4).

## Spec

### CLI
```
python -m palimpsest.harvester catalog --query '<per CONFIRMED>' [--limit N]
python -m palimpsest.harvester fetch [--limit N]
python -m palimpsest.harvester status
```
Two phases, deliberately separate: `catalog` walks search results and upserts `documents` rows (status `cataloged`) WITHOUT downloading PDFs — cheap, resumable, lets the human inspect the slice before committing bandwidth. `fetch` downloads PDFs for cataloged rows.

### Politeness (hard constraints)
- Token-bucket limiter: `cfg.harvest["rate_limit_rps"]` (1.0) across ALL requests.
- User-Agent: `cfg.harvest["user_agent"]`.
- On 429/503: exponential backoff from `backoff_initial_s` to `backoff_max_s`, honor `Retry-After` header if present.
- **Kill-switch:** 3 consecutive 403s ⇒ abort the run, log loudly, write a `HUMAN_DO_THIS.md` entry ("OSTI may have blocked us — stop and email opennet@osti.gov"). Do not retry around a block.
- Respect robots.txt findings recorded in CONFIRMED-OPENNET.md.

### catalog
- Build the search request from CONFIRMED params + `cfg.harvest["accession_prefix"]`.
- Paginate per CONFIRMED pagination rules; for each result upsert:
  `doc_id, accession, title, year, has_fulltext, source_url, status='cataloged'`.
- Resumable: re-running upserts; already-present rows untouched (compare by doc_id).

### fetch
- Select `status='cataloged'` rows, oldest first, up to `--limit`.
- Download via the CONFIRMED purl pattern → atomic write to `{root}/raw/{doc_id}.pdf` (tmp + rename).
- Record `sha256`, `local_path`, `fetched_at`, `status='fetched'`.
- Skip if file already exists AND sha256 recorded (idempotent).
- After each successful fetch: `POST {broker}/enqueue {"type":"ocr","doc_id":...}`.
- A failed download marks the row `status='error'` with the reason; it does NOT halt the run.

### Database access (locked decision)
The harvester runs strictly ON gonktop and opens the DB directly via `palimpsest.db.connect` alongside the broker. This is safe: WAL mode supports multiple processes on a LOCAL filesystem; the corruption risk the iron rule guards against is NETWORK filesystem access. The iron rule (00-ARCHITECTURE §1, rule 1) reads: "No process off gonktop ever opens the database." Do not add catalog endpoints to the broker. The harvester must hold write transactions briefly (single-row upserts, `busy_timeout` already set by `db.connect`).

## Acceptance (paste output)
```
python -m palimpsest.harvester catalog --limit 25     # 25 rows, status cataloged
python -m palimpsest.harvester catalog --limit 25     # rerun: 0 new rows
python -m palimpsest.harvester fetch --limit 5        # 5 PDFs in {root}/raw/, 5 ocr jobs enqueued
python -m palimpsest.harvester fetch --limit 5        # rerun: skips already-fetched
python -m palimpsest.harvester status                 # counts by status
grep -c 'GET' harvest.log  →  verify spacing ≥ ~1s between request timestamps
```
tests: rate limiter timing (mock clock), backoff on 429 (mock transport), kill-switch on 3×403, atomic write (no partial file on simulated interrupt), idempotent re-fetch.

## Out of scope
OCR, choosing the slice (human provides `--query`), bulk runs beyond `--limit` smoke tests until the human green-lights the slice.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
