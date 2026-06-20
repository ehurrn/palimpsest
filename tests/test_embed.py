import pytest
import numpy as np
from palimpsest.tasks.embed import chunk_text

def test_chunker_boundaries():
    text = "word " * 200 # roughly 1000 chars
    chunk_chars = 800
    chunk_overlap = 150
    chunks = chunk_text(text, chunk_chars, chunk_overlap)
    
    assert len(chunks) > 1
    for chunk in chunks:
        # Check chunk length (it might be less than chunk_chars due to whitespace splitting)
        assert len(chunk["text"]) <= chunk_chars
        # Check no mid-word split (if "word " is the unit)
        # Note: chunk_text strips the text
        assert chunk["text"].endswith("word")
    
    # Check overlap (The logic in embed.py should produce overlapping chunks)
    # Actually checking overlap is slightly complex because I stripped the text.
    # The start/end indices cover the overlap.
    # Let's verify the indices overlap logic.
    for i in range(len(chunks) - 1):
        assert chunks[i+1]["char_start"] < chunks[i]["char_end"]
