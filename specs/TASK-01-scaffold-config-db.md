# TASK-01 — Repo Scaffold, Config Loader, Database Schema

**Read `specs/00-ARCHITECTURE.md` fully before starting. §2 (layout), §3 (config), §5 (schema) are your spec; this packet adds only mechanics.**

## Objective
Create the `~/dev/palimpsest` repo skeleton, `config.toml` exactly as §3, a config loader, and the SQLite schema with a migration entrypoint.

## Depends on
Nothing (run in parallel with TASK-00/00b).

## Deliverables
```
~/dev/palimpsest/
  config.toml          # verbatim from 00-ARCHITECTURE §3
  pyproject.toml
  palimpsest/__init__.py
  palimpsest/config.py
  palimpsest/db.py
  tests/test_config.py
  tests/test_db.py
```

## Spec

### pyproject.toml
Project `palimpsest`, version `0.1.0`, `requires-python = ">=3.11"`. Dependencies for THIS task only: `tomli; python_version < '3.12'` (else stdlib `tomllib`). Later tasks add their own deps — leave a `# deps added by TASK-NN` comment convention.

### palimpsest/config.py
```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Config:
    raw: dict            # full parsed toml
    storage_root: Path
    db_path: Path
    # convenience accessors for every section as plain dicts:
    broker: dict; mcp: dict; harvest: dict; ocr: dict
    features: dict; embed: dict; gapjoin: dict; models: dict; nodes: dict

def load(path: str | Path | None = None) -> Config: ...
```
Rules:
- Default path: `PALIMPSEST_CONFIG` env var, else `<repo_root>/config.toml`.
- Expand the literal token `{storage.root}` inside any string value (only `db.path` uses it today, implement generically).
- Validate on load; raise `ConfigError` (define it) listing ALL missing keys at once, not just the first.
- No global singleton; callers call `load()` and pass `Config` around.

### palimpsest/db.py
```python
def connect(cfg: Config) -> sqlite3.Connection: ...
def migrate(cfg: Config) -> None: ...
```
- `connect`: creates parent dirs, opens with `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`, `row_factory = sqlite3.Row`.
- `migrate`: executes the DDL from 00-ARCHITECTURE §5 verbatim (use `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`), plus a `schema_version(version INTEGER)` table set to 1. Idempotent: safe to run repeatedly.
- CLI: `python -m palimpsest.db migrate` runs migrate and prints the resulting table list.

## Acceptance (run these; paste output)
```
cd ~/dev/palimpsest
python -m pytest tests/ -q                      # all pass
PALIMPSEST_CONFIG=config.toml python - <<'EOF'
from palimpsest.config import load
c = load(); print(c.db_path)                    # expanded path, no '{storage.root}'
EOF
python -m palimpsest.db migrate                  # prints >= 8 table names
python -m palimpsest.db migrate                  # second run: no error (idempotent)
```
tests/test_config.py must cover: happy path, missing-key error lists all missing keys, `{storage.root}` expansion. tests/test_db.py: migrate twice, then insert a `gap_candidates` row with NULL redaction_id → expect IntegrityError (provenance invariant enforced).

Note: if `/Volumes/palimpsest` doesn't exist on the dev machine, tests must use a tmpdir-overridden config — do NOT require the real SSD for tests.

## Out of scope
Broker, HTTP, any network code, any other module.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
