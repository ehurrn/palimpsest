# palimpsest/server.py
import argparse
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, Any
import numpy as np
import httpx
import faiss
from mcp.server.fastmcp import FastMCP

from palimpsest.config import load, Config

logger = logging.getLogger(__name__)

mcp = FastMCP("Palimpsest")

# Cache for FAISS index
_cached_index = None
_index_mtime = 0

def get_faiss_index(cfg: Config):
    global _cached_index, _index_mtime
    faiss_path = cfg.storage_root / "index" / "faiss.idx"
    if not faiss_path.exists():
        return None
    try:
        mtime = faiss_path.stat().st_mtime
        if _cached_index is None or mtime > _index_mtime:
            _cached_index = faiss.read_index(str(faiss_path))
            _index_mtime = mtime
        return _cached_index
    except Exception as e:
        logger.error(f"Failed to read FAISS index: {e}")
        return None

def get_ro_connection(db_path: Path) -> sqlite3.Connection:
    """Connect to SQLite database in read-only mode."""
    # Use URI format to specify read-only mode
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def load_approved_person_ids(conn: sqlite3.Connection) -> set[int]:
    """Return the set of entity_ids approved in the review_queue.

    Fetched once per tool call so the mask helpers can resolve approval with a
    set membership check instead of one SELECT per entity (avoids an N+1).
    """
    cur = conn.execute("SELECT entity_id FROM review_queue WHERE status = 'approved'")
    return {row[0] for row in cur.fetchall()}


def _is_approved(
    entity_id: int,
    conn: sqlite3.Connection,
    approved_ids: set[int] | None,
) -> bool:
    """Resolve approval, using a precomputed set when available else a point query."""
    if approved_ids is not None:
        return entity_id in approved_ids
    cur = conn.execute(
        "SELECT 1 FROM review_queue WHERE entity_id = ? AND status = 'approved'",
        (entity_id,),
    )
    return cur.fetchone() is not None


def mask_person(
    entity_row: Dict[str, Any],
    conn: sqlite3.Connection,
    approved_ids: set[int] | None = None,
) -> str:
    """Return entity text if living_status='deceased_historical' and approved in review_queue; else pseudonym."""
    if entity_row.get("kind") != "person":
        return entity_row.get("text", "")

    approved = False
    if entity_row.get("living_status") == "deceased_historical":
        approved = _is_approved(entity_row["entity_id"], conn, approved_ids)

    if approved:
        return entity_row.get("text", "")
    else:
        return f"PERSON-{entity_row['entity_id']:04d}"

def get_masked_text_for_page(
    doc_id: str,
    page_no: int,
    text: str,
    conn: sqlite3.Connection,
    approved_ids: set[int] | None = None,
) -> str:
    """Replace non-approved person entities in page text with pseudonyms."""
    if not text:
        return ""
    cur = conn.execute(
        "SELECT entity_id, char_start, char_end, living_status FROM entities WHERE doc_id = ? AND page_no = ? AND kind = 'person'",
        (doc_id, page_no)
    )
    entities = cur.fetchall()

    valid_ents = []
    for ent in entities:
        if ent["char_start"] is not None and ent["char_end"] is not None:
            valid_ents.append(ent)

    # Sort in descending order to avoid offset shifting
    valid_ents.sort(key=lambda e: e["char_start"], reverse=True)

    masked = list(text)
    for ent in valid_ents:
        approved = False
        if ent["living_status"] == "deceased_historical":
            approved = _is_approved(ent["entity_id"], conn, approved_ids)

        if not approved:
            pseudonym = f"PERSON-{ent['entity_id']:04d}"
            start = ent["char_start"]
            end = ent["char_end"]
            if 0 <= start <= len(text) and 0 <= end <= len(text) and start <= end:
                masked[start:end] = list(pseudonym)

    return "".join(masked)

def mask_context_text(
    doc_id: str,
    page_no: int,
    context: str,
    conn: sqlite3.Connection,
    approved_ids: set[int] | None = None,
) -> str:
    """Mask non-approved person entity mentions inside substring contexts."""
    if not context:
        return ""
    cur = conn.execute(
        "SELECT entity_id, text, living_status FROM entities WHERE doc_id = ? AND page_no = ? AND kind = 'person'",
        (doc_id, page_no)
    )
    entities = cur.fetchall()

    for ent in entities:
        approved = False
        if ent["living_status"] == "deceased_historical":
            approved = _is_approved(ent["entity_id"], conn, approved_ids)

        if not approved:
            pseudonym = f"PERSON-{ent['entity_id']:04d}"
            pattern = re.compile(r'\b' + re.escape(ent["text"]) + r'\b', re.IGNORECASE)
            context = pattern.sub(pseudonym, context)

    return context

def get_citation(row: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """Helper to construct citation dict from query row."""
    doc_id = row[f"{prefix}doc_id"]
    return {
        "doc_id": doc_id,
        "page_no": row[f"{prefix}page_no"],
        "source_url": row[f"{prefix}source_url"] or f"https://www.osti.gov/opennet/servlets/purl/{doc_id}.pdf",
        "title": row[f"{prefix}title"],
        "accession": row[f"{prefix}accession"]
    }

@mcp.tool()
def palimpsest_find_redaction_gaps(min_score: float = 0.65, status: str = "candidate", kind: str = None, limit: int = 20) -> str:
    """List redaction gaps with scores above the threshold and masked person entities."""
    logger.info(f"Tool call: palimpsest_find_redaction_gaps(min_score={min_score}, status={status!r}, kind={kind!r}, limit={limit})")
    
    try:
        cfg = load()
        conn = get_ro_connection(cfg.db_path)
    except Exception as e:
        return json.dumps({"error": f"Database connection failed: {e}"})
        
    query = """
        SELECT g.gap_id, g.score, g.score_cosine, g.score_anchor, g.score_kind, g.method, g.status,
               r.redaction_id, r.kind as r_kind, r.label as r_label, r.context_before, r.context_after, r.page_no as r_page_no,
               d_red.doc_id as r_doc_id, d_red.title as r_title, d_red.accession as r_accession, d_red.source_url as r_source_url,
               e.entity_id, e.kind as e_kind, e.text as e_text, e.norm as e_norm, e.char_start as e_char_start, e.char_end as e_char_end,
               e.living_status as e_living_status, e.page_no as e_page_no,
               d_ent.doc_id as e_doc_id, d_ent.title as e_title, d_ent.accession as e_accession, d_ent.source_url as e_source_url
        FROM gap_candidates g
        JOIN redactions r ON g.redaction_id = r.redaction_id
        JOIN documents d_red ON r.doc_id = d_red.doc_id
        JOIN entities e ON g.clear_entity_id = e.entity_id
        JOIN documents d_ent ON e.doc_id = d_ent.doc_id
        WHERE g.score >= ? AND g.status = ?
    """
    params = [min_score, status]
    if kind:
        query += " AND e.kind = ?"
        params.append(kind)
        
    query += " ORDER BY g.score DESC LIMIT ?"
    params.append(limit)
    
    try:
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        approved_ids = load_approved_person_ids(conn)

        results = []
        for row in rows:
            r_citation = get_citation(row, "r_")
            e_citation = get_citation(row, "e_")

            ent_row = {
                "entity_id": row["entity_id"],
                "kind": row["e_kind"],
                "text": row["e_text"],
                "living_status": row["e_living_status"]
            }
            masked_text = mask_person(ent_row, conn, approved_ids)

            # Context for clear entity
            cur_p = conn.execute("SELECT text FROM pages WHERE doc_id = ? AND page_no = ?", (row["e_doc_id"], row["e_page_no"]))
            page_row = cur_p.fetchone()
            p_text = page_row["text"] if page_row else ""
            masked_p_text = get_masked_text_for_page(row["e_doc_id"], row["e_page_no"], p_text, conn, approved_ids)

            c_start = row["e_char_start"]
            c_end = row["e_char_end"]
            if c_start is not None and c_end is not None:
                start_idx = max(0, c_start - 300)
                end_idx = min(len(masked_p_text), c_end + 300)
                e_context = masked_p_text[start_idx:end_idx]
            else:
                e_context = ""

            r_ctx_before = mask_context_text(row["r_doc_id"], row["r_page_no"], row["context_before"], conn, approved_ids)
            r_ctx_after = mask_context_text(row["r_doc_id"], row["r_page_no"], row["context_after"], conn, approved_ids)
            
            results.append({
                "gap_id": row["gap_id"],
                "score": row["score"],
                "score_components": {
                    "cosine": row["score_cosine"],
                    "anchor": row["score_anchor"],
                    "kind": row["score_kind"]
                },
                "method": row["method"],
                "status": row["status"],
                "redaction": {
                    "kind": row["r_kind"],
                    "label": row["r_label"],
                    "context_before": r_ctx_before,
                    "context_after": r_ctx_after,
                    "citation": r_citation
                },
                "clear_entity": {
                    "kind": row["e_kind"],
                    "text": masked_text,
                    "context": e_context,
                    "citation": e_citation
                },
                "requires_review": row["e_kind"] == "person" and masked_text.startswith("PERSON-")
            })
            
        conn.close()
        logger.info(f"Returned {len(results)} gap candidates.")
        return json.dumps(results, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
def palimpsest_search(query: str, limit: int = 10) -> str:
    """Semantic search over page chunks with person masking applied."""
    logger.info(f"Tool call: palimpsest_search(query={query!r}, limit={limit})")
    
    try:
        cfg = load()
        conn = get_ro_connection(cfg.db_path)
    except Exception as e:
        return json.dumps({"error": f"Database connection failed: {e}"})
        
    index = get_faiss_index(cfg)
    if index is None:
        conn.close()
        return json.dumps({"error": "FAISS index not built or not found."})
        
    # Get query embedding
    try:
        resp = httpx.post(
            "http://localhost:11434/api/embeddings",
            json={
                "model": cfg.embed["model"],
                "prompt": query,
                "keep_alive": cfg.models.get("keep_alive", "24h")
            },
            timeout=10.0
        )
        resp.raise_for_status()
        q_emb = resp.json()["embedding"]
    except Exception as e:
        conn.close()
        return json.dumps({"error": f"Ollama embedding query failed: {e}"})
        
    query_vec = np.array([q_emb], dtype=np.float32)
    norms = np.linalg.norm(query_vec, axis=1, keepdims=True)
    query_vec = np.where(norms > 0, query_vec / norms, query_vec)
    
    D, I = index.search(query_vec, limit)
    hit_chunk_ids = [int(cid) for cid in I[0] if cid != -1]
    
    if not hit_chunk_ids:
        conn.close()
        return json.dumps([])
        
    placeholders = ",".join("?" for _ in hit_chunk_ids)
    query_str = f"""
        SELECT c.chunk_id, c.doc_id, c.page_no, c.text, c.char_start, c.char_end,
               d.title, d.accession, d.source_url
        FROM chunks c
        JOIN documents d ON c.doc_id = d.doc_id
        WHERE c.chunk_id IN ({placeholders})
    """
    
    try:
        cur = conn.execute(query_str, hit_chunk_ids)
        rows = cur.fetchall()
        
        # Map row details by chunk_id
        row_map = {row["chunk_id"]: row for row in rows}
        approved_ids = load_approved_person_ids(conn)

        results = []
        for idx, cid in enumerate(I[0]):
            if cid == -1 or cid not in row_map:
                continue
            row = row_map[cid]

            # Fetch and mask page text
            cur_p = conn.execute("SELECT text FROM pages WHERE doc_id = ? AND page_no = ?", (row["doc_id"], row["page_no"]))
            page_row = cur_p.fetchone()
            p_text = page_row["text"] if page_row else ""
            masked_p_text = get_masked_text_for_page(row["doc_id"], row["page_no"], p_text, conn, approved_ids)
            
            c_start = row["char_start"]
            c_end = row["char_end"]
            masked_chunk_text = masked_p_text[c_start:c_end] if (c_start is not None and c_end is not None) else row["text"]
            
            results.append({
                "text": masked_chunk_text,
                "score": float(D[0][idx]),
                "citation": {
                    "doc_id": row["doc_id"],
                    "page_no": row["page_no"],
                    "source_url": row["source_url"] or f"https://www.osti.gov/opennet/servlets/purl/{row['doc_id']}.pdf",
                    "title": row["title"],
                    "accession": row["accession"]
                }
            })
            
        conn.close()
        logger.info(f"Returned {len(results)} search hits.")
        return json.dumps(results, indent=2)
    except Exception as e:
        conn.close()
        return json.dumps({"error": str(e)})

@mcp.tool()
def palimpsest_get_document(doc_id: str, page_no: int = None) -> str:
    """Retrieve document details, page contents, and markers with person masking applied."""
    logger.info(f"Tool call: palimpsest_get_document(doc_id={doc_id!r}, page_no={page_no})")
    
    try:
        cfg = load()
        conn = get_ro_connection(cfg.db_path)
    except Exception as e:
        return json.dumps({"error": f"Database connection failed: {e}"})
        
    try:
        # Fetch metadata
        cur = conn.execute("SELECT doc_id, title, accession, source_url, status, page_count FROM documents WHERE doc_id = ?", (doc_id,))
        doc = cur.fetchone()
        if not doc:
            conn.close()
            return json.dumps({"error": "Document not found"})
            
        doc_meta = {
            "doc_id": doc["doc_id"],
            "title": doc["title"],
            "accession": doc["accession"],
            "source_url": doc["source_url"] or f"https://www.osti.gov/opennet/servlets/purl/{doc_id}.pdf",
            "status": doc["status"],
            "page_count": doc["page_count"]
        }
        
        # Fetch pages
        if page_no is not None:
            cur_p = conn.execute("SELECT page_no, text FROM pages WHERE doc_id = ? AND page_no = ?", (doc_id, page_no))
        else:
            cur_p = conn.execute("SELECT page_no, text FROM pages WHERE doc_id = ? ORDER BY page_no ASC", (doc_id,))
        pages_rows = cur_p.fetchall()
        approved_ids = load_approved_person_ids(conn)

        pages = []
        for p_row in pages_rows:
            p_num = p_row["page_no"]
            masked_text = get_masked_text_for_page(doc_id, p_num, p_row["text"], conn, approved_ids)
            pages.append({
                "page_no": p_num,
                "text": masked_text
            })

        # Fetch redactions
        cur_r = conn.execute("SELECT redaction_id, page_no, kind, label, x0, y0, x1, y1, context_before, context_after FROM redactions WHERE doc_id = ?", (doc_id,))
        red_rows = cur_r.fetchall()
        redactions = []
        for r_row in red_rows:
            p_num = r_row["page_no"]
            r_ctx_before = mask_context_text(doc_id, p_num, r_row["context_before"], conn, approved_ids)
            r_ctx_after = mask_context_text(doc_id, p_num, r_row["context_after"], conn, approved_ids)
            redactions.append({
                "redaction_id": r_row["redaction_id"],
                "page_no": p_num,
                "kind": r_row["kind"],
                "label": r_row["label"],
                "bbox": [r_row["x0"], r_row["y0"], r_row["x1"], r_row["y1"]],
                "context_before": r_ctx_before,
                "context_after": r_ctx_after
            })
            
        # Fetch entities
        cur_e = conn.execute("SELECT entity_id, page_no, kind, text, norm, char_start, char_end, x0, y0, x1, y1, living_status FROM entities WHERE doc_id = ?", (doc_id,))
        ent_rows = cur_e.fetchall()
        entities = []
        for e_row in ent_rows:
            masked_text = mask_person(dict(e_row), conn, approved_ids)
            entities.append({
                "entity_id": e_row["entity_id"],
                "page_no": e_row["page_no"],
                "kind": e_row["kind"],
                "text": masked_text,
                "norm": e_row["norm"],
                "char_start": e_row["char_start"],
                "char_end": e_row["char_end"],
                "bbox": [e_row["x0"], e_row["y0"], e_row["x1"], e_row["y1"]]
            })
            
        conn.close()
        logger.info("Returned document details.")
        return json.dumps({
            "metadata": doc_meta,
            "pages": pages,
            "redactions": redactions,
            "entities": entities
        }, indent=2)
    except Exception as e:
        conn.close()
        return json.dumps({"error": str(e)})

@mcp.tool()
def palimpsest_get_entity(norm: str, kind: str = None, limit: int = 50) -> str:
    """Retrieve all occurrences of a normalized entity name with person masking applied."""
    logger.info(f"Tool call: palimpsest_get_entity(norm={norm!r}, kind={kind!r}, limit={limit})")
    
    try:
        cfg = load()
        conn = get_ro_connection(cfg.db_path)
    except Exception as e:
        return json.dumps({"error": f"Database connection failed: {e}"})
        
    query = """
        SELECT e.entity_id, e.doc_id, e.page_no, e.kind, e.text, e.norm, e.char_start, e.char_end, e.living_status,
               d.title, d.accession, d.source_url
        FROM entities e
        JOIN documents d ON e.doc_id = d.doc_id
        WHERE e.norm = ?
    """
    params = [norm]
    if kind:
        query += " AND e.kind = ?"
        params.append(kind)
        
    query += " LIMIT ?"
    params.append(limit)
    
    try:
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        approved_ids = load_approved_person_ids(conn)

        results = []
        for row in rows:
            ent_row = {
                "entity_id": row["entity_id"],
                "kind": row["kind"],
                "text": row["text"],
                "living_status": row["living_status"]
            }
            masked_text = mask_person(ent_row, conn, approved_ids)
            
            results.append({
                "entity_id": row["entity_id"],
                "kind": row["kind"],
                "text": masked_text,
                "norm": row["norm"],
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "citation": {
                    "doc_id": row["doc_id"],
                    "page_no": row["page_no"],
                    "source_url": row["source_url"] or f"https://www.osti.gov/opennet/servlets/purl/{row['doc_id']}.pdf",
                    "title": row["title"],
                    "accession": row["accession"]
                }
            })
            
        conn.close()
        logger.info(f"Returned {len(results)} occurrences for entity {norm!r}.")
        return json.dumps(results, indent=2)
    except Exception as e:
        conn.close()
        return json.dumps({"error": str(e)})

@mcp.tool()
def palimpsest_queue_status() -> str:
    """Retrieve the job queue, document pipeline counts, and gap join metrics."""
    logger.info("Tool call: palimpsest_queue_status()")
    
    try:
        cfg = load()
        conn = get_ro_connection(cfg.db_path)
    except Exception as e:
        return json.dumps({"error": f"Database connection failed: {e}"})
        
    # 1. Fetch broker status
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}/status"
    broker_status = {}
    try:
        resp = httpx.get(broker_url, timeout=3.0)
        if resp.status_code == 200:
            broker_status = resp.json()
        else:
            broker_status = {"error": f"Broker returned HTTP {resp.status_code}"}
    except Exception:
        broker_status = {"error": "broker unreachable"}
        
    try:
        # 2. Document counts by status
        cur = conn.execute("SELECT status, COUNT(*) FROM documents GROUP BY status")
        doc_counts = {row[0]: row[1] for row in cur.fetchall()}
        
        # 3. Gapjoin runs
        cur = conn.execute("SELECT COUNT(*) FROM redactions")
        total_red = cur.fetchone()[0]
        
        cur = conn.execute("SELECT COUNT(*) FROM gapjoin_runs")
        joined_red = cur.fetchone()[0]
        
        cur = conn.execute("""
            SELECT COUNT(*) 
            FROM redactions r 
            JOIN gapjoin_runs g ON r.redaction_id = g.redaction_id 
            WHERE (length(coalesce(r.context_before, '')) + length(coalesce(r.context_after, '')) + 1) < 40
        """)
        skipped_red = cur.fetchone()[0]
        
        cur = conn.execute("SELECT COUNT(*) FROM gap_candidates")
        total_candidates = cur.fetchone()[0]
        
        conn.close()
        
        result = {
            "broker_queue": broker_status,
            "document_pipeline": doc_counts,
            "gap_join_metrics": {
                "total_redactions": total_red,
                "joined_redactions": joined_red,
                "skipped_redactions": skipped_red,
                "gap_candidates": total_candidates
            }
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        conn.close()
        return json.dumps({"error": str(e)})

@mcp.tool()
def palimpsest_review_queue(limit: int = 50) -> str:
    """Retrieve unapproved person entities in the review queue for manual auditing."""
    logger.info(f"Tool call: palimpsest_review_queue(limit={limit})")
    
    try:
        cfg = load()
        conn = get_ro_connection(cfg.db_path)
    except Exception as e:
        return json.dumps({"error": f"Database connection failed: {e}"})
        
    try:
        cur = conn.execute("""
            SELECT r.review_id, r.entity_id, r.reason, r.status,
                   e.doc_id, e.page_no
            FROM review_queue r
            JOIN entities e ON r.entity_id = e.entity_id
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        
        results = []
        for row in rows:
            eid = row["entity_id"]
            pseudonym = f"PERSON-{eid:04d}"
            
            # Extract gap_id from reason string if present
            # e.g. "person in gap candidate #42"
            m = re.search(r'#(\d+)', row["reason"])
            gap_id = int(m.group(1)) if m else None
            
            results.append({
                "review_id": row["review_id"],
                "entity_id": eid,
                "pseudonym": pseudonym,
                "reason": row["reason"],
                "status": row["status"],
                "gap_id": gap_id
            })
            
        conn.close()
        logger.info(f"Returned {len(results)} review queue entries.")
        return json.dumps(results, indent=2)
    except Exception as e:
        conn.close()
        return json.dumps({"error": str(e)})

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Palimpsest MCP Server")
    parser.add_argument("--config", type=str, default="config.toml", help="Path to config.toml")
    args = parser.parse_args()
    
    # Set the config path env var
    if args.config:
        os.environ["PALIMPSEST_CONFIG"] = args.config
        
    cfg = load()
    
    logger.info(f"Starting FastMCP server on host 0.0.0.0, port {cfg.mcp['port']}")
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = cfg.mcp["port"]
    mcp.run(transport="sse")
