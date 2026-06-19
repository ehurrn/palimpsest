# tests/test_indexer_sharding.py
"""Tests for decade-sharded FAISS index build and gapjoin multi-shard search."""
import json
import sqlite3
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
    lines = [json.loads(line) for line in pending.read_text().splitlines() if line.strip()]
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
