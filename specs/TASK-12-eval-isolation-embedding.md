# TASK-12 — Eval DB isolation + deterministic embedding + synthetic index

**Depends on:** TASK-11 (schema v7, `[eval]` config).
**Builds:** the substrate the runner stands on — an isolated eval database, a
deterministic embedding function, and a synthetic FAISS index writer. After this
packet the harness can construct a clean, off-production sandbox to run scorers
against.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §1, §2, §6.

## Context you need (restated)

- `Config` is a frozen dataclass (`palimpsest/config.py`). Use
  `dataclasses.replace(cfg, ...)` to derive a variant. It now has an `eval` dict
  field (TASK-11) with `eval_db_path` and `artifact_path`.
- `palimpsest/db.py::migrate(cfg)` creates the schema in `cfg.db_path`.
- Scorers read the FAISS index from **`cfg.storage_root / "index" / "faiss.idx"`**
  (see `type_a.py` line ~94). The index is a `faiss.IndexIDMap2(IndexFlatIP(dim))`
  populated with `add_with_ids`; `type_a` later calls `index.reconstruct(chunk_id)`,
  which requires `IndexIDMap2`. Query vectors are L2-normalized before search, so
  inner product == cosine.
- `numpy` and `faiss` are already project dependencies.
- The deterministic embedding is **lexical, not semantic** — disclosed as
  plumbing-only. It must be reproducible across processes, so use `hashlib`
  (never Python's salted `hash()`).

## Files

- Create: `palimpsest/eval/__init__.py` (empty)
- Create: `palimpsest/eval/embedding.py`
- Create: `palimpsest/eval/isolation.py`
- Test: `tests/test_eval_embedding.py`, `tests/test_eval_isolation.py`

---

- [ ] **Step 1: Create the package marker**

Create empty file `palimpsest/eval/__init__.py`:

```python
```

- [ ] **Step 2: Write the failing embedding test**

Create `tests/test_eval_embedding.py`:

```python
import numpy as np
from palimpsest.eval.embedding import deterministic_embed


class _Cfg:
    embed = {"dim": 768}


def _cos(a, b):
    a, b = np.array(a), np.array(b)
    return float(a @ b)  # inputs are unit vectors


def test_deterministic_same_text_same_vector():
    v1 = deterministic_embed(_Cfg(), "oak ridge 1957 dosimetry")
    v2 = deterministic_embed(_Cfg(), "oak ridge 1957 dosimetry")
    assert v1 == v2
    assert len(v1) == 768
    assert abs(np.linalg.norm(v1) - 1.0) < 1e-5


def test_overlap_more_similar_than_disjoint():
    base = deterministic_embed(_Cfg(), "the subject received fifteen rem at oak ridge")
    overlap = deterministic_embed(_Cfg(), "subject received rem oak ridge report")
    disjoint = deterministic_embed(_Cfg(), "zebra umbrella xylophone quartz")
    assert _cos(base, overlap) > _cos(base, disjoint)


def test_empty_text_is_unit_vector():
    v = deterministic_embed(_Cfg(), "")
    assert abs(np.linalg.norm(v) - 1.0) < 1e-5
```

- [ ] **Step 3: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_embedding.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.embedding`.

- [ ] **Step 4: Implement the deterministic embedding**

Create `palimpsest/eval/embedding.py`:

```python
"""Deterministic, dependency-free lexical embedding for the eval harness.

Feature-hashing (the "hashing trick"): each token maps to a dimension by a
stable SHA-1 hash with a signed bucket, then the vector is L2-normalized.
Reproducible across processes (uses hashlib, not the salted built-in hash()).

This is LEXICAL similarity, not semantic. It exists so the harness plumbing runs
without Ollama; precision measured with it is NOT valid (see EVAL-TRUST-GATE §6).
For a real calibration run, pass the production Ollama embed_fn instead.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from palimpsest.config import Config

_TOKEN = re.compile(r"[a-z0-9]+")


def _bucket(token: str) -> tuple[int, int]:
    digest = hashlib.sha1(token.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "big")
    sign = 1 if (digest[4] & 1) else -1
    return idx, sign


def deterministic_embed(cfg: Config, text: str) -> list[float]:
    """Return a unit-norm lexical embedding of *text* with dim from cfg.embed."""
    dim = int(getattr(cfg, "embed", {}).get("dim", 768))
    vec = np.zeros(dim, dtype=np.float32)
    for tok in _TOKEN.findall((text or "").lower()):
        idx, sign = _bucket(tok)
        vec[idx % dim] += sign
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec /= norm
    else:
        vec[0] = 1.0
    return vec.tolist()
```

- [ ] **Step 5: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_embedding.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Write the failing isolation test**

Create `tests/test_eval_isolation.py`:

```python
import sqlite3
import textwrap

import faiss
import pytest

from palimpsest.config import load
from palimpsest.eval.isolation import make_eval_config, fresh_eval_db, write_index


def _cfg(tmp_path):
    toml = textwrap.dedent(f"""
    [storage]
    root = "{tmp_path}"
    [db]
    path = "{{storage.root}}/db/palimpsest.db"
    [broker]
    host="h"
    port=1
    [mcp]
    port=2
    [harvest]
    base_url="u"
    [ocr]
    engine_preference=["tesseract"]
    [features]
    redaction_context_chars=300
    [embed]
    model="nomic-embed-text"
    dim=768
    [gapjoin]
    score_threshold=0.65
    [models]
    keep_alive="24h"
    [nodes]
    [eval]
    eval_db_path="{{storage.root}}/eval/eval.db"
    artifact_path="{{storage.root}}/eval/calibration.json"
    """)
    p = tmp_path / "config.toml"
    p.write_text(toml)
    return load(p)


def test_make_eval_config_repoints(tmp_path):
    cfg = _cfg(tmp_path)
    ev = make_eval_config(cfg)
    assert ev.db_path == (tmp_path / "eval" / "eval.db")
    assert ev.storage_root == (tmp_path / "eval")
    # production paths unchanged on the original
    assert cfg.db_path == (tmp_path / "db" / "palimpsest.db")


def test_make_eval_config_rejects_collision(tmp_path):
    cfg = _cfg(tmp_path)
    bad = cfg.eval.copy()
    bad["eval_db_path"] = str(cfg.db_path)
    import dataclasses
    cfg2 = dataclasses.replace(cfg, eval=bad)
    with pytest.raises(ValueError):
        make_eval_config(cfg2)


def test_fresh_eval_db_migrates_isolated(tmp_path):
    cfg = _cfg(tmp_path)
    ev = make_eval_config(cfg)
    fresh_eval_db(ev)
    assert ev.db_path.exists()
    assert not cfg.db_path.exists()  # production DB never created
    conn = sqlite3.connect(ev.db_path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 7
    conn.close()


def test_write_index_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    ev = make_eval_config(cfg)
    out = write_index(ev, {500: [1.0] + [0.0] * 767, 600: [0.0, 1.0] + [0.0] * 766})
    assert out.exists()
    idx = faiss.read_index(str(out))
    assert idx.ntotal == 2
    rec = idx.reconstruct(500)
    assert abs(float(rec[0]) - 1.0) < 1e-5
```

- [ ] **Step 7: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_isolation.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.isolation`.

- [ ] **Step 8: Implement isolation**

Create `palimpsest/eval/isolation.py`:

```python
"""Build an isolated, disposable evaluation sandbox.

Scorers read both the DB (cfg.db_path) and the FAISS index
(cfg.storage_root/index/faiss.idx). To run them without touching production we
derive a Config whose storage_root and db_path point under {storage.root}/eval,
migrate a clean DB there, and write a synthetic index there.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import faiss
import numpy as np

from palimpsest.config import Config
from palimpsest.db import migrate


def make_eval_config(cfg: Config) -> Config:
    eval_db = cfg.eval.get("eval_db_path")
    if not eval_db:
        raise ValueError("config [eval].eval_db_path is required for eval runs")
    eval_db_path = Path(eval_db)
    if str(eval_db_path) == str(cfg.db_path):
        raise ValueError("eval_db_path must differ from the production db_path")
    return dataclasses.replace(
        cfg, storage_root=eval_db_path.parent, db_path=eval_db_path
    )


def fresh_eval_db(eval_cfg: Config) -> None:
    """Delete any existing eval DB (+ WAL sidecars) and migrate a clean one.

    Must be given a Config produced by make_eval_config.
    """
    eval_cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (
        eval_cfg.db_path,
        Path(str(eval_cfg.db_path) + "-wal"),
        Path(str(eval_cfg.db_path) + "-shm"),
    ):
        if path.exists():
            path.unlink()
    migrate(eval_cfg)


def _unit(vec) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(arr)
    return arr / norm if norm > 0 else arr


def write_index(eval_cfg: Config, chunk_vectors: dict[int, list[float]]) -> Path:
    """Write a faiss.IndexIDMap2(IndexFlatIP) of {chunk_id: vector} for the eval run."""
    dim = int(eval_cfg.embed.get("dim", 768))
    index_dir = eval_cfg.storage_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    if chunk_vectors:
        ids = np.array(list(chunk_vectors.keys()), dtype=np.int64)
        vecs = np.array([_unit(v) for v in chunk_vectors.values()], dtype=np.float32)
        index.add_with_ids(vecs, ids)
    out = index_dir / "faiss.idx"
    faiss.write_index(index, str(out))
    return out
```

- [ ] **Step 9: Run it, verify it passes**

Run: `uv run pytest tests/test_eval_isolation.py -v`
Expected: PASS (4 tests).

- [ ] **Step 10: Full suite + lint + commit**

Run: `uv run pytest -q`
Run: `uv run ruff check palimpsest/eval tests/test_eval_embedding.py tests/test_eval_isolation.py`

```bash
git add palimpsest/eval/__init__.py palimpsest/eval/embedding.py palimpsest/eval/isolation.py \
        tests/test_eval_embedding.py tests/test_eval_isolation.py
git commit -m "feat(eval): isolated eval DB, deterministic lexical embedding, synthetic index writer"
```

## Out of scope
- No case generation, no scorer invocation, no grading. TASK-13+.
- Do not wire the real Ollama embedder here; `deterministic_embed` is the only
  embedding this packet ships.

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Hard blocker → record in
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, move to the next task.
