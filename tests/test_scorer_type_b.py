# tests/test_scorer_type_b.py
"""Tests for TypeBScorer (Type b — undisclosed dosage)."""
import sqlite3
import numpy as np
import faiss
from palimpsest.db import migrate
from palimpsest.scorers.type_b import TypeBScorer
from palimpsest.scorers.base import Candidate
from palimpsest.tasks.features import normalize, process_features


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.gapjoin = {
            "w_cosine": 0.5, "w_anchor": 0.3, "w_kind": 0.2,
            "score_threshold": 0.65, "topk_embedding_candidates": 50,
        }
        self.embed  = {"dim": 768, "model": "nomic-embed"}
        self.models = {"keep_alive": "24h"}
        self.features = {"redaction_context_chars": 300, "redaction_context_lines": 2}


def _mock_embed(cfg, text):
    return [1.0] + [0.0] * 767


def _build_faiss(tmp_path, vectors: dict[int, np.ndarray], dim: int = 768):
    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    if vectors:
        ids  = np.array(list(vectors.keys()), dtype=np.int64)
        vecs = np.array(list(vectors.values()), dtype=np.float32)
        index.add_with_ids(vecs, ids)
    faiss.write_index(index, str(index_dir / "faiss.idx"))


def test_subject_ref_normalization():
    assert normalize("subject_ref", "Subject 5")  == "subject 5"
    assert normalize("subject_ref", "Patient X12") == "patient x12"
    assert normalize("subject_ref", "Case  9")    == "case 9"


def test_subject_ref_extraction():
    cfg = DummyConfig.__new__(DummyConfig)
    cfg.features = {"redaction_context_chars": 300, "redaction_context_lines": 2}
    ocr_data = [{"page_no": 1, "lines": [
        {"text": "Subject 5 had a high dose.", "bbox": [0.1, 0.1, 0.9, 0.2]}
    ]}]
    res = process_features(None, ocr_data, cfg)
    subjects = [e for e in res["entities"] if e["kind"] == "subject_ref"]
    assert len(subjects) == 1
    assert subjects[0]["text"] == "Subject 5"
    assert subjects[0]["norm"] == "subject 5"


def test_type_b_dosage_proximity_and_deduplication(tmp_path):
    """TypeBScorer deduplicates dosage candidates by norm, keeping highest score."""
    cfg = DummyConfig(tmp_path)
    migrate(cfg)

    conn = sqlite3.connect(cfg.db_path)
    with conn:
        for doc in ("doc_A", "doc_B", "doc_C"):
            conn.execute(f"INSERT INTO documents (doc_id, status) VALUES ('{doc}', 'indexed')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_A', 1, 'Redacted context page with some info.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_B', 1, 'Subject X received 15 rem.')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_C', 1, 'A page talking about 15 rem with no subject details.')")
        conn.execute("""INSERT INTO redactions
            (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after)
            VALUES (200, 'doc_A', 1, 'deleted_text', 'DELETED', 0.1, 0.1, 0.2, 0.2,
                    'preceding context info here', 'succeeding context info here')""")
        # doc_A subject+dosage (triggers co-occurrence and dosage-norm boosts)
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (301, 'doc_A', 1, 'subject_ref', 'Subject X', 'subject x', 5, 14)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (302, 'doc_A', 1, 'dosage', '15 rem', '15 rem', 16, 22)""")
        # doc_B: dosage close to subject (distance = 10, proximity high)
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (101, 'doc_B', 1, 'dosage', '15 rem', '15 rem', 19, 25)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (102, 'doc_B', 1, 'subject_ref', 'Subject X', 'subject x', 0, 9)""")
        # doc_C: same dosage, farther from subject
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (201, 'doc_C', 1, 'dosage', '15 rem', '15 rem', 19, 25)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm, char_start, char_end)
            VALUES (202, 'doc_C', 1, 'subject_ref', 'Subject Y', 'subject y', 100, 109)""")
        conn.execute("""INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text)
            VALUES (1001, 'doc_B', 1, 0, 50, 'Subject X received 15 rem.')""")
        conn.execute("""INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text)
            VALUES (2001, 'doc_C', 1, 0, 50, 'A page talking about 15 rem.')""")
    conn.close()

    v_b = np.zeros(768, dtype=np.float32)
    v_b[0] = 0.8
    v_c = np.zeros(768, dtype=np.float32)
    v_c[0] = 0.8
    _build_faiss(tmp_path, {1001: v_b, 2001: v_c})

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    scorer = TypeBScorer(embed_fn=_mock_embed)
    scorer.run(conn, cfg)

    # Deduplication: only one candidate per dosage norm — doc_B wins (higher proximity)
    candidates = conn.execute(
        "SELECT * FROM gap_candidates WHERE redaction_id = 200"
    ).fetchall()
    assert len(candidates) == 1
    assert candidates[0]["clear_entity_id"] == 101  # doc_B dosage entity
    conn.close()


def test_type_b_top_filters_to_dosage_only(tmp_path):
    """top() returns only dosage-kind entities."""
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    with conn:
        conn.execute("INSERT INTO documents (doc_id) VALUES ('doc_X')")
        conn.execute("INSERT INTO documents (doc_id) VALUES ('doc_Y')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_X', 1, 'x')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_Y', 1, 'y')")
        conn.execute("""INSERT INTO redactions
            (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1)
            VALUES (1, 'doc_X', 1, 'deleted_text', 'DEL', 0,0,1,1)""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (10, 'doc_Y', 1, 'dosage', '10 rem', '10 rem')""")
        conn.execute("""INSERT INTO entities
            (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (20, 'doc_Y', 1, 'person', 'Bob', 'bob')""")
        # Insert candidates manually: one dosage, one person
        conn.execute("""INSERT INTO gap_candidates
            (redaction_id, clear_entity_id, score, score_cosine, score_anchor,
             score_kind, method, status)
            VALUES (1, 10, 0.80, 0.8, 1.0, 0.5, 'both', 'candidate')""")
        conn.execute("""INSERT INTO gap_candidates
            (redaction_id, clear_entity_id, score, score_cosine, score_anchor,
             score_kind, method, status)
            VALUES (1, 20, 0.75, 0.7, 0.9, 0.5, 'both', 'candidate')""")

    scorer = TypeBScorer(embed_fn=_mock_embed)
    results = scorer.top(conn, limit=10)

    # Only the dosage entity (10) should be returned
    assert len(results) == 1
    assert results[0].type_key == "type_b"
    assert results[0].entity_ids == [10]
    assert all(isinstance(r, Candidate) for r in results)
    conn.close()


def test_type_b_top_respects_limit(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    with conn:
        conn.execute("INSERT INTO documents (doc_id) VALUES ('doc_X')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_X', 1, 'x')")
        conn.execute("""INSERT INTO redactions
            (redaction_id, doc_id, page_no, kind, label, x0, y0, x1, y1)
            VALUES (1, 'doc_X', 1, 'deleted_text', 'DEL', 0,0,1,1)""")
        for i in range(5):
            conn.execute(f"""INSERT INTO entities
                (entity_id, doc_id, page_no, kind, text, norm)
                VALUES ({i+1}, 'doc_X', 1, 'dosage', '{i} rem', '{i} rem')""")
            conn.execute(f"""INSERT INTO gap_candidates
                (redaction_id, clear_entity_id, score, score_cosine,
                 score_anchor, score_kind, method, status)
                VALUES (1, {i+1}, {0.70 + i*0.01}, 0.7, 0.9, 0.5, 'both', 'candidate')""")

    scorer = TypeBScorer(embed_fn=_mock_embed)
    results = scorer.top(conn, limit=3)
    assert len(results) == 3
    conn.close()
