from typing import Any, Dict, List

import httpx

from palimpsest.config import Config


def chunk_text(text: str, chunk_chars: int, chunk_overlap: int) -> List[Dict[str, Any]]:
    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_chars, text_len)
        if end < text_len:
            # break on the last whitespace before the limit to avoid mid-word splits
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space

        chunk_content = text[start:end].strip()
        if chunk_content:
            chunks.append(
                {
                    "text": chunk_content,
                    "char_start": start,
                    "char_end": end,
                }
            )

        # Stop once a chunk reaches the end of the text. Without this guard, a
        # final chunk whose end == text_len and (end - chunk_overlap) <= start
        # never advances `start`, looping forever and growing `chunks` until the
        # process is OOM-killed.
        if end >= text_len:
            break
        next_start = end - chunk_overlap
        start = next_start if next_start > start else end  # guarantee progress

    return chunks


def embed_handler(cfg: Config, doc_id: str, ocr_data: Dict[str, Any]) -> Dict[str, Any]:
    all_chunks = []

    for page in ocr_data.get("pages", []):
        page_no = page.get("page_no")
        text = page.get("text", "")
        if not text:
            continue

        chunks = chunk_text(text, cfg.embed["chunk_chars"], cfg.embed["chunk_overlap"])

        for chunk in chunks:
            with httpx.Client() as client:
                response = client.post(
                    "http://localhost:11434/api/embeddings",
                    json={
                        "model": cfg.embed["model"],
                        "prompt": chunk["text"],
                        "keep_alive": cfg.models["keep_alive"],
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                embedding = response.json().get("embedding")

                all_chunks.append(
                    {
                        "page_no": page_no,
                        "char_start": chunk["char_start"],
                        "char_end": chunk["char_end"],
                        "text": chunk["text"],
                        "embedding": embedding,
                    }
                )

    return {"chunks": all_chunks}
