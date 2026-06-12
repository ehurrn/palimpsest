# palimpsest/indexer.py
import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple, Set
import faiss
import numpy as np
import httpx

from palimpsest.config import load, Config
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

def get_slot_expectation(kind: str, label: str) -> str:
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
        candidates = {}
        
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
            
            D, I = index.search(query_vec, topk)
            hit_chunk_ids = [int(cid) for cid in I[0] if cid != -1]
            
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
                    for idx, cid in enumerate(I[0]):
                        if cid != -1:
                            chunk_cosines[int(cid)] = float(D[0][idx])
                            
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
                                            if candidates[eid]["score_cosine"] is None or cosine > candidates[eid]["score_cosine"]:
                                                candidates[eid]["score_cosine"] = cosine
                                        else:
                                            candidates[eid] = {
                                                "entity": ent,
                                                "method": "embedding",
                                                "score_cosine": cosine,
                                                "hit_chunk_ids": [ch["chunk_id"]]
                                            }

                                        
        # 5. Score Candidates
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
                            ctx_norm = np.linalg.norm(ctx_emb)
                            norm_ctx = ctx_emb / ctx_norm if ctx_norm > 0 else ctx_emb
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
            
            # 6. Persist to gap_candidates
            if tot_score >= score_threshold:
                # Write to database (UPSERT on conflict)
                with conn:
                    cur = conn.execute("""
                        INSERT OR REPLACE INTO gap_candidates 
                        (redaction_id, clear_entity_id, score, score_cosine, score_anchor, score_kind, method, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate')
                        RETURNING gap_id
                    """, (redaction_id, eid, tot_score, sc_cosine, sc_anchor, sc_kind, cand["method"]))
                    gap_row = cur.fetchone()
                    gap_id = gap_row["gap_id"] if gap_row else None
                    
                    # 7. Auto-flag for HITL if kind is person
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Palimpsest Indexer and Gap Join CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    subparsers.add_parser("build", help="Fold pending_embeddings.jsonl into FAISS")
    subparsers.add_parser("gapjoin", help="Run the redaction-gap join algorithm")
    subparsers.add_parser("stats", help="Show indexing and join statistics")
    
    args = parser.parse_args()
    
    cfg = load()
    
    if args.command == "build":
        build_index(cfg)
    elif args.command == "gapjoin":
        run_gapjoin(cfg)
    elif args.command == "stats":
        print_stats(cfg)
