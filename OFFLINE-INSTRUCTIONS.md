# Palimpsest — Agent Onboarding & Development Handoff

> **Last updated:** 2026-06-19
> Briefing for any agent (local Ollama, Gemini, Claude) continuing work on this project.
> Read this entire document before touching any code.

---

## 1. What This Project Does

Palimpsest ingests declassified US nuclear test documents from OSTI OpenNet (NV* accession series),
OCRs them, extracts entities, and finds six categories of investigative findings:

- **Type a** — Text redacted in document A can be proven from unredacted text in document B
- **Type b** — A radiation dose is redacted/anonymized but reconstructable from other records
- **Type c** — An anonymous subject can be linked to a named individual via shared attributes
- **Type d** — An experiment has initiation records but no follow-up outcome records (missing results)
- **Type e** — A document cites a regulation but the cited activity appears to violate it
- **Type f** — A document series is missing one or more entries (suppressed installment)

No person's name is surfaced without explicit HITL approval. This is a hard invariant.

---

## 2. Machines & Roles

| Machine | Role | Address |
|---------|------|---------|
| **gonktop** | Broker, SQLite DB, primary worker | 192.168.0.58 |
| **M4** (Mac Mini) | Local worker, development | localhost |
| **M5** (MacBook Pro) | Secondary worker (when available) | SSH required |

**Broker:** `http://192.168.0.58:8077` — sole writer to the DB. Workers never open SQLite directly.
**MCP server (read-only):** `http://192.168.0.58:8078`

Worker capabilities per node (set in `config.toml`):
```
gonktop = ["ocr", "features", "embed", "gapjoin"]
m4      = ["ocr", "features", "embed", "classify"]
m5      = ["extract", "ocr", "features", "embed"]
```

---

## 3. Environment

**Always use `uv run`** — never bare `python` or `python3`:

```bash
uv run python -m palimpsest.db migrate      # schema migration
uv run pytest -x -q                         # run tests
uv run ruff check palimpsest/               # lint (fix all warnings before committing)
uv run ty check palimpsest/<file>.py        # type check (fix errors, no suppression)
```

**Git — work on main, push directly:**
```bash
git pull origin main
git push origin main
# Remote: git@github.com:ehurrn/palimpsest.git
# config.toml is gitignored — each host keeps its own copy, never overwrite
```

**Before any merge from a worktree:** run `git status` first. agy (Gemini) leaves
uncommitted changes on the main disk. Stash before merging, pop after.

---

## 4. Before Starting Any Session

```bash
# 1. Read the log — check what's been done and claimed
cat WORK-LOG.md | tail -40

# 2. Log that you're starting
echo "- Starting <task>: <description>" >> WORK-LOG.md

# 3. Pull latest
git pull origin main

# 4. Migrate schema
uv run python -m palimpsest.db migrate

# 5. Run tests (must be green before you touch anything)
uv run pytest -x -q

# 6. Preflight (checks broker, Ollama, spaCy, FAISS index)
uv run python -m palimpsest.preflight
```

When you finish: log a "Completed X" entry to `WORK-LOG.md`, commit, and push.

---

## 5. Current State (as of 2026-06-19)

### Schema version
**v6** is current. Run `uv run python -m palimpsest.db migrate` before first use.

| Version | Table added | Finding type |
|---------|-------------|--------------|
| v3 | `regulation_citations`, `violation_candidates` | Type e |
| v4 | `series_gap_candidates` | Type f |
| v5 | `outcome_gap_candidates` | Type d |
| v6 | `identity_link_candidates` | Type c |

### Finding-type implementation status — ALL SIX COMPLETE

| Type | Name | Status |
|------|------|--------|
| **a** | Redacted-text corroboration | ✅ Phase 1 |
| **b** | Undisclosed radiation dosage | ✅ Phase 2 |
| **c** | Anonymous identity linkage | ✅ Phase 2 |
| **d** | Outcome suppression gap | ✅ Phase 2 |
| **e** | Regulatory-violation citation | ✅ Phase 2 |
| **f** | Document-series suppression | ✅ Phase 2 |

### Test suite
**101 tests, all green** as of last push. Run before and after any changes.

### Corpus ingestion (in progress as of 2026-06-19)
The harvester is actively downloading the full NV* catalog from OSTI (87,000+ docs discovered,
ingesting in batches). Workers on gonktop and M4 are processing OCR → features → embed
continuously. The post-processing indexer pipeline (`violationjoin → build → gapjoin →
seriesjoin → outcomegap → identitylink`) runs after each full queue drain.

---

## 6. Pipeline Flow

```
OSTI OpenNet
     │
     ▼
palimpsest.harvester catalog     ← pages all NV* accession docs into documents table
palimpsest.harvester fetch       ← downloads PDFs, enqueues ocr jobs via broker
     │
     ▼  (broker at 192.168.0.58:8077)
Workers: ocr → features → embed
     │
     ▼
palimpsest.indexer violationjoin ← Type e: regulation citations vs. activity dates
palimpsest.indexer build         ← rebuilds FAISS index from all embedded chunks
palimpsest.indexer gapjoin       ← Type a: semantic search for corroborating text
palimpsest.indexer seriesjoin    ← Type f: detect missing series installments
palimpsest.indexer outcomegap    ← Type d: detect missing outcome reports
palimpsest.indexer identitylink  ← Type c: link anonymous subjects to named persons
     │
     ▼
palimpsest.review people/gaps/links  ← HITL gate (human approves before surfacing)
     │
     ▼
palimpsest.server (port 8078)        ← read-only MCP server for investigators
```

---

## 7. Core Modules

| File | Purpose |
|------|---------|
| `palimpsest/broker.py` | FastAPI job queue: enqueue / lease / complete / fail / heartbeat |
| `palimpsest/harvester.py` | CLI: catalog (scrape OSTI) + fetch (download PDFs) |
| `palimpsest/worker.py` | Worker daemon: lease-execute loop with heartbeat + SIGTERM |
| `palimpsest/tasks/ocr.py` | OCR: Vision → Tesseract fallback, confidence filter |
| `palimpsest/tasks/features.py` | NER + 8 custom entity kinds (person, date, dosage, protocol_code, reg_cite, seq_ref, subject_ref, outcome_ref) |
| `palimpsest/tasks/embed.py` | Chunk text → nomic-embed-text vectors → broker store |
| `palimpsest/indexer.py` | All join/scoring subcommands + FAISS index build |
| `palimpsest/review.py` | HITL gate: `people`, `gaps`, `links`, `heuristic`, `audit` |
| `palimpsest/server.py` | Read-only MCP server, masks unapproved persons |
| `palimpsest/preflight.py` | 8-check preflight: config, storage, DB, broker, worker, Ollama, spaCy, FAISS |
| `palimpsest/db.py` | Schema migrations, WAL mode, FK enforcement |
| `palimpsest/config.py` | Config loader (config.toml, gitignored per host) |

---

## 8. Entity Kinds

All extracted by `palimpsest/tasks/features.py`:

| Kind | Normalizer | Notes |
|------|-----------|-------|
| `person` | lowercase stripped | Gate: `potentially_living` by default |
| `date` | YYYY-MM-DD | Parsed from many formats |
| `dosage` | `<value> <unit>` | e.g. `1.2 rem` |
| `protocol_code` | uppercase | e.g. `CAL-12`, `NV-032` |
| `reg_cite` | uppercase stripped | e.g. `45 CFR 46`, `10 CFR 50` |
| `seq_ref` | lowercase | Series references, e.g. `vol. 3`, `part ii` |
| `subject_ref` | lowercase | Anonymized subjects: `subject 3`, `patient a` |
| `outcome_ref` | `future_ref:<text>` or `outcome_ind:<text>` | `future_ref:` = expected-but-absent outcome |

---

## 9. Safety Invariants — Never Violate

1. **Provenance invariant:** every de-redaction claim cites `doc_id` + page number for BOTH
   the redacted source and the corroborating clear-text source. No citation = discard.

2. **Identity gate:** no plaintext person name in any output unless `living_status = 'deceased_historical'`
   AND `status = 'approved'` in `review_queue`, OR the heuristic subcommand has cleared them.
   The gate applies in the MCP server, review CLI, and any output you generate.

3. **No direct DB writes from workers:** all mutations go through the broker at `192.168.0.58:8077`.

4. **WORK-LOG.md:** log "Starting X" when you begin, "Completed X" when done. Read it first.
   agy (Gemini) also writes to this log — check before claiming a task.

5. **`config.toml` is gitignored** — each host has its own. Never commit it. Never overwrite
   another host's copy.

---

## 10. Key Config Values (config.toml)

```toml
[harvest]
rate_limit_rps = 0.5          # OSTI download rate — do not increase
accession_prefix = "NV"       # NV* series

[gapjoin]
score_threshold = 0.65        # minimum score for a gap candidate
w_cosine = 0.5
w_anchor = 0.3
w_kind   = 0.2

[embed]
model = "nomic-embed-text"
dim = 768
chunk_chars = 800
chunk_overlap = 150
```

All tunables live in config.toml. Do not hardcode thresholds in Python.

---

## 11. Useful Commands

```bash
# Database
uv run python -m palimpsest.db migrate
uv run python -m palimpsest.db stats

# Harvester
uv run python -m palimpsest.harvester catalog          # scrape OSTI catalog
uv run python -m palimpsest.harvester fetch --limit 50 # download PDFs, enqueue OCR
uv run python -m palimpsest.harvester status           # doc counts by status

# Worker (start one per machine)
uv run python -m palimpsest.worker --node m4           # or m5, gonktop

# Indexer (run on gonktop after queue drains)
uv run python -m palimpsest.indexer violationjoin
uv run python -m palimpsest.indexer build
uv run python -m palimpsest.indexer gapjoin
uv run python -m palimpsest.indexer seriesjoin
uv run python -m palimpsest.indexer outcomegap
uv run python -m palimpsest.indexer identitylink
uv run python -m palimpsest.indexer stats

# Review CLI (HITL)
uv run python -m palimpsest.review people              # list pending person reviews
uv run python -m palimpsest.review gaps                # list gap candidates
uv run python -m palimpsest.review links               # list identity link candidates
uv run python -m palimpsest.review heuristic           # apply date heuristic
uv run python -m palimpsest.review audit               # audit log

# Preflight
uv run python -m palimpsest.preflight

# Check broker from anywhere
curl -s http://192.168.0.58:8077/status | python3 -m json.tool

# Check logs on gonktop
ssh herren@192.168.0.58 'tail -30 /tmp/palimpsest-worker-gonktop.log'
ssh herren@192.168.0.58 'tail -30 /tmp/harvest-fetch3.log'
ssh herren@192.168.0.58 'tail -30 /tmp/postproc.log'
```

---

## 12. Key Files

| File | Purpose |
|------|---------|
| `WORK-LOG.md` | Session log — read first, write on start + finish |
| `HUMAN_DO_THIS.md` | Escalations requiring human action |
| `specs/FINDING-TYPES.md` | Full spec for all six finding-types |
| `specs/00-ARCHITECTURE.md` | System architecture decisions |
| `deploy/GONKTOP-SETUP.md` | gonktop ops runbook |
| `reports/phase1-verification.md` | Phase 1 findings (two verified de-redactions) |
| `config.toml` | Local config (gitignored per host) |

---

## 13. What's Next

Phase 2 implementation is complete. Ongoing work:

1. **Corpus ingestion** — harvester is downloading the full NV* catalog (87k+ docs). Let it run.
   The post-processing indexer pipeline fires automatically after each queue drain.

2. **Review findings** — once ingestion stabilizes, run the review CLI to work through
   gap candidates, violation candidates, series gaps, outcome gaps, and identity links.
   Each finding needs human verification before it can be published.

3. **Scheduled harvesting** — set up a periodic launchd/cron job on gonktop to run
   `catalog` daily and `fetch --limit N` every few hours to pick up new OSTI releases.
