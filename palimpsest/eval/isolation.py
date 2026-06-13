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
