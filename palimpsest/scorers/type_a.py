# palimpsest/scorers/type_a.py
"""Type a scorer — redacted-text corroboration via gap join.

Extracted from palimpsest/indexer.py::run_gapjoin().
See specs/FINDING-TYPES.md §Type a for the detector and corroboration rule.

Scoring formula (per candidate entity):
    score = w_cosine * cosine(ctx, chunk) + w_anchor * anchor_overlap + w_kind * kind_prior
    Default weights (from config): w_cosine=0.5, w_anchor=0.3, w_kind=0.2
    Default threshold: 0.65

Dosage entities additionally receive:
    +0.10 * proximity_score  (exp(-dist/500) where dist = char distance to nearest subject/person)
    +0.15 if subject_ref/person norms overlap between redaction page and candidate page
    +0.15 if dosage norm appears in redaction context text
    (capped at 1.0)

Candidates above threshold are written to gap_candidates.
Person-kind candidates also enqueue a pending review in review_queue.
"""
from __future__ import annotations

import datetime
import logging
import math
import sqlite3
from collections import defaultdict
from typing import Callable, List

import faiss
import numpy as np

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)


def get_ollama_embedding(cfg: Config, text: str) -> List[float]:
    """Get embedding vector from local Ollama service."""
    import httpx
    ollama_url = cfg.models.get("ollama_url", "http://localhost:11434")
    try:
        resp = httpx.post(
            f"{ollama_url}/api/embeddings",
            json={
                "model": cfg.embed["model"],
                "prompt": text,
                "keep_alive": cfg.models.get("keep_alive", "24h"),
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        logger.error("Failed to fetch embedding from Ollama: %s", e)
        raise Exception(f"Ollama embedding failed: {e}")


def get_slot_expectation(kind: str, label: str) -> str | None:
    """Guess expected slot entity kind from a redaction stamp label."""
    if kind == "exemption_stamp" and label:
        clean_label = label.replace(" ", "").lower()
        if "b)(6)" in clean_label or "b)(7)" in clean_label:
            return "person"
    return None


def _fetch_entities_by_page(
    conn: sqlite3.Connection, pages: set[tuple[str, int]]
) -> dict[tuple[str, int], list[sqlite3.Row]]:
    """Bulk-load entity rows grouped by (doc_id, page_no) to avoid N+1 queries."""
    by_page: dict[tuple[str, int], list[sqlite3.Row]] = {}
    if not pages:
        return by_page
    flat: list[str | int] = []
    for doc_id, page_no in pages:
        flat.append(doc_id)
        flat.append(page_no)
    placeholders = ",".join("(?,?)" for _ in pages)
    cur = conn.execute(
        "SELECT entity_id, doc_id, page_no, kind, norm, char_start, char_end "
        f"FROM entities WHERE (doc_id, page_no) IN (VALUES {placeholders}) "
        "ORDER BY entity_id",
        flat,
    )
    for row in cur.fetchall():
        by_page.setdefault((row["doc_id"], row["page_no"]), []).append(row)
    return by_page


class TypeAScorer:
    type_key = "type_a"
    candidates_table = "gap_candidates"

    def __init__(self, embed_fn: Callable[[Config, str], List[float]] | None = None):
        """Initialise the scorer.

        Args:
            embed_fn: Optional embedding function for tests. If None, uses
                      get_ollama_embedding (requires a running Ollama service).
                      Tests must pass a mock here — do not call this in tests
                      without providing embed_fn.
        """
        self._embed_fn = embed_fn if embed_fn is not None else get_ollama_embedding

    def run(self, conn: sqlite3.Connection, config: Config) -> list[Candidate]:
        """Run the redaction-gap join algorithm.

        For each unjoined redaction:
          1. Skip if context < 40 chars (mark as processed anyway).
          2. Find anchor entities on the same page.
          3. Find candidate entities via anchor-overlap route and/or embedding route.
          4. Score each candidate; persist those above threshold to gap_candidates.
          5. Auto-enqueue person-kind candidates to review_queue.

        Returns Candidate objects for rows newly inserted in this call.
        """
        index_path = config.storage_root / "index" / "faiss.idx"
        if not index_path.exists():
            logger.error(
                "FAISS index not found at %s. Run 'palimpsest build' first.", index_path
            )
            return []

        index = faiss.read_index(str(index_path))
        assert index.metric_type == faiss.METRIC_INNER_PRODUCT, (
            f"FAISS index must use METRIC_INNER_PRODUCT (IndexFlatIP); "
            f"got metric_type={index.metric_type}. Rebuild the index."
        )

        cur = conn.execute("""
            SELECT r.redaction_id, r.doc_id, r.page_no, r.kind, r.label,
                   r.x0, r.y0, r.x1, r.y1, r.context_before, r.context_after
            FROM redactions r
            LEFT JOIN gapjoin_runs g ON r.redaction_id = g.redaction_id
            WHERE g.redaction_id IS NULL
            LIMIT 1000
        """)
        redactions = cur.fetchall()

        if not redactions:
            logger.info("No new redactions to join.")
            return []

        logger.info(
            "TypeAScorer.run(): processing %d redaction(s).", len(redactions)
        )
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        w_cosine = float(config.gapjoin.get("w_cosine", 0.5))
        w_anchor = float(config.gapjoin.get("w_anchor", 0.3))
        w_kind   = float(config.gapjoin.get("w_kind", 0.2))
        score_threshold = float(config.gapjoin.get("score_threshold", 0.65))
        topk = int(config.gapjoin.get("topk_embedding_candidates", 50))

        inserted: list[Candidate] = []

        for r in redactions:
            redaction_id = r["redaction_id"]
            ctx_before   = r["context_before"] or ""
            ctx_after    = r["context_after"] or ""
            ctx          = (ctx_before + " " + ctx_after).strip()

            if len(ctx) < 40:
                with conn:
                    conn.execute(
                        "INSERT INTO gapjoin_runs (redaction_id, run_at) VALUES (?, ?)",
                        (redaction_id, now_str),
                    )
                continue

            # Anchor entities on the redaction page
            page_entities = conn.execute(
                "SELECT entity_id, kind, text, norm, x0, y0, x1, y1 "
                "FROM entities WHERE doc_id = ? AND page_no = ?",
                (r["doc_id"], r["page_no"]),
            ).fetchall()

            heights = []
            for ent in page_entities:
                if ent["y1"] is not None and ent["y0"] is not None:
                    heights.append(ent["y1"] - ent["y0"])
            if r["y1"] is not None and r["y0"] is not None:
                heights.append(r["y1"] - r["y0"])
            median_h = float(np.median(heights)) if heights else 0.02
            if np.isnan(median_h) or median_h <= 0:
                median_h = 0.02

            ry_center = (
                (r["y0"] + r["y1"]) / 2
                if r["y0"] is not None and r["y1"] is not None
                else None
            )

            A: set[str] = set()
            ctx_before_lower = ctx_before.lower()
            ctx_after_lower  = ctx_after.lower()
            for ent in page_entities:
                is_near = False
                if (ry_center is not None
                        and ent["y0"] is not None and ent["y1"] is not None):
                    ey_center = (ent["y0"] + ent["y1"]) / 2
                    if abs(ey_center - ry_center) <= 2.5 * median_h:
                        is_near = True
                is_in_ctx = ent["text"].lower() in ctx_before_lower or \
                            ent["text"].lower() in ctx_after_lower
                if is_near or is_in_ctx:
                    A.add(ent["norm"])

            expectation = get_slot_expectation(r["kind"], r["label"])
            candidates: dict[int, dict] = {}

            # Anchor route
            if len(A) >= 2:
                placeholders = ",".join("?" for _ in A)
                anchor_query = f"""
                    SELECT e.entity_id, e.doc_id, e.page_no, e.kind, e.text,
                           e.norm, e.char_start, e.char_end, e.x0, e.y0, e.x1, e.y1
                    FROM entities e
                    JOIN (
                        SELECT doc_id, page_no FROM entities
                        WHERE doc_id != ? AND norm IN ({placeholders})
                        GROUP BY doc_id, page_no
                        HAVING COUNT(DISTINCT norm) >= 2
                    ) matched_pages
                    ON e.doc_id = matched_pages.doc_id
                    AND e.page_no = matched_pages.page_no
                """
                for ent in conn.execute(anchor_query, [r["doc_id"]] + list(A)).fetchall():
                    eid = ent["entity_id"]
                    candidates[eid] = {
                        "entity": ent,
                        "method": "anchor",
                        "score_cosine": None,
                        "hit_chunk_ids": [],
                    }

            # Embedding route
            ctx_emb = None
            try:
                ctx_emb = self._embed_fn(config, ctx)
            except Exception as e:
                logger.warning(
                    "Skipping embedding route for redaction %d: %s", redaction_id, e
                )

            if ctx_emb is not None:
                query_vec = np.array([ctx_emb], dtype=np.float32)
                norms_vec = np.linalg.norm(query_vec, axis=1, keepdims=True)
                query_vec = np.where(norms_vec > 0, query_vec / norms_vec, query_vec)
                _D, _idx = index.search(query_vec, topk)
                hit_chunk_ids = [int(cid) for cid in _idx[0] if cid != -1]

                if hit_chunk_ids:
                    placeholders = ",".join("?" for _ in hit_chunk_ids)
                    hit_chunks = conn.execute(
                        f"SELECT chunk_id, doc_id, page_no, char_start, char_end "
                        f"FROM chunks WHERE doc_id != ? AND chunk_id IN ({placeholders})",
                        [r["doc_id"]] + hit_chunk_ids,
                    ).fetchall()

                    page_pairs = {(ch["doc_id"], ch["page_no"]) for ch in hit_chunks}
                    if page_pairs:
                        pair_placeholders = " OR ".join(
                            "(doc_id = ? AND page_no = ?)" for _ in page_pairs
                        )
                        params: list = []
                        for d, p in page_pairs:
                            params.extend([d, p])
                        candidate_ents = conn.execute(
                            "SELECT entity_id, doc_id, page_no, kind, text, norm, "
                            f"char_start, char_end, x0, y0, x1, y1 "
                            f"FROM entities WHERE {pair_placeholders}",
                            params,
                        ).fetchall()

                        chunk_cosines = {
                            int(cid): float(_D[0][i])
                            for i, cid in enumerate(_idx[0])
                            if cid != -1
                        }
                        for ent in candidate_ents:
                            for ch in hit_chunks:
                                if (ent["doc_id"] == ch["doc_id"]
                                        and ent["page_no"] == ch["page_no"]):
                                    if (ent["char_start"] is not None
                                            and ent["char_end"] is not None
                                            and ent["char_start"] >= ch["char_start"]
                                            and ent["char_end"] <= ch["char_end"]):
                                        eid = ent["entity_id"]
                                        cosine = chunk_cosines[ch["chunk_id"]]
                                        if eid in candidates:
                                            candidates[eid]["method"] = "both"
                                            prev = candidates[eid].get("score_cosine")
                                            if prev is None or cosine > prev:
                                                candidates[eid]["score_cosine"] = cosine
                                        else:
                                            candidates[eid] = {
                                                "entity": ent,
                                                "method": "embedding",
                                                "score_cosine": cosine,
                                                "hit_chunk_ids": [ch["chunk_id"]],
                                            }

            # Pre-fetch entity rows for all candidate pages + redaction page to
            # eliminate N+1 queries in the scoring loop below.
            scoring_pages: set[tuple[str, int]] = {
                (cand["entity"]["doc_id"], cand["entity"]["page_no"])
                for cand in candidates.values()
            }
            scoring_pages.add((r["doc_id"], r["page_no"]))
            entities_by_page = _fetch_entities_by_page(conn, scoring_pages)

            # Score each candidate
            scored: list[dict] = []
            for eid, cand in candidates.items():
                e = cand["entity"]

                sc_cosine = cand["score_cosine"]
                if sc_cosine is None:
                    chunk_row = conn.execute(
                        "SELECT chunk_id FROM chunks "
                        "WHERE doc_id = ? AND page_no = ? "
                        "AND char_start <= ? AND char_end >= ? LIMIT 1",
                        (e["doc_id"], e["page_no"], e["char_start"], e["char_end"]),
                    ).fetchone()
                    if chunk_row and ctx_emb is not None:
                        try:
                            chunk_vec = index.reconstruct(int(chunk_row["chunk_id"]))
                            norm_c = np.linalg.norm(chunk_vec)
                            if norm_c > 0:
                                chunk_vec = chunk_vec / norm_c
                            ctx_arr  = np.array(ctx_emb, dtype=np.float32)
                            ctx_norm = np.linalg.norm(ctx_arr)
                            norm_ctx = ctx_arr / ctx_norm if ctx_norm > 0 else ctx_arr
                            sc_cosine = float(np.dot(norm_ctx, chunk_vec))
                        except Exception:
                            sc_cosine = 0.0
                    else:
                        sc_cosine = 0.0

                # Anchor overlap — resolved from pre-fetched page cache
                page_ents = entities_by_page.get((e["doc_id"], e["page_no"]), [])
                anchors_on_e_page = {row["norm"] for row in page_ents}
                sc_anchor = min(len(A & anchors_on_e_page) / max(len(A), 1.0), 1.0)
                sc_kind   = 1.0 if (expectation and e["kind"] == expectation) else 0.5

                tot_score = w_cosine * sc_cosine + w_anchor * sc_anchor + w_kind * sc_kind

                if e["kind"] == "dosage":
                    # Proximity to nearest subject/person — use cached page entities
                    subj_person = [
                        row for row in page_ents
                        if row["kind"] in ("subject_ref", "person")
                    ]
                    min_dist = None
                    for o in subj_person:
                        if (o["char_start"] is None or o["char_end"] is None
                                or e["char_start"] is None or e["char_end"] is None):
                            continue
                        if o["char_start"] < e["char_end"] and e["char_start"] < o["char_end"]:
                            dist = 0
                        else:
                            dist = min(
                                abs(e["char_start"] - o["char_end"]),
                                abs(o["char_start"] - e["char_end"]),
                            )
                        if min_dist is None or dist < min_dist:
                            min_dist = dist
                    proximity_score = math.exp(-min_dist / 500) if min_dist is not None else 0.0
                    tot_score += 0.1 * proximity_score

                    cand_subj = {
                        row["norm"] for row in subj_person
                    }
                    red_page_ents = entities_by_page.get((r["doc_id"], r["page_no"]), [])
                    red_subj = {
                        row["norm"] for row in red_page_ents
                        if row["kind"] in ("subject_ref", "person")
                    }
                    if cand_subj & red_subj:
                        tot_score += 0.15

                    has_red_dosage = any(
                        row["kind"] == "dosage" and row["norm"] == e["norm"]
                        for row in red_page_ents
                    )
                    in_context = (
                        e["norm"] in ctx_before.lower()
                        or e["norm"] in ctx_after.lower()
                    )
                    if has_red_dosage or in_context:
                        tot_score += 0.15

                    tot_score = min(tot_score, 1.0)

                scored.append({
                    "eid": eid, "cand": cand,
                    "tot_score": tot_score,
                    "sc_cosine": sc_cosine,
                    "sc_anchor": sc_anchor,
                    "sc_kind": sc_kind,
                })

            # Deduplicate dosage candidates by norm (keep highest score per norm)
            dosage_groups: dict[str, list] = defaultdict(list)
            non_dosage: list[dict] = []
            for sc in scored:
                if sc["cand"]["entity"]["kind"] == "dosage":
                    dosage_groups[sc["cand"]["entity"]["norm"]].append(sc)
                else:
                    non_dosage.append(sc)
            final = [max(g, key=lambda x: x["tot_score"])
                     for g in dosage_groups.values()] + non_dosage

            for sc in final:
                if sc["tot_score"] < score_threshold:
                    continue
                eid  = sc["eid"]
                cand = sc["cand"]
                e    = cand["entity"]
                with conn:
                    cur2 = conn.execute("""
                        INSERT OR REPLACE INTO gap_candidates
                          (redaction_id, clear_entity_id, score,
                           score_cosine, score_anchor, score_kind, method, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate')
                        RETURNING gap_id
                    """, (redaction_id, eid, sc["tot_score"],
                          sc["sc_cosine"], sc["sc_anchor"], sc["sc_kind"],
                          cand["method"]))
                    gap_row = cur2.fetchone()
                    gap_id  = gap_row["gap_id"] if gap_row else None

                    if e["kind"] == "person" and gap_id is not None:
                        if not conn.execute(
                            "SELECT 1 FROM review_queue WHERE entity_id = ?", (eid,)
                        ).fetchone():
                            conn.execute(
                                "INSERT INTO review_queue (entity_id, reason, status) "
                                "VALUES (?, ?, 'pending')",
                                (eid, f"person in gap candidate #{gap_id}"),
                            )
                    # Use gap_row directly — no SELECT changes() needed
                    if gap_row is not None:
                        inserted.append(Candidate(
                            type_key=self.type_key,
                            score=sc["tot_score"],
                            doc_ids=[r["doc_id"], e["doc_id"]],
                            page_refs=[
                                f"{r['doc_id']} p.{r['page_no']}",
                                f"{e['doc_id']} p.{e['page_no']}",
                            ],
                            summary=(
                                f"Gap: redaction in {r['doc_id']} p.{r['page_no']} "
                                f"→ {e['kind']} '{e['norm']}' in "
                                f"{e['doc_id']} p.{e['page_no']}, "
                                f"score={sc['tot_score']:.2f}"
                            ),
                            entity_ids=[eid],
                        ))

            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO gapjoin_runs (redaction_id, run_at) "
                    "VALUES (?, ?)",
                    (redaction_id, now_str),
                )

        logger.info("TypeAScorer.run() complete.")
        return inserted

    def top(self, conn: sqlite3.Connection, limit: int = 20) -> list[Candidate]:
        """Return top-N gap candidates ordered by score DESC."""
        rows = conn.execute(
            "SELECT gc.gap_id, gc.redaction_id, gc.clear_entity_id, gc.score, "
            "gc.method, r.doc_id AS red_doc_id, r.page_no AS red_page_no, "
            "e.doc_id AS ent_doc_id, e.page_no AS ent_page_no, "
            "e.kind, e.norm "
            "FROM gap_candidates gc "
            "JOIN redactions r ON gc.redaction_id = r.redaction_id "
            "JOIN entities e ON gc.clear_entity_id = e.entity_id "
            "WHERE gc.status = 'candidate' "
            "ORDER BY gc.score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_candidate(row) for row in rows]

    def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
        return Candidate(
            type_key=self.type_key,
            score=float(row["score"]),
            doc_ids=[row["red_doc_id"], row["ent_doc_id"]],
            page_refs=[
                f"{row['red_doc_id']} p.{row['red_page_no']}",
                f"{row['ent_doc_id']} p.{row['ent_page_no']}",
            ],
            summary=(
                f"Gap: redaction in {row['red_doc_id']} p.{row['red_page_no']} "
                f"→ {row['kind']} '{row['norm']}' in "
                f"{row['ent_doc_id']} p.{row['ent_page_no']}, "
                f"score={row['score']:.2f}"
            ),
            entity_ids=[row["clear_entity_id"]],
        )
