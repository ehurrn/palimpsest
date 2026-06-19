# palimpsest/tasks/embed.py
import json
import logging
import time
import httpx
from typing import List, Dict, Any, Callable, Tuple

from palimpsest.config import Config
from palimpsest.tasks import handler, PermanentJobError

logger = logging.getLogger(__name__)

def chunk_text(text: str, chunk_chars: int, chunk_overlap: int) -> List[Tuple[int, int, str]]:
    """
    Chunk the text into windows of chunk_chars with chunk_overlap.
    Never split mid-word (break at last whitespace before limit).
    Returns list of (char_start, char_end, chunk_substring).
    """
    if not text:
        return []
    
    chunks = []
    text_len = len(text)
    start = 0
    
    while start < text_len:
        # Initial end guess
        end = start + chunk_chars
        if end >= text_len:
            end = text_len
        else:
            # Look back to find last whitespace to avoid splitting mid-word
            last_space = text.rfind(' ', start, end)
            last_newline = text.rfind('\n', start, end)
            last_ws = max(last_space, last_newline)
            
            if last_ws != -1 and last_ws > start:
                end = last_ws
                
        chunk_str = text[start:end]
        chunks.append((start, end, chunk_str))
        
        if end >= text_len:
            break
            
        # Next start: start from end minus overlap
        next_start = end - chunk_overlap

        if next_start > start:
            # Find start of word (last space before or at next_start)
            # to avoid starting a chunk in the middle of a word
            while next_start > start and not text[next_start - 1].isspace():
                next_start -= 1
        if next_start <= start:
            next_start = start + 1 if end == start else end
            
        start = next_start
        if start >= text_len:
            break
            
    return chunks

def process_embed(ocr_data: List[Dict[str, Any]], cfg: Config, embed_fn: Callable[[str], List[float]]) -> Dict[str, Any]:
    """Core chunking and embedding logic, isolated for unit testing."""
    chunk_chars = cfg.embed.get("chunk_chars", 800)
    chunk_overlap = cfg.embed.get("chunk_overlap", 150)
    
    chunks_result = []
    total_chunks = 0
    start_time = time.time()
    
    for page in ocr_data:
        page_no = page["page_no"]
        text = page.get("text", "")
        if not text:
            # Check lines if text field is missing
            lines = page.get("lines", [])
            text = "\n".join(line["text"] for line in lines)
            
        if not text.strip():
            continue
            
        page_chunks = chunk_text(text, chunk_chars, chunk_overlap)
        for char_start, char_end, chunk_text_str in page_chunks:
            # Sequentially embed each chunk
            embedding = embed_fn(chunk_text_str)
            chunks_result.append({
                "page_no": page_no,
                "char_start": char_start,
                "char_end": char_end,
                "text": chunk_text_str,
                "embedding": embedding
            })
            total_chunks += 1
            
    duration = time.time() - start_time
    if total_chunks > 0:
        rate = total_chunks / duration
        logger.info(f"Embedded {total_chunks} chunks in {duration:.2f}s ({rate:.2f} chunks/sec)")
        
    return {
        "chunks": chunks_result
    }

@handler("embed")
def embed_task(cfg: Config, job: dict) -> dict:
    """Worker task handler for generating embeddings."""
    doc_id = job["doc_id"]
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"

    payload_raw = job.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else (payload_raw or {})
    except (json.JSONDecodeError, TypeError):
        payload = {}
    doc_year: int | None = payload.get("year")

    # 1. Fetch OCR JSON from broker
    try:
        ocr_resp = httpx.get(f"{broker_url}/ocr/{doc_id}.json", timeout=30.0)
        if ocr_resp.status_code == 404:
            raise PermanentJobError(f"OCR file not found for doc_id {doc_id}")
        ocr_resp.raise_for_status()
        ocr_data = ocr_resp.json()
    except httpx.HTTPError as e:
        raise Exception(f"Failed to fetch OCR JSON from broker: {e}")

    # 2. Define embedding function via local Ollama API
    def ollama_embed(prompt: str) -> List[float]:
        try:
            resp = httpx.post(
                "http://localhost:11434/api/embeddings",
                json={
                    "model": cfg.embed["model"],
                    "prompt": prompt,
                    "keep_alive": cfg.models["keep_alive"]
                },
                timeout=30.0
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except httpx.HTTPError as e:
            raise Exception(f"Ollama embedding API call failed: {e}")

    # 3. Process
    result = process_embed(ocr_data, cfg, ollama_embed)
    if doc_year is not None:
        result["year"] = doc_year
    return result
