import httpx
from typing import List, Dict, Any
from palimpsest.config import Config

def chunk_text(text: str, chunk_chars: int, chunk_overlap: int) -> List[Dict[str, Any]]:
    chunks = []
    start = 0
    text_len = len(text)
    
    while start < text_len:
        end = min(start + chunk_chars, text_len)
        if end < text_len:
            # find last whitespace before chunk limit
            last_space = text.rfind(' ', start, end)
            if last_space != -1:
                end = last_space
        
        chunk_content = text[start:end].strip()
        if chunk_content:
            chunks.append({
                "text": chunk_content,
                "char_start": start,
                "char_end": end
            })
        
        start = end - chunk_overlap
        if start < 0:
            start = 0
            
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
                        "keep_alive": cfg.models["keep_alive"]
                    },
                    timeout=30.0
                )
                response.raise_for_status()
                embedding = response.json().get("embedding")
                
                all_chunks.append({
                    "page_no": page_no,
                    "char_start": chunk["char_start"],
                    "char_end": chunk["char_end"],
                    "text": chunk["text"],
                    "embedding": embedding
                })
    
    return {"chunks": all_chunks}
