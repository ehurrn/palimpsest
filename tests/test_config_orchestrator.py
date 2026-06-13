# tests/test_config_orchestrator.py
"""Tests for orchestrator config field (optional section)."""
import textwrap
import pytest
from pathlib import Path
from palimpsest.config import load, ConfigError


def _write_config(tmp_path: Path, toml_str: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(toml_str))
    return p


BASE_TOML = """
    [storage]
    root = "/tmp/palimpsest_test"

    [db]
    path = "{storage.root}/palimpsest.db"

    [broker]
    url = "http://localhost:9999"

    [mcp]
    host = "localhost"
    port = 8765

    [harvest]
    max_docs = 100

    [ocr]
    engine = "tesseract"

    [features]
    redaction_context_chars = 300

    [embed]
    model = "nomic-embed"
    dim = 768

    [gapjoin]
    score_threshold = 0.65

    [models]
    keep_alive = "24h"

    [nodes]
    worker = "http://localhost:9001"
"""


def test_config_loads_without_orchestrator_section(tmp_path):
    """Config without [orchestrator] section loads and defaults to empty dict."""
    p = _write_config(tmp_path, BASE_TOML)
    cfg = load(p)
    assert cfg.orchestrator == {}


def test_config_loads_orchestrator_section(tmp_path):
    """Config with [orchestrator] section populates the field correctly."""
    toml_with_orch = BASE_TOML + """
    [orchestrator]
    heartbeat_interval_secs = 900
    low_water_mark = 10
    broker_timeout_secs = 5
    """
    p = _write_config(tmp_path, toml_with_orch)
    cfg = load(p)
    assert cfg.orchestrator["heartbeat_interval_secs"] == 900
    assert cfg.orchestrator["low_water_mark"] == 10
    assert cfg.orchestrator["broker_timeout_secs"] == 5


def test_orchestrator_defaults_used_when_empty(tmp_path):
    """Empty orchestrator section -> empty dict; orchestrator reads defaults."""
    toml_empty_orch = BASE_TOML + "\n[orchestrator]\n"
    p = _write_config(tmp_path, toml_empty_orch)
    cfg = load(p)
    assert isinstance(cfg.orchestrator, dict)
    # Orchestrator code will read .get("heartbeat_interval_secs", 900) -> 900
    assert cfg.orchestrator.get("heartbeat_interval_secs", 900) == 900
