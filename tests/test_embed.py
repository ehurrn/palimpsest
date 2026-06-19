# tests/test_embed.py
import pytest
from palimpsest.tasks.embed import chunk_text, process_embed, embed_task

@pytest.fixture
def test_config():
    class DummyConfig:
        embed = {
            "chunk_chars": 20,
            "chunk_overlap": 5,
            "model": "test-model"
        }
        models = {
            "keep_alive": "24h"
        }
        broker = {
            "host": "localhost",
            "port": 8077
        }
    return DummyConfig()

def test_chunker_basic():
    # Basic word boundary chunking
    text = "hello world foo bar baz"
    # length of text = 23
    # chunk_chars = 10, chunk_overlap = 3
    chunks = chunk_text(text, 10, 3)
    # Let's inspect where it splits:
    # "hello world foo bar baz"
    # first chunk: start 0, max end 10 -> "hello worl" -> last space is at 5 -> chunk = "hello" (0, 5)
    # next start: 5 - 3 = 2 -> snaps to word start -> 5 (since 2 is in middle of hello)
    # second chunk: start 5, max end 15 -> " world foo " -> last space is at 11 -> chunk = " world" (5, 11)
    # next start: 11 - 3 = 8 -> snaps to word start -> 6 (world start is at 6)
    # third chunk: start 6, max end 16 -> "world foo " -> chunk = "world" (6, 11)
    # etc.
    assert len(chunks) > 0
    # Make sure we reconstruct correct substrings
    for start, end, sub in chunks:
        assert text[start:end] == sub

def test_chunker_boundaries_no_mid_word():
    # Chunker should not split mid-word unless necessary
    text = "one two three four five six"
    # chunk_chars = 15, chunk_overlap = 5
    # "one two three " is 14 chars. "one two three f" is 15.
    # space is at index 13.
    chunks = chunk_text(text, 15, 5)
    
    # Assert none of the chunks (except maybe if word length > chunk_chars) start or end mid-word
    for start, end, sub in chunks:
        # Check that character before start is whitespace, or start itself is whitespace, or start is 0
        if start > 0:
            assert text[start - 1].isspace() or text[start].isspace()
        # Check that character at end is whitespace, or character before end is whitespace, or end is text length
        if end < len(text):
            assert text[end].isspace() or text[end - 1].isspace()


def test_chunker_exact_length():
    # Test text of exact length equal to chunk_chars
    text = "hello world" # 11 chars
    chunks = chunk_text(text, 11, 3)
    assert len(chunks) == 1
    assert chunks[0] == (0, 11, "hello world")

def test_chunker_empty():
    assert chunk_text("", 10, 3) == []

def test_process_embed(test_config):
    # Test process_embed function with mocked batch embedding fn
    ocr_data = [
        {
            "page_no": 1,
            "text": "This is page one text. It has some words."
        },
        {
            "page_no": 2,
            "text": "" # empty page
        }
    ]

    dummy_vector = [0.1] * 768
    def mock_embed(prompts):
        return [dummy_vector] * len(prompts)

    res = process_embed(ocr_data, test_config, mock_embed)
    assert "chunks" in res
    chunks = res["chunks"]
    # Should have chunks for page 1 but not page 2
    assert len(chunks) > 0
    for ch in chunks:
        assert ch["page_no"] == 1
        assert ch["embedding"] == dummy_vector
        assert len(ch["text"]) > 0

def test_handler_http_mocking(test_config, monkeypatch):
    class DummyResponse:
        def __init__(self, status_code, json_data=None):
            self.status_code = status_code
            self.json_data = json_data
        def json(self):
            return self.json_data
        def raise_for_status(self):
            pass
            
    def mock_get(url, *args, **kwargs):
        if "/ocr/" in url:
            return DummyResponse(200, json_data=[{
                "page_no": 1,
                "text": "Hello world"
            }])
        return DummyResponse(404)
        
    def mock_post(url, json=None, *args, **kwargs):
        if url.endswith("/api/embed"):
            n = len((json or {}).get("input", [""]))
            return DummyResponse(200, json_data={"embeddings": [[0.2] * 768] * n})
        return DummyResponse(404)
        
    monkeypatch.setattr("httpx.get", mock_get)
    monkeypatch.setattr("httpx.post", mock_post)
    
    job = {"doc_id": "123"}
    res = embed_task(test_config, job)
    assert len(res["chunks"]) == 1
    assert res["chunks"][0]["embedding"] == [0.2] * 768
