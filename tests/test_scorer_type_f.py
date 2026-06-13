# tests/test_scorer_type_f.py
"""Tests for TypeFScorer (Type f — document series suppression gap)."""
import sqlite3
import pytest
from palimpsest.db import migrate
from palimpsest.tasks.features import normalize, process_features
from palimpsest.scorers.type_f import TypeFScorer
from palimpsest.scorers.base import Candidate


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.gapjoin = {"score_threshold": 0.65}


def _setup(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return cfg, conn


def test_seq_ref_normalization():
    # Verify prefix and padding normalization
    assert normalize("seq_ref", "nv0042452") == "NV0042452"
    assert normalize("seq_ref", "NV-12345") == "NV-12345"
    assert normalize("seq_ref", "Report No. 3") == "REPORT-NO-3"
    assert normalize("seq_ref", "Report Number 12") == "REPORT-NO-12"
    assert normalize("seq_ref", "nv- 999") == "NV-999"


def test_seq_ref_extraction():
    # Verify extraction from text
    class FeatureDummyConfig:
        features = {
            "redaction_context_chars": 300,
            "redaction_context_lines": 2,
        }
    cfg = FeatureDummyConfig()
    ocr_data = [{
        "page_no": 1,
        "lines": [
            {"text": "The details are in NV0042452 and Report No. 3.", "bbox": [0.1, 0.1, 0.9, 0.2]}
        ]
    }]
    res = process_features(None, ocr_data, cfg)
    entities = res["entities"]
    seq_refs = [e for e in entities if e["kind"] == "seq_ref"]
    assert len(seq_refs) == 2
    norms = {e["norm"] for e in seq_refs}
    assert "NV0042452" in norms
    assert "REPORT-NO-3" in norms


def test_type_f_no_accessions_returns_empty(tmp_path):
    cfg, conn = _setup(tmp_path)
    scorer = TypeFScorer()
    result = scorer.run(conn, cfg)
    assert result == []
    conn.close()


def test_type_f_gap_ratio_below_threshold_ignored(tmp_path):
    """Series with gap_ratio <= 20% produces no candidates."""
    cfg, conn = _setup(tmp_path)

    # NV0001–NV0005, all present (0% gap)
    with conn:
        for i in range(1, 6):
            acc = f"NV{i:04d}"
            conn.execute(
                "INSERT INTO documents (doc_id, accession) VALUES (?, ?)",
                (f"doc_{i}", acc),
            )
    scorer = TypeFScorer()
    result = scorer.run(conn, cfg)
    assert result == []
    conn.close()


def test_type_f_single_flanking_reference_scores_0_70(tmp_path):
    """Missing accession with one flanking cross-reference scores 0.70."""
    cfg, conn = _setup(tmp_path)

    # NV0001 and NV0003 present, NV0002 missing (gap_ratio = 1/3 ≈ 33%)
    # NV0001 has a seq_ref entity pointing to NV0002
    with conn:
        conn.execute("INSERT INTO documents (doc_id, accession) VALUES ('doc_1', 'NV0001')")
        conn.execute("INSERT INTO documents (doc_id, accession) VALUES ('doc_3', 'NV0003')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_1', 1, 'ref NV0002')")
        # seq_ref entity in doc_1 pointing at NV0002
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_1', 1, 'seq_ref', 'NV0002', 'NV0002')
        """)

    scorer = TypeFScorer()
    results = scorer.run(conn, cfg)

    assert len(results) == 1
    assert results[0].score == pytest.approx(0.70, abs=1e-6)
    assert results[0].type_key == "type_f"
    assert results[0].doc_ids == ["doc_1"]
    assert results[0].entity_ids == [1]

    # Verify candidates in DB
    rows = conn.execute("SELECT * FROM series_gap_candidates").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["series_prefix"] == "NV"
    assert row["missing_number"] == 2
    assert row["missing_accession"] == "NV0002"
    assert row["flanking_doc_id"] == "doc_1"
    assert row["ref_entity_id"] == 1
    assert pytest.approx(row["score"]) == 0.70
    assert row["status"] == "candidate"

    conn.close()


def test_type_f_both_flanking_references_scores_0_90(tmp_path):
    """Missing accession with both flanking cross-references scores 0.90."""
    cfg, conn = _setup(tmp_path)

    with conn:
        conn.execute("INSERT INTO documents (doc_id, accession) VALUES ('doc_1', 'NV0001')")
        conn.execute("INSERT INTO documents (doc_id, accession) VALUES ('doc_3', 'NV0003')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_1', 1, 'ref NV0002')")
        conn.execute("INSERT INTO pages (doc_id, page_no, text) VALUES ('doc_3', 1, 'ref NV0002')")
        # Both flanking docs have seq_ref to NV0002
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (1, 'doc_1', 1, 'seq_ref', 'NV0002', 'NV0002')
        """)
        conn.execute("""
            INSERT INTO entities (entity_id, doc_id, page_no, kind, text, norm)
            VALUES (2, 'doc_3', 1, 'seq_ref', 'NV0002', 'NV0002')
        """)

    scorer = TypeFScorer()
    results = scorer.run(conn, cfg)

    assert len(results) == 1
    assert results[0].score == pytest.approx(0.90, abs=1e-6)
    assert set(results[0].doc_ids) == {"doc_1", "doc_3"}
    conn.close()


def test_type_f_top_returns_ordered_candidates(tmp_path):
    cfg, conn = _setup(tmp_path)

    with conn:
        conn.execute("""
            INSERT INTO series_gap_candidates
              (series_prefix, missing_number, missing_accession,
               flanking_doc_id, ref_entity_id, score, status)
            VALUES ('NV', 2, 'NV0002', 'doc_1', 1, 0.90, 'candidate')
        """)
        conn.execute("""
            INSERT INTO series_gap_candidates
              (series_prefix, missing_number, missing_accession,
               flanking_doc_id, ref_entity_id, score, status)
            VALUES ('NV', 5, 'NV0005', 'doc_4', 2, 0.70, 'candidate')
        """)

    scorer = TypeFScorer()
    results = scorer.top(conn, limit=10)

    assert len(results) == 2
    assert all(isinstance(r, Candidate) for r in results)
    assert results[0].score == pytest.approx(0.90, abs=1e-6)
    assert results[1].score == pytest.approx(0.70, abs=1e-6)
    conn.close()


def test_type_f_top_respects_limit(tmp_path):
    cfg, conn = _setup(tmp_path)

    with conn:
        for i in range(5):
            conn.execute(f"""
                INSERT INTO series_gap_candidates
                  (series_prefix, missing_number, missing_accession,
                   flanking_doc_id, ref_entity_id, score, status)
                VALUES ('NV', {i+2}, 'NV{i+2:04d}', 'doc_{i}', {i+1}, {0.70 + i*0.01}, 'candidate')
            """)

    scorer = TypeFScorer()
    results = scorer.top(conn, limit=3)
    assert len(results) == 3
    conn.close()
