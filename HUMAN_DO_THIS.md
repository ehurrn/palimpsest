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
