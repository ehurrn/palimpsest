# tests/test_indexer_sharding.py
"""Tests for decade-sharded FAISS index build and gapjoin multi-shard search."""
import json
import sqlite3
from pathlib import Path
import pytest
import faiss
import numpy as np

from palimpsest.config import load
from palimpsest.db import migrate, connect
from palimpsest.indexer import _process_redactions, _load_shard_indexes

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

def mock_embed_fn(cfg, text):
    return [0.1, 0.2, 0.3, 0.4]

def test_gapjoin_with_shards(temp_cfg):
    conn = connect(temp_cfg)
    with conn:
        conn.execute("INSERT INTO documents (doc_id, status) VALUES (?, ?)", ("doc1", "indexed"))
        conn.execute("INSERT INTO documents (doc_id, status) VALUES (?, ?)", ("doc2", "indexed"))
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES (?, ?, ?)", ("doc1", 1, "the context text here"))
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES (?, ?, ?)", ("doc2", 1, "the context text here"))
        
        # Redaction in doc1 — context must be >= 40 chars combined to pass the skip gate
        conn.execute("INSERT INTO redactions (redaction_id, doc_id, page_no, kind, context_before, context_after) VALUES (?, ?, ?, ?, ?, ?)", (1, "doc1", 1, "text", "the context text before the redaction here", "and the context text after the redaction here"))
        
        # Entity in doc2 (the target for gap join)
        conn.execute("INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (10, "doc2", 1, "person", "john doe", "john doe", 0, 8))
        
        # Chunk in doc2
        conn.execute("INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text, shard_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (100, "doc2", 1, 0, 20, "the context text here", "shard1"))

    # Setup mock FAISS
    dim = 4
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    vec = np.array([[0.1, 0.2, 0.3, 0.4]], dtype=np.float32)
    vec = vec / np.linalg.norm(vec)
    index.add_with_ids(vec, np.array([100], dtype=np.int64))
    
    shard_indexes = [("shard1", index)]
    shard_idx_map = {"shard1": index}
    
    # Run gap join
    redactions = conn.execute("SELECT * FROM redactions").fetchall()
    
    _process_redactions(temp_cfg, conn, redactions, shard_indexes, shard_idx_map, mock_embed_fn)
    
    # Check results
    candidates = conn.execute("SELECT * FROM gap_candidates WHERE redaction_id = 1").fetchall()
    assert len(candidates) > 0
    assert candidates[0]["clear_entity_id"] == 10
    
    conn.close()
