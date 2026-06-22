"""Eval runner: generate → isolated DB → real scorers → grade → persist."""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import subprocess

from palimpsest.config import Config
from palimpsest.eval.embedding import deterministic_embed
from palimpsest.eval.generators import (
    gen_type_a_cases,
    gen_type_b_cases,
    gen_type_c_cases,
    gen_type_d_cases,
)
from palimpsest.eval.isolation import fresh_eval_db, make_eval_config, write_index
from palimpsest.eval.oracle import grade
from palimpsest.scorers.type_a import TypeAScorer
from palimpsest.scorers.type_c import TypeCScorer
from palimpsest.scorers.type_d import TypeDScorer


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _corpus_hash(cases) -> str:
    blob = json.dumps(
        [(c.case_uid, c.type_key, c.case_kind, c.truth) for c in cases],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_cases(conn, eval_cfg, run_id, cases, embed_fn):
    chunk_vectors: dict[int, list[float]] = {}
    chunk_seq = 1
    case_ids: dict[str, int] = {}
    with conn:
        for case in cases:
            cur = conn.execute(
                "INSERT INTO eval_cases (run_id, type_key, case_kind, spec, truth) "
                "VALUES (?,?,?,?,?)",
                (
                    run_id,
                    case.type_key,
                    case.case_kind,
                    json.dumps({"case_uid": case.case_uid}),
                    json.dumps(case.truth),
                ),
            )
            case_ids[case.case_uid] = cur.lastrowid
            seen = set()
            for doc in case.docs:
                if doc.doc_id in seen:
                    continue
                seen.add(doc.doc_id)
                conn.execute(
                    "INSERT INTO documents (doc_id, year, status) VALUES (?,?, 'indexed')",
                    (doc.doc_id, doc.year),
                )
            for page in case.pages:
                conn.execute(
                    "INSERT INTO pages (doc_id, page_no, text) VALUES (?,?,?)",
                    (page.doc_id, page.page_no, page.text),
                )
                for e in page.entities:
                    conn.execute(
                        "INSERT INTO entities "
                        "(doc_id,page_no,kind,text,norm,char_start,char_end) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            page.doc_id,
                            page.page_no,
                            e["kind"],
                            e["text"],
                            e["norm"],
                            e["char_start"],
                            e["char_end"],
                        ),
                    )
                cid = chunk_seq
                chunk_seq += 1
                conn.execute(
                    "INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) "
                    "VALUES (?,?,?,?,?,?)",
                    (cid, page.doc_id, page.page_no, 0, len(page.text), page.text),
                )
                chunk_vectors[cid] = embed_fn(eval_cfg, page.text)
                if page.redaction:
                    r = page.redaction
                    conn.execute(
                        "INSERT INTO redactions "
                        "(doc_id,page_no,kind,label,context_before,context_after) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            page.doc_id,
                            page.page_no,
                            r["kind"],
                            r["label"],
                            r["context_before"],
                            r["context_after"],
                        ),
                    )
    write_index(eval_cfg, chunk_vectors)
    return case_ids


def _grade_and_store(conn, run_id, cases, case_ids):
    with conn:
        for case in cases:
            cid = case_ids[case.case_uid]
            if case.type_key in ("type_a", "type_b"):
                rows = conn.execute(
                    "SELECT gc.score AS score, e.norm AS norm "
                    "FROM gap_candidates gc "
                    "JOIN redactions r ON gc.redaction_id = r.redaction_id "
                    "JOIN entities e ON gc.clear_entity_id = e.entity_id "
                    "WHERE r.doc_id = ?",
                    (f"{case.case_uid}_A",),
                ).fetchall()
                answer = case.truth["answer_norm"]
            elif case.type_key == "type_d":
                # Detection task: a candidate on the initiation doc == a flagged
                # gap. Grade the marker value "gap" against the case's truth.
                rows = conn.execute(
                    "SELECT score AS score, 'gap' AS norm FROM outcome_gap_candidates "
                    "WHERE initiation_doc_id = ?",
                    (f"{case.case_uid}_I",),
                ).fetchall()
                answer = case.truth.get("answer_norm")
            else:
                rows = conn.execute(
                    "SELECT ilc.score AS score, e.norm AS norm "
                    "FROM identity_link_candidates ilc "
                    "JOIN entities e ON ilc.named_entity_id = e.entity_id "
                    "WHERE ilc.subject_doc_id = ?",
                    (f"{case.case_uid}_S",),
                ).fetchall()
                answer = case.truth["true_named_norm"]
            preds = [(float(r[0]), r[1]) for r in rows]
            for res in grade(answer, preds):
                conn.execute(
                    "INSERT INTO eval_results "
                    "(run_id, case_id, type_key, raw_score, score_components, predicted, label, confidence) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        cid,
                        case.type_key,
                        res.raw_score,
                        None,
                        res.predicted,
                        res.label,
                        None,
                    ),
                )


def run_eval(
    cfg: Config,
    *,
    embed_fn=None,
    n_per_kind: int = 5,
    seed: int | None = None,
    types: tuple[str, ...] = ("type_a", "type_b", "type_c"),
) -> int:
    """Run a full synthetic eval; return the run_id. Writes only to the eval DB."""
    seed = seed if seed is not None else int(cfg.eval.get("default_seed", 1337))
    embed_fn = embed_fn or deterministic_embed
    eval_cfg = make_eval_config(cfg)
    fresh_eval_db(eval_cfg)

    cases = []
    if "type_a" in types:
        cases += gen_type_a_cases(n_per_kind, seed)
    if "type_b" in types:
        cases += gen_type_b_cases(n_per_kind, seed)
    if "type_c" in types:
        cases += gen_type_c_cases(n_per_kind, seed)
    if "type_d" in types:
        cases += gen_type_d_cases(n_per_kind, seed)

    conn = sqlite3.connect(eval_cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO eval_runs (started_at, scorer_git_sha, corpus_hash, seed, config_snapshot) "
            "VALUES (?,?,?,?,?)",
            (now, _git_sha(), _corpus_hash(cases), seed, json.dumps(cfg.eval)),
        )
        conn.commit()
        run_id = cur.lastrowid
        assert run_id is not None  # lastrowid is set after a successful INSERT

        case_ids = _load_cases(conn, eval_cfg, run_id, cases, embed_fn)

        if any(c.type_key in ("type_a", "type_b") for c in cases):
            TypeAScorer(embed_fn=embed_fn).run(conn, eval_cfg)
        if any(c.type_key == "type_c" for c in cases):
            TypeCScorer().run(conn, eval_cfg)
        if any(c.type_key == "type_d" for c in cases):
            TypeDScorer().run(conn, eval_cfg)

        _grade_and_store(conn, run_id, cases, case_ids)

        conn.execute(
            "UPDATE eval_runs SET finished_at=? WHERE run_id=?",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(), run_id),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()
