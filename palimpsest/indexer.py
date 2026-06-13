# palimpsest/indexer.py
import argparse
import datetime
import json
import logging
from typing import Callable, List
import faiss
import numpy as np
import httpx

import math
import re
from collections import defaultdict
from palimpsest.config import load, Config
from palimpsest.db import connect
from palimpsest.tasks.features import normalize

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

def build_index(cfg: Config):
    """Fold pending_embeddings.jsonl into the FAISS index."""
    index_dir = cfg.storage_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    
    faiss_path = index_dir / "faiss.idx"
    pending_path = index_dir / "pending_embeddings.jsonl"
    processing_path = index_dir / "pending_embeddings.processing"
    done_path = index_dir / "pending_embeddings.done"
    
    # Check if there are pending embeddings
    if not pending_path.exists() or pending_path.stat().st_size == 0:
        if not processing_path.exists() or processing_path.stat().st_size == 0:
            logger.info("No pending embeddings to index.")
            return

    # Atomic transition to processing
    if pending_path.exists() and pending_path.stat().st_size > 0:
        if processing_path.exists():
            processing_path.unlink()
        pending_path.rename(processing_path)
        
    # Read the pending records
    chunk_ids = []
    embeddings = []
    with open(processing_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            chunk_ids.append(rec["chunk_id"])
            embeddings.append(rec["embedding"])
            
    if not chunk_ids:
        logger.info("No valid records found in processing file.")
        if processing_path.exists():
            processing_path.unlink()
        return
        
    # Load or create FAISS index
    if faiss_path.exists():
        logger.info(f"Loading existing FAISS index from {faiss_path}")
        index = faiss.read_index(str(faiss_path))
    else:
        logger.info("Creating new FAISS index.")
        index = faiss.IndexIDMap2(faiss.IndexFlatIP(cfg.embed.get("dim", 768)))
        
    # L2-normalize vectors for cosine similarity
    vecs = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    # Avoid division by zero
    vecs = np.where(norms > 0, vecs / norms, vecs)
    
    ids = np.array(chunk_ids, dtype=np.int64)
    
    # Add to index
    index.add_with_ids(vecs, ids)
    
    # Save index
    faiss.write_index(index, str(faiss_path))
    logger.info(f"Indexed {len(chunk_ids)} vectors. Saved to {faiss_path}")
    
    # Update documents status to 'indexed'
    conn = connect(cfg)
    placeholders = ",".join("?" for _ in chunk_ids)
    cur = conn.execute(f"SELECT DISTINCT doc_id FROM chunks WHERE chunk_id IN ({placeholders})", chunk_ids)
    doc_ids = [row["doc_id"] for row in cur.fetchall()]
    
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with conn:
        for doc_id in doc_ids:
            conn.execute(
                "UPDATE documents SET status='indexed', indexed_at=? WHERE doc_id=?",
                (now, doc_id)
            )
            
    # Rename processing to done (overwriting previous done file if any)
    if done_path.exists():
        done_path.unlink()
    processing_path.rename(done_path)
    
    # Create empty pending file
    pending_path.touch()
    with open(pending_path, "w") as f:
        f.truncate(0)
        
    logger.info("Index build completed successfully.")

def run_gapjoin(cfg: Config, embed_fn: Callable[[Config, str], List[float]] = get_ollama_embedding):
    """Run the redaction-gap join algorithm."""
    conn = connect(cfg)
    
    # Load FAISS index
    index_path = cfg.storage_root / "index" / "faiss.idx"
    if not index_path.exists():
        logger.error("FAISS index not found. Please run 'build' first.")
        return
        
    index = faiss.read_index(str(index_path))
    
    # Query redactions that have not yet been joined
    cur = conn.execute("""
        SELECT r.redaction_id, r.doc_id, r.page_no, r.kind, r.label, r.x0, r.y0, r.x1, r.y1, r.context_before, r.context_after
        FROM redactions r
        LEFT JOIN gapjoin_runs g ON r.redaction_id = g.redaction_id
        WHERE g.redaction_id IS NULL
    """)
    redactions = cur.fetchall()
    
    if not redactions:
        logger.info("No new redactions to join.")
        return
        
    logger.info(f"Processing redaction-gap join for {len(redactions)} redactions...")
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
            # Query FAISS
            query_vec = np.array([ctx_emb], dtype=np.float32)
            norms = np.linalg.norm(query_vec, axis=1, keepdims=True)
            query_vec = np.where(norms > 0, query_vec / norms, query_vec)
            
            _D, _idx = index.search(query_vec, topk)
            hit_chunk_ids = [int(cid) for cid in _idx[0] if cid != -1]
            
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
                    
                    chunk_cosines = {}
                    for idx, cid in enumerate(_idx[0]):
                        if cid != -1:
                            chunk_cosines[int(cid)] = float(_D[0][idx])
                            
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
        scored_candidates: list[dict] = []
        for eid, cand in list(candidates.items()):
            e = cand["entity"]
            
            # score_cosine: if not computed (anchor-only candidate), try to find its chunk and get cosine
            sc_cosine = cand["score_cosine"]
            if sc_cosine is None:
                # Find chunk covering this entity's span
                cur = conn.execute(
                    "SELECT chunk_id FROM chunks WHERE doc_id = ? AND page_no = ? AND char_start <= ? AND char_end >= ? LIMIT 1",
                    (e["doc_id"], e["page_no"], e["char_start"], e["char_end"])
                )
                chunk_row = cur.fetchone()
                if chunk_row:
                    try:
                        chunk_id = int(chunk_row["chunk_id"])
                        # Reconstruct chunk vector from FAISS
                        chunk_vec = index.reconstruct(chunk_id)
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
            cur = conn.execute("SELECT DISTINCT norm FROM entities WHERE doc_id = ? AND page_no = ?", (e["doc_id"], e["page_no"]))
            anchors_on_e_page = set(row["norm"] for row in cur.fetchall())
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
                # Proximity score check: nearest subject_ref or person on candidate page
                cur_other = conn.execute(
                    "SELECT char_start, char_end FROM entities WHERE doc_id = ? AND page_no = ? AND kind IN ('subject_ref', 'person')",
                    (e["doc_id"], e["page_no"])
                )
                other_ents = cur_other.fetchall()
                min_dist = None
                for o in other_ents:
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
                cur_c = conn.execute(
                    "SELECT norm FROM entities WHERE doc_id = ? AND page_no = ? AND kind IN ('subject_ref', 'person')",
                    (e["doc_id"], e["page_no"])
                )
                cand_subj = {row["norm"] for row in cur_c.fetchall()}
                
                cur_r = conn.execute(
                    "SELECT norm FROM entities WHERE doc_id = ? AND page_no = ? AND kind IN ('subject_ref', 'person')",
                    (r["doc_id"], r["page_no"])
                )
                red_subj = {row["norm"] for row in cur_r.fetchall()}
                
                if cand_subj & red_subj:
                    tot_score += 0.15
                    
                # Check dosage value match
                cur_d = conn.execute(
                    "SELECT 1 FROM entities WHERE doc_id = ? AND page_no = ? AND kind = 'dosage' AND norm = ? LIMIT 1",
                    (r["doc_id"], r["page_no"], e["norm"])
                )
                has_red_dosage = cur_d.fetchone() is not None
                
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
            
    logger.info("Redaction-gap join run completed.")

def run_violation_join(cfg: Config):
    """Type-e detector: find pages citing a regulation and score them as violation candidates.

    Scoring: the document year is compared to the regulation's effective_date.
    - Temporal violation (doc date < reg effective date): score = 0.70 base.
    - Each additional corroborating reg_cite entity on the same page: +0.10 (cap 0.95).
    Candidates at or above score_threshold go into violation_candidates.
    """
    conn = connect(cfg)
    threshold = float(cfg.gapjoin.get("score_threshold", 0.65))

    # Load all regulations with their effective year
    regs: dict[int, dict] = {
        int(row["reg_id"]): {
            "citation": str(row["citation"]),
            "effective_date": row["effective_date"],
            "effective_year": int(str(row["effective_date"])[:4]) if row["effective_date"] else None,
        }
        for row in conn.execute("SELECT reg_id, citation, effective_date FROM regulation_citations").fetchall()
    }

    if not regs:
        logger.warning("No regulations seeded — run db.py migrate first")
        return

    # Fetch all reg_cite entities, joined to their document year
    rows = conn.execute("""
        SELECT e.entity_id, e.doc_id, e.page_no, e.norm, d.year AS doc_year
        FROM entities e
        JOIN documents d ON e.doc_id = d.doc_id
        WHERE e.kind = 'reg_cite'
    """).fetchall()

    inserted = 0
    for row in rows:
        entity_id = row["entity_id"]
        doc_id = row["doc_id"]
        page_no = row["page_no"]
        doc_year = row["doc_year"]
        cite_norm = row["norm"]

        # Match this norm to a seeded regulation
        matched_reg_id = None
        for reg_id, reg in regs.items():
            if reg["citation"].lower() in cite_norm.lower() or cite_norm.lower() in reg["citation"].lower():
                matched_reg_id = reg_id
                break

        if matched_reg_id is None:
            continue

        reg = regs[matched_reg_id]
        reg_year = reg["effective_year"]

        # Temporal scoring
        if doc_year and reg_year and doc_year < reg_year:
            base_score = 0.70
            violation_type = "pre_regulation"
        else:
            base_score = 0.65
            violation_type = "possible_violation"

        # Count corroborating reg_cite entities on the same page
        corroborating = conn.execute("""
            SELECT COUNT(*) FROM entities
            WHERE doc_id = ? AND page_no = ? AND kind = 'reg_cite' AND entity_id != ?
        """, (doc_id, page_no, entity_id)).fetchone()[0]
        score = min(base_score + corroborating * 0.10, 0.95)

        if score < threshold:
            continue

        with conn:
            conn.execute("""
                INSERT OR IGNORE INTO violation_candidates
                  (doc_id, page_no, reg_id, reg_cite_entity_id, doc_year, violation_type, score, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate')
            """, (doc_id, page_no, matched_reg_id, entity_id, doc_year, violation_type, score))
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1

    logger.info(f"Violation join complete: {inserted} new candidates (threshold={threshold})")


def run_series_join(cfg: Config):
    """Run series gap join analysis (Type f)."""
    conn = connect(cfg)
    
    # 1. Parse accessions from documents table
    cur = conn.execute("SELECT doc_id, accession FROM documents WHERE accession IS NOT NULL AND accession != ''")
    rows = cur.fetchall()
    
    # Group accessions by prefix
    # prefix -> list of (num, doc_id, padding_len, accession_str)
    groups = defaultdict(list)
    
    for row in rows:
        doc_id = row["doc_id"]
        acc = row["accession"]
        # Split into prefix and number
        m = re.match(r'^([^\d]+)(\d+)$', acc.strip())
        if m:
            prefix = m.group(1)
            digits_str = m.group(2)
            num = int(digits_str)
            padding_len = len(digits_str)
            groups[prefix].append((num, doc_id, padding_len, acc))
            
    inserted = 0
    for prefix, accs in groups.items():
        if not accs:
            continue
        # Sort by number
        accs.sort(key=lambda x: x[0])
        present_nums = [x[0] for x in accs]
        present_set = set(present_nums)
        
        # Mapping from number to (doc_id, accession_str, padding_len)
        num_map = {num: (doc_id, acc, pad_len) for num, doc_id, pad_len, acc in accs}
        
        min_num = min(present_set)
        max_num = max(present_set)
        total_range_count = max_num - min_num + 1
        
        if total_range_count <= 1:
            continue
            
        missing_count = total_range_count - len(present_set)
        gap_ratio = missing_count / total_range_count
        
        # Only process if gap ratio > 20%
        if gap_ratio <= 0.20:
            continue
            
        # Standard padding_len to use if we need to format missing accession
        # Let's use the padding_len of the first document in the group
        default_pad = accs[0][2]
        
        # Identify missing numbers in the range
        for num in range(min_num + 1, max_num):
            if num in present_set:
                continue
                
            # Form missing accession
            missing_acc = f"{prefix}{num:0{default_pad}d}"
            norm_missing_acc = normalize("seq_ref", missing_acc)
            
            # Check flanking documents (N-1 or N+1 sequence)
            doc_id_prev = num_map.get(num - 1, [None])[0] if (num - 1) in present_set else None
            doc_id_next = num_map.get(num + 1, [None])[0] if (num + 1) in present_set else None
            
            ref_prev = False
            entity_id_prev = None
            if doc_id_prev:
                cur_ent = conn.execute(
                    "SELECT entity_id FROM entities WHERE doc_id = ? AND kind = 'seq_ref' AND norm = ? LIMIT 1",
                    (doc_id_prev, norm_missing_acc)
                )
                ent_row = cur_ent.fetchone()
                if ent_row:
                    ref_prev = True
                    entity_id_prev = ent_row["entity_id"]
                    
            ref_next = False
            entity_id_next = None
            if doc_id_next:
                cur_ent = conn.execute(
                    "SELECT entity_id FROM entities WHERE doc_id = ? AND kind = 'seq_ref' AND norm = ? LIMIT 1",
                    (doc_id_next, norm_missing_acc)
                )
                ent_row = cur_ent.fetchone()
                if ent_row:
                    ref_next = True
                    entity_id_next = ent_row["entity_id"]
                    
            # Compute score
            if ref_prev and ref_next:
                score = 0.90
            elif ref_prev or ref_next:
                score = 0.70
            else:
                score = 0.50
                
            if score >= 0.65:
                # Flanking doc ID and entity ID to reference
                flanking_doc_id = doc_id_prev if ref_prev else doc_id_next
                ref_entity_id = entity_id_prev if ref_prev else entity_id_next
                
                with conn:
                    conn.execute("""
                        INSERT INTO series_gap_candidates 
                          (series_prefix, missing_number, missing_accession, flanking_doc_id, ref_entity_id, score, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'candidate')
                        ON CONFLICT(missing_accession) DO UPDATE SET
                          score = excluded.score,
                          flanking_doc_id = excluded.flanking_doc_id,
                          ref_entity_id = excluded.ref_entity_id
                    """, (prefix, num, missing_acc, flanking_doc_id, ref_entity_id, score))
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        inserted += 1
                        
    logger.info(f"Series join complete: {inserted} new series gap candidates")


def print_stats(cfg: Config):
    """Print indexing and gap join statistics."""
    conn = connect(cfg)
    
    # Redaction counts
    cur = conn.execute("SELECT COUNT(*) FROM redactions")
    total_red = cur.fetchone()[0]
    
    cur = conn.execute("SELECT COUNT(*) FROM gapjoin_runs")
    joined_red = cur.fetchone()[0]
    
    # Skipped redactions
    cur = conn.execute("""
        SELECT COUNT(*) 
        FROM redactions r 
        JOIN gapjoin_runs g ON r.redaction_id = g.redaction_id 
        WHERE (length(coalesce(r.context_before, '')) + length(coalesce(r.context_after, '')) + 1) < 40
    """)
    skipped_red = cur.fetchone()[0]
    
    # Candidate counts
    cur = conn.execute("SELECT COUNT(*) FROM gap_candidates")
    total_cand = cur.fetchone()[0]
    
    # By method
    cur = conn.execute("SELECT method, COUNT(*) FROM gap_candidates GROUP BY method")
    method_counts = {row[0]: row[1] for row in cur.fetchall()}
    
    # By score decile
    cur = conn.execute("SELECT CAST(score * 10 AS INTEGER) as decile, COUNT(*) as cnt FROM gap_candidates GROUP BY decile")
    decile_counts = {row["decile"]: row["cnt"] for row in cur.fetchall()}
    
    # review_queue pending
    cur = conn.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'pending'")
    pending_reviews = cur.fetchone()[0]
    
    print("=== Palimpsest Indexer Stats ===")
    print(f"Redactions total:    {total_red}")
    print(f"Redactions joined:   {joined_red}")
    print(f"Redactions skipped:  {skipped_red}")
    print(f"Gap Candidates:      {total_cand}")
    for method, cnt in method_counts.items():
        print(f"  - via {method}: {cnt}")
    print("Score Deciles:")
    for d in sorted(decile_counts.keys()):
        lower = d / 10.0
        upper = (d + 1) / 10.0
        print(f"  [{lower:.1f} - {upper:.1f}): {decile_counts[d]}")
    print(f"Pending HITL reviews: {pending_reviews}")

    # Violation candidates (Type e)
    cur = conn.execute("SELECT COUNT(*) FROM violation_candidates")
    total_vc = cur.fetchone()[0]
    cur = conn.execute("SELECT violation_type, COUNT(*) FROM violation_candidates GROUP BY violation_type")
    vc_types = {row[0]: row[1] for row in cur.fetchall()}
    print(f"\nViolation Candidates (Type e): {total_vc}")
    for vtype, cnt in vc_types.items():
        print(f"  - {vtype}: {cnt}")

    # Series gap candidates (Type f)
    cur = conn.execute("SELECT COUNT(*) FROM series_gap_candidates")
    total_sg = cur.fetchone()[0]
    cur = conn.execute("SELECT status, COUNT(*) FROM series_gap_candidates GROUP BY status")
    sg_status = {row[0]: row[1] for row in cur.fetchall()}
    print(f"\nSeries Gap Candidates (Type f): {total_sg}")
    for status, cnt in sg_status.items():
        print(f"  - {status}: {cnt}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Palimpsest Indexer and Gap Join CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("build", help="Fold pending_embeddings.jsonl into FAISS")
    subparsers.add_parser("gapjoin", help="Run the redaction-gap join algorithm")
    subparsers.add_parser("violationjoin", help="Score reg_cite entities as Type-e violation candidates")
    subparsers.add_parser("seriesjoin", help="Run series gap join analysis")
    subparsers.add_parser("stats", help="Show indexing and join statistics")

    args = parser.parse_args()

    cfg = load()

    if args.command == "build":
        build_index(cfg)
    elif args.command == "gapjoin":
        run_gapjoin(cfg)
    elif args.command == "violationjoin":
        run_violation_join(cfg)
    elif args.command == "seriesjoin":
        run_series_join(cfg)
    elif args.command == "stats":
        print_stats(cfg)
