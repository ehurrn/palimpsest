## HUMAN_DO_THIS
- Please execute the created script to reprioritize feature jobs: `python3 /Users/herren/dev/palimpsest/scripts/update_jobs.py`

## 2026-06-19 05:32 — Features pipeline starved by OCR job_id ordering

**Status:** Features jobs have job_ids 18604+ while OCR pending starts at 15526. 
Broker leases by `priority ASC, job_id ASC` — OCR will be picked for the next ~12 hours
(3030 OCR pending at ~4/min = ~750 min). Features won't run until OCR queue clears.

**Fix options (pick one):**
1. Add `/jobs/reprioritize` endpoint to broker → call `POST /jobs/reprioritize?type=features&priority=2`
2. Or from gonktop: `~/.local/bin/uv run python3 -c "import sqlite3; c=sqlite3.connect('/home/herren/palimpsest-data/db/palimpsest.db'); c.execute(\"UPDATE jobs SET priority=2 WHERE state='pending' AND type='features'\"); c.commit(); print('done')"`
3. Or dedicate gonktop to features by temporarily setting `nodes.gonktop = ["features","embed"]` in config.toml and restarting the gonktop worker

**Also:** Job 2940 (doc 1605899) has att=4 (exceeds max_attempts=3) but is stuck leased.
May need manual kill + revive.

## 2026-06-21 — Phase 2 carry-over blockers (palimpsest-phase2-plan.md §0)

The implementable Phase 2 *code* (FINDING-TYPES taxonomy, all six scorers, the four
new detector entity kinds, regulation seed, identity-gate masking) is built and the
suite is green (222). The following carry-over items gate further Phase 2 progress and
require human/infra action — they cannot be done from the dev box or via pytest:

1. **[BLOCKER — safety] Identity HITL gate DATA remediation.** The working corpus DB
   (on gonktop, not in this repo) holds a *bulk approval of all ~5,258 person entities*
   and *bulk verification of all gap candidates*, violating Architecture Iron Rule #3.
   The gate *enforcement* is already coded (`server.py` masks unless
   `deceased_historical` AND per-entity approved; `review.py` has per-entity review +
   a >75-yr document-age heuristic). What needs a human:
   - **Decision (plan §7.2):** revert-and-re-review vs. re-mask-then-review.
   - **Decision:** the defensible bulk-classification rule for `deceased_historical`
     at scale (5,258 is not hand-reviewable; e.g. document-date + birth-year heuristic,
     NOT a blanket approve). `review.py classify_living_status` already defaults to the
     >75-yr rule — confirm or replace it.
   - **Action:** run the remediation against the gonktop DB and sign off. No Phase 2
     output ships until this is restored.
2. **[infra] Repair Ollama on M4** (missing `llama-server` / local 500 on embed), then
   re-enable `embed` for `m4` in `config.toml`. (Also in TODO.md.)
3. **[infra] OCR coverage** — confirm `tesseract` is installed on every node a worker
   may run on (esp. gonktop) so OCR jobs don't go dead at scale.
4. **[human] OSTI bulk-download terms** — email opennet@osti.gov for NV* bulk-research
   terms + rate limits BEFORE the full-series harvest (plan §3 / §6.5).

Also gating autonomous code work (plan §7, "resolve in Cowork"): corpus boundary
(full NV* vs sub-collection), FAISS index sharding vs. gonktop RAM, and mesh-integration
depth for Lane A. The Lane A orchestrator (§6.4) and harvester scaling (§6.5) need the
mesh broker and the OSTI terms reply, respectively, before they can be built+verified
end to end.
