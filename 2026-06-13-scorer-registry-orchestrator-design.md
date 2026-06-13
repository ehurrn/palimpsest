# Palimpsest — Scorer Registry & Lane A Orchestrator Design

*Author: Claude (Manager). Date: 2026-06-13.*  
*Implements: Approach A from brainstorming session (scorer registry + thin orchestrator).*

---

## 0. Purpose and Scope

This spec describes two tightly-coupled changes:

1. **Scorer registry** — extract all finding-type scoring logic out of `palimpsest/indexer.py` into a new `palimpsest/scorers/` package. Each finding-type (a–f) gets its own module. `indexer.py` becomes a thin CLI shell that delegates to the registry.

2. **Lane A orchestrator** — a new `palimpsest/orchestrator.py` module with two entry points: a `heartbeat` daemon that runs every 15 minutes pacing the batch pipeline, and an `investigate` command that pulls top-N candidates for a finding-type and formats them for the `palimpsest-investigator` skill.

**Out of scope:** Changes to `broker.py`, `server.py`, `worker.py`, `tasks/`, or the DB schema. No new tables. No new HTTP services. The orchestrator is a CLI tool + launchd daemon, not an HTTP service.

**Implementation note:** This spec is written for implementation by a less capable local model. Every interface is given as a literal Python signature. Every file is given as an exact path. Every acceptance test is a runnable shell command with expected output. Do not infer anything; if something seems ambiguous, follow the spec literally and note the ambiguity in a comment.

---

## 1. Repository layout after this change

```
palimpsest/
  scorers/
    __init__.py        ← registry dict + import-all bootstrap
    base.py            ← Candidate dataclass + Scorer Protocol
    type_a.py          ← redacted-text corroboration (extracted from indexer.py)
    type_b.py          ← undisclosed dosage (extracted from indexer.py)
    type_c.py          ← identity linkage (extracted from indexer.py)
    type_d.py          ← outcome suppression gap (extracted from indexer.py)
    type_e.py          ← regulatory violation (extracted from indexer.py)
    type_f.py          ← series gap (extracted from indexer.py)
  indexer.py           ← CLI shell only, ~300 lines (was ~1070)
  orchestrator.py      ← new: heartbeat + investigate entry points

tests/
  test_scorers_base.py ← Candidate dataclass + registry bootstrap
  test_scorer_type_a.py
  test_scorer_type_b.py
  test_scorer_type_c.py
  test_scorer_type_d.py
  test_scorer_type_e.py
  test_scorer_type_f.py
  test_orchestrator.py ← heartbeat logic + investigate output

deploy/
  com.palimpsest.orchestrator.plist  ← new launchd plist
```

Existing test files (`test_gapjoin.py`, `test_identity.py`, `test_outcome.py`, `test_series.py`, `test_violation.py`) are **deleted** after their logic is migrated to the new `test_scorer_type_X.py` files. Do not delete them until the new tests pass.

---

## 2. `palimpsest/scorers/base.py`

This file defines the shared data types. Every scorer module imports from here. Do not define `Candidate` or `Scorer` anywhere else.

```python
# palimpsest/scorers/base.py
from __future__ import annotations
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from palimpsest.config import Config


@dataclass
class Candidate:
    """Common currency between scorers and the orchestrator.

    Every field is mandatory. Scorers must populate all fields.
    Empty lists are allowed for doc_ids/page_refs/entity_ids only
    when the finding type genuinely has no secondary source (Type d absence
    findings may have entity_ids=[] when no matching entity exists in the DB).
    """
    type_key: str          # e.g. "type_c" — must match the scorer's type_key attr
    score: float           # 0.0–1.0, higher is stronger
    doc_ids: list[str]     # source document IDs (provenance invariant: ≥1 required)
    page_refs: list[str]   # human-readable page citations, e.g. ["NV0001234 p.3", "NV0005678 p.11"]
    summary: str           # one-line description for the investigator skill / HITL prompt
    entity_ids: list[int]  # DB entity row IDs; used by server.py for MCP lookups


@runtime_checkable
class Scorer(Protocol):
    """Protocol every scorer module must satisfy.

    Implementing classes must set type_key and candidates_table as class
    attributes (not instance attributes) so the orchestrator can inspect
    them without instantiation.
    """
    type_key: str           # e.g. "type_c"
    candidates_table: str   # DB table storing this type's candidates,
                            # e.g. "identity_link_candidates"

    def run(self, conn: sqlite3.Connection, config: Config) -> list[Candidate]:
        """Score all pending candidates and insert new rows into candidates_table.

        This is the bulk scorer — it reads the DB, computes scores, and writes
        new candidate rows. Idempotent: INSERT OR IGNORE everywhere.
        Returns the list of Candidate objects it inserted (not all rows in the table).
        """
        ...

    def top(self, conn: sqlite3.Connection, limit: int = 20) -> list[Candidate]:
        """Return the top-N candidates from candidates_table ordered by score DESC.

        Does NOT run scoring — only reads existing rows. Used by the orchestrator's
        `investigate` command and by the heartbeat candidate-count check.
        Returns at most `limit` results.
        """
        ...
```

---

## 3. `palimpsest/scorers/__init__.py`

This file is the registry. It imports all six scorer modules and registers their instances.

```python
# palimpsest/scorers/__init__.py
"""Scorer registry.

Import this module to get SCORERS, a dict mapping type_key → Scorer instance.
All six finding-type scorers are registered at import time.

Usage:
    from palimpsest.scorers import SCORERS
    candidates = SCORERS["type_c"].run(conn, config)
"""
from palimpsest.scorers.type_a import TypeAScorer
from palimpsest.scorers.type_b import TypeBScorer
from palimpsest.scorers.type_c import TypeCScorer
from palimpsest.scorers.type_d import TypeDScorer
from palimpsest.scorers.type_e import TypeEScorer
from palimpsest.scorers.type_f import TypeFScorer

_ALL_SCORERS = [
    TypeAScorer(),
    TypeBScorer(),
    TypeCScorer(),
    TypeDScorer(),
    TypeEScorer(),
    TypeFScorer(),
]

SCORERS: dict[str, object] = {s.type_key: s for s in _ALL_SCORERS}

__all__ = ["SCORERS"]
```

---

## 4. Scorer modules (`type_a.py` through `type_f.py`)

### 4.1 Extraction rule

Each scorer module is created by **moving** the corresponding function from `indexer.py` into the new file and wrapping it in a class. The function body must not be changed — only the calling convention changes (the function becomes a method, `cfg: Config` becomes `self.run(conn, config)`).

The mapping is:

| New file | Current function in indexer.py | `type_key` | `candidates_table` |
|---|---|---|---|
| `type_a.py` | `run_gapjoin()` | `"type_a"` | `"gap_candidates"` |
| `type_b.py` | dosage proximity section inside `run_gapjoin()` | `"type_b"` | `"gap_candidates"` |
| `type_c.py` | `run_identity_link()` | `"type_c"` | `"identity_link_candidates"` |
| `type_d.py` | `run_outcome_gap()` | `"type_d"` | `"outcome_gap_candidates"` |
| `type_e.py` | `run_violation_join()` | `"type_e"` | `"violation_candidates"` |
| `type_f.py` | `run_series_join()` | `"type_f"` | `"series_gap_candidates"` |

**Note on type_a vs type_b:** `run_gapjoin()` currently handles both Type a (text corroboration) and Type b (dosage proximity) because they share the gap_candidates table. For this refactor, put the full `run_gapjoin()` body in `type_a.py`. `type_b.py` is a lightweight wrapper that calls `TypeAScorer().run()` and then filters `gap_candidates` to rows where `kind='dosage'`. This avoids duplicating the gapjoin logic. Document this clearly in the type_b module docstring.

### 4.2 Class template

Every scorer follows this exact template. Replace `TYPE_X`, `type_x`, `table_name`, and fill in the `run()` and `top()` bodies.

```python
# palimpsest/scorers/type_x.py
"""Type X scorer — <one-line description>.

Extracted from palimpsest/indexer.py::run_x_function().
See specs/FINDING-TYPES.md §Type X for the detector and corroboration rule.
"""
from __future__ import annotations
import logging
import sqlite3

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)


class TypeXScorer:
    type_key = "type_x"
    candidates_table = "table_name"

    def run(self, conn: sqlite3.Connection, config: Config) -> list[Candidate]:
        """<docstring copied from the original indexer.py function>"""
        # --- PASTE THE BODY OF run_x_function() HERE ---
        # Replace cfg with config throughout.
        # Replace connect(cfg) with conn (connection is passed in — do not open a new one).
        # All other logic is identical.
        ...

    def top(self, conn: sqlite3.Connection, limit: int = 20) -> list[Candidate]:
        """Return top-N candidates from candidates_table ordered by score DESC."""
        rows = conn.execute(
            f"SELECT * FROM {self.candidates_table} WHERE status = 'candidate' "
            f"ORDER BY score DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [self._row_to_candidate(row) for row in rows]

    def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
        """Convert a DB row from candidates_table to a Candidate.

        Each scorer implements this for its own table schema.
        """
        raise NotImplementedError
```

### 4.3 `_row_to_candidate` implementations

Each scorer must implement `_row_to_candidate` to match its table schema. The schemas are defined in `db.py`. The exact implementations are:

**TypeAScorer / TypeBScorer** (`gap_candidates`):
```python
def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
    return Candidate(
        type_key=self.type_key,
        score=float(row["score"]),
        doc_ids=[row["source_doc_id"], row["candidate_doc_id"]],
        page_refs=[
            f"{row['source_doc_id']} p.{row['source_page_no']}",
            f"{row['candidate_doc_id']} p.{row['candidate_page_no']}",
        ],
        summary=f"Gap candidate: {row.get('kind', 'unknown')} entity, score={row['score']:.2f}",
        entity_ids=[row["entity_id"]] if row["entity_id"] else [],
    )
```

**TypeCScorer** (`identity_link_candidates`):
```python
def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
    return Candidate(
        type_key=self.type_key,
        score=float(row["score"]),
        doc_ids=[row["anon_doc_id"], row["named_doc_id"]],
        page_refs=[
            f"{row['anon_doc_id']} p.{row['anon_page_no']}",
            f"{row['named_doc_id']} p.{row['named_page_no']}",
        ],
        summary=(
            f"Identity link: subject_ref '{row['subject_norm']}' → "
            f"person '{row['person_norm']}', score={row['score']:.2f}"
        ),
        entity_ids=[row["subject_entity_id"], row["person_entity_id"]],
    )
```

**TypeDScorer** (`outcome_gap_candidates`):
```python
def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
    return Candidate(
        type_key=self.type_key,
        score=float(row["score"]),
        doc_ids=[row["doc_id"]],
        page_refs=[f"{row['doc_id']} p.{row['page_no']}"],
        summary=(
            f"Outcome gap: protocol '{row['protocol_code']}' has initiation "
            f"record but no outcome document, score={row['score']:.2f}"
        ),
        entity_ids=[row["outcome_entity_id"]] if row["outcome_entity_id"] else [],
    )
```

**TypeEScorer** (`violation_candidates`):
```python
def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
    return Candidate(
        type_key=self.type_key,
        score=float(row["score"]),
        doc_ids=[row["doc_id"]],
        page_refs=[f"{row['doc_id']} p.{row['page_no']}"],
        summary=(
            f"Regulatory violation: {row['violation_type']} against reg_id={row['reg_id']}, "
            f"doc_year={row['doc_year']}, score={row['score']:.2f}"
        ),
        entity_ids=[row["reg_cite_entity_id"]],
    )
```

**TypeFScorer** (`series_gap_candidates`):
```python
def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
    return Candidate(
        type_key=self.type_key,
        score=float(row["score"]),
        doc_ids=[row["ref_doc_id"]],
        page_refs=[f"{row['ref_doc_id']} p.{row['ref_page_no']}"],
        summary=(
            f"Series gap: missing accession '{row['missing_accession']}' "
            f"referenced by {row['ref_doc_id']}, score={row['score']:.2f}"
        ),
        entity_ids=[row["seq_ref_entity_id"]] if row["seq_ref_entity_id"] else [],
    )
```

---

## 5. `palimpsest/indexer.py` after refactor

After extracting the scorer functions, `indexer.py` retains only:

1. `get_ollama_embedding()` — stays in indexer.py because it is also used by the embed task
2. `get_slot_expectation()` — stays (used by gapjoin logic, now in type_a.py; type_a.py imports it from indexer)
3. `build_index()` — stays (FAISS index build, not a scorer)
4. The `argparse` CLI (`main()`) — stays, but each subcommand delegates to the scorer

**The CLI subcommands after refactor:**

```python
# In indexer.py main():
from palimpsest.scorers import SCORERS
from palimpsest.db import connect
from palimpsest.config import load

def cmd_gapjoin(args):
    cfg = load(args.config)
    conn = connect(cfg)
    SCORERS["type_a"].run(conn, cfg)

def cmd_seriesjoin(args):
    cfg = load(args.config)
    conn = connect(cfg)
    SCORERS["type_f"].run(conn, cfg)

def cmd_outcomegap(args):
    cfg = load(args.config)
    conn = connect(cfg)
    SCORERS["type_d"].run(conn, cfg)

def cmd_violationjoin(args):
    cfg = load(args.config)
    conn = connect(cfg)
    SCORERS["type_e"].run(conn, cfg)

def cmd_identitylink(args):
    cfg = load(args.config)
    conn = connect(cfg)
    SCORERS["type_c"].run(conn, cfg)

def cmd_build(args):
    cfg = load(args.config)
    build_index(cfg)

def cmd_stats(args):
    cfg = load(args.config)
    conn = connect(cfg)
    for key, scorer in SCORERS.items():
        count = conn.execute(
            f"SELECT COUNT(*) FROM {scorer.candidates_table} WHERE status='candidate'"
        ).fetchone()[0]
        print(f"{key}: {count} candidates in {scorer.candidates_table}")
```

All existing CLI subcommand names (`gapjoin`, `seriesjoin`, `outcomegap`, `violationjoin`, `identitylink`, `build`, `stats`) are preserved exactly. No external interface changes.

**Expected line count after refactor:** `indexer.py` should be approximately 250–350 lines (down from 1,070). If it is significantly longer, the scorer logic was not fully extracted.

---

## 6. `palimpsest/orchestrator.py`

Full module. Implement exactly as specified.

```python
# palimpsest/orchestrator.py
"""Lane A Orchestrator.

Two entry points:
  palimpsest orchestrate heartbeat   — 15-min daemon loop (run via launchd)
  palimpsest orchestrate investigate — on-demand candidate pull for investigation

The heartbeat loop does three things each tick:
  1. Queue depth check: if pending jobs < queue_low_water_mark, log a warning.
  2. Candidate sweep: for each scorer, count new candidates; if any type's
     count crossed candidate_investigate_threshold since last tick, log a flag.
  3. Worker liveness: if broker /status shows no heartbeat in 10 min, log an alert.

The investigate command pulls top-N candidates for a given type, formats them
as a Markdown citation block, and writes to stdout or a file. This output is
consumed by the palimpsest-investigator skill.
"""
from __future__ import annotations
import argparse
import datetime
import json
import logging
import time
from pathlib import Path

import httpx

from palimpsest.config import load, Config
from palimpsest.db import connect
from palimpsest.scorers import SCORERS
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ---------------------------------------------------------------------------
# Heartbeat helpers
# ---------------------------------------------------------------------------

def _check_queue_depth(conn, config: Config) -> int:
    """Return count of pending jobs. Logs a warning if below low-water mark."""
    low_water = int(config.orchestrator.get("queue_low_water_mark", 100))
    count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
    ).fetchone()[0]
    if count < low_water:
        logger.warning(
            "Queue depth %d is below low-water mark %d — "
            "consider running: palimpsest harvest fetch",
            count, low_water
        )
    else:
        logger.info("Queue depth: %d pending jobs", count)
    return count


def _check_candidate_counts(
    conn,
    last_counts: dict[str, int],
) -> dict[str, int]:
    """Count candidates per scorer type. Log a flag if count grew significantly.

    Returns updated counts dict (type_key → current count).
    """
    threshold = 50  # hardcoded; config key added in future if needed
    current: dict[str, int] = {}
    for key, scorer in SCORERS.items():
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {scorer.candidates_table} "
                f"WHERE status = 'candidate'"
            ).fetchone()[0]
            current[key] = count
            prev = last_counts.get(key, 0)
            delta = count - prev
            if delta >= threshold:
                logger.info(
                    "INVESTIGATE FLAG: %s has %d new candidates (total=%d) — "
                    "run: palimpsest orchestrate investigate --type %s",
                    key, delta, count, key
                )
            else:
                logger.info("%s: %d candidates (%+d since last tick)", key, count, delta)
        except Exception as e:
            logger.error("Candidate count failed for %s: %s", key, e)
            current[key] = last_counts.get(key, 0)
    return current


def _check_worker_liveness(config: Config) -> None:
    """Call broker /status and warn if no worker heartbeat in 10 minutes."""
    broker_url = config.broker.get("url", "http://localhost:8077")
    try:
        resp = httpx.get(f"{broker_url}/status", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        workers = data.get("workers", [])
        now = datetime.datetime.utcnow()
        alive = [
            w for w in workers
            if w.get("last_heartbeat") and
            (now - datetime.datetime.fromisoformat(w["last_heartbeat"])).total_seconds() < 600
        ]
        if not alive:
            logger.warning(
                "No worker heartbeat in the last 10 minutes. "
                "Workers registered: %d. Check M4/M5/gonktop workers.",
                len(workers)
            )
        else:
            logger.info("Workers alive: %d / %d", len(alive), len(workers))
    except Exception as e:
        logger.error("Broker liveness check failed: %s", e)


# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------

def run_heartbeat(config: Config) -> None:
    """Run the heartbeat daemon loop. Blocks indefinitely. Exits on SIGTERM/SIGINT."""
    interval = int(config.orchestrator.get("heartbeat_interval_secs", 900))
    logger.info("Orchestrator heartbeat starting (interval=%ds)", interval)
    conn = connect(config)
    last_counts: dict[str, int] = {}

    import signal
    running = True

    def _stop(sig, frame):
        nonlocal running
        logger.info("Orchestrator received signal %s — stopping after this tick", sig)
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running:
        logger.info("--- Heartbeat tick at %s ---", datetime.datetime.utcnow().isoformat())
        try:
            _check_queue_depth(conn, config)
        except Exception as e:
            logger.error("Queue depth check failed: %s", e)
        try:
            last_counts = _check_candidate_counts(conn, last_counts)
        except Exception as e:
            logger.error("Candidate sweep failed: %s", e)
        try:
            _check_worker_liveness(config)
        except Exception as e:
            logger.error("Worker liveness check failed: %s", e)

        if running:
            logger.info("Next tick in %d seconds", interval)
            time.sleep(interval)

    logger.info("Orchestrator heartbeat stopped.")


# ---------------------------------------------------------------------------
# Investigate command
# ---------------------------------------------------------------------------

def run_investigate(config: Config, type_key: str, limit: int, output: Path | None) -> None:
    """Pull top-N candidates for type_key and write a Markdown citation block.

    Output format (one block per candidate):
    ---
    ## Candidate <N> — <type_key> (score=<score>)
    **Summary:** <summary>
    **Source documents:** <doc_ids joined by ', '>
    **Page references:** <page_refs joined by ', '>
    **Entity IDs (for MCP lookup):** <entity_ids joined by ', '>
    ---

    The output is append-only. If --output is given and the file exists, a
    timestamp header is prepended and results are appended (not overwritten).
    """
    if type_key not in SCORERS:
        raise SystemExit(
            f"Unknown type_key '{type_key}'. "
            f"Valid keys: {', '.join(sorted(SCORERS.keys()))}"
        )

    conn = connect(config)
    scorer = SCORERS[type_key]
    candidates: list[Candidate] = scorer.top(conn, limit=limit)

    if not candidates:
        msg = f"No candidates found for {type_key} (table: {scorer.candidates_table}).\n"
        if output:
            _append(output, msg)
        else:
            print(msg, end="")
        return

    lines: list[str] = [
        f"\n\n<!-- investigate run: {datetime.datetime.utcnow().isoformat()} "
        f"type={type_key} limit={limit} -->\n"
    ]
    for i, cand in enumerate(candidates, start=1):
        lines.append(f"---\n")
        lines.append(f"## Candidate {i} — {cand.type_key} (score={cand.score:.3f})\n")
        lines.append(f"**Summary:** {cand.summary}\n")
        lines.append(f"**Source documents:** {', '.join(cand.doc_ids)}\n")
        lines.append(f"**Page references:** {', '.join(cand.page_refs)}\n")
        lines.append(
            f"**Entity IDs (for MCP lookup):** "
            f"{', '.join(str(e) for e in cand.entity_ids) or 'none'}\n"
        )

    result = "".join(lines)

    if output:
        _append(output, result)
        logger.info("Wrote %d candidates to %s", len(candidates), output)
    else:
        print(result)


def _append(path: Path, text: str) -> None:
    """Append text to path, creating the file if it does not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="palimpsest orchestrate",
        description="Lane A orchestrator: heartbeat daemon and investigation sessions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # heartbeat subcommand
    hb = sub.add_parser("heartbeat", help="Run the 15-minute heartbeat daemon loop.")
    hb.add_argument("--config", default="config.toml", help="Path to config.toml")

    # investigate subcommand
    inv = sub.add_parser(
        "investigate",
        help="Pull top-N candidates for a finding type and write a citation block.",
    )
    inv.add_argument("--config", default="config.toml", help="Path to config.toml")
    inv.add_argument(
        "--type",
        dest="type_key",
        required=True,
        choices=list(SCORERS.keys()),
        help="Finding type to investigate (type_a through type_f).",
    )
    inv.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of candidates to return (default: 20).",
    )
    inv.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to append output to. If omitted, writes to stdout.",
    )

    args = parser.parse_args()
    cfg = load(args.config)

    if args.command == "heartbeat":
        run_heartbeat(cfg)
    elif args.command == "investigate":
        run_investigate(cfg, args.type_key, args.limit, args.output)


if __name__ == "__main__":
    main()
```

---

## 7. `config.toml` additions

Add an `[orchestrator]` section to `config.toml` and `config.toml.example`:

```toml
[orchestrator]
heartbeat_interval_secs = 900          # 15 minutes
queue_low_water_mark = 100             # warn when pending jobs drop below this
candidate_investigate_threshold = 50   # flag when a type gains this many new candidates per tick
```

The `config.py` `Config` dataclass must expose `config.orchestrator` as a dict. Check `config.py` — if `Config` uses `__getattr__` or dynamic section access (as it does for `config.gapjoin`, `config.broker`, etc.), no change to `config.py` is needed; just add the section to the TOML files.

---

## 8. `deploy/com.palimpsest.orchestrator.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.palimpsest.orchestrator</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/herren/dev/palimpsest/venv/bin/python</string>
    <string>-m</string>
    <string>palimpsest.orchestrator</string>
    <string>heartbeat</string>
    <string>--config</string>
    <string>/Users/herren/dev/palimpsest/config.toml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/herren/dev/palimpsest</string>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/palimpsest-orchestrator.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/palimpsest-orchestrator.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
```

Note `RunAtLoad` is `false` — load manually with `launchctl load` when ready to run. This is the same pattern as the broker and server plists.

---

## 9. `pyproject.toml` entry point

Add `orchestrate` to the CLI entry points so `palimpsest orchestrate` works:

```toml
[project.scripts]
# existing entries preserved:
palimpsest = "palimpsest.indexer:main"
# add:
palimpsest-orchestrate = "palimpsest.orchestrator:main"
```

Or, if the existing `palimpsest` entry point dispatches subcommands via a top-level `__main__.py`, add `orchestrate` as a subcommand there. Check the existing `pyproject.toml` pattern and follow it.

---

## 10. Tests

### 10.1 `tests/test_scorers_base.py`

```python
"""Test Candidate dataclass and registry bootstrap."""
import pytest
from palimpsest.scorers.base import Candidate, Scorer
from palimpsest.scorers import SCORERS


def test_registry_has_all_six_types():
    assert set(SCORERS.keys()) == {"type_a", "type_b", "type_c", "type_d", "type_e", "type_f"}


def test_each_scorer_satisfies_protocol():
    for key, scorer in SCORERS.items():
        assert isinstance(scorer, Scorer), f"{key} does not satisfy Scorer protocol"
        assert hasattr(scorer, "type_key")
        assert hasattr(scorer, "candidates_table")
        assert callable(scorer.run)
        assert callable(scorer.top)


def test_candidate_requires_all_fields():
    c = Candidate(
        type_key="type_a",
        score=0.8,
        doc_ids=["NV0001"],
        page_refs=["NV0001 p.3"],
        summary="test",
        entity_ids=[42],
    )
    assert c.type_key == "type_a"
    assert c.score == 0.8
```

### 10.2 Per-type scorer tests (`test_scorer_type_X.py`)

Each file follows this pattern. **This is a template — do not use it literally.** Replace every `<candidates_table>` with the actual table name for that scorer (e.g. `gap_candidates`, `identity_link_candidates`). Replace every `(...)` in INSERT statements with the actual column list and values matching the schema in `db.py`. Create one file per type: `test_scorer_type_a.py` through `test_scorer_type_f.py`.

```python
"""Test TypeXScorer in isolation using an in-memory SQLite DB."""
import sqlite3
import pytest
from palimpsest.scorers.type_x import TypeXScorer
from palimpsest.scorers.base import Candidate


@pytest.fixture
def conn():
    """In-memory DB with just enough schema for TypeX."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Create only the tables TypeXScorer reads/writes.
    # Copy the CREATE TABLE statement from db.py for this scorer's candidates_table.
    c.execute("""
        CREATE TABLE <candidates_table> (
            ... -- copy from db.py
        )
    """)
    c.commit()
    return c


def test_top_returns_empty_on_empty_table(conn):
    scorer = TypeXScorer()
    results = scorer.top(conn, limit=20)
    assert results == []


def test_top_returns_candidates_ordered_by_score(conn):
    # Insert two rows with known scores
    conn.execute(
        "INSERT INTO <candidates_table> (...) VALUES (...)",
        (... score=0.9 ...)
    )
    conn.execute(
        "INSERT INTO <candidates_table> (...) VALUES (...)",
        (... score=0.7 ...)
    )
    conn.commit()
    scorer = TypeXScorer()
    results = scorer.top(conn, limit=20)
    assert len(results) == 2
    assert results[0].score == 0.9
    assert results[1].score == 0.7
    assert all(isinstance(r, Candidate) for r in results)


def test_top_respects_limit(conn):
    for i in range(5):
        conn.execute("INSERT INTO <candidates_table> (...) VALUES (...)", (...))
    conn.commit()
    scorer = TypeXScorer()
    results = scorer.top(conn, limit=3)
    assert len(results) == 3
```

The existing `test_identity.py`, `test_outcome.py`, `test_series.py`, `test_violation.py`, `test_gapjoin.py` tests cover the `run()` logic. Migrate them to the new `test_scorer_type_X.py` files by changing the import from `from palimpsest.indexer import run_identity_link` to `from palimpsest.scorers.type_c import TypeCScorer` and adjusting the call convention.

### 10.3 `tests/test_orchestrator.py`

```python
"""Test orchestrator heartbeat helpers and investigate output."""
import sqlite3
import pytest
from unittest.mock import MagicMock, patch
from palimpsest.orchestrator import (
    _check_queue_depth,
    _check_candidate_counts,
    run_investigate,
)
from palimpsest.scorers.base import Candidate


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE jobs (status TEXT)")
    c.execute("INSERT INTO jobs VALUES ('pending')")
    c.execute("INSERT INTO jobs VALUES ('pending')")
    c.execute("INSERT INTO jobs VALUES ('complete')")
    c.commit()
    return c


@pytest.fixture
def config():
    cfg = MagicMock()
    cfg.orchestrator = {"queue_low_water_mark": 5, "heartbeat_interval_secs": 900}
    cfg.broker = {"url": "http://localhost:8077"}
    return cfg


def test_check_queue_depth_returns_count(conn, config):
    count = _check_queue_depth(conn, config)
    assert count == 2


def test_check_queue_depth_warns_below_low_water(conn, config, caplog):
    import logging
    config.orchestrator["queue_low_water_mark"] = 10  # above our 2 pending
    with caplog.at_level(logging.WARNING):
        _check_queue_depth(conn, config)
    assert "low-water" in caplog.text


def test_check_candidate_counts_flags_new_candidates(conn, caplog):
    import logging
    # Patch SCORERS with a mock that returns a known count
    mock_scorer = MagicMock()
    mock_scorer.candidates_table = "jobs"
    mock_scorer.type_key = "type_a"
    with patch("palimpsest.orchestrator.SCORERS", {"type_a": mock_scorer}):
        conn.execute("CREATE TABLE IF NOT EXISTS gap_candidates (status TEXT)")
        # Simulate 60 candidates existing, 0 in last tick → delta = 60 > threshold 50
        conn.execute = MagicMock(return_value=MagicMock(fetchone=lambda: (60,)))
        with caplog.at_level(logging.INFO):
            result = _check_candidate_counts(conn, {"type_a": 0})
    assert "INVESTIGATE FLAG" in caplog.text


def test_investigate_writes_markdown(tmp_path, config):
    mock_scorer = MagicMock()
    mock_scorer.candidates_table = "gap_candidates"
    mock_scorer.top.return_value = [
        Candidate(
            type_key="type_a",
            score=0.85,
            doc_ids=["NV0001", "NV0002"],
            page_refs=["NV0001 p.3", "NV0002 p.7"],
            summary="Test finding",
            entity_ids=[1, 2],
        )
    ]
    output_path = tmp_path / "findings.md"
    with patch("palimpsest.orchestrator.SCORERS", {"type_a": mock_scorer}):
        with patch("palimpsest.orchestrator.connect", return_value=MagicMock()):
            run_investigate(config, "type_a", limit=20, output=output_path)
    content = output_path.read_text()
    assert "Candidate 1" in content
    assert "score=0.850" in content
    assert "NV0001" in content
    assert "Test finding" in content


def test_investigate_appends_not_overwrites(tmp_path, config):
    output_path = tmp_path / "findings.md"
    output_path.write_text("EXISTING CONTENT\n")
    mock_scorer = MagicMock()
    mock_scorer.candidates_table = "gap_candidates"
    mock_scorer.top.return_value = []
    with patch("palimpsest.orchestrator.SCORERS", {"type_a": mock_scorer}):
        with patch("palimpsest.orchestrator.connect", return_value=MagicMock()):
            run_investigate(config, "type_a", limit=5, output=output_path)
    content = output_path.read_text()
    assert "EXISTING CONTENT" in content  # original content preserved
```

---

## 11. Acceptance tests

Run these commands after implementation. All must pass before the task is considered complete.

```bash
# From /Users/herren/dev/palimpsest with venv active:

# 1. Full test suite still green
python -m pytest tests/ -q
# Expected: 101+ tests passing, 0 failures

# 2. Scorer registry imports cleanly
python -c "from palimpsest.scorers import SCORERS; print(list(SCORERS.keys()))"
# Expected: ['type_a', 'type_b', 'type_c', 'type_d', 'type_e', 'type_f']

# 3. All scorers satisfy the protocol
python -c "
from palimpsest.scorers import SCORERS
from palimpsest.scorers.base import Scorer
for k, s in SCORERS.items():
    assert isinstance(s, Scorer), f'{k} broken'
    print(f'{k}: OK')
"
# Expected: six lines of "<type_x>: OK"

# 4. indexer.py CLI subcommands still present
python -m palimpsest.indexer --help
# Expected: help text listing gapjoin, seriesjoin, outcomegap, violationjoin,
#           identitylink, build, stats subcommands

# 5. Orchestrator CLI entry points present
python -m palimpsest.orchestrator --help
# Expected: help text listing heartbeat and investigate subcommands

python -m palimpsest.orchestrator investigate --help
# Expected: --type, --limit, --output flags listed

# 6. indexer.py line count below 400
wc -l palimpsest/indexer.py
# Expected: < 400

# 7. Orchestrator investigate runs without error on a live DB
# (requires config.toml pointing at a real DB with at least 0 candidates)
python -m palimpsest.orchestrator investigate \
  --config config.toml \
  --type type_a \
  --limit 5
# Expected: either "No candidates found" or a Markdown block with ≥1 candidate
```

---

## 12. Migration checklist (do in this order)

This order matters. Do not skip steps or reorder.

1. Create `palimpsest/scorers/` directory and `__init__.py`, `base.py`.
2. Create `type_a.py` through `type_f.py` by extracting from `indexer.py` (do not delete from `indexer.py` yet).
3. Write `test_scorers_base.py`. Run it. Fix until green.
4. Migrate each existing test file to `test_scorer_type_X.py`. Run after each migration. Fix until green.
5. Refactor `indexer.py` CLI to import from `SCORERS` and delete the extracted function bodies.
6. Run full test suite. Fix until green.
7. Write `orchestrator.py`.
8. Write `test_orchestrator.py`. Run it. Fix until green.
9. Add `[orchestrator]` section to `config.toml` and `config.toml.example`.
10. Write `deploy/com.palimpsest.orchestrator.plist`.
11. Update `pyproject.toml` entry points.
12. Run all acceptance tests from §11.
13. Delete old test files (`test_gapjoin.py`, `test_identity.py`, `test_outcome.py`, `test_series.py`, `test_violation.py`) only after new tests cover the same assertions.
14. Update `WORK-LOG.md`.
15. Commit: `refactor: scorer registry + Lane A orchestrator`.
