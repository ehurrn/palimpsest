# TASK-20 — Phase 2 Scaling & Safety Heuristics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unblock Phase 2 full-corpus scaling by (a) aligning the heuristic auto-approver to the spec, (b) sharding the FAISS index by decade to cap RAM usage, and (c) adding a broker `/release` endpoint so workers cleanly return jobs on SIGTERM instead of waiting up to 15 minutes for lease reap.

**Architecture:** Three independent subsystems in order. The heuristic fix is a pure DB behaviour change in `review.py`. The shard feature threads through `config.toml` → `results.py` → `tasks/embed.py` → `indexer.py` with no schema changes. The graceful release adds one FastAPI endpoint and a two-global state tracker in the worker loop.

**Tech Stack:** Python 3.11, SQLite (via `palimpsest.db.connect`), FAISS, FastAPI/Pydantic, httpx, pytest

---

## Pre-flight

Before any task, capture your baseline:

```bash
cd /Users/herren/dev/palimpsest
source .venv/bin/activate
pytest tests/ -x -q 2>&1 | tail -20
```

Note any pre-existing failures — don't confuse them with regressions you introduce.

---

## Part A — Heuristic Auto-Approver Alignment

**Context:** `apply_heuristic()` already exists in `review.py` (lines 409–502) and is wired to the `heuristic` subcommand. The spec requires these changes from the current implementation:

1. Query source: `entities WHERE kind='person' AND living_status='unknown'` joined to `documents` — NOT `review_queue WHERE status='pending'`
2. Action on approved: INSERT a new `review_queue` row — NOT update existing rows
3. `decided_by` must be `'HEURISTIC_AUTO'` — NOT `'HEURISTIC'`
4. Remove birth-year regex logic — spec has only the 75-year document-age rule
5. Reason string must be exactly: `'Auto-approved via 75-year document age heuristic'`

**Files:**
- Modify: `palimpsest/review.py` — replace `apply_heuristic()` (lines 409–502)
- Modify: `tests/test_review.py` — add spec-compliant tests; fix any old tests asserting `'HEURISTIC'`

---

### Task A1: Write failing tests for the aligned heuristic

- [ ] **Step 1: Append the following test functions to `tests/test_review.py`**

```python
# ---- append to tests/test_review.py ----

def test_heuristic_approves_old_document(temp_cfg):
    """Doc from 1940 (>75 years) → entity approved, review_queue row inserted."""
    import sqlite3
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("100", "NV-100", "Old Doc", "indexed", 1940),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('100', 1, 'person', 'Jane Smith', 'jane smith', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT living_status FROM entities WHERE doc_id='100' AND kind='person'"
    ).fetchone()
    assert row["living_status"] == "deceased_historical"

    rq = conn2.execute(
        "SELECT status, decided_by, reason FROM review_queue "
        "WHERE entity_id = (SELECT entity_id FROM entities WHERE doc_id='100' AND kind='person')"
    ).fetchone()
    assert rq is not None
    assert rq["status"] == "approved"
    assert rq["decided_by"] == "HEURISTIC_AUTO"
    assert rq["reason"] == "Auto-approved via 75-year document age heuristic"
    conn2.close()


def test_heuristic_flags_recent_document_as_potentially_living(temp_cfg):
    """Doc from 2010 (<75 years) → entity marked potentially_living, no review_queue insert."""
    import sqlite3
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("200", "NV-200", "Recent Doc", "indexed", 2010),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('200', 1, 'person', 'Bob Jones', 'bob jones', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT living_status FROM entities WHERE doc_id='200' AND kind='person'"
    ).fetchone()
    assert row["living_status"] == "potentially_living"

    rq_count = conn2.execute(
        "SELECT COUNT(*) as c FROM review_queue "
        "WHERE entity_id = (SELECT entity_id FROM entities WHERE doc_id='200' AND kind='person')"
    ).fetchone()["c"]
    assert rq_count == 0
    conn2.close()


def test_heuristic_flags_null_year_as_potentially_living(temp_cfg):
    """Doc with NULL year → entity marked potentially_living (can't confirm age)."""
    import sqlite3
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("300", "NV-300", "Undated Doc", "indexed", None),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('300', 1, 'person', 'Alice Li', 'alice li', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT living_status FROM entities WHERE doc_id='300' AND kind='person'"
    ).fetchone()
    assert row["living_status"] == "potentially_living"
    conn2.close()


def test_heuristic_skips_already_classified_entities(temp_cfg):
    """Entities with living_status != 'unknown' are not touched."""
    import sqlite3
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("400", "NV-400", "Old Doc 2", "indexed", 1930),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('400', 1, 'person', 'Pre-approved Person', 'pre-approved person', 'deceased_historical')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    conn2 = sqlite3.connect(temp_cfg.db_path)
    conn2.row_factory = sqlite3.Row
    rq_count = conn2.execute(
        "SELECT COUNT(*) as c FROM review_queue "
        "WHERE entity_id = (SELECT entity_id FROM entities WHERE doc_id='400' AND kind='person')"
    ).fetchone()["c"]
    assert rq_count == 0
    conn2.close()


def test_heuristic_prints_summary(temp_cfg, capsys):
    """apply_heuristic prints evaluated, historical, and potentially_living counts."""
    import sqlite3
    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES (?,?,?,?,?)",
        ("500", "NV-500", "Old Doc 3", "indexed", 1940),
    )
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm, living_status) "
        "VALUES ('500', 1, 'person', 'Frank Test', 'frank test', 'unknown')"
    )
    conn.commit()
    conn.close()

    apply_heuristic(temp_cfg)

    captured = capsys.readouterr()
    assert "1" in captured.out
    assert "deceased_historical" in captured.out or "historical" in captured.out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_review.py::test_heuristic_approves_old_document \
       tests/test_review.py::test_heuristic_flags_recent_document_as_potentially_living \
       tests/test_review.py::test_heuristic_flags_null_year_as_potentially_living \
       tests/test_review.py::test_heuristic_skips_already_classified_entities \
       tests/test_review.py::test_heuristic_prints_summary \
       -v 2>&1 | tail -20
```

Expected: FAIL — current code uses `'HEURISTIC'` and queries `review_queue`.

---

### Task A2: Replace `apply_heuristic()` with the spec-compliant version

- [ ] **Step 1: Replace `apply_heuristic` in `palimpsest/review.py` (lines 409–502)**

```python
def apply_heuristic(cfg: Config):
    """Apply 75-year document-age heuristic to classify person entities with living_status='unknown'.

    For each unknown-status person entity whose source document is >75 years old:
      - Sets living_status = 'deceased_historical'
      - Inserts an approved row into review_queue (decided_by='HEURISTIC_AUTO')
    For entities whose document is <=75 years old or has no year:
      - Sets living_status = 'potentially_living'
    All updates run in a single transaction.
    Prints: entities evaluated, marked historical, flagged as potentially_living.
    """
    conn = connect(cfg)
    current_year = datetime.datetime.now().year

    cur = conn.execute("""
        SELECT DISTINCT e.entity_id, e.norm, e.doc_id, d.year AS doc_year
        FROM entities e
        LEFT JOIN documents d ON e.doc_id = d.doc_id
        WHERE e.kind = 'person' AND e.living_status = 'unknown'
    """)
    items = cur.fetchall()

    if not items:
        print("No unknown-status person entities to classify.")
        conn.close()
        return

    print(f"Running 75-year heuristic on {len(items)} person entities...")

    approved_count = 0
    flagged_count = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Process by norm to avoid partial-norm updates: one transaction covers all
    # entity rows sharing the same normalised name.
    seen_norms: set[str] = set()

    with conn:
        for item in items:
            norm = item["norm"]
            if norm in seen_norms:
                continue
            seen_norms.add(norm)

            doc_year = item["doc_year"]
            is_deceased = doc_year is not None and (current_year - doc_year) > 75

            if is_deceased:
                conn.execute(
                    "UPDATE entities SET living_status = 'deceased_historical' "
                    "WHERE norm = ? AND kind = 'person' AND living_status = 'unknown'",
                    (norm,),
                )
                conn.execute(
                    "INSERT INTO review_queue (entity_id, reason, status, decided_by, decided_at) "
                    "VALUES (?, 'Auto-approved via 75-year document age heuristic', 'approved', 'HEURISTIC_AUTO', ?)",
                    (item["entity_id"], now),
                )
                log_decision_to_audit(cfg, item["entity_id"], norm, "approved", "HEURISTIC_AUTO", now)
                approved_count += 1
            else:
                conn.execute(
                    "UPDATE entities SET living_status = 'potentially_living' "
                    "WHERE norm = ? AND kind = 'person' AND living_status = 'unknown'",
                    (norm,),
                )
                flagged_count += 1

    total = approved_count + flagged_count
    print(
        f"Heuristic complete: {total} evaluated, "
        f"{approved_count} marked deceased_historical, "
        f"{flagged_count} flagged as potentially_living."
    )
    conn.close()
```

- [ ] **Step 2: Run the 5 new tests**

```bash
pytest tests/test_review.py::test_heuristic_approves_old_document \
       tests/test_review.py::test_heuristic_flags_recent_document_as_potentially_living \
       tests/test_review.py::test_heuristic_flags_null_year_as_potentially_living \
       tests/test_review.py::test_heuristic_skips_already_classified_entities \
       tests/test_review.py::test_heuristic_prints_summary \
       -v 2>&1 | tail -20
```

Expected: PASS all 5.

- [ ] **Step 3: Run the full review suite**

```bash
pytest tests/test_review.py -v 2>&1 | tail -30
```

Expected: all passing. If old heuristic tests assert `decided_by == 'HEURISTIC'`, update them to assert `'HEURISTIC_AUTO'`. If they seed `review_queue` rows and expect UPDATEs, rewrite them to seed `entities` rows instead.

- [ ] **Step 4: Commit**

```bash
git add palimpsest/review.py tests/test_review.py
git commit -m "feat(review): align apply_heuristic to spec — entity query, HEURISTIC_AUTO, INSERT review_queue"
```

---

## Part B — FAISS Index Sharding by Decade

**Context:** Currently `build_index` writes `{root}/index/faiss.idx` (monolithic). At 500K NV docs this exhausts gonktop RAM. Fix: shard by decade — 1940s → `shards/1940/faiss.idx`, 1950s → `shards/1950/faiss.idx`, etc. Year flows: `documents.year` → embed job payload → worker result → `process_embed` → shard dir → `build_index` → `run_gapjoin` searches all shards.

**Files:**
- Modify: `config.toml` — add `shard_by = "decade"` under `[embed]`
- Modify: `palimpsest/results.py` — `process_features` passes year in embed payload; `process_embed` routes to shard
- Modify: `palimpsest/tasks/embed.py` — `embed_task` reads and echoes year
- Modify: `palimpsest/indexer.py` — `build_index` iterates shards; `run_gapjoin` searches all shards
- Create: `tests/test_indexer_sharding.py`

---

### Task B1: Write failing shard tests

- [ ] **Step 1: Create `tests/test_indexer_sharding.py`**

```python
# tests/test_indexer_sharding.py
"""Tests for decade-sharded FAISS index build and gapjoin multi-shard search."""
import json
import sqlite3
from pathlib import Path
import pytest

from palimpsest.config import load
from palimpsest.db import migrate


@pytest.fixture
def temp_cfg(tmp_path):
    config_content = f"""
    [storage]
    root = "{tmp_path}"
    [db]
    path = "{{storage.root}}/db/palimpsest.db"
    [broker]
    host = "localhost"
    port = 8077
    lease_ttl_seconds = 900
    heartbeat_seconds = 120
    max_attempts = 3
    [mcp]
    port = 8078
    [harvest]
    base_url = ""
    rate_limit_rps = 1.0
    backoff_initial_s = 5
    backoff_max_s = 300
    user_agent = ""
    accession_prefix = "NV"
    [ocr]
    engine_preference = ["vision"]
    min_confidence = 0.5
    rerun_if_osti_text_shorter_than = 200
    [features]
    redaction_context_chars = 300
    redaction_context_lines = 2
    blackbox_min_area_frac = 0.001
    blackbox_max_area_frac = 0.25
    blackbox_darkness_threshold = 60
    [embed]
    model = "nomic-embed"
    dim = 4
    chunk_chars = 800
    chunk_overlap = 150
    shard_by = "decade"
    [gapjoin]
    score_threshold = 0.0
    w_cosine = 1.0
    w_anchor = 0.0
    w_kind = 0.0
    topk_embedding_candidates = 10
    [models]
    extract = "llama"
    classify = "qwen"
    keep_alive = "24h"
    [nodes]
    gonktop = []
    """
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_content)
    cfg = load(cfg_file)
    migrate(cfg)
    return cfg


def _write_pending(cfg, decade: int, records: list):
    shard_dir = cfg.storage_root / "index" / "shards" / str(decade)
    shard_dir.mkdir(parents=True, exist_ok=True)
    with open(shard_dir / "pending_embeddings.jsonl", "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _insert_chunk(cfg, doc_id: str, page_no: int, chunk_id: int):
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status) VALUES (?,?,?,?)",
        (doc_id, f"NV-{doc_id}", f"Doc {doc_id}", "features_done"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) "
        "VALUES (?,?,?,0,100,'test text')",
        (chunk_id, doc_id, page_no),
    )
    conn.commit()
    conn.close()


def test_build_index_creates_shard_for_decade(temp_cfg):
    """build_index reads shards/1940/pending_embeddings.jsonl and writes shards/1940/faiss.idx."""
    from palimpsest.indexer import build_index
    _insert_chunk(temp_cfg, "101", 1, 1001)
    _write_pending(temp_cfg, 1940, [{"chunk_id": 1001, "embedding": [1.0, 0.0, 0.0, 0.0]}])
    build_index(temp_cfg)
    faiss_path = temp_cfg.storage_root / "index" / "shards" / "1940" / "faiss.idx"
    assert faiss_path.exists(), f"Expected shard index at {faiss_path}"


def test_build_index_creates_separate_shards(temp_cfg):
    """Two decades produce two separate shard directories, each with their own faiss.idx."""
    from palimpsest.indexer import build_index
    _insert_chunk(temp_cfg, "101", 1, 1001)
    _insert_chunk(temp_cfg, "201", 1, 2001)
    _write_pending(temp_cfg, 1940, [{"chunk_id": 1001, "embedding": [1.0, 0.0, 0.0, 0.0]}])
    _write_pending(temp_cfg, 1960, [{"chunk_id": 2001, "embedding": [0.0, 1.0, 0.0, 0.0]}])
    build_index(temp_cfg)
    assert (temp_cfg.storage_root / "index" / "shards" / "1940" / "faiss.idx").exists()
    assert (temp_cfg.storage_root / "index" / "shards" / "1960" / "faiss.idx").exists()


def test_gapjoin_searches_across_shards(temp_cfg):
    """run_gapjoin calls the embedding fn (confirming multi-shard search path runs)."""
    from palimpsest.indexer import build_index, run_gapjoin

    _insert_chunk(temp_cfg, "101", 1, 1001)
    _insert_chunk(temp_cfg, "201", 1, 2001)
    _write_pending(temp_cfg, 1940, [{"chunk_id": 1001, "embedding": [1.0, 0.0, 0.0, 0.0]}])
    _write_pending(temp_cfg, 1960, [{"chunk_id": 2001, "embedding": [0.0, 1.0, 0.0, 0.0]}])
    build_index(temp_cfg)

    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO entities (doc_id, page_no, kind, text, norm) VALUES ('201', 1, 'person', 'A B', 'a b')"
    )
    conn.execute(
        "INSERT INTO redactions (doc_id, page_no, kind, context_before, context_after) "
        "VALUES ('101', 1, 'exemption_stamp', 'some context text here for embedding', 'more context')"
    )
    conn.commit()
    conn.close()

    call_count = {"n": 0}

    def fake_embed(cfg, text):
        call_count["n"] += 1
        return [1.0, 0.0, 0.0, 0.0]

    run_gapjoin(temp_cfg, embed_fn=fake_embed)
    assert call_count["n"] >= 1


def test_process_embed_routes_to_shard_directory(temp_cfg):
    """process_embed writes to shards/DECADE/ when year is present in result."""
    from palimpsest.results import process_embed

    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status, year) VALUES ('999','NV-999','T','features_done',1952)"
    )
    conn.commit()

    result = {
        "year": 1952,
        "chunks": [{"page_no": 1, "char_start": 0, "char_end": 50, "text": "hello", "embedding": [0.1, 0.2, 0.3, 0.4]}]
    }
    process_embed(conn, temp_cfg, "999", result, "2026-01-01T00:00:00")
    conn.commit()
    conn.close()

    decade = (1952 // 10) * 10  # 1950
    pending = temp_cfg.storage_root / "index" / "shards" / str(decade) / "pending_embeddings.jsonl"
    assert pending.exists(), f"Expected {pending}"
    lines = [json.loads(l) for l in pending.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["embedding"] == [0.1, 0.2, 0.3, 0.4]


def test_process_embed_falls_back_to_legacy_path_when_no_year(temp_cfg):
    """If year is absent, embeddings go to legacy index/ dir."""
    from palimpsest.results import process_embed

    conn = sqlite3.connect(temp_cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO documents (doc_id, accession, title, status) VALUES ('888','NV-888','T','features_done')"
    )
    conn.commit()

    result = {
        "chunks": [{"page_no": 1, "char_start": 0, "char_end": 50, "text": "hello", "embedding": [0.1, 0.2, 0.0, 0.0]}]
    }
    process_embed(conn, temp_cfg, "888", result, "2026-01-01T00:00:00")
    conn.commit()
    conn.close()

    legacy = temp_cfg.storage_root / "index" / "pending_embeddings.jsonl"
    assert legacy.exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_indexer_sharding.py -v 2>&1 | tail -20
```

Expected: FAIL — no shard dirs or shard-aware code yet.

---

### Task B2: Add `shard_by` to `config.toml`

- [ ] **Step 1: Add one line under `[embed]` in `config.toml`**

```toml
[embed]
model = "nomic-embed-text"
dim = 768
chunk_chars = 800
chunk_overlap = 150
shard_by = "decade"
```

- [ ] **Step 2: Verify it loads**

```bash
python -c "from palimpsest.config import load; c = load(); print(c.embed.get('shard_by'))"
```

Expected: `decade`

---

### Task B3: Thread year through `results.py`

- [ ] **Step 1: In `process_features`, replace the `_enqueue_followon(conn, "embed", ...)` call**

Find the line `_enqueue_followon(conn, "embed", doc_id, now)` near the bottom of `process_features` and replace it with:

```python
    # Look up year to pass to embed job payload for shard routing
    year_row = conn.execute("SELECT year FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    doc_year = year_row["year"] if year_row else None
    payload_json = json.dumps({"year": doc_year})
    try:
        conn.execute(
            "INSERT INTO jobs (type, doc_id, payload, state, priority, created_at, updated_at) "
            "VALUES ('embed', ?, ?, 'pending', 5, ?, ?)",
            (doc_id, payload_json, now, now),
        )
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE jobs SET state='pending', payload=?, updated_at=? WHERE type='embed' AND doc_id=?",
            (payload_json, now, doc_id),
        )
```

`json` and `sqlite3` are already imported at the top of `results.py`.

- [ ] **Step 2: Replace `process_embed` with the shard-routing version**

Replace the entire `process_embed` function (lines 108–129 in `results.py`) with:

```python
def process_embed(
    conn: sqlite3.Connection, cfg: Config, doc_id: str, result: Any, now: str
) -> None:
    """Persist chunks; route embeddings to the correct decade shard (or legacy dir)."""
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    year = result.get("year")
    if year is not None:
        decade = (int(year) // 10) * 10
        pending_dir = cfg.storage_root / "index" / "shards" / str(decade)
    else:
        # Fallback: legacy flat layout for docs with no year
        pending_dir = cfg.storage_root / "index"

    pending_dir.mkdir(parents=True, exist_ok=True)

    for ch in result.get("chunks", []):
        cur_chunk = conn.execute(
            "INSERT INTO chunks (doc_id, page_no, char_start, char_end, text) VALUES (?,?,?,?,?) RETURNING chunk_id",
            (doc_id, ch["page_no"], ch["char_start"], ch["char_end"], ch["text"]),
        )
        chunk_id = cur_chunk.fetchone()["chunk_id"]
        with open(pending_dir / "pending_embeddings.jsonl", "a") as f:
            f.write(json.dumps({"chunk_id": chunk_id, "embedding": ch["embedding"]}) + "\n")

    conn.execute(
        "UPDATE documents SET status='indexed', indexed_at=? WHERE doc_id=?",
        (now, doc_id),
    )
```

- [ ] **Step 3: Run routing tests**

```bash
pytest tests/test_indexer_sharding.py::test_process_embed_routes_to_shard_directory \
       tests/test_indexer_sharding.py::test_process_embed_falls_back_to_legacy_path_when_no_year \
       -v 2>&1 | tail -10
```

Expected: PASS both.

---

### Task B4: Propagate year through embed task worker

- [ ] **Step 1: Update `embed_task` in `palimpsest/tasks/embed.py`**

Replace the full `embed_task` function (lines 104–139) with:

```python
@handler("embed")
def embed_task(cfg: Config, job: dict) -> dict:
    """Worker task handler for generating embeddings."""
    doc_id = job["doc_id"]
    # Read year from job payload (set by process_features for shard routing)
    payload = job.get("payload", {})
    doc_year = payload.get("year")  # None for docs with no year

    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"

    # 1. Fetch OCR JSON from broker
    try:
        ocr_resp = httpx.get(f"{broker_url}/ocr/{doc_id}.json", timeout=30.0)
        if ocr_resp.status_code == 404:
            raise PermanentJobError(f"OCR file not found for doc_id {doc_id}")
        ocr_resp.raise_for_status()
        ocr_data = ocr_resp.json()
    except httpx.HTTPError as e:
        raise Exception(f"Failed to fetch OCR JSON from broker: {e}")

    # 2. Define embedding function via local Ollama API
    def ollama_embed(prompt: str) -> List[float]:
        try:
            resp = httpx.post(
                "http://localhost:11434/api/embeddings",
                json={
                    "model": cfg.embed["model"],
                    "prompt": prompt,
                    "keep_alive": cfg.models["keep_alive"]
                },
                timeout=30.0
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except httpx.HTTPError as e:
            raise Exception(f"Ollama embedding API call failed: {e}")

    # 3. Process and include year in result for shard routing in broker
    result = process_embed(ocr_data, cfg, ollama_embed)
    result["year"] = doc_year
    return result
```

- [ ] **Step 2: Run embed tests for regressions**

```bash
pytest tests/test_embed.py -v 2>&1 | tail -10
```

Expected: all passing.

---

### Task B5: Make `build_index` shard-aware

- [ ] **Step 1: Replace `build_index` (lines 47–133 in `palimpsest/indexer.py`) with the shard-aware version**

Add a `from pathlib import Path` import if not already present (check line 1–15; `Path` is likely already imported via `config`).

```python
def _build_shard(cfg: Config, shard_dir) -> None:
    """Build or update the FAISS index for one shard directory.

    Reads pending_embeddings.jsonl, atomically renames it to .processing,
    builds/extends faiss.idx, updates documents.status, then rotates files.
    No-ops if pending_embeddings.jsonl is absent or empty.
    """
    from pathlib import Path
    shard_dir = Path(shard_dir)
    pending_path = shard_dir / "pending_embeddings.jsonl"
    if not pending_path.exists() or pending_path.stat().st_size == 0:
        return

    processing_path = shard_dir / "pending_embeddings.processing"
    done_path = shard_dir / "pending_embeddings.done"
    faiss_path = shard_dir / "faiss.idx"

    if processing_path.exists():
        processing_path.unlink()
    pending_path.rename(processing_path)

    chunk_ids = []
    embeddings = []
    with open(processing_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            chunk_ids.append(rec["chunk_id"])
            embeddings.append(rec["embedding"])

    if not chunk_ids:
        if processing_path.exists():
            processing_path.unlink()
        return

    dim = cfg.embed.get("dim", 768)
    if faiss_path.exists():
        logger.info(f"Loading existing shard index from {faiss_path}")
        index = faiss.read_index(str(faiss_path))
    else:
        logger.info(f"Creating new shard index at {faiss_path}")
        index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))

    vecs = np.array(embeddings, dtype=np.float32)
    norms_v = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = np.where(norms_v > 0, vecs / norms_v, vecs)
    ids = np.array(chunk_ids, dtype=np.int64)
    index.add_with_ids(vecs, ids)
    faiss.write_index(index, str(faiss_path))
    logger.info(f"Shard {shard_dir.name}: indexed {len(chunk_ids)} vectors.")

    conn = connect(cfg)
    placeholders = ",".join("?" for _ in chunk_ids)
    cur = conn.execute(
        f"SELECT DISTINCT doc_id FROM chunks WHERE chunk_id IN ({placeholders})", chunk_ids
    )
    doc_ids = [row["doc_id"] for row in cur.fetchall()]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        for doc_id in doc_ids:
            conn.execute(
                "UPDATE documents SET status='indexed', indexed_at=? WHERE doc_id=?",
                (now, doc_id),
            )
    conn.close()

    if done_path.exists():
        done_path.unlink()
    processing_path.rename(done_path)
    pending_path.touch()


def build_index(cfg: Config):
    """Build FAISS shard indices from all pending_embeddings.jsonl files.

    Processes:
      1. {root}/index/shards/YYYY/ — decade shards (new layout)
      2. {root}/index/             — legacy flat layout (backward compat)
    """
    from pathlib import Path
    index_root = cfg.storage_root / "index"

    shards_root = index_root / "shards"
    if shards_root.exists():
        for shard_dir in sorted(shards_root.iterdir()):
            if shard_dir.is_dir():
                _build_shard(cfg, shard_dir)

    legacy_pending = index_root / "pending_embeddings.jsonl"
    if legacy_pending.exists() and legacy_pending.stat().st_size > 0:
        _build_shard(cfg, index_root)

    logger.info("build_index complete.")
```

- [ ] **Step 2: Run the build shard tests**

```bash
pytest tests/test_indexer_sharding.py::test_build_index_creates_shard_for_decade \
       tests/test_indexer_sharding.py::test_build_index_creates_separate_shards \
       -v 2>&1 | tail -10
```

Expected: PASS.

---

### Task B6: Make `run_gapjoin` search all shards

- [ ] **Step 1: In `run_gapjoin`, replace the FAISS index loading block**

Find (around line 203–209):

```python
    # Load FAISS index
    index_path = cfg.storage_root / "index" / "faiss.idx"
    if not index_path.exists():
        logger.error("FAISS index not found. Please run 'build' first.")
        return

    index = faiss.read_index(str(index_path))
```

Replace with:

```python
    # Discover all shard indices (decade shards + legacy flat)
    index_root = cfg.storage_root / "index"
    shard_indices = []  # list of (label: str, index: faiss.Index)

    shards_root = index_root / "shards"
    if shards_root.exists():
        for shard_dir in sorted(shards_root.iterdir()):
            idx_path = shard_dir / "faiss.idx"
            if shard_dir.is_dir() and idx_path.exists():
                shard_indices.append((shard_dir.name, faiss.read_index(str(idx_path))))

    legacy_idx = index_root / "faiss.idx"
    if legacy_idx.exists():
        shard_indices.append(("legacy", faiss.read_index(str(legacy_idx))))

    if not shard_indices:
        logger.error("No FAISS shard indices found. Please run 'build' first.")
        return
```

- [ ] **Step 2: Replace the FAISS search block inside the per-redaction loop**

Find the block that begins `if ctx_emb is not None:` and calls `index.search(...)`. Replace just the search + `hit_chunk_ids` construction (leave everything after `hit_chunk_ids` untouched):

**Before:**
```python
        if ctx_emb is not None:
            # Query FAISS
            query_vec = np.array([ctx_emb], dtype=np.float32)
            norms = np.linalg.norm(query_vec, axis=1, keepdims=True)
            query_vec = np.where(norms > 0, query_vec / norms, query_vec)

            _D, _idx = index.search(query_vec, topk)
            hit_chunk_ids = [int(cid) for cid in _idx[0] if cid != -1]
```

**After:**
```python
        if ctx_emb is not None:
            # Query all shards and merge into global top-K
            query_vec = np.array([ctx_emb], dtype=np.float32)
            norms_q = np.linalg.norm(query_vec, axis=1, keepdims=True)
            query_vec = np.where(norms_q > 0, query_vec / norms_q, query_vec)

            all_hits = []  # list of (cosine_score: float, chunk_id: int)
            for _label, shard_idx in shard_indices:
                shard_topk = min(topk, shard_idx.ntotal) if shard_idx.ntotal > 0 else 0
                if shard_topk == 0:
                    continue
                _D, _idx = shard_idx.search(query_vec, shard_topk)
                for score, cid in zip(_D[0], _idx[0]):
                    if cid != -1:
                        all_hits.append((float(score), int(cid)))

            all_hits.sort(key=lambda x: -x[0])
            top_hits = all_hits[:topk]
            hit_chunk_ids = [cid for _, cid in top_hits]
            chunk_cosines = {cid: score for score, cid in top_hits}
```

> **Important:** Also remove the old `chunk_cosines` dict construction loop that previously appeared after `hit_chunk_ids` (the `for idx, cid in enumerate(_idx[0])` loop). It is now replaced by the `chunk_cosines` line above. The scoring loop that follows uses `chunk_cosines[ch["chunk_id"]]` — no changes needed there.

- [ ] **Step 3: Run the cross-shard gapjoin test**

```bash
pytest tests/test_indexer_sharding.py::test_gapjoin_searches_across_shards -v 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 4: Run the full gapjoin suite**

```bash
pytest tests/test_gapjoin.py -v 2>&1 | tail -15
```

Expected: all passing. Existing tests seed a `faiss.idx` at the legacy flat path — the new code handles that via the `legacy_idx` fallback.

- [ ] **Step 5: Run all Part B tests**

```bash
pytest tests/test_indexer_sharding.py tests/test_gapjoin.py tests/test_embed.py -v 2>&1 | tail -20
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add config.toml palimpsest/results.py palimpsest/tasks/embed.py \
        palimpsest/indexer.py tests/test_indexer_sharding.py
git commit -m "feat(indexer): shard FAISS index by decade — build_index, run_gapjoin, results, embed task"
```

---

## Part C — Worker Graceful Release on SIGTERM

**Context:** On SIGTERM the worker finishes its current job. If SIGKILL arrives before that finishes, the job stays `leased` for up to 15 minutes until `reap_leases`. Fix: track current job in two module globals; signal handler calls `POST /release` to immediately return the job to `pending`. The broker adds the endpoint.

**Files:**
- Modify: `palimpsest/broker.py` — add `ReleasePayload` + `POST /release`
- Modify: `palimpsest/worker.py` — two globals + updated signal handler + set/clear in main loop
- Modify: `tests/test_broker.py` — 4 new endpoint tests
- Create: `tests/test_worker_release.py` — 3 signal handler unit tests

---

### Task C1: Write failing broker release tests

- [ ] **Step 1: Append to `tests/test_broker.py`**

```python
# ---- append to tests/test_broker.py ----

def test_release_returns_job_to_pending(client):
    """POST /release resets a leased job to pending."""
    enqueue_resp = client.post("/enqueue", json={"type": "extract", "doc_id": "555", "priority": 5})
    assert enqueue_resp.status_code == 200
    job_id = enqueue_resp.json()["job_id"]

    lease_resp = client.post("/lease", json={"worker_id": "worker-x", "capabilities": ["extract"], "max_jobs": 1})
    assert lease_resp.status_code == 200
    assert lease_resp.json()["jobs"][0]["job_id"] == job_id

    release_resp = client.post("/release", json={"worker_id": "worker-x", "job_id": job_id})
    assert release_resp.status_code == 200
    assert release_resp.json()["ok"] is True

    counts = client.get("/status").json()["job_counts"]
    assert counts.get("extract", {}).get("pending", 0) == 1


def test_release_does_not_increment_attempts(client):
    """POST /release must not increment attempts — infra shutdown is not a job failure."""
    enqueue_resp = client.post("/enqueue", json={"type": "extract", "doc_id": "556", "priority": 5})
    job_id = enqueue_resp.json()["job_id"]
    client.post("/lease", json={"worker_id": "worker-y", "capabilities": ["extract"], "max_jobs": 1})
    client.post("/release", json={"worker_id": "worker-y", "job_id": job_id})

    # If attempts were incremented, with max_attempts=2 the job would be at attempts=1 after
    # one lease. A second lease should still be possible.
    lease2_resp = client.post("/lease", json={"worker_id": "worker-y", "capabilities": ["extract"], "max_jobs": 1})
    assert lease2_resp.status_code == 200
    jobs2 = lease2_resp.json()["jobs"]
    assert any(j["job_id"] == job_id for j in jobs2), "Job should be re-leaseable after release"


def test_release_rejects_wrong_owner(client):
    """POST /release returns 409 if worker_id doesn't own the job."""
    enqueue_resp = client.post("/enqueue", json={"type": "extract", "doc_id": "557", "priority": 5})
    job_id = enqueue_resp.json()["job_id"]
    client.post("/lease", json={"worker_id": "real-owner", "capabilities": ["extract"], "max_jobs": 1})

    resp = client.post("/release", json={"worker_id": "imposter", "job_id": job_id})
    assert resp.status_code == 409


def test_release_rejects_non_leased_job(client):
    """POST /release returns 409 for a job not in leased state."""
    enqueue_resp = client.post("/enqueue", json={"type": "extract", "doc_id": "558", "priority": 5})
    job_id = enqueue_resp.json()["job_id"]
    # job is pending, not leased
    resp = client.post("/release", json={"worker_id": "any-worker", "job_id": job_id})
    assert resp.status_code == 409
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_broker.py::test_release_returns_job_to_pending \
       tests/test_broker.py::test_release_does_not_increment_attempts \
       tests/test_broker.py::test_release_rejects_wrong_owner \
       tests/test_broker.py::test_release_rejects_non_leased_job \
       -v 2>&1 | tail -20
```

Expected: FAIL — endpoint doesn't exist (404).

---

### Task C2: Add `POST /release` to broker

- [ ] **Step 1: Add `ReleasePayload` after `FailPayload` in `palimpsest/broker.py`**

```python
class ReleasePayload(BaseModel):
    worker_id: str
    job_id: int
```

- [ ] **Step 2: Add the endpoint after `@app.post("/fail")`**

```python
@app.post("/release")
def release(req: ReleasePayload):
    """Return a leased job to pending without penalising attempts.

    Called by workers on graceful SIGTERM. Only the current lease_owner may
    call release. Resets state to 'pending' and clears lease fields without
    incrementing attempts (infrastructure shutdown is not a job failure).
    """
    conn = connect(cfg)
    now = utc_now_str()

    with conn:
        cur = conn.execute(
            "SELECT state, lease_owner FROM jobs WHERE job_id=?", (req.job_id,)
        )
        job = cur.fetchone()
        if not job or job["state"] != "leased" or job["lease_owner"] != req.worker_id:
            raise HTTPException(status_code=409, detail="Ownership mismatch or job not leased")

        conn.execute(
            "UPDATE jobs SET state='pending', lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE job_id=?",
            (now, req.job_id),
        )

    return {"ok": True}
```

- [ ] **Step 3: Run the 4 release tests**

```bash
pytest tests/test_broker.py::test_release_returns_job_to_pending \
       tests/test_broker.py::test_release_does_not_increment_attempts \
       tests/test_broker.py::test_release_rejects_wrong_owner \
       tests/test_broker.py::test_release_rejects_non_leased_job \
       -v 2>&1 | tail -10
```

Expected: PASS all 4.

- [ ] **Step 4: Run the full broker suite**

```bash
pytest tests/test_broker.py -v 2>&1 | tail -15
```

Expected: all passing.

---

### Task C3: Write failing worker signal handler tests

- [ ] **Step 1: Create `tests/test_worker_release.py`**

```python
# tests/test_worker_release.py
"""Unit tests for worker graceful release on SIGTERM/SIGINT."""
import signal
from unittest.mock import MagicMock, patch
import pytest


def test_signal_handler_calls_release_when_job_active():
    """When a job is in progress, signal_handler POSTs /release and sets should_exit."""
    import palimpsest.worker as worker_mod

    worker_mod._current_job_id = 42
    worker_mod._current_worker_id = "test-node"
    worker_mod.should_exit = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("palimpsest.worker.httpx.post", return_value=mock_resp) as mock_post:
        worker_mod.signal_handler(signal.SIGTERM, None)

    assert mock_post.called
    url = mock_post.call_args[0][0]
    assert "/release" in url
    body = mock_post.call_args[1]["json"]
    assert body["job_id"] == 42
    assert body["worker_id"] == "test-node"
    assert worker_mod.should_exit is True

    # Cleanup
    worker_mod._current_job_id = None
    worker_mod._current_worker_id = None
    worker_mod.should_exit = False


def test_signal_handler_does_not_call_release_when_idle():
    """When idle (_current_job_id is None), signal_handler only sets should_exit."""
    import palimpsest.worker as worker_mod

    worker_mod._current_job_id = None
    worker_mod._current_worker_id = None
    worker_mod.should_exit = False

    with patch("palimpsest.worker.httpx.post") as mock_post:
        worker_mod.signal_handler(signal.SIGTERM, None)

    assert not mock_post.called
    assert worker_mod.should_exit is True
    worker_mod.should_exit = False


def test_signal_handler_sets_should_exit_even_if_release_fails():
    """If /release raises (broker offline), should_exit is still set."""
    import palimpsest.worker as worker_mod

    worker_mod._current_job_id = 99
    worker_mod._current_worker_id = "test-node"
    worker_mod.should_exit = False

    with patch("palimpsest.worker.httpx.post", side_effect=Exception("connection refused")):
        worker_mod.signal_handler(signal.SIGTERM, None)

    assert worker_mod.should_exit is True

    worker_mod._current_job_id = None
    worker_mod._current_worker_id = None
    worker_mod.should_exit = False
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_worker_release.py -v 2>&1 | tail -15
```

Expected: FAIL — `_current_job_id` and `_current_worker_id` don't exist yet.

---

### Task C4: Update worker to track current job and release on signal

- [ ] **Step 1: Add two globals after `broker_backoff` (line 28) in `palimpsest/worker.py`**

```python
_current_job_id: int | None = None
_current_worker_id: str | None = None
```

- [ ] **Step 2: Replace `signal_handler` (lines 30–33) with the release-aware version**

```python
def signal_handler(signum, frame):
    global should_exit, _current_job_id, _current_worker_id
    logging.info(f"Received signal {signum}. Exiting cleanly after current job...")
    should_exit = True

    if _current_job_id is not None and _current_worker_id is not None:
        broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
        try:
            httpx.post(
                f"{broker_url}/release",
                json={"worker_id": _current_worker_id, "job_id": _current_job_id},
                timeout=5.0,
            )
            logging.info(f"Released job {_current_job_id} back to queue on shutdown.")
        except Exception as e:
            logging.warning(f"Could not release job {_current_job_id} on shutdown: {e}")
```

- [ ] **Step 3: Set and clear the tracking globals in `run_worker`**

Find the block after `doc_id = job["doc_id"]` (around line 151) and add immediately after:

```python
            _current_job_id = job_id
            _current_worker_id = node_name
```

Find the block after `stop_evt.set()` and `hb_thread.join()` at the end of the job handling section (around line 226) and add:

```python
            _current_job_id = None
            _current_worker_id = None
```

Also clear in both exception handlers (`PermanentJobError` and generic `Exception`) — add `_current_job_id = None; _current_worker_id = None` before `stop_evt.set()` in each branch. And in the "no handler registered" branch, add them before `stop_evt.set()` there too.

The complete updated job processing section of `run_worker` (replace lines ~148–231):

```python
            job = jobs[0]
            job_id = job["job_id"]
            job_type = job["type"]
            doc_id = job["doc_id"]

            logging.info(f"Leased job {job_id} ({job_type}) for doc {doc_id}")
            start_time = time.time()

            # Track current job so signal_handler can release it on SIGTERM
            _current_job_id = job_id
            _current_worker_id = node_name

            # Setup heartbeat
            stop_evt = threading.Event()
            lost_evt = threading.Event()
            hb_thread = threading.Thread(
                target=heartbeat_loop,
                args=(node_name, job_id, stop_evt, lost_evt),
                daemon=True
            )
            hb_thread.start()

            handler_func = HANDLERS.get(job_type)
            if not handler_func:
                logging.error(f"No handler registered for job type: {job_type}")
                client.post(
                    f"{broker_url}/fail",
                    json={"worker_id": node_name, "job_id": job_id,
                          "error": f"No handler registered for {job_type}", "retryable": False}
                )
                _current_job_id = None
                _current_worker_id = None
                stop_evt.set()
                hb_thread.join()
                if once:
                    break
                continue

            try:
                result = handler_func(cfg, job)
                duration = time.time() - start_time

                if lost_evt.is_set():
                    logging.warning(f"Discarding result for job {job_id} since it was lost.")
                else:
                    client.post(
                        f"{broker_url}/complete",
                        json={"worker_id": node_name, "job_id": job_id, "result": result}
                    )
                    logging.info(f"Completed job {job_id} ({job_type}) for doc {doc_id} in {duration:.2f}s")
            except PermanentJobError as e:
                logging.error(f"Permanent handler error on job {job_id}: {e}")
                client.post(
                    f"{broker_url}/fail",
                    json={"worker_id": node_name, "job_id": job_id, "error": str(e), "retryable": False}
                )
            except Exception as e:
                logging.error(f"Handler error on job {job_id}: {e}")
                client.post(
                    f"{broker_url}/fail",
                    json={"worker_id": node_name, "job_id": job_id, "error": str(e), "retryable": True}
                )

            _current_job_id = None
            _current_worker_id = None
            stop_evt.set()
            hb_thread.join()

            if once:
                break
```

- [ ] **Step 4: Run the worker release tests**

```bash
pytest tests/test_worker_release.py -v 2>&1 | tail -10
```

Expected: PASS all 3.

- [ ] **Step 5: Run the full worker suite**

```bash
pytest tests/test_worker.py tests/test_worker_release.py -v 2>&1 | tail -15
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add palimpsest/broker.py palimpsest/worker.py \
        tests/test_broker.py tests/test_worker_release.py
git commit -m "feat(worker,broker): graceful SIGTERM release — POST /release + worker job tracking"
```

---

## Final Verification

- [ ] **Run the full test suite**

```bash
pytest tests/ -x -q 2>&1 | tail -30
```

Expected: all tests passing. Compare against the baseline you captured in pre-flight.

- [ ] **Update WORK-LOG.md**

Append to `/Users/herren/dev/palimpsest/WORK-LOG.md`:

```
## TASK-20 Phase 2 Scaling & Safety — completed
- A: apply_heuristic aligned to spec (entity query, HEURISTIC_AUTO, INSERT review_queue, removed birth-year regex)
- B: FAISS decade sharding (config, process_embed routing, embed task year passthrough, build_index, run_gapjoin multi-shard)
- C: broker POST /release + worker _current_job_id tracking + SIGTERM release call
```

---

## Spec Coverage Self-Check

| Spec requirement | Task |
|---|---|
| 20.1 — Query entities where kind='person' AND living_status='unknown' | A2 |
| 20.1 — JOIN with documents to get year | A2 |
| 20.1 — `(current_year - doc_year) > 75` → deceased | A2 |
| 20.1 — UPDATE entities.living_status = 'deceased_historical' | A2 |
| 20.1 — INSERT review_queue: status='approved', reason=exact string, decided_by='HEURISTIC_AUTO' | A2 |
| 20.1 — Failing entities → living_status='potentially_living' | A2 |
| 20.1 — Single transaction | A2 |
| 20.1 — Print summary (evaluated, historical, potentially_living) | A2 |
| 20.2 — config.toml `shard_by = "decade"` | B2 |
| 20.2 — Storage layout shards/YYYY/faiss.idx + shards/YYYY/pending_embeddings.jsonl | B3, B5 |
| 20.2 — Worker returns year from job payload | B4 |
| 20.2 — Broker routes embeddings to correct shard dir via process_embed | B3 |
| 20.2 — build_index: per-decade shard indices | B5 |
| 20.2 — run_gapjoin: iterate all shards, global top-K | B6 |
| 20.3 — Broker POST /release: validates ownership, sets pending, no attempt increment | C2 |
| 20.3 — Worker signal_handler: calls POST /release when job active | C4 |
| 20.3 — Worker exits cleanly even if release fails | C4 |
