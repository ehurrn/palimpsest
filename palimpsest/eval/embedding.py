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
