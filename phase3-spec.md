# TASK-20 — Phase 2 Scaling & Safety Heuristics

**Objective:** Unblock Phase 2 full-corpus scaling by implementing the identity heuristic auto-approver, sharding the FAISS index to prevent RAM exhaustion on gonktop, and adding explicit job release for worker daemon shutdowns.

## 20.1 Identity Heuristic Auto-Approver
**Target:** `palimpsest/review.py`

**Context:** Iron Rule #3 dictates no person is surfaced without HITL approval. However, the Phase 2 plan notes that manual review of thousands of entities is impossible. We need to implement the age-based safety heuristics mentioned in the README.

**Requirements:**
1. Implement the `heuristic` subcommand in `palimpsest/review.py` (referenced in README but currently a stub).
2. Query `entities` where `kind = 'person'` and `living_status = 'unknown'`.
3. Join with the `documents` table to retrieve `documents.year`.
4. **Heuristic Rule:** If `(current_year - documents.year) > 75`, the subject is presumed deceased for research purposes.
5. For entities meeting the rule:
   - Update `entities.living_status` to `'deceased_historical'`.
   - Insert a corresponding row into `review_queue` with `status = 'approved'`, `reason = 'Auto-approved via 75-year document age heuristic'`, and `decided_by = 'HEURISTIC_AUTO'`.
6. For entities failing the rule:
   - Update `entities.living_status` to `'potentially_living'`. (These remain masked as `PERSON-XXXX` in all outputs).
7. Execute these updates in a single SQLite transaction to maintain audit integrity.
8. Print a summary to stdout: number of entities evaluated, number marked historical, number flagged as potentially living.

## 20.2 FAISS Index Sharding
**Target:** `palimpsest/indexer.py`, `palimpsest/tasks/embed.py`, `config.toml`

**Context:** The current design writes a monolithic `{root}/index/faiss.idx`. Scaling to 500K NV documents will exceed gonktop's RAM. We must shard the index.

**Requirements:**
1. Modify `config.toml` under `[embed]` to include `shard_by = "decade"`.
2. Update the storage layout. Instead of `{root}/index/faiss.idx`, use `{root}/index/shards/YYYY/faiss.idx` (where YYYY represents the decade, e.g., 1950, 1960). Same for `chunk_map.json`.
3. Modify `palimpsest.tasks.embed`: When writing chunk embeddings, the worker should return the `year` from the job payload so the broker can route the embedding to the correct shard directory. *(Note: Since workers don't write to DB/storage directly per architecture, the broker must handle the shard routing during the `/complete` payload processing).*
4. Modify `palimpsest.indexer.build`: Aggregate chunks and train/build independent FAISS indices per decade shard.
5. Modify `palimpsest.indexer.gapjoin`: Update the algorithm to iterate over available shards. When seeking corroboration for a candidate redaction, search across all loaded shards sequentially, collecting the global top-K before applying the anchor/kind scoring.

## 20.3 Worker Graceful Release
**Target:** `palimpsest/broker.py`, `palimpsest/worker.py`

**Context:** `worker.py` tries to finish its current job upon receiving SIGINT. If the supervisor sends SIGKILL before it finishes, the job is stuck in `leased` state until `reap_leases` triggers (up to 15 minutes). We need the worker to explicitly release jobs back to the queue on shutdown.

**Requirements:**
1. **Broker:** Add a `POST /release` endpoint. Accepts `worker_id` and `job_id`. Validates ownership, then immediately updates the job `state = 'pending'` and increments a `releases` counter or just updates `updated_at` without penalizing the `attempts` count (since this is an infrastructure shutdown, not a job failure).
2. **Worker:** In `signal_handler` in `worker.py`, if a job is currently leased and actively processing an LLM generation step (which can be interrupted), catch the signal, immediately call `POST /release`, and exit. 
3. *Constraint:* OCR jobs in Apple Vision (`ocrmac`) block the thread and might be harder to interrupt cleanly. Focus the interrupt/release logic primarily on the heavy `extract` task block.
