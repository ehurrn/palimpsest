# tests/test_e2e_finding_types.py
"""End-to-end finding-type tests (Phase 2).

These prove the *whole* chain for each new finding-type on synthetic documents:

    documents/pages  ->  tasks.features.process_features   (real detector)
                     ->  results.process_features          (real persistence)
                     ->  scorers.<type>.run()              (real scorer)
                     ->  a candidate row + Candidate object

Unlike the per-scorer unit tests (which hand-insert entities), these run the real
feature extractor so a broken detector regex *or* a features<->scorer contract
drift is caught. Embeddings (type b) are injected deterministically; the FAISS
index + chunks stand in for the embed stage, which is out of "features" scope.
"""
from __future__ import annotations

import sqlite3

import faiss
import numpy as np

from palimpsest.config import Config
from palimpsest.db import migrate
from palimpsest.results import process_features as persist_features
from palimpsest.scorers.base import Candidate
from palimpsest.scorers.type_b import TypeBScorer
from palimpsest.scorers.type_d import TypeDScorer
from palimpsest.scorers.type_e import TypeEScorer
from palimpsest.scorers.type_f import TypeFScorer
from palimpsest.tasks.features import process_features as extract_features

_NOW = "2026-06-22T00:00:00+00:00"


def _make_cfg(tmp_path) -> Config:
    """A real (frozen) Config so scorers/persistence get exactly what production gives."""
    return Config(
        raw={},
        storage_root=tmp_path,
        db_path=tmp_path / "db" / "palimpsest.db",
        broker={},
        mcp={},
        harvest={},
        ocr={},
        features={"redaction_context_chars": 300, "redaction_context_lines": 2},
        embed={"dim": 768, "model": "nomic-embed"},
        gapjoin={
            "w_cosine": 0.5,
            "w_anchor": 0.3,
            "w_kind": 0.2,
            "score_threshold": 0.65,
            "topk_embedding_candidates": 50,
        },
        models={"keep_alive": "24h"},
        nodes={},
        orchestrator={},
    )


def _open(cfg: Config) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _persist_doc(
    conn: sqlite3.Connection,
    cfg: Config,
    doc_id: str,
    lines: list[dict],
    *,
    year: int | None = None,
    accession: str | None = None,
) -> dict:
    """Insert a 1-page document, run the REAL extractor, persist via the REAL processor.

    Returns the raw feature-extraction result for sanity assertions.
    """
    ocr_data = [{"page_no": 1, "lines": lines}]
    page_text = "\n".join(line["text"] for line in lines)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO documents (doc_id, year, accession, status) "
            "VALUES (?, ?, ?, 'ocr_done')",
            (doc_id, year, accession),
        )
        conn.execute(
            "INSERT OR REPLACE INTO pages (doc_id, page_no, text) VALUES (?, 1, ?)",
            (doc_id, page_text),
        )
    result = extract_features(b"", ocr_data, cfg)
    with conn:
        persist_features(conn, cfg, doc_id, result, _NOW)
    return result


def _entity_kinds(conn: sqlite3.Connection, doc_id: str) -> set[str]:
    return {r["kind"] for r in conn.execute("SELECT kind FROM entities WHERE doc_id=?", (doc_id,))}


# ── Type e — regulatory-violation citation ────────────────────────────────────


def test_e2e_type_e_reg_violation(tmp_path):
    """reg_cite detector -> TypeEScorer -> violation_candidates, using the seeded 45 CFR 46."""
    cfg = _make_cfg(tmp_path)
    migrate(cfg)  # seeds regulation_citations (45 CFR 46 = reg_id 1)
    conn = _open(cfg)

    _persist_doc(
        conn,
        cfg,
        "19480001",
        [{"text": "The procedure was conducted under 45 CFR 46 and the Common Rule.",
          "bbox": [0.0, 0.0, 1.0, 0.05]}],
        year=1948,  # predates the rule -> pre_regulation
    )
    assert "reg_cite" in _entity_kinds(conn, "19480001"), "detector emitted no reg_cite entity"

    candidates = TypeEScorer().run(conn, cfg)

    assert candidates, "type_e produced no candidate from a seeded reg_cite"
    assert all(isinstance(c, Candidate) for c in candidates)
    assert any(c.doc_ids == ["19480001"] and c.score >= 0.65 for c in candidates)
    row = conn.execute(
        "SELECT * FROM violation_candidates WHERE doc_id='19480001'"
    ).fetchone()
    assert row is not None and row["score"] >= 0.65
    conn.close()


# ── Type f — document-series suppression ──────────────────────────────────────


def test_e2e_type_f_series_gap(tmp_path):
    """seq_ref detector -> TypeFScorer -> series_gap_candidates for a referenced missing accession."""
    cfg = _make_cfg(tmp_path)
    migrate(cfg)
    conn = _open(cfg)

    # Series NV0000001, NV0000002, (missing NV0000003), NV0000004 -> gap_ratio 0.25.
    # The prev-flank doc references the missing accession in its body text.
    _persist_doc(conn, cfg, "NV0000001",
                 [{"text": "First report in the test series.", "bbox": [0.0, 0.0, 1.0, 0.05]}],
                 accession="NV0000001")
    _persist_doc(conn, cfg, "NV0000002",
                 [{"text": "Continued from report NV0000003 of this series.", "bbox": [0.0, 0.0, 1.0, 0.05]}],
                 accession="NV0000002")
    _persist_doc(conn, cfg, "NV0000004",
                 [{"text": "Final report in the test series.", "bbox": [0.0, 0.0, 1.0, 0.05]}],
                 accession="NV0000004")

    # Detector sanity: the flank produced a seq_ref normalized to the missing accession.
    flank_seq = {r["norm"] for r in conn.execute(
        "SELECT norm FROM entities WHERE doc_id='NV0000002' AND kind='seq_ref'"
    )}
    assert "NV0000003" in flank_seq, f"seq_ref detector missed the cross-reference: {flank_seq}"

    candidates = TypeFScorer().run(conn, cfg)

    assert candidates, "type_f produced no series-gap candidate"
    assert any("NV0000003" in c.page_refs[0] for c in candidates)
    row = conn.execute(
        "SELECT * FROM series_gap_candidates WHERE missing_accession='NV0000003'"
    ).fetchone()
    assert row is not None and row["score"] >= 0.65
    conn.close()


# ── Type d — outcome-suppression gap ──────────────────────────────────────────


def test_e2e_type_d_outcome_gap(tmp_path):
    """protocol_code + date + future_ref detectors -> TypeDScorer -> outcome_gap_candidates."""
    cfg = _make_cfg(tmp_path)
    migrate(cfg)
    conn = _open(cfg)

    # Initiation doc: a protocol code, a date (spaCy DATE), and a future-report promise,
    # but NO outcome-indicator -> the outcome is "missing".
    _persist_doc(
        conn,
        cfg,
        "19530001",
        [
            {"text": "Protocol CAL 12 commenced on June 12, 1953 at the test site.",
             "bbox": [0.0, 0.0, 1.0, 0.05]},
            {"text": "A final report is to be submitted to the committee.",
             "bbox": [0.0, 0.05, 1.0, 0.10]},
        ],
        year=1953,
    )
    kinds = _entity_kinds(conn, "19530001")
    assert "protocol_code" in kinds, f"protocol_code detector missed: {kinds}"
    assert "date" in kinds, f"date (spaCy) detector missed: {kinds}"
    assert "outcome_ref" in kinds, f"outcome_ref detector missed: {kinds}"
    # The future-reference variant specifically (not an outcome indicator).
    assert any(
        r["norm"].startswith("future_ref:")
        for r in conn.execute(
            "SELECT norm FROM entities WHERE doc_id='19530001' AND kind='outcome_ref'"
        )
    )

    candidates = TypeDScorer().run(conn, cfg)

    assert candidates, "type_d produced no outcome-gap candidate"
    assert any("CAL-12" in c.summary for c in candidates)
    row = conn.execute(
        "SELECT * FROM outcome_gap_candidates WHERE protocol_code='CAL-12'"
    ).fetchone()
    assert row is not None and row["score"] >= 0.65
    conn.close()


# ── Type b — undisclosed radiation dosage ─────────────────────────────────────


def _mock_embed(cfg: Config, text: str) -> list[float]:
    """Deterministic unit embedding so the gapjoin cosine route is reproducible."""
    return [1.0] + [0.0] * 767


def _build_faiss(tmp_path, vectors: dict[int, np.ndarray], dim: int = 768) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    if vectors:
        ids = np.array(list(vectors.keys()), dtype=np.int64)
        vecs = np.array(list(vectors.values()), dtype=np.float32)
        index.add_with_ids(vecs, ids)
    faiss.write_index(index, str(index_dir / "faiss.idx"))


def test_e2e_type_b_dosage_gap(tmp_path):
    """Redaction + subject_ref/dosage detectors -> TypeB(=gapjoin) -> a dosage gap_candidate.

    doc_A hides a dose behind a [deleted] mark next to a subject; doc_B carries the
    clear "15 rem" beside the same subject. The embedding route links them.
    """
    cfg = _make_cfg(tmp_path)
    migrate(cfg)
    conn = _open(cfg)

    # Clear corroborating doc: subject + explicit dose.
    _persist_doc(
        conn,
        cfg,
        "20000002",
        [{"text": "Subject 7 received 15 rem during the controlled test exposure.",
          "bbox": [0.0, 0.0, 1.0, 0.05]}],
    )
    # Redacted doc: same subject, dose hidden behind a deleted-text mark.
    # Multiple lines so the redaction gets real surrounding context (the context
    # window is built from the lines around the marker, not the marker's own line).
    _persist_doc(
        conn,
        cfg,
        "20000001",
        [
            {"text": "Subject 7 was enrolled in the controlled exposure study at the test facility.",
             "bbox": [0.0, 0.00, 1.0, 0.04]},
            {"text": "The administered dose was [deleted] during the procedure performed that day.",
             "bbox": [0.0, 0.04, 1.0, 0.08]},
            {"text": "Subject 7 received the exposure as recorded in the official protocol log.",
             "bbox": [0.0, 0.08, 1.0, 0.12]},
        ],
    )

    # Detector sanity on both ends of the link.
    assert "deleted_text" in {
        r["kind"] for r in conn.execute("SELECT kind FROM redactions WHERE doc_id='20000001'")
    }
    assert {"subject_ref", "dosage"} <= _entity_kinds(conn, "20000002")

    # Stand in for the embed stage: one chunk covering doc_B's page + a matching vector.
    dose_ent = conn.execute(
        "SELECT entity_id FROM entities WHERE doc_id='20000002' AND kind='dosage'"
    ).fetchone()
    assert dose_ent is not None
    with conn:
        conn.execute(
            "INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) "
            "VALUES (9001, '20000002', 1, 0, 100000, 'Subject 7 received 15 rem during the controlled test exposure.')"
        )
    vec = np.zeros(768, dtype=np.float32)
    vec[0] = 1.0
    _build_faiss(tmp_path, {9001: vec})

    candidates = TypeBScorer(embed_fn=_mock_embed).run(conn, cfg)

    assert candidates, "type_b produced no dosage candidate"
    assert all(isinstance(c, Candidate) for c in candidates)
    assert any("dosage" in c.summary.lower() for c in candidates)
    # The candidate links to doc_B's clear dosage entity, written to gap_candidates.
    row = conn.execute(
        "SELECT * FROM gap_candidates WHERE clear_entity_id=?", (dose_ent["entity_id"],)
    ).fetchone()
    assert row is not None and row["score"] >= 0.65
    conn.close()
