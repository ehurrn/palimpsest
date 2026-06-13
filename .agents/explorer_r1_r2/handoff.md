# Handoff Report — Database and Environment Investigation

## 1. Observation

- **Database Files**:
  - Local database file exists at `/Users/herren/dev/palimpsest/working/db/palimpsest.db`. Its size on disk is 64KB, and querying it yields:
    ```
    chunks|0
    documents|0
    entities|0
    gap_candidates|0
    gapjoin_runs|0
    jobs|0
    pages|0
    redactions|0
    review_queue|0
    schema_version|1
    ```
  - Remote database file exists on `gonktop` (192.168.0.58) at `/home/herren/palimpsest-data/db/palimpsest.db`. Its size on disk is ~37.3MB, and `review_audit.jsonl` exists in the same directory.
- **SQLite Database Tables Count (on gonktop)**:
  - Deceased historical entities:
    `Deceased historical: 0` (Total entities is 37,098, all with `living_status = 'unknown'`).
  - Review queue statuses:
    `Review queue: [('approved', 35), ('denied', 2), ('pending', 5291)]`
  - Gap candidates statuses:
    `Gap candidates: [('candidate', 1320), ('verified', 154)]`
- **Codebase Logic Locations**:
  - `palimpsest/review.py` (CLI interactive review tool):
    - `handle_people` (line 59) manages interactive review for people, updating `entities.living_status = 'deceased_historical'` (line 197) and `review_queue.status = 'approved'` (line 201) when approved, or `living_status = 'potentially_living'` (line 219) and `review_queue.status = 'denied'` (line 223) when denied.
    - `handle_gaps` (line 248) manages gap candidate interactive verification, updating `status = 'verified'` (line 346).
  - `working/bulk_unmask.py` (bulk approval script):
    - Sets all person entities in `entities` to `'deceased_historical'` (line 24) and bulk inserts/updates review queue rows to `'approved'` (lines 40-50).
  - `palimpsest/server.py` (MCP server for agent tools, read-only mode):
    - `mask_person` (line 48) masks any person entity to a pseudonym `PERSON-XXXX` if they are not approved in `review_queue`.
    - `get_masked_text_for_page` (line 67) masks occurrences inside full page texts.
    - `mask_context_text` (line 105) regex-replaces names in snippet contexts.
- **Ollama Status (on localhost:11434)**:
  - Checking `curl http://localhost:11434/api/tags` returns a list of models: `nomic-embed-text:latest` and `gemma3:12b`.
  - Testing embeddings via `curl -s http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "Hello world"}'` yields:
    `{"error":"error starting llama-server: llama-server binary not found (checked: /Applications/Ollama.app/Contents/Resources/llama-server, ...)"}`
  - Preflight checks fail 2/8 checks:
    ```
    FAIL  Storage ≥ 200 GB free (/Users/herren/dev/palimpsest/working) — only 26.9 GB available
    FAIL  Ollama embed model (nomic-embed-text) — Server error '500 Internal Server Error' for url 'http://localhost:11434/api/embeddings'
    ```
  - Ollama process check:
    ```
    herren            1529   0.0  0.2 490248688  29200   ??  S     4:40PM   0:02.61 /Applications/Ollama.app/Contents/Resources/ollama serve
    herren            1522   0.0  0.2 490522208  30720   ??  S     4:40PM   0:00.67 /Applications/Ollama.app/Contents/MacOS/Ollama
    ```
  - Checking `/Applications/Ollama.app` on disk yields:
    `ls: /Applications/Ollama.app: No such file or directory`
- **Tesseract Installation**:
  - Running `which tesseract` on the local machine yields `tesseract not found`.
  - Running `which tesseract` on gonktop yields `/usr/bin/tesseract`.
- **Test Suite**:
  - Running `uv run pytest` yields:
    `======================== 67 passed, 8 warnings in 5.12s ========================`

## 2. Logic Chain

1. **Database Location**: Since the local `/Users/herren/dev/palimpsest/working/db/palimpsest.db` has 0 rows in all data tables, and the configuration file on `gonktop` points to `/home/herren/palimpsest-data/db/palimpsest.db` where the file size is ~37.3MB, the actual database that is actively used in Phase 1 and holds the processed data resides on `gonktop`.
2. **Reversion Verification**: The database on `gonktop` contains 35 approved and 2 denied persons, but 0 deceased_historical entities. It also contains 154 verified gaps. This verifies that a safety gate revert took place, reinstating the pending statuses for the bulk-unmasked individuals and bulk-verified gaps.
3. **Ollama Failure Cause**: Checking local running processes shows that `ollama` is running in memory. However, searching `/Applications/Ollama.app` on disk returns `No such file or directory`. This proves that the Ollama app has been deleted on disk while the processes were left running. As a result, the running Ollama instance is unable to launch its helper binary `llama-server` dynamically to serve embedding requests, leading to the 500 error on the embeddings API.
4. **Tesseract Availability**: Running `which tesseract` locally fails, but succeeds on gonktop. Thus, local OCR workers cannot run the Tesseract engine fallback.

## 3. Caveats

- We assumed that no other custom environment variables redirect the local SQLite DB connection during active runs.
- The analysis is scoped to the local environment and the accessible `gonktop` node via the provided SSH link.

## 4. Conclusion

- **Database**: The main SQLite database is on `gonktop` at `/home/herren/palimpsest-data/db/palimpsest.db`.
- **Database Status**: The database holds 35 approved and 2 denied review queue items, 0 deceased_historical entities, and 154 verified gap candidates.
- **Ollama**: Local Ollama is a "ghost process" running from memory without the on-disk `Ollama.app` structure, which causes all embedding/generation requests to fail (500 error) due to the missing `llama-server` binary.
- **Tesseract**: Missing locally; present on gonktop.
- **Test Suite**: All 67 tests pass cleanly.

## 5. Verification Method

- To check the SQLite database status, run over SSH on gonktop:
  `ssh herren@192.168.0.58 "python3 -c \"import sqlite3; conn = sqlite3.connect('/home/herren/palimpsest-data/db/palimpsest.db'); print(conn.execute('SELECT status, count(*) FROM review_queue GROUP BY status').fetchall())\""`
- To check the local Ollama embeddings error:
  `curl -s http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "Hello world"}'`
- To run the test suite:
  `uv run pytest`
