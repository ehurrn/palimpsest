# TASK-11 — Eval schema (v7) + `[eval]` config

**Depends on:** existing schema (v6), `palimpsest/db.py`, `palimpsest/config.py`.
**Builds:** the storage + config substrate for the evaluation harness and trust
gate. No behavior yet — just tables, columns, and config keys.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §5. If this packet and that file
disagree, that file wins.

## Context you need (do not assume — restated here)

`palimpsest/db.py` has one function `migrate(cfg)` that opens the DB
(`connect(cfg)`), then inside a single `with conn:` block runs a sequence of
`CREATE TABLE IF NOT EXISTS` and `INSERT OR IGNORE INTO schema_version (version)
VALUES (N)` statements, ascending. `import sqlite3` is already present.
`schema_version` is `(version INTEGER PRIMARY KEY)`. The latest line today is
`INSERT OR IGNORE INTO schema_version (version) VALUES (6);`.

`palimpsest/config.py` defines a `@dataclass(frozen=True) Config` with fields
`raw, storage_root, db_path, broker, mcp, harvest, ocr, features, embed,
gapjoin, models, nodes, orchestrator`. `load()` validates that required TOML
sections exist, expands `{storage.root}` inside `db.path`, and constructs
`Config(...)` with keyword args. `orchestrator` is already optional via
`data.get("orchestrator", {})`.

SQLite has **no** `ADD COLUMN IF NOT EXISTS`; re-running an `ALTER TABLE … ADD
COLUMN` raises `sqlite3.OperationalError: duplicate column name`. The migration
must swallow that so `migrate` stays idempotent.

## Files

- Modify: `palimpsest/db.py` (append a v7 block at the end of the `with conn:` body)
- Modify: `palimpsest/config.py` (add `eval` field + load it)
- Modify: `config.toml` and `config.toml.example` (append `[eval]`)
- Test: `tests/test_eval_schema.py` (new)

---

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_eval_schema.py`:

```python
import sqlite3
from palimpsest.db import migrate


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_v7_tables_and_columns(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version >= 7

    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"eval_runs", "eval_cases", "eval_results"} <= names

    for table in ("gap_candidates", "identity_link_candidates"):
        cols = _cols(conn, table)
        assert {"confidence", "confidence_method", "gate_tier"} <= cols
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    migrate(cfg)  # must not raise (ALTER TABLE re-run)
    conn = sqlite3.connect(cfg.db_path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 7
    conn.close()
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_schema.py -v`
Expected: FAIL — `eval_runs` missing / `confidence` column missing.

- [ ] **Step 3: Add the v7 migration block**

In `palimpsest/db.py`, immediately after the line
`conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (6);")`
and still inside the `with conn:` block, add:

```python
        # Schema v7 — Evaluation harness + trust gate (specs/EVAL-TRUST-GATE.md §5)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_runs (
          run_id          INTEGER PRIMARY KEY,
          started_at      TEXT NOT NULL,
          finished_at     TEXT,
          scorer_git_sha  TEXT,
          corpus_hash     TEXT,
          seed            INTEGER,
          config_snapshot TEXT,
          notes           TEXT
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_cases (
          case_id   INTEGER PRIMARY KEY,
          run_id    INTEGER NOT NULL REFERENCES eval_runs(run_id),
          type_key  TEXT NOT NULL,
          case_kind TEXT NOT NULL,
          spec      TEXT NOT NULL,
          truth     TEXT NOT NULL
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
          result_id        INTEGER PRIMARY KEY,
          run_id           INTEGER NOT NULL REFERENCES eval_runs(run_id),
          case_id          INTEGER NOT NULL REFERENCES eval_cases(case_id),
          type_key         TEXT NOT NULL,
          raw_score        REAL,
          score_components TEXT,
          predicted        TEXT,
          label            TEXT NOT NULL,
          confidence       REAL
        );""")
        for _table in ("gap_candidates", "identity_link_candidates"):
            for _col, _decl in (
                ("confidence", "REAL"),
                ("confidence_method", "TEXT"),
                ("gate_tier", "TEXT"),
            ):
                try:
                    conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_decl}")
                except sqlite3.OperationalError:
                    pass  # column already exists — migration is idempotent
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (7);")
```

- [ ] **Step 4: Run schema tests, verify pass**

Run: `uv run pytest tests/test_eval_schema.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Write the failing config test**

Append to `tests/test_eval_schema.py`:

```python
import textwrap
from palimpsest.config import load

_BASE_TOML = """
[storage]
root = "{root}"
[db]
path = "{{storage.root}}/db/palimpsest.db"
[broker]
host = "h"
port = 1
[mcp]
port = 2
[harvest]
base_url = "u"
[ocr]
engine_preference = ["tesseract"]
[features]
redaction_context_chars = 300
[embed]
model = "nomic-embed-text"
dim = 768
[gapjoin]
score_threshold = 0.65
[models]
keep_alive = "24h"
[nodes]
"""


def _write_cfg(tmp_path, extra=""):
    p = tmp_path / "config.toml"
    p.write_text(_BASE_TOML.format(root=str(tmp_path)) + extra)
    return p


def test_eval_section_loads_and_expands(tmp_path):
    extra = textwrap.dedent("""
    [eval]
    target_precision = 0.9
    min_cases = 40
    artifact_path = "{storage.root}/eval/calibration.json"
    eval_db_path = "{storage.root}/eval/eval.db"
    """)
    cfg = load(_write_cfg(tmp_path, extra))
    assert cfg.eval["target_precision"] == 0.9
    assert cfg.eval["artifact_path"] == f"{tmp_path}/eval/calibration.json"
    assert cfg.eval["eval_db_path"] == f"{tmp_path}/eval/eval.db"


def test_eval_section_optional(tmp_path):
    cfg = load(_write_cfg(tmp_path))  # no [eval]
    assert cfg.eval == {}
```

- [ ] **Step 6: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_schema.py -k eval_section -v`
Expected: FAIL — `Config` has no attribute `eval` / `TypeError` on construction.

- [ ] **Step 7: Add `eval` to Config**

In `palimpsest/config.py`:
1. Add a field to the dataclass, after `orchestrator: dict`:
   ```python
       eval: dict
   ```
2. In `load()`, after the `db_path_str = …` line, add:
   ```python
       eval_cfg = dict(data.get("eval", {}))
       for _k in ("artifact_path", "eval_db_path"):
           if isinstance(eval_cfg.get(_k), str):
               eval_cfg[_k] = eval_cfg[_k].replace("{storage.root}", root_str)
   ```
3. In the `return Config(...)` call, add the keyword argument:
   ```python
           eval=eval_cfg,
   ```

- [ ] **Step 8: Run config tests, verify pass**

Run: `uv run pytest tests/test_eval_schema.py -v`
Expected: PASS (all four tests).

- [ ] **Step 9: Add `[eval]` to the real config files**

Append to **both** `config.toml` and `config.toml.example`:

```toml
[eval]
target_precision = 0.90      # Wilson lower-bound precision floor for surfaceable
wilson_z         = 1.96
min_cases        = 40        # per type; below this the gate is disabled
default_seed     = 1337
gate_enforcement = "enforce" # off | annotate | enforce
artifact_path    = "{storage.root}/eval/calibration.json"
eval_db_path     = "{storage.root}/eval/eval.db"
```

- [ ] **Step 10: Full suite + commit**

Run: `uv run pytest -q` (expect all green, prior tests unaffected)
Run: `uv run ruff check palimpsest/db.py palimpsest/config.py tests/test_eval_schema.py`

```bash
git add palimpsest/db.py palimpsest/config.py config.toml config.toml.example tests/test_eval_schema.py
git commit -m "feat(eval): schema v7 (eval tables + gate columns) and [eval] config"
```

## Out of scope
- No `eval/` package code. No reading these tables. That is TASK-12+.
- Do not change any existing table or any scorer.

## Blocker protocol
Before starting, append a line to `~/dev/palimpsest/WORK-LOG.md` noting you have
started TASK-11; append again when complete. If you hit a hard blocker (missing
dependency, environment failure you cannot resolve), document it in
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop this task, and move to the next.
