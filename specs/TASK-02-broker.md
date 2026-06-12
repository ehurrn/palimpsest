# TASK-02 — Job Broker (HTTP API over SQLite, single writer)

**Read `specs/00-ARCHITECTURE.md` §1, §3, §5 (jobs table), §9 first. Iron rule: this process is the ONLY one that opens the database.**

## Objective
`palimpsest/broker.py`: a long-running HTTP service on gonktop, port `cfg.broker["port"]` (8077), implementing the job-queue protocol and the read-only file endpoint.

## Depends on
TASK-01 (config, db).

## Deliverables
```
palimpsest/broker.py
tests/test_broker.py
```

## Spec

Framework: **FastAPI + uvicorn** (add to pyproject: `fastapi`, `uvicorn`). One worker process (`uvicorn --workers 1`) — single writer is the point. All timestamps UTC ISO-8601 strings.

### Endpoints

`POST /enqueue`
```json
{"type": "ocr", "doc_id": "16007132", "priority": 5, "payload": {}}
```
→ `{"job_id": 17, "state": "pending"}`. If `(type, doc_id)` exists: return the existing job with `"deduped": true` (idempotent; if existing state is `failed`/`dead`, reset it to `pending`, attempts unchanged).

`POST /lease`
```json
{"worker_id": "m4", "capabilities": ["ocr", "embed", "classify"], "max_jobs": 1}
```
→ `{"jobs": [{"job_id": 17, "type": "ocr", "doc_id": "...", "payload": {},
"lease_expires_at": "..."}]}` (possibly empty).
Selection: `state='pending' AND type IN capabilities ORDER BY priority, job_id LIMIT max_jobs`, atomically set `state='leased'`, `lease_owner=worker_id`, `lease_expires_at = now + cfg.broker["lease_ttl_seconds"]`, `attempts += 1`. Single UPDATE...RETURNING or transaction — no read-then-write race.

`POST /heartbeat` `{"worker_id": "m4", "job_ids": [17]}` → extends lease_expires_at for jobs owned by this worker; returns `{"extended": [17], "lost": []}` (`lost` = jobs no longer owned, worker must abandon them).

`POST /complete`
```json
{"worker_id": "m4", "job_id": 17, "result": { ... type-specific, see below }}
```
→ `{"ok": true}`. Verifies ownership (else 409). Sets `state='done'`. **Then performs the type-specific persistence (§Result handling).**

`POST /fail` `{"worker_id":"m4","job_id":17,"error":"...","retryable":true}` → ownership-checked; retryable & attempts < max_attempts ⇒ `pending`; else `dead` (or `failed` if not retryable). Store error.

`GET /status` → counts by `(type, state)`, plus per-worker last-seen, plus 10 most recent `dead` jobs with errors.

`GET /file/{doc_id}.pdf` → streams `{root}/raw/{doc_id}.pdf`, 404 if absent. Validate `doc_id` is digits only (path-traversal guard).

### Lease reaper
Background task every 60s: `leased AND lease_expires_at < now` ⇒ back to `pending` (attempts already counted at lease). `attempts >= max_attempts` ⇒ `dead`. This is what makes M5's intermittency safe.

### Result handling (broker-side persistence)
On `/complete`, by job type:
- `ocr`: result is the full page-array JSON (00-ARCHITECTURE §6). Write `{root}/ocr/{doc_id}.json` (atomic: tmp file + rename), upsert `pages` rows, set `documents.status='ocr_done'`, `ocr_at=now`, `page_count`.
- `features`: result is the features JSON (§7). Write `{root}/features/{doc_id}.json`, upsert `redactions` + `entities` rows (delete-then-insert for the doc — idempotent), set `status='features_done'`.
- `embed`: result is `{"chunks": [{"page_no":1,"char_start":0,"char_end":800,"text":"...","embedding":[768 floats]}]}`. Upsert `chunks` rows (delete-then-insert per doc); append embeddings to `{root}/index/pending_embeddings.jsonl` as `{"chunk_id": N, "embedding": [...]}` (the indexer, TASK-07, consumes this — broker does NOT touch FAISS).
- `extract`: Phase-1 stub — store result JSON to `{root}/features/{doc_id}.extract.json` only.

Payload size: accept request bodies up to 50 MB (a 300-page OCR result is large).

## Acceptance (paste output)
```
python -m palimpsest.broker --config config.toml &      # starts on :8077
curl -s -XPOST :8077/enqueue -d '{"type":"ocr","doc_id":"111"}' -H 'content-type: application/json'
curl -s -XPOST :8077/enqueue -d '{"type":"ocr","doc_id":"111"}' ...   # → deduped:true
curl -s -XPOST :8077/lease -d '{"worker_id":"t","capabilities":["ocr"],"max_jobs":1}' ...
curl -s :8077/status
```
tests/test_broker.py (FastAPI TestClient, tmpdir config): enqueue-dedupe; lease excludes non-matching capability; double-lease returns nothing; complete by non-owner → 409; fail×max_attempts → dead; reaper returns expired lease to pending; `/file/../etc/passwd` style id → 422/404; ocr complete writes pages rows + json file.

## Out of scope
Worker daemon, FAISS, MCP, harvesting. Broker never initiates connections to workers.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
