# tests/test_indexer_sharding.py
"""Tests for decade-sharded FAISS index build and gapjoin multi-shard search."""
import json
import sqlite3
from pathlib import Path
import pytest
import faiss
import numpy as np

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

# (I will add the test functions here once I verify they pass)
# For now, just confirming setup.
def test_setup(temp_cfg):
    assert temp_cfg.embed.get("shard_by") == "decade"
