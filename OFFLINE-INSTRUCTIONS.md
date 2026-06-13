# Palimpsest — Offline Development Handoff

> **Last updated:** 2026-06-13  
> Briefing for local Ollama models continuing work when cloud session limits are hit.  
> MacBook Pro (m4) is the development machine. Broker + production DB live on **gonktop** (192.168.0.58).

---

## 1. Environment

| Machine | Role | URL |
|---------|------|-----|
| MacBook Pro (m4) | Development, local worker | Ollama: http://localhost:11434 |
| gonktop (192.168.0.58) | Broker (8077), DB, primary worker | Ollama: http://192.168.0.58:11434 |

**Always use `uv run`** — never bare `python` or `python3`:
```bash
uv run python -m palimpsest.db migrate      # schema migration
uv run pytest -x -q                         # run tests
uv run ruff check palimpsest/               # lint
uv run ty check palimpsest/<file>.py        # type check
```

**Git:**
```bash
git pull origin main
git push origin main
# Remote: git@github.com:ehurrn/palimpsest.git
# config.toml is gitignored — each host keeps its own, never overwrite
```

---

## 2. Local Ollama Models (MacBook Pro)

| Model | Role |
|-------|------|
| `qwen3.6-heretic-27b:latest` | Lead architect / code generation |
| `gemma-4-12b-it-abliterated:latest` | Instruction-following, code review |
| `granite-4.1-8b-claude-opus-thinking:latest` | Step-by-step planning, debugging |
| `lfm2-8b-qwen3.6-distill:latest` | Fast edits, unit test writing |
| `nomic-embed-text:latest` | Chunk embeddings (pipeline use) |

Check Ollama before starting:
```bash
ollama ps                                          # models in GPU memory
ollama list                                        # all installed
curl -s http://localhost:11434/api/tags | python3 -m json.tool
```

**Known issue:** `llama-server` binary was missing on m4 at some point. If embedding fails, reinstall Ollama (`brew reinstall ollama`) and re-pull `nomic-embed-text`.

---

## 3. Architecture Overview

```
Harvester → Broker (8077, gonktop) → Workers
                │
                └── SQLite DB (gonktop ~/dev/palimpsest/db/)
                        │
                Tasks: ocr → features → embed
                        │
                Indexer: gapjoin / violationjoin / seriesjoin / outcomegap
                        │
                Review CLI (HITL gate)
                        │
                MCP Server (8078, read-only)
```

**Core modules:**
- `palimpsest/broker.py` — FastAPI job queue (enqueue/lease/complete/fail/heartbeat)
- `palimpsest/tasks/features.py` — NER + regex entity extraction
- `palimpsest/indexer.py` — all join/scoring subcommands
- `palimpsest/review.py` — HITL gate (people / gaps / heuristic / audit)
- `palimpsest/server.py` — Read-only MCP server (masks non-approved persons)
- `palimpsest/preflight.py` — 8-check preflight (run before any session)

---

## 4. Current State (as of 2026-06-13)

### Schema version
**v5** is current. Always run `uv run python -m palimpsest.db migrate` before first use.

Tables added in Phase 2:
| Table | Type | Added in |
|-------|------|----------|
| `regulation_citations` | Type e violations | v3 |
| `violation_candidates` | Type e violations | v3 |
| `series_gap_candidates` | Type f series gaps | v4 |
| `outcome_gap_candidates` | Type d outcome gaps | v5 |

### Finding-type implementation status

| Type | Name | Status |
|------|------|--------|
| **a** | Redacted-text corroboration | ✅ Phase 1 complete |
| **b** | Undisclosed radiation dosage | ✅ Complete (`subject_ref` entity, dosage proximity scorer) |
| **c** | Anonymous identity linkage | ❌ **Not started — next task** |
| **d** | Outcome suppression gap | ✅ Complete (`outcome_ref` entity, `outcomegap` CLI, schema v5) |
| **e** | Regulatory-violation citation | ✅ Complete (`reg_cite` entity, `violationjoin` CLI, schema v3) |
| **f** | Document-series suppression | ✅ Complete (`seq_ref` entity, `seriesjoin` CLI, schema v4) |

### Identity gate status
**Enforced.** All person entities default to `potentially_living`. The bulk-approval bypass from Phase 1 has been reverted. No name surfaces without either:
1. Individual HITL approval via `review people` + `approve <id>` setting `living_status='deceased_historical'`, OR
2. The date heuristic: `review heuristic` clears entities where `doc_year - birth_year > 100` OR `doc_age > 75 years`.

### Test suite
**91 tests, all green** as of last push to `main`. Run before and after any changes:
```bash
uv run pytest -x -q
```

---

## 5. Safety Invariants — Never Violate

1. **Provenance invariant**: every de-redaction claim cites `doc_id` + page number for BOTH the redacted source and the corroborating clear-text source. No citation = discard immediately.

2. **Identity gate**: no plaintext person name in any output unless `living_status = 'deceased_historical'` AND `status = 'approved'` in `review_queue`, OR the heuristic subcommand has cleared them.

3. **No direct DB write from workers**: all mutations go through the broker at `192.168.0.58:8077`. Workers never open the SQLite file directly.

4. **Write to WORK-LOG.md**: log a "Starting X" entry when you begin a task and a "Completed X" entry when done. Read it first to avoid duplicating work.

---

## 6. Before Starting Any Session

```bash
# 1. Read the log
cat WORK-LOG.md | tail -30

# 2. Pull latest
git pull origin main

# 3. Check schema
uv run python -m palimpsest.db migrate

# 4. Run tests
uv run pytest -x -q

# 5. Preflight (checks broker, Ollama, spaCy, FAISS)
uv run python -m palimpsest.preflight
```

---

## 7. Next Task: Type c — Anonymous Subject Identity Linkage

This is the **only remaining Phase 2 finding-type**. It is also the highest-risk because it requires the mandatory identity HITL gate.

### Spec summary (full spec in `specs/FINDING-TYPES.md`)

**What it finds:** A subject anonymized in one document (e.g. "Subject 3", "Patient A") who can be linked to a named individual in another document via shared non-identifying attributes: institution + year + role + diagnosis pattern.

**Detector (features.py):**  
`subject_ref` entity kind is already implemented (from Type b). A page qualifies as a type-c candidate if it contains a `subject_ref` entity AND at least 2 of: `org`, `date`, `dosage`.

**Corroboration rule (indexer.py):**  
A second document contains a named `person` entity whose `org` + `date` attributes match the anonymous subject within fuzzy tolerances (same org norm within edit distance 2, same year ± 2). The linkage score formula:

```
score = org_match_score × 0.5 + date_proximity_score × 0.3 + dosage_match_bonus × 0.2
```

Where:
- `org_match_score` = 1.0 if edit_distance(org_norm_a, org_norm_b) ≤ 2, else 0.0
- `date_proximity_score` = max(0, 1 - abs(year_a - year_b) / 3)
- `dosage_match_bonus` = 0.2 if both pages have a matching `dosage` norm, else 0.0

Minimum threshold: `score ≥ 0.65`.

**Identity gate:** Mandatory. The linkage is NEVER surfaced until the named person holds `status = 'approved'` AND `living_status = 'deceased_historical'` in `review_queue`.

### Implementation steps

**Step 1 — New DB table (indexer.py or db.py)**

Add schema migration v6 (if not already present):
```sql
CREATE TABLE IF NOT EXISTS identity_link_candidates (
    id              INTEGER PRIMARY KEY,
    subject_doc_id  TEXT NOT NULL,
    subject_page    INTEGER NOT NULL,
    subject_ref     TEXT NOT NULL,          -- e.g. "Subject 3"
    named_doc_id    TEXT NOT NULL,
    named_page      INTEGER NOT NULL,
    named_entity_id INTEGER,               -- FK to entities.id
    org_match       REAL,
    date_proximity  REAL,
    dosage_bonus    REAL,
    score           REAL,
    status          TEXT DEFAULT 'candidate', -- candidate | verified | rejected
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (named_entity_id) REFERENCES entities(id)
);
```

Bump `EXPECTED_VERSION` in `preflight.py` to 6.

**Step 2 — Scorer (indexer.py)**

Add function `run_identity_link(cfg)`:
```python
def run_identity_link(cfg):
    """Type c: match anonymous subject_ref pages to named person pages by org+date."""
    # 1. Query all pages that have a subject_ref entity AND (org OR date OR dosage)
    # 2. For each candidate page, retrieve its org/date/dosage normalized values
    # 3. Query all pages that have a PERSON entity with approved living_status
    # 4. For each (candidate_page, named_page) pair:
    #    - compute org_match_score (edit distance on org norm)
    #    - compute date_proximity_score
    #    - compute dosage_match_bonus
    #    - if score >= 0.65, insert into identity_link_candidates
    # 5. Deduplicate by (subject_doc_id, subject_page, named_entity_id)
```

Add CLI subcommand `identitylink` (following the pattern of `violationjoin`, `seriesjoin`, `outcomegap`).

**Step 3 — Review gate (review.py)**

Add a `links` subcommand to `review.py` that:
- Lists `identity_link_candidates` where `status = 'candidate'`
- For each, checks that the named entity IS approved+deceased_historical before displaying ANY name
- Allows `approve <id>` / `reject <id>` with a reason

**Step 4 — Tests (tests/test_identity.py)**

Required test cases:
1. `test_org_match_score` — verify edit-distance scoring
2. `test_date_proximity_score` — verify year-gap formula  
3. `test_dosage_bonus` — verify bonus applies only when both pages have matching dosage norms
4. `test_identity_link_below_threshold` — scores < 0.65 are not inserted
5. `test_identity_link_requires_approved_person` — linkage only surfaces when person is approved

**Step 5 — After implementation**
```bash
uv run pytest -x -q          # all tests green
uv run ruff check palimpsest/
git add -p
git commit -m "feat: Type c identity linkage (run_identity_link, schema v6)"
git push origin main
# Update WORK-LOG.md
```

---

## 8. Infrastructure TODOs (lower priority than Type c)

- [ ] Re-enable `embed` capability for `m4` in `config.toml` once Ollama is verified healthy (see Section 2 known issue).
- [ ] Scale harvester to retrieve remaining NV* accession series beyond current 1,000-doc sample.
- [ ] Configure Lane A orchestrator on mesh broker (192.168.0.58:8766) for automated pipeline routing.

---

## 9. Useful Commands Reference

```bash
# Database
uv run python -m palimpsest.db migrate
uv run python -m palimpsest.db stats

# Indexer join subcommands
uv run python -m palimpsest.indexer gapjoin        # Type a
uv run python -m palimpsest.indexer violationjoin  # Type e
uv run python -m palimpsest.indexer seriesjoin     # Type f
uv run python -m palimpsest.indexer outcomegap     # Type d
# uv run python -m palimpsest.indexer identitylink # Type c (to be built)

# Review CLI
uv run python -m palimpsest.review people          # list pending person reviews
uv run python -m palimpsest.review gaps            # list gap candidates
uv run python -m palimpsest.review heuristic       # apply date heuristic to clear old entities
uv run python -m palimpsest.review audit           # audit log

# Preflight
uv run python -m palimpsest.preflight

# Worker (on gonktop)
ssh herren@192.168.0.58 'tail -30 /tmp/palimpsest-worker.log'
ssh herren@192.168.0.58 'curl -s http://localhost:8077/status'

# Stats
uv run python -m palimpsest.indexer stats
```

---

## 10. Key Files

| File | Purpose |
|------|---------|
| `WORK-LOG.md` | Session log — read first, write on start+finish |
| `HUMAN_DO_THIS.md` | Escalations requiring human action |
| `TODO.md` | Prioritized task list |
| `specs/FINDING-TYPES.md` | Full spec for all six finding-types |
| `palimpsest-phase2-plan.md` | Phase 2 roadmap and blockers |
| `config.toml` | Local config (gitignored) |
| `reports/phase1-verification.md` | Phase 1 findings (two verified de-redactions) |
| `deploy/GONKTOP-SETUP.md` | gonktop ops runbook |
