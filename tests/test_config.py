import pytest
import os
from pathlib import Path
from palimpsest.config import load, ConfigError

def test_load_happy(tmp_path):
    # Create a dummy config
    conf_file = tmp_path / "config.toml"
    conf_file.write_text("""
[storage]
root = "/tmp/pal"
[db]
path = "{storage.root}/pal.db"
[broker]
port = 8080
[mcp]
port = 8081
[harvest]
base_url = "http://test"
[ocr]
engine_preference = ["t"]
[features]
redaction_context_chars = 100
blackbox_min_area_frac = 0.01
blackbox_max_area_frac = 0.1
blackbox_darkness_threshold = 50
[embed]
model = "m"
dim = 100
chunk_chars = 100
chunk_overlap = 10
[gapjoin]
score_threshold = 0.5
[models]
extract = "m"
classify = "m"
keep_alive = "1h"
[nodes]
n = []
""")
    cfg = load(conf_file)
    assert cfg.storage_root == Path("/tmp/pal")
    assert cfg.db_path == Path("/tmp/pal/pal.db")

def test_missing_sections(tmp_path):
    conf_file = tmp_path / "config.toml"
    conf_file.write_text("[storage]\nroot='/tmp'")
    with pytest.raises(ConfigError, match="Missing config sections"):
        load(conf_file)
