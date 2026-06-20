# palimpsest/indexer.py
import argparse
import datetime
import json
import logging
import math
import sqlite3
from collections import defaultdict
from typing import Callable, List

import faiss
import httpx
import numpy as np

from palimpsest.config import Config, load
from palimpsest.db import connect

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def get_ollama_embedding(cfg: Config, text: str) -> List[float]:
    """Get embedding vector from local Ollama service on gonktop."""
    try:
        resp = httpx.post(
            "http://localhost:11434/api/embeddings",
            json={
                "model": cfg.embed["model"],
                "prompt": text,
                "keep_alive": cfg.models.get("keep_alive", "24h")
            },
            timeout=10.0
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception as e:
        logger.error(f"Failed to fetch embedding from Ollama: {e}")
        # Return dummy vector for fallback or fail depending on environment
        raise Exception(f"Ollama embedding failed: {e}")

def get_slot_expectation(kind: str, label: str) -> str | None:
    """Guess the expected slot entity kind based on redaction stamp label."""
    if kind == "exemption_stamp" and label:
        clean_label = label.replace(" ", "").lower()
        if "b)(6)" in clean_label or "b)(7)" in clean_label:
            return "person"
    return None

def _build_shard(shard_dir, cfg: Config) -> list[int]:
    """Build/update FAISS index for one directory. Returns list of chunk_ids indexed."""
    from pathlib import Path
    shard_dir = Path(shard_dir)
    faiss_path = shard_dir / "faiss.idx"
    pending_path = shard_dir / "pending_embeddings.jsonl"
    processing_path = shard_dir / "pending_embeddings.processing"
    done_path = shard_dir / "pending_embeddings.done"

    if not pending_path.exists() or pending_path.stat().st_size == 0:
        if not processing_path.exists() or processing_path.stat().st_size == 0:
            return []

    if pending_path.exists() and pending_path.stat().st_size > 0:
        if processing_path.exists():
            processing_path.unlink()
        pending_path.rename(processing_path)

    chunk_ids: list[int] = []
    embeddings: list[list[float]] = []
    with open(processing_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            chunk_ids.append(rec["chunk_id"])
            embeddings.append(rec["embedding"])

    if not chunk_ids:
        if processing_path.exists():
            processing_path.unlink()
        return []

    if faiss_path.exists():
        index = faiss.read_index(str(faiss_path))
    else:
        index = faiss.IndexIDMap2(faiss.IndexFlatIP(cfg.embed.get("dim", 768)))

    vecs = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = np.where(norms > 0, vecs / norms, vecs)
    index.add_with_ids(vecs, np.array(chunk_ids, dtype=np.int64))
    faiss.write_index(index, str(faiss_path))

    if done_path.exists():
        done_path.unlink()
    processing_path.rename(done_path)
    pending_path.touch()
    with open(pending_path, "w") as f:
        f.truncate(0)

    # Record which shard each chunk belongs to for deterministic routing
    shard_name = shard_dir.name
    db_conn = connect(cfg)
    with db_conn:
        placeholders = ",".join("?" for _ in chunk_ids)
        db_conn.execute(
            f"UPDATE chunks SET shard_id = ? WHERE chunk_id IN ({placeholders})",
            [shard_name] + chunk_ids,
        )

    return chunk_ids


def build_index(cfg: Config):
    """Fold pending_embeddings.jsonl into FAISS, routing to decade shards when available."""
    index_dir = cfg.storage_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)

    all_chunk_ids: list[int] = []

    shards_root = index_dir / "shards"
    if shards_root.exists():
        for shard_dir in sorted(shards_root.iterdir()):
            if shard_dir.is_dir():
                ids = _build_shard(shard_dir, cfg)
                if ids:
                    logger.info(f"Shard {shard_dir.name}: indexed {len(ids)} vectors.")
                    all_chunk_ids.extend(ids)

    legacy_ids = _build_shard(index_dir, cfg)
    if legacy_ids:
        logger.info(f"Legacy flat index: indexed {len(legacy_ids)} vectors.")
        all_chunk_ids.extend(legacy_ids)

    if not all_chunk_ids:
        logger.info("No pending embeddings to index.")
        return

    conn = connect(cfg)
    placeholders = ",".join("?" for _ in all_chunk_ids)
    cur = conn.execute(
        f"SELECT DISTINCT doc_id FROM chunks WHERE chunk_id IN ({placeholders})",
        all_chunk_ids,
    )
    doc_ids = [row["doc_id"] for row in cur.fetchall()]

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        for doc_id in doc_ids:
            conn.execute(
                "UPDATE documents SET status='indexed', indexed_at=? WHERE doc_id=?",
                (now, doc_id),
            )

    logger.info(f"Index build completed. Total vectors indexed: {len(all_chunk_ids)}.")

def _fetch_entities_by_page(
    conn: sqlite3.Connection, pages: set[tuple[str, int]]
) -> dict[tuple[str, int], list[sqlite3.Row]]:
    """Bulk-load entities grouped by (doc_id, page_no).

    Replaces the per-candidate entity SELECTs in run_gapjoin's scoring loop (an
    N+1 over candidates) with a single row-value ``IN (VALUES ...)`` query over
    every page being scored. Rows are ordered by entity_id within each page to
    match the insertion-order scan the per-page queries previously returned.

    Args:
        conn: Active SQLite connection.
        pages: Distinct (doc_id, page_no) keys to load.

    Returns:
        Mapping of (doc_id, page_no) -> list of entity rows on that page.
    """
    by_page: dict[tuple[str, int], list[sqlite3.Row]] = {}
    if not pages:
        return by_page
    flat: list[str | int] = []
    for doc_id, page_no in pages:
        flat.append(doc_id)
        flat.append(page_no)
    placeholders = ",".join("(?,?)" for _ in pages)
    cur = conn.execute(
        "SELECT entity_id, doc_id, page_no, kind, text, norm, char_start, char_end "
        f"FROM entities WHERE (doc_id, page_no) IN (VALUES {placeholders}) "
        "ORDER BY entity_id",
        flat,
    )
    for row in cur.fetchall():
        by_page.setdefault((row["doc_id"], row["page_no"]), []).append(row)
    return by_page


def _fetch_chunks_by_page(
    conn: sqlite3.Connection, pages: set[tuple[str, int]]
) -> dict[tuple[str, int], list[sqlite3.Row]]:
    """Bulk-load chunk spans grouped by (doc_id, page_no), ordered by chunk_id.

    Lets the scoring loop find the first chunk covering an entity span in memory
    (replicating the old ``... LIMIT 1`` query) instead of one SELECT per
    anchor-only candidate.
    """
    by_page: dict[tuple[str, int], list[sqlite3.Row]] = {}
    if not pages:
        return by_page
    flat: list[str | int] = []
    for doc_id, page_no in pages:
        flat.append(doc_id)
        flat.append(page_no)
    placeholders = ",".join("(?,?)" for _ in pages)
    cur = conn.execute(
        "SELECT chunk_id, doc_id, page_no, char_start, char_end "
        f"FROM chunks WHERE (doc_id, page_no) IN (VALUES {placeholders}) "
        "ORDER BY chunk_id",
        flat,
    )
    for row in cur.fetchall():
        by_page.setdefault((row["doc_id"], row["page_no"]), []).append(row)
    return by_page


def _load_shard_indexes(cfg: Config) -> tuple[list[tuple[str, faiss.Index]], dict[str, faiss.Index]] | None:
    """Load all FAISS shard indexes from storage.

    Args:
        cfg: Loaded configuration.

    Returns:
        Tuple of (shard_indexes list, shard_idx_map dict), or None if no indexes found.
    """
    shard_indexes: list[tuple[str, faiss.Index]] = []
    shards_root = cfg.storage_root / "index" / "shards"
    if shards_root.exists():
        for shard_dir in sorted(shards_root.iterdir()):
            idx_path = shard_dir / "faiss.idx"
            if shard_dir.is_dir() and idx_path.exists():
                shard_indexes.append((shard_dir.name, faiss.read_index(str(idx_path))))
    legacy_path = cfg.storage_root / "index" / "faiss.idx"
    if legacy_path.exists():
        shard_indexes.append(("legacy", faiss.read_index(str(legacy_path))))
    if not shard_indexes:
        return None
    shard_idx_map: dict[str, faiss.Index] = {name: idx for name, idx in shard_indexes}
    return shard_indexes, shard_idx_map


def _process_redactions(
    cfg: Config,
    conn: sqlite3.Connection,
    redactions: list,
    shard_indexes: list[tuple[str, faiss.Index]],
    shard_idx_map: dict[str, faiss.Index],
    embed_fn: Callable[[Config, str], List[float]],
) -> int:
    """Process a list of redaction rows through the gap-join algorithm.

    Args:
        cfg: Loaded configuration.
        conn: Active DB connection (caller owns it).
        redactions: List of redaction rows from the DB.
        shard_indexes: Ordered list of (name, index) pairs for FAISS search.
        shard_idx_map: Name-keyed map of indexes for chunk reconstruction.
        embed_fn: Embedding function to call for context vectors.

    Returns:
        Number of redactions processed.
    """
    conn.row_factory = sqlite3.Row
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    w_cosine = cfg.gapjoin.get("w_cosine", 0.5)
    w_anchor = cfg.gapjoin.get("w_anchor", 0.3)
    w_kind = cfg.gapjoin.get("w_kind", 0.2)
    score_threshold = cfg.gapjoin.get("score_threshold", 0.65)
    topk = cfg.gapjoin.get("topk_embedding_candidates", 50)

    for r in redactions:
        redaction_id = r["redaction_id"]

        # 1. Context
        ctx_before = r["context_before"] or ""
        ctx_after = r["context_after"] or ""
        ctx = (ctx_before + " " + ctx_after).strip()

        if len(ctx) < 40:
            # Skip: un-contextualized boxes are noise
            with conn:
                conn.execute(
                    "INSERT INTO gapjoin_runs (redaction_id, run_at) VALUES (?, ?)",
                    (redaction_id, now_str)
                )
            continue

        # 2. Anchors (entities in same ±2-line band)
        # Fetch all entities on the same page
        cur = conn.execute(
            "SELECT entity_id, kind, text, norm, x0, y0, x1, y1 FROM entities WHERE doc_id = ? AND page_no = ?",
            (r["doc_id"], r["page_no"])
        )
        page_entities = cur.fetchall()

        # Estimate median line height on the page
        heights = []
        for ent in page_entities:
            if ent["y1"] is not None and ent["y0"] is not None:
                heights.append(ent["y1"] - ent["y0"])
        if r["y1"] is not None and r["y0"] is not None:
            heights.append(r["y1"] - r["y0"])

        median_h = np.median(heights) if heights else 0.02
        if np.isnan(median_h) or median_h <= 0:
            median_h = 0.02

        ry_center = (r["y0"] + r["y1"]) / 2 if (r["y0"] is not None and r["y1"] is not None) else None

        A = set()
        ctx_before_lower = ctx_before.lower()
        ctx_after_lower = ctx_after.lower()

        for ent in page_entities:
            # Check coordinate proximity (within 2.5 line heights)
            is_near = False
            if ry_center is not None and ent["y0"] is not None and ent["y1"] is not None:
                ey_center = (ent["y0"] + ent["y1"]) / 2
                if abs(ey_center - ry_center) <= 2.5 * median_h:
                    is_near = True

            # Check substring presence
            is_in_ctx = False
            ent_text_lower = ent["text"].lower()
            if ent_text_lower in ctx_before_lower or ent_text_lower in ctx_after_lower:
                is_in_ctx = True

            if is_near or is_in_ctx:
                A.add(ent["norm"])

        # 3. Slot kind guess
        expectation = get_slot_expectation(r["kind"], r["label"])

        # Candidates dict to deduplicate: entity_id -> {entity, method, score_cosine, score_anchor}
        candidates: dict[int, dict] = {}

        # 4a. Anchor Route
        if len(A) >= 2:
            placeholders = ",".join("?" for _ in A)
            query = f"""
            SELECT e.entity_id, e.doc_id, e.page_no, e.kind, e.text, e.norm, e.char_start, e.char_end, e.x0, e.y0, e.x1, e.y1
            FROM entities e
            JOIN (
                SELECT doc_id, page_no
                FROM entities
                WHERE doc_id != ? AND norm IN ({placeholders})
                GROUP BY doc_id, page_no
                HAVING COUNT(DISTINCT norm) >= 2
            ) matched_pages ON e.doc_id = matched_pages.doc_id AND e.page_no = matched_pages.page_no
            """
            cur = conn.execute(query, [r["doc_id"]] + list(A))
            for ent in cur.fetchall():
                eid = ent["entity_id"]
                candidates[eid] = {
                    "entity": ent,
                    "method": "anchor",
                    "score_cosine": None, # Will compute later
                    "hit_chunk_ids": []
                }

        # 4b. Embedding Route
        try:
            ctx_emb = embed_fn(cfg, ctx)
        except Exception as e:
            logger.warning(f"Skipping embedding route for redaction {redaction_id} due to embedding failure: {e}")
            ctx_emb = None

        if ctx_emb is not None:
            # Multi-shard FAISS search with global top-K merge
            query_vec = np.array([ctx_emb], dtype=np.float32)
            norms = np.linalg.norm(query_vec, axis=1, keepdims=True)
            query_vec = np.where(norms > 0, query_vec / norms, query_vec)

            _all_pairs: list[tuple[float, int]] = []
            for _, shard_idx in shard_indexes:
                _D_s, _I_s = shard_idx.search(query_vec, topk)
                for d, i in zip(_D_s[0], _I_s[0]):
                    if i != -1:
                        _all_pairs.append((float(d), int(i)))
            _all_pairs.sort(key=lambda x: x[0], reverse=True)
            _all_pairs = _all_pairs[:topk]
            hit_chunk_ids = [i for _, i in _all_pairs]

            if hit_chunk_ids:
                placeholders = ",".join("?" for _ in hit_chunk_ids)
                query = f"""
                SELECT chunk_id, doc_id, page_no, char_start, char_end
                FROM chunks
                WHERE doc_id != ? AND chunk_id IN ({placeholders})
                """
                cur = conn.execute(query, [r["doc_id"]] + hit_chunk_ids)
                hit_chunks = cur.fetchall()

                page_pairs = set((ch["doc_id"], ch["page_no"]) for ch in hit_chunks)

                if page_pairs:
                    pair_placeholders = " OR ".join("(doc_id = ? AND page_no = ?)" for _ in page_pairs)
                    params = []
                    for d, p in page_pairs:
                        params.extend([d, p])

                    cur = conn.execute(
                        f"SELECT entity_id, doc_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1 FROM entities WHERE {pair_placeholders}",
                        params
                    )
                    candidate_entities_on_pages = cur.fetchall()

                    chunk_cosines = {i: d for d, i in _all_pairs}

                    for ent in candidate_entities_on_pages:
                        for ch in hit_chunks:
                            if ent["doc_id"] == ch["doc_id"] and ent["page_no"] == ch["page_no"]:
                                if ent["char_start"] is not None and ent["char_end"] is not None:
                                    if ent["char_start"] >= ch["char_start"] and ent["char_end"] <= ch["char_end"]:
                                        # Inside hit chunk!
                                        eid = ent["entity_id"]
                                        cosine = chunk_cosines[ch["chunk_id"]]

                                        if eid in candidates:
                                            # Met by both routes!
                                            candidates[eid]["method"] = "both"
                                            prev_cosine = candidates[eid].get("score_cosine")
                                            if prev_cosine is None or cosine > prev_cosine:
                                                candidates[eid]["score_cosine"] = cosine
                                        else:
                                            candidates[eid] = {
                                                "entity": ent,
                                                "method": "embedding",
                                                "score_cosine": cosine,
                                                "hit_chunk_ids": [ch["chunk_id"]]
                                            }


        # 5. Score Candidates
        # Pre-fetch entities + chunks for every page we'll score against (all
        # candidate pages plus the redaction's own page) in two bulk queries,
        # so the loop below does in-memory lookups instead of per-candidate SQL.
        scoring_pages: set[tuple[str, int]] = {
            (cand["entity"]["doc_id"], cand["entity"]["page_no"])
            for cand in candidates.values()
        }
        scoring_pages.add((r["doc_id"], r["page_no"]))
        entities_by_page = _fetch_entities_by_page(conn, scoring_pages)
        chunks_by_page = _fetch_chunks_by_page(conn, scoring_pages)

        scored_candidates: list[dict] = []
        for eid, cand in list(candidates.items()):
            e = cand["entity"]

            # score_cosine: if not computed (anchor-only candidate), try to find its chunk and get cosine
            sc_cosine = cand["score_cosine"]
            if sc_cosine is None:
                # Find the first chunk covering this entity's span (matches the
                # old "... LIMIT 1" first-match over chunk_id order).
                chunk_row = None
                if e["char_start"] is not None and e["char_end"] is not None:
                    for ch in chunks_by_page.get((e["doc_id"], e["page_no"]), []):
                        if (
                            ch["char_start"] is not None
                            and ch["char_end"] is not None
                            and ch["char_start"] <= e["char_start"]
                            and ch["char_end"] >= e["char_end"]
                        ):
                            chunk_row = ch
                            break
                if chunk_row:
                    try:
                        chunk_id = int(chunk_row["chunk_id"])
                        # Look up which shard holds this chunk for deterministic routing
                        shard_row = conn.execute(
                            "SELECT shard_id FROM chunks WHERE chunk_id = ?", (chunk_id,)
                        ).fetchone()
                        chunk_vec = None
                        if shard_row and shard_row["shard_id"]:
                            target_idx = shard_idx_map.get(shard_row["shard_id"])
                            if target_idx is not None:
                                chunk_vec = target_idx.reconstruct(chunk_id)
                        if chunk_vec is None:
                            raise RuntimeError(f"chunk {chunk_id} not found in shard {shard_row}")
                        # L2-normalize
                        norm_c = np.linalg.norm(chunk_vec)
                        if norm_c > 0:
                            chunk_vec = chunk_vec / norm_c
                        # Cosine similarity with ctx
                        if ctx_emb is not None:
                            ctx_arr = np.array(ctx_emb, dtype=np.float32)
                            ctx_norm = np.linalg.norm(ctx_arr)
                            norm_ctx = ctx_arr / ctx_norm if ctx_norm > 0 else ctx_arr
                            sc_cosine = float(np.dot(norm_ctx, chunk_vec))
                        else:
                            sc_cosine = 0.0
                    except Exception as ex:
                        logger.debug(f"Failed to reconstruct or score chunk {chunk_row['chunk_id']}: {ex}")
                        sc_cosine = 0.0
                else:
                    sc_cosine = 0.0

            # score_anchor
            anchors_on_e_page = {
                ent["norm"]
                for ent in entities_by_page.get((e["doc_id"], e["page_no"]), [])
            }
            intersection = A.intersection(anchors_on_e_page)
            sc_anchor = len(intersection) / max(len(A), 1.0)
            sc_anchor = min(sc_anchor, 1.0)

            # score_kind
            if expectation is not None:
                sc_kind = 1.0 if e["kind"] == expectation else 0.0
            else:
                sc_kind = 0.5

            # Total score
            tot_score = w_cosine * sc_cosine + w_anchor * sc_anchor + w_kind * sc_kind

            if e["kind"] == "dosage":
                # subject_ref/person entities on the candidate's page (in memory)
                subj_person_cand = [
                    ent
                    for ent in entities_by_page.get((e["doc_id"], e["page_no"]), [])
                    if ent["kind"] in ("subject_ref", "person")
                ]
                # Proximity score check: nearest subject_ref or person on candidate page
                min_dist = None
                for o in subj_person_cand:
                    o_start = o["char_start"]
                    o_end = o["char_end"]
                    if o_start is None or o_end is None or e["char_start"] is None or e["char_end"] is None:
                        continue
                    if o_start < e["char_end"] and e["char_start"] < o_end:
                        dist = 0
                    else:
                        dist = min(abs(e["char_start"] - o_end), abs(o_start - e["char_end"]))
                    if min_dist is None or dist < min_dist:
                        min_dist = dist

                proximity_score = 0.0
                if min_dist is not None:
                    proximity_score = math.exp(-min_dist / 500)

                tot_score += 0.1 * proximity_score

                # Check subject reference co-occurrence on both candidate and redaction pages
                cand_subj = {ent["norm"] for ent in subj_person_cand}

                red_page_ents = entities_by_page.get((r["doc_id"], r["page_no"]), [])
                red_subj = {
                    ent["norm"]
                    for ent in red_page_ents
                    if ent["kind"] in ("subject_ref", "person")
                }

                if cand_subj & red_subj:
                    tot_score += 0.15

                # Check dosage value match on the redaction page
                has_red_dosage = any(
                    ent["kind"] == "dosage" and ent["norm"] == e["norm"]
                    for ent in red_page_ents
                )

                in_context = (e["norm"] in ctx_before.lower()) or (e["norm"] in ctx_after.lower())
                if has_red_dosage or in_context:
                    tot_score += 0.15

                tot_score = min(tot_score, 1.0)

            scored_candidates.append({
                "eid": eid,
                "cand": cand,
                "tot_score": tot_score,
                "sc_cosine": sc_cosine,
                "sc_anchor": sc_anchor,
                "sc_kind": sc_kind
            })

        # Group and deduplicate
        final_candidates = []
        dosage_groups = defaultdict(list)
        non_dosage_candidates = []

        for sc in scored_candidates:
            if sc["cand"]["entity"]["kind"] == "dosage":
                dosage_groups[sc["cand"]["entity"]["norm"]].append(sc)
            else:
                non_dosage_candidates.append(sc)

        for norm, group in dosage_groups.items():
            best_sc = max(group, key=lambda x: x["tot_score"])
            final_candidates.append(best_sc)

        final_candidates.extend(non_dosage_candidates)

        # Now persist final candidates
        for sc in final_candidates:
            tot_score = sc["tot_score"]
            if tot_score >= score_threshold:
                eid = sc["eid"]
                cand = sc["cand"]
                e = cand["entity"]
                with conn:
                    cur = conn.execute("""
                        INSERT OR REPLACE INTO gap_candidates
                        (redaction_id, clear_entity_id, score, score_cosine, score_anchor, score_kind, method, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate')
                        RETURNING gap_id
                    """, (redaction_id, eid, tot_score, sc["sc_cosine"], sc["sc_anchor"], sc["sc_kind"], cand["method"]))
                    gap_row = cur.fetchone()
                    gap_id = gap_row["gap_id"] if gap_row else None

                    if e["kind"] == "person" and gap_id is not None:
                        chk = conn.execute("SELECT 1 FROM review_queue WHERE entity_id = ?", (eid,))
                        if not chk.fetchone():
                            conn.execute("""
                                INSERT INTO review_queue (entity_id, reason, status)
                                VALUES (?, ?, 'pending')
                            """, (eid, f"person in gap candidate #{gap_id}"))


        # Mark redaction as joined
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO gapjoin_runs (redaction_id, run_at) VALUES (?, ?)",
                (redaction_id, now_str)
            )

    return len(redactions)


def run_gapjoin_for_doc(
    cfg: Config,
    conn: sqlite3.Connection,
    doc_id: str,
    embed_fn: Callable[[Config, str], List[float]] = get_ollama_embedding,
) -> int:
    """Run gap join for all pending redactions in a single document.

    Args:
        cfg: Loaded configuration.
        conn: Active DB connection (caller owns it).
        doc_id: Document to process.
        embed_fn: Embedding function for context vectors.

    Returns:
        Number of redactions processed.
    """
    conn.row_factory = sqlite3.Row
    loaded = _load_shard_indexes(cfg)
    if loaded is None:
        logger.error("No FAISS index found. Please run 'build' first.")
        return 0
    shard_indexes, shard_idx_map = loaded

    cur = conn.execute(
        """
        SELECT r.redaction_id, r.doc_id, r.page_no, r.kind, r.label,
               r.x0, r.y0, r.x1, r.y1, r.context_before, r.context_after
        FROM redactions r
        LEFT JOIN gapjoin_runs g ON r.redaction_id = g.redaction_id
        WHERE g.redaction_id IS NULL AND r.doc_id = ?
        """,
        (doc_id,),
    )
    redactions = cur.fetchall()

    if not redactions:
        logger.info("gap_join: no pending redactions for doc %s.", doc_id)
        return 0

    logger.info(
        "gap_join: processing %d redaction(s) for doc %s.", len(redactions), doc_id
    )
    return _process_redactions(cfg, conn, redactions, shard_indexes, shard_idx_map, embed_fn)


def run_gapjoin(cfg: Config, embed_fn: Callable[[Config, str], List[float]] = get_ollama_embedding):
    """Run the redaction-gap join algorithm across all documents.

    Args:
        cfg: Loaded configuration.
        embed_fn: Embedding function for context vectors.
    """
    conn = connect(cfg)
    conn.row_factory = sqlite3.Row

    loaded = _load_shard_indexes(cfg)
    if loaded is None:
        logger.error("No FAISS index found. Please run 'build' first.")
        conn.close()
        return
    shard_indexes, shard_idx_map = loaded

    cur = conn.execute("""
        SELECT r.redaction_id, r.doc_id, r.page_no, r.kind, r.label, r.x0, r.y0, r.x1, r.y1, r.context_before, r.context_after
        FROM redactions r
        LEFT JOIN gapjoin_runs g ON r.redaction_id = g.redaction_id
        WHERE g.redaction_id IS NULL
    """)
    redactions = cur.fetchall()

    if not redactions:
        logger.info("No new redactions to join.")
        conn.close()
        return

    logger.info(f"Processing redaction-gap join for {len(redactions)} redactions...")
    _process_redactions(cfg, conn, redactions, shard_indexes, shard_idx_map, embed_fn)
    conn.close()
    logger.info("Redaction-gap join run completed.")


def run_violation_join(cfg: Config) -> None:
    """Type-e detector — delegate to TypeEScorer.

    Kept here for backward-compat CLI (`palimpsest-index violationjoin`).
    """
    from palimpsest.scorers.type_e import TypeEScorer
    conn = connect(cfg)
    try:
        TypeEScorer().run(conn, cfg)
    finally:
        conn.close()


def run_series_join(cfg: Config) -> None:
    """Type-f detector — delegate to TypeFScorer.

    Kept here for backward-compat CLI (`palimpsest-index seriesjoin`).
    """
    from palimpsest.scorers.type_f import TypeFScorer
    conn = connect(cfg)
    try:
        TypeFScorer().run(conn, cfg)
    finally:
        conn.close()


def run_outcome_gap(cfg: Config) -> None:
    """Type-d detector — delegate to TypeDScorer.

    Kept here for backward-compat CLI (`palimpsest-index outcomegap`).
    """
    from palimpsest.scorers.type_d import TypeDScorer
    conn = connect(cfg)
    try:
        TypeDScorer().run(conn, cfg)
    finally:
        conn.close()


# _edit_distance moved to palimpsest/scorers/type_c.py
from palimpsest.scorers.type_c import _edit_distance  # noqa: F401, E402


def run_identity_link(cfg: Config) -> None:
    """Type-c detector — delegate to TypeCScorer.

    Kept here for backward-compat CLI (`palimpsest-index identitylink`).
    """
    from palimpsest.scorers.type_c import TypeCScorer
    conn = connect(cfg)
    try:
        TypeCScorer().run(conn, cfg)
    finally:
        conn.close()


def print_stats(cfg: Config) -> None:
    """Print index and gap-join statistics to stdout.

    Reports document pipeline counts, FAISS index size, redaction-gap join
    progress, and per-scorer candidate counts.
    """
    conn = connect(cfg)

    print("=== Document pipeline ===")
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM documents GROUP BY status ORDER BY status"
    ).fetchall()
    if rows:
        for row in rows:
            print(f"  {row['status']}: {row['n']}")
    else:
        print("  (no documents)")

    index_path = cfg.storage_root / "index" / "faiss.idx"
    print("\n=== FAISS index ===")
    if index_path.exists():
        index = faiss.read_index(str(index_path))
        print(f"  vectors: {index.ntotal}")
    else:
        print("  (not built)")

    total_red = conn.execute("SELECT COUNT(*) FROM redactions").fetchone()[0]
    joined_red = conn.execute("SELECT COUNT(*) FROM gapjoin_runs").fetchone()[0]
    print("\n=== Redaction-gap join ===")
    print(f"  total redactions:  {total_red}")
    print(f"  joined redactions: {joined_red}")

    print("\n=== Scorer candidates ===")
    from palimpsest.scorers import SCORERS

    for key, scorer_cls in SCORERS.items():
        table = scorer_cls.candidates_table
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {key} ({table}): {n}")
        except Exception as e:
            print(f"  {key} ({table}): n/a ({e})")

    conn.close()


def main() -> None:
    """CLI entry point for palimpsest-index."""
    parser = argparse.ArgumentParser(description="Palimpsest Indexer and Gap Join CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("build", help="Fold pending_embeddings.jsonl into FAISS")
    subparsers.add_parser("gapjoin", help="Run the redaction-gap join algorithm")
    subparsers.add_parser("violationjoin", help="Score reg_cite entities as Type-e violation candidates")
    subparsers.add_parser("seriesjoin", help="Run series gap join analysis")
    subparsers.add_parser("outcomegap", help="Run outcome suppression gap join (Type d)")
    subparsers.add_parser("identitylink", help="Run anonymous identity linkage join (Type c)")
    subparsers.add_parser("stats", help="Show indexing and join statistics")

    args = parser.parse_args()
    cfg = load()

    dispatch = {
        "build":        lambda: build_index(cfg),
        "gapjoin":      lambda: run_gapjoin(cfg),
        "violationjoin": lambda: run_violation_join(cfg),
        "seriesjoin":   lambda: run_series_join(cfg),
        "outcomegap":   lambda: run_outcome_gap(cfg),
        "identitylink": lambda: run_identity_link(cfg),
        "stats":        lambda: print_stats(cfg),
    }
    dispatch[args.command]()


if __name__ == "__main__":
    main()

