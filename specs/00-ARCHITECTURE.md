# Palimpsest — Architecture Contract (Phase 1)

**This document is the single source of truth for every shared interface.** Task packets (TASK-00 … TASK-10) restate the parts they need, but if a packet ever contradicts this file, THIS FILE WINS. Workers: do not invent fields, paths, ports, or endpoints not defined here.

## 1. System overview

```
                         ┌────────────────────────────────────────────┐
                         │ gonktop (Xeon 128GB, CPU, always-on)       │
                         │                                            │
  OSTI OpenNet ◄─────────┤ harvester.py (cron/manual, rate-limited)   │
                         │                                            │
                         │ broker.py  ── owns ──► palimpsest.db       │
                         │   HTTP :8077            (SQLite, WAL,      │
                         │   /lease /complete       SINGLE WRITER)    │
                         │   /fail /heartbeat                         │
                         │   /enqueue /status      {storage.root}/    │
                         │   /file/{doc_id}.pdf      raw/ ocr/        │
                         │   /ocr/{doc_id}.json                       │
                         │                           features/ index/ │
                         │ indexer.py (FAISS in RAM, gap join)        │
                         │ server.py  ── MCP, HTTP :8078, READ-ONLY   │
                         │ review.py  ── HITL approvals, LOCAL CLI    │
                         └────────────▲───────────────▲───────────────┘
                                      │ HTTP only     │ HTTP only
                         ┌────────────┴─────┐  ┌──────┴────────────┐
                         │ M4 Mini (16GB)   │  │ M5 Pro (24GB)     │
                         │ worker.py daemon │  │ worker.py daemon  │
                         │ caps: ocr, embed,│  │ caps: extract,    │
                         │  classify        │  │  ocr              │
                         │ Ollama qwen2.5:3b│  │ Ollama llama3.1:8b│
                         │ + nomic-embed    │  │ (intermittent —   │
                         │ (always on)      │  │  drains when up)  │
                         └──────────────────┘  └───────────────────┘
```

**Iron rules:**
1. **No process off gonktop ever opens `palimpsest.db`.** Workers (M4/M5) never touch the database or the storage volume directly — HTTP to the broker only. (SQLite over network filesystems corrupts; don't argue, comply.) On gonktop itself, the broker is the primary writer; the harvester, indexer, MCP server, and review CLI may open the same local DB in WAL mode with short transactions.
2. **No finding without two citations.** Enforced by schema (see `gap_candidates`).
3. **No person surfaced without HITL approval.** Enforced by the masking rule (§8).
4. **All tunables live in `config.toml`.** No magic numbers in code.
5. **No finding surfaced for publication below its calibrated precision bar.** Enforced by the trust gate (gate_tier + the min_tier default in server.py; see specs/EVAL-TRUST-GATE.md). Strictly additive to rule 3 — the gate can only withhold or flag a finding, never unmask a person.

## 2. Repository layout

```
~/dev/palimpsest/
  config.toml                  # the only config file
  pyproject.toml               # deps; python >= 3.11
  palimpsest/
    __init__.py
    config.py                  # loads/validates config.toml (TASK-01)
    db.py                      # schema DDL + migrations (TASK-01)
    broker.py                  # job queue HTTP API (TASK-02)
    harvester.py               # OpenNet fetch (TASK-03)
    worker.py                  # node daemon (TASK-04)
    tasks/
      ocr.py                   # (TASK-05)
      features.py              # redaction marks + entities (TASK-06)
      embed.py                 # (TASK-07)
    indexer.py                 # FAISS + gap join (TASK-07)
    server.py                  # MCP server (TASK-08)
    review.py                  # HITL CLI (TASK-09)
  skills/palimpsest-investigator/SKILL.md   # (TASK-09)
  tests/                       # pytest; each task adds its own
  specs/                       # these documents
```

## 3. `config.toml` (canonical; TASK-01 creates it)

```toml
[storage]
root = "/Volumes/palimpsest"          # external SSD on gonktop (Decision F10)

[db]
path = "{storage.root}/db/palimpsest.db"   # config.py expands {storage.root}

[broker]
host = "gonktop.local"
port = 8077
lease_ttl_seconds = 900
heartbeat_seconds = 120
max_attempts = 3

[mcp]
port = 8078

[harvest]
base_url = "https://www.osti.gov/opennet"
rate_limit_rps = 1.0
backoff_initial_s = 5
backoff_max_s = 300
user_agent = "palimpsest-research/0.1 (contact: j.eric.herren@gmail.com)"
accession_prefix = "NV"

[ocr]
engine_preference = ["vision", "tesseract"]
min_confidence = 0.5
rerun_if_osti_text_shorter_than = 200   # chars/page; else trust OSTI layer

[features]
redaction_context_chars = 300
redaction_context_lines = 2
blackbox_min_area_frac = 0.001          # of page area
blackbox_max_area_frac = 0.25
blackbox_darkness_threshold = 60        # 0-255 grayscale mean below = dark

[embed]
model = "nomic-embed-text"              # via Ollama (Decision F11)
dim = 768
chunk_chars = 800
chunk_overlap = 150

[gapjoin]
score_threshold = 0.65
w_cosine = 0.5
w_anchor = 0.3
w_kind = 0.2
topk_embedding_candidates = 50

[models]
extract = "llama3.1:8b"                 # M5
classify = "qwen2.5:3b"                 # M4
keep_alive = "24h"

[nodes]
# capability map; worker.py reads its own hostname's entry
gonktop = []                            # gonktop runs NO worker daemon
m4 = ["ocr", "embed", "classify"]
m5 = ["extract", "ocr"]
```

## 4. Storage layout (all under `storage.root`, written ONLY by broker/gonktop processes)

```
{root}/db/palimpsest.db
{root}/raw/{doc_id}.pdf                 # as downloaded, never modified
{root}/ocr/{doc_id}.json                # per-doc page array, schema §6
{root}/features/{doc_id}.json           # redactions + entities, schema §7
{root}/index/faiss.idx                  # FAISS IndexFlatIP + IDMap
{root}/index/chunk_map.json             # faiss int id -> chunk_id
```

`doc_id` = OSTI document id, digits as string, e.g. `"16007132"`. Used verbatim in filenames, DB keys, and purl URLs.

## 5. Database schema (SQLite, WAL; DDL lives in `db.py`)

```sql
CREATE TABLE documents (
  doc_id        TEXT PRIMARY KEY,
  accession     TEXT,                  -- e.g. 'NV0123456'
  title         TEXT,
  year          INTEGER,
  has_fulltext  INTEGER DEFAULT 0,     -- OSTI-supplied text layer present
  source_url    TEXT,
  local_path    TEXT,                  -- {root}/raw/{doc_id}.pdf
  sha256        TEXT,
  page_count    INTEGER,
  status        TEXT DEFAULT 'cataloged',
    -- cataloged|fetched|ocr_done|features_done|indexed|error
  fetched_at    TEXT, ocr_at TEXT, features_at TEXT, indexed_at TEXT,
  error         TEXT
);

CREATE TABLE pages (
  doc_id     TEXT NOT NULL REFERENCES documents(doc_id),
  page_no    INTEGER NOT NULL,         -- 1-based
  width      REAL, height REAL,        -- PDF points
  ocr_source TEXT,                     -- 'osti'|'vision'|'tesseract'
  text       TEXT,                     -- reading-order plain text
  PRIMARY KEY (doc_id, page_no)
);

CREATE TABLE redactions (
  redaction_id INTEGER PRIMARY KEY,
  doc_id   TEXT NOT NULL, page_no INTEGER NOT NULL,
  kind     TEXT NOT NULL,              -- 'exemption_stamp'|'deleted_text'|'black_box'
  label    TEXT,                       -- e.g. '(b)(1)', 'DELETED'
  x0 REAL, y0 REAL, x1 REAL, y1 REAL,  -- normalized 0-1, TOP-LEFT origin
  context_before TEXT, context_after TEXT,   -- per [features] window config
  FOREIGN KEY (doc_id, page_no) REFERENCES pages(doc_id, page_no)
);

CREATE TABLE entities (
  entity_id INTEGER PRIMARY KEY,
  doc_id   TEXT NOT NULL, page_no INTEGER NOT NULL,
  kind     TEXT NOT NULL,  -- 'person'|'date'|'dosage'|'location'|'org'|'protocol_code'
  text     TEXT NOT NULL,              -- as it appears
  norm     TEXT NOT NULL,              -- normalized form (§7.3)
  char_start INTEGER, char_end INTEGER,    -- offsets into pages.text
  x0 REAL, y0 REAL, x1 REAL, y1 REAL,
  living_status TEXT DEFAULT 'unknown',
    -- 'unknown'|'potentially_living'|'deceased_historical'  (persons only)
  FOREIGN KEY (doc_id, page_no) REFERENCES pages(doc_id, page_no)
);
CREATE INDEX idx_entities_norm ON entities(norm, kind);

CREATE TABLE chunks (
  chunk_id INTEGER PRIMARY KEY,
  doc_id TEXT NOT NULL, page_no INTEGER NOT NULL,
  char_start INTEGER, char_end INTEGER,
  text TEXT NOT NULL
  -- embedding lives in FAISS, keyed by chunk_id
);

CREATE TABLE gap_candidates (
  gap_id        INTEGER PRIMARY KEY,
  redaction_id  INTEGER NOT NULL REFERENCES redactions(redaction_id),
  clear_entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
  score REAL NOT NULL,
  score_cosine REAL, score_anchor REAL, score_kind REAL,  -- components, logged
  method TEXT NOT NULL,                -- 'anchor'|'embedding'|'both'
  status TEXT DEFAULT 'candidate',     -- 'candidate'|'verified'|'rejected'
  reviewed_by TEXT, reviewed_at TEXT, notes TEXT
);
-- PROVENANCE INVARIANT: both FKs NOT NULL. A gap row IS its two citations.

CREATE TABLE jobs (
  job_id    INTEGER PRIMARY KEY,
  type      TEXT NOT NULL,             -- 'ocr'|'features'|'embed'|'extract'
  doc_id    TEXT NOT NULL,
  payload   TEXT DEFAULT '{}',         -- JSON
  state     TEXT DEFAULT 'pending',    -- pending|leased|done|failed|dead
  attempts  INTEGER DEFAULT 0,
  priority  INTEGER DEFAULT 5,         -- lower = sooner
  lease_owner TEXT, lease_expires_at TEXT,
  created_at TEXT, updated_at TEXT, error TEXT,
  UNIQUE (type, doc_id)                -- idempotency key
);

CREATE TABLE review_queue (
  review_id INTEGER PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
  reason TEXT,                         -- e.g. 'person in gap candidate #N'
  status TEXT DEFAULT 'pending',       -- 'pending'|'approved'|'denied'
  decided_by TEXT, decided_at TEXT
);
```

## 6. OCR page JSON (`{root}/ocr/{doc_id}.json`)

Array of page objects:

```json
[{
  "page_no": 1,
  "width": 612.0, "height": 792.0,
  "ocr_source": "vision",
  "lines": [
    {"text": "PROJECT 56 FIELD REPORT", "bbox": [0.12, 0.08, 0.71, 0.10], "conf": 0.97}
  ],
  "text": "PROJECT 56 FIELD REPORT\n..."
}]
```

**Coordinate convention (everywhere in this system):** bbox = `[x0, y0, x1, y1]`, **normalized 0–1, origin TOP-LEFT**, x right, y down. Apple Vision returns bottom-left-origin — converters MUST flip y (`y_norm = 1 - vision_y - vision_h`). Getting this wrong breaks redaction adjacency silently.

## 7. Features JSON (`{root}/features/{doc_id}.json`)

```json
{
  "doc_id": "16007132",
  "redactions": [
    {"page_no": 3, "kind": "exemption_stamp", "label": "(b)(1)",
     "bbox": [0.62, 0.41, 0.70, 0.43],
     "context_before": "...subject received", "context_after": "rem whole body..."}
  ],
  "entities": [
    {"page_no": 3, "kind": "dosage", "text": "15 rem", "norm": "15 rem",
     "char_start": 412, "char_end": 418, "bbox": [0.31, 0.45, 0.38, 0.47]}
  ]
}
```

### 7.3 Entity normalization (`norm`)
- person: lowercase, strip titles (dr/mr/mrs/lt/col/etc.), collapse whitespace, `"Last, First"` → `"first last"`.
- date: ISO `YYYY-MM-DD` where resolvable; else `YYYY-MM` or `YYYY`.
- dosage: `"{number} {unit}"` with unit lowercased from {r, rad, rem, mr, mrem, uci, mci, curie}.
- protocol_code: uppercase, hyphenated: `CAL-12`, `CHI-3`, `HP-9`.
- location/org: lowercase, collapse whitespace.

## 8. HITL masking rule (applies to MCP server and ALL generated reports)

A `person` entity may be rendered by name **only if** `living_status = 'deceased_historical'` **and** an `approved` row exists for it in `review_queue`. Otherwise render as `PERSON-{entity_id:04d}`. No exceptions, no tool parameter to bypass. Approvals only via `python -m palimpsest.review` on gonktop (TASK-09).

## 9. Job lifecycle (broker HTTP API — full spec in TASK-02)

```
enqueue → pending → (lease) → leased → complete → done
                       │            └→ fail(retryable) → pending (attempts+1)
                       │                  attempts ≥ max_attempts → dead
                       └ lease expires (no heartbeat) → pending (attempts+1)
```

Workers are long-lived daemons: register capabilities → poll `/lease` → heartbeat while working → `/complete` with result payload → repeat. Job effects are idempotent: re-running `(type, doc_id)` overwrites the same artifact path and upserts the same rows.

## 10. Build order & dependency graph

```
TASK-00 (ml-pipeline recon) ──┐                    [no code deps; informs all]
TASK-00b (OpenNet probe) ─────┼──► TASK-03 harvester
TASK-01 scaffold/config/db ───┼──► TASK-02 broker ──► TASK-04 worker daemon
                              │                          ├─► TASK-05 ocr
                              │                          ├─► TASK-06 features
                              │                          └─► TASK-07 embed
TASK-07 index + gap join  (needs 05,06 output on a real slice)
TASK-08 MCP server        (needs 07 schema populated)
TASK-09 review CLI + skill (needs 08)
TASK-10 verification run   (needs all; produces the kill-or-scale evidence)
```

Parallelizable: 00, 00b, 01 from the start; 05/06/07-embed once 04 exists.

## 11. Rules for worker models (restated in every packet)

1. Implement ONLY what your packet specifies. Touching other modules = failure.
2. Every path, port, model name, threshold comes from `config.toml` via `palimpsest.config`. Hardcoding = failure.
3. If blocked (login, missing dependency on the host, ambiguity this contract doesn't resolve): STOP that task, write the exact blocker + context to `~/dev/HUMAN_DO_THIS.md`, move to your next task.
4. Run your packet's acceptance tests before reporting done. Paste real output, not claims.
5. Python ≥ 3.11, type hints on public functions, stdlib `logging` (no print), small functions, no cleverness. Boring code is correct code.
6. Network etiquette to OSTI is a hard constraint (TASK-03), not an optimization.
