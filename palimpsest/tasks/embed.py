# palimpsest/tasks/embed.py
import json
import logging
import time
import httpx
import spacy
from typing import List, Dict, Any, Callable, Tuple

from palimpsest.config import Config
from palimpsest.tasks import handler, PermanentJobError

logger = logging.getLogger(__name__)

_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def chunk_text(text: str, chunk_chars: int, chunk_overlap: int) -> List[Tuple[int, int, str]]:
    """
    Chunk text at sentence boundaries, grouping sentences up to chunk_chars.
    Carries the last sentence of the previous chunk into the next chunk as overlap.
    Returns list of (char_start, char_end, chunk_substring).
    """
    if not text:
        return []

    nlp = _get_nlp()
    doc = nlp(text)
    sentences = list(doc.sents)

    if not sentences:
        return [(0, len(text), text)]

    chunks: List[Tuple[int, int, str]] = []
    current_sents: list = []
    current_chars = 0

    for sent in sentences:
        sent_len = len(sent.text)

        if current_chars + sent_len > chunk_chars and current_sents:
            # Emit current chunk
            start = current_sents[0].start_char
            end = current_sents[-1].end_char
            chunks.append((start, end, text[start:end]))
            # Carry over last sentence as semantic overlap
            overlap = current_sents[-1]
            current_sents = [overlap, sent]
            current_chars = len(overlap.text) + sent_len
        else:
            current_sents.append(sent)
            current_chars += sent_len

    if current_sents:
        start = current_sents[0].start_char
        end = current_sents[-1].end_char
        chunks.append((start, end, text[start:end]))

    return chunks


def process_embed(
    ocr_data: List[Dict[str, Any]],
    cfg: Config,
    embed_fn: Callable[[List[str]], List[List[float]]],
) -> Dict[str, Any]:
    """Core chunking and batch embedding logic, isolated for unit testing."""
    chunk_chars = cfg.embed.get("chunk_chars", 800)
    chunk_overlap = cfg.embed.get("chunk_overlap", 150)

    chunk_meta: List[Dict[str, Any]] = []
    all_texts: List[str] = []

    for page in ocr_data:
        page_no = page["page_no"]
        text = page.get("text", "")
        if not text:
            lines = page.get("lines", [])
            text = "\n".join(line["text"] for line in lines)

        if not text.strip():
            continue

        for char_start, char_end, chunk_str in chunk_text(text, chunk_chars, chunk_overlap):
            chunk_meta.append({"page_no": page_no, "char_start": char_start, "char_end": char_end})
            all_texts.append(chunk_str)

    if not all_texts:
        return {"chunks": []}

    start_time = time.time()
    embeddings = embed_fn(all_texts)
    duration = time.time() - start_time

    rate = len(all_texts) / duration if duration > 0 else 0
    logger.info(f"Embedded {len(all_texts)} chunks in {duration:.2f}s ({rate:.2f} chunks/sec)")

    chunks_result = [
        {
            "page_no": meta["page_no"],
            "char_start": meta["char_start"],
            "char_end": meta["char_end"],
            "text": text_str,
            "embedding": emb,
        }
        for meta, text_str, emb in zip(chunk_meta, all_texts, embeddings)
    ]

    return {"chunks": chunks_result}


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

    # 2. Batch embedding function via Ollama /api/embed
    def ollama_embed(texts: List[str]) -> List[List[float]]:
        try:
            resp = httpx.post(
                "http://localhost:11434/api/embed",
                json={
                    "model": cfg.embed["model"],
                    "input": texts,
                    "keep_alive": cfg.models["keep_alive"],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            return resp.json()["embeddings"]
        except httpx.HTTPError as e:
            raise Exception(f"Ollama embedding API call failed: {e}")

    # 3. Process
    result = process_embed(ocr_data, cfg, ollama_embed)
    if doc_year is not None:
        result["year"] = doc_year
    return result
