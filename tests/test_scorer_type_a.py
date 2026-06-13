# tests/test_scorer_type_a.py
"""Tests for TypeAScorer (Type a — redacted-text corroboration gap join)."""
import sqlite3
import numpy as np
import faiss
import pytest

from palimpsest.db import migrate
from palimpsest.scorers.type_a import TypeAScorer, get_slot_expectation
from palimpsest.scorers.base import Candidate


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.gapjoin = {
            "w_cosine": 0.5,
            "w_anchor": 0.3,
            "w_kind": 0.2,
            "score_threshold": 0.65,
            "topk_embedding_candidates": 50,
        }
        self.embed  = {"dim": 768, "model": "nomic-embed"}
        self.models = {"keep_alive": "24h"}


def _build_faiss(tmp_path, vectors: dict[int, np.ndarray], dim: int = 768):
    """Write a FAISS IndexIDMap2 with given {chunk_id: vector} to tmp_path/index/faiss.idx."""
    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    if vectors:
        ids = np.array(list(vectors.keys()), dtype=np.int64)
        vecs = np.array(list(vectors.values()), dtype=np.float32)
        index.add_with_ids(vecs, ids)
    faiss.write_index(index, str(index_dir / "faiss.idx"))


def _mock_embed(cfg, text):
    return [1.0] + [0.0] * 767


def test_slot_expectation_b6_b7():
    assert get_slot_expectation("exemption_stamp", "(b)(6)") == "person"
    assert get_slot_expectation("exemption_stamp", "(b)(7)") == "person"


def test_slot_expectation_other_returns_none():
    assert get_slot_expectation("exemption_stamp", "(b)(1)") is None
    assert get_slot_expectation("deleted_text", "DELETED") is None
    assert get_slot_expectation("black_box", None) is None


@pytest.fixture
def gapjoin_db(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    with conn:
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_A', 'indexed')")
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_B', 'indexed')")
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('doc_C', 'indexed')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_A', 1, 'This has a [deleted] redaction here.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_B', 1, 'Clear text with oak ridge and 1957-03-02.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_C', 1, 'Unrelated page contents.')")
        conn.execute("""
            INSERT INTO redactions
              (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1,
               context_before, context_after)
            VALUES (100, 'doc_A', 1, 'deleted_text', 'DELETED', 0.1, 0.1, 0.2, 0.2,
                    'preceding context info here', 'succeeding context info here')
        """)
        # Anchor entities on doc_A page 1
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (1, 'doc_A', 1, 'location', 'Oak Ridge', 'oak ridge', 10, 20, 0.1, 0.1, 0.2, 0.15)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (2, 'doc_A', 1, 'date', '1957-03-02', '1957-03-02', 25, 35, 0.1, 0.16, 0.2, 0.22)""")
        # Candidate entities on doc_B page 1 (shares both anchors)
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (3, 'doc_B', 1, 'location', 'Oak Ridge', 'oak ridge', 10, 20, 0.1, 0.1, 0.2, 0.15)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (4, 'doc_B', 1, 'date', '1957-03-02', '1957-03-02', 25, 35, 0.1, 0.16, 0.2, 0.22)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (5, 'doc_B', 1, 'dosage', '15 rem', '15 rem', 40, 50, 0.1, 0.23, 0.2, 0.28)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (6, 'doc_B', 1, 'person', 'John Smith', 'john smith', 55, 65, 0.1, 0.29, 0.2, 0.35)""")
        # Unrelated entity on doc_C
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1)
            VALUES (7, 'doc_C', 1, 'person', 'Alice Johnson', 'alice johnson', 10, 20, 0.1, 0.1, 0.2, 0.15)""")
        # Chunks
        conn.execute("""INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text)
            VALUES (500, 'doc_B', 1, 0, 100, 'Clear text with oak ridge and 1957-03-02.')""")
        conn.execute("""INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text)
            VALUES (600, 'doc_C', 1, 0, 100, 'Unrelated page contents.')""")

    conn.close()

    # FAISS: chunk 500 has cosine sim 0.8 with [1,0,0,...], chunk 600 is orthogonal
    v_b = np.zeros(768, dtype=np.float32); v_b[0] = 0.8; v_b[1] = 0.6
    v_c = np.zeros(768, dtype=np.float32); v_c[2] = 1.0
    _build_faiss(tmp_path, {500: v_b, 600: v_c})

    return cfg


def test_type_a_gapjoin_algorithm(gapjoin_db):
    cfg = gapjoin_db
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    scorer = TypeAScorer(embed_fn=_mock_embed)
    scorer.run(conn, cfg)

    # gapjoin_runs records redaction 100
    run = conn.execute("SELECT * FROM gapjoin_runs WHERE redaction_id = 100").fetchone()
    assert run is not None

    # 4 gap candidates: entities 3,4,5,6 from doc_B
    candidates = conn.execute(
        "SELECT * FROM gap_candidates ORDER BY clear_entity_id"
    ).fetchall()
    assert len(candidates) == 4

    # Entity 5 (dosage): score ≈ 0.899 (from test_gapjoin.py)
    cand5 = next(c for c in candidates if c["clear_entity_id"] == 5)
    assert pytest.approx(cand5["score_cosine"], abs=1e-6) == 0.8
    assert pytest.approx(cand5["score_anchor"], abs=1e-6) == 1.0
    assert pytest.approx(cand5["score_kind"],   abs=1e-6) == 0.5
    assert pytest.approx(cand5["score"],        abs=1e-6) == 0.899004989
    assert cand5["method"] == "both"

    # Entity 6 (person): score ≈ 0.8
    cand6 = next(c for c in candidates if c["clear_entity_id"] == 6)
    assert pytest.approx(cand6["score"], abs=1e-6) == 0.8
    assert cand6["method"] == "both"

    # Person entity 6 auto-queued for HITL review
    reviews = conn.execute("SELECT * FROM review_queue").fetchall()
    assert len(reviews) == 1
    assert reviews[0]["entity_id"] == 6
    assert "person in gap candidate #" in reviews[0]["reason"]
    assert reviews[0]["status"] == "pending"

    conn.close()


def test_type_a_idempotent(gapjoin_db):
    cfg = gapjoin_db
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    scorer = TypeAScorer(embed_fn=_mock_embed)
    scorer.run(conn, cfg)
    # Reset gapjoin_runs to allow a second run
    conn.execute("DELETE FROM gapjoin_runs")
    conn.commit()
    scorer.run(conn, cfg)

    assert conn.execute("SELECT COUNT(*) FROM gap_candidates").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1
    conn.close()


def test_type_a_short_context_skip(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        INSERT INTO redactions
          (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1,
           context_before, context_after)
        VALUES (200, 'doc_D', 1, 'deleted_text', 'DELETED', 0.1, 0.1, 0.2, 0.2,
                'short before', 'short after')
    """)
    conn.commit()

    _build_faiss(tmp_path, {})   # empty index

    scorer = TypeAScorer(embed_fn=_mock_embed)
    scorer.run(conn, cfg)

    conn.row_factory = sqlite3.Row
    assert conn.execute(
        "SELECT COUNT(*) FROM gapjoin_runs WHERE redaction_id = 200"
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM gap_candidates").fetchone()[0] == 0
    conn.close()


def test_type_a_top_returns_ordered(gapjoin_db):
    cfg = gapjoin_db
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    scorer = TypeAScorer(embed_fn=_mock_embed)
    scorer.run(conn, cfg)

    results = scorer.top(conn, limit=10)
    assert len(results) > 0
    assert all(isinstance(r, Candidate) for r in results)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    conn.close()
