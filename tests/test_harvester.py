import os
import pytest
import time
import httpx
from pathlib import Path
from unittest.mock import MagicMock, patch

from palimpsest.config import load
from palimpsest.db import connect, migrate



@pytest.fixture(scope="module", autouse=True)
def setup_config(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("harvester_test_root")
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    
    config_content = f"""
    [storage]
    root = "{tmp_path}"
    [db]
    path = "{{storage.root}}/db/palimpsest.db"
    [broker]
    host = "localhost"
    port = 8077
    lease_ttl_seconds = 900
    heartbeat_seconds = 120
    max_attempts = 3
    [mcp]
    port = 8078
    [harvest]
    base_url = "https://www.osti.gov/opennet"
    rate_limit_rps = 100.0
    backoff_initial_s = 0.01
    backoff_max_s = 0.1
    user_agent = "test-agent"
    accession_prefix = "NV"
    [ocr]
    engine_preference = ["vision"]
    min_confidence = 0.5
    rerun_if_osti_text_shorter_than = 200
    [features]
    redaction_context_chars = 300
    redaction_context_lines = 2
    blackbox_min_area_frac = 0.001
    blackbox_max_area_frac = 0.25
    blackbox_darkness_threshold = 60
    [embed]
    model = "nomic-embed"
    dim = 768
    chunk_chars = 800
    chunk_overlap = 150
    [gapjoin]
    score_threshold = 0.65
    w_cosine = 0.5
    w_anchor = 0.3
    w_kind = 0.2
    topk_embedding_candidates = 50
    [models]
    extract = "llama"
    classify = "qwen"
    keep_alive = "24h"
    [nodes]
    gonktop = []
    """
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_content)
    os.environ["PALIMPSEST_CONFIG"] = str(cfg_file)
    
    # Run migrations
    from palimpsest.config import load
    from palimpsest.db import migrate
    migrate(load(cfg_file))
    
    yield cfg_file

@pytest.fixture
def clean_db():
    from palimpsest.config import load
    from palimpsest.db import connect
    cfg = load()
    conn = connect(cfg)
    with conn:
        conn.execute("DELETE FROM documents;")
        conn.execute("DELETE FROM jobs;")
    return conn

def test_rate_limiter():
    from palimpsest.harvester import rate_limit_sleep
    start = time.time()
    rate_limit_sleep(50.0)
    rate_limit_sleep(50.0)
    duration = time.time() - start
    assert duration >= 0.01

@patch("httpx.Client.get")
def test_harvester_kill_switch_on_403(mock_get, clean_db, tmp_path):
    from palimpsest.harvester import catalog, consecutive_403_count
    import palimpsest.harvester as harvester
    harvester.consecutive_403_count = 0
    
    # Setup mock returning 403 repeatedly
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_get.return_value = mock_resp
    
    # Save original content of HUMAN_DO_THIS.md
    human_file = Path("/Users/herren/dev/palimpsest/HUMAN_DO_THIS.md")
    orig_content = human_file.read_text() if human_file.exists() else None
    if human_file.exists():
         human_file.unlink()
         
    try:
        with pytest.raises(SystemExit):
            catalog(limit=5)
            
        # Check that HUMAN_DO_THIS.md was created with the message
        assert human_file.exists()
        content = human_file.read_text()
        assert "OSTI may have blocked us" in content
    finally:
        # Restore original content
        if orig_content is not None:
            human_file.write_text(orig_content)
        elif human_file.exists():
            human_file.unlink()

@patch("httpx.Client.get")
def test_harvester_catalog_success(mock_get, clean_db):
    from palimpsest.harvester import catalog
    # Setup mock search results HTML
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = """
    <html>
      <div class='search-result-counts'>Showing 1 to 2 of 2 entries</div>
      <table id='search-results-table'>
        <tbody>
          <tr>
            <td><a href='/opennet/detail?osti-id=932729'>A Title</a></td>
            <td>Author A</td>
            <td>NV0339713</td>
            <td>DocNum A</td>
            <td>Other</td>
            <td>NSTEC</td>
            <td>2008 Jun 26</td>
            <td>2008 Feb 28</td>
            <td></td>
            <td></td>
            <td></td>
          </tr>
          <tr>
            <td><a href='/opennet/detail?osti-id=16007515'>Another Title</a></td>
            <td>Author B</td>
            <td>NV0758437</td>
            <td>DocNum B</td>
            <td>Other</td>
            <td>RECC</td>
            <td>1999 Jul 13</td>
            <td>1993 Sep 08</td>
            <td></td>
            <td><a href='/opennet/servlets/purl/16007515.pdf'>pdf</a></td>
            <td></td>
          </tr>
        </tbody>
      </table>
    </html>
    """
    mock_get.return_value = mock_resp
    
    catalog(limit=10)
    
    # Assert database populated
    conn = clean_db
    cur = conn.execute("SELECT doc_id, accession, has_fulltext, status FROM documents ORDER BY doc_id ASC")
    rows = cur.fetchall()
    assert len(rows) == 2
    
    # doc 16007515
    assert rows[0]["doc_id"] == "16007515"
    assert rows[0]["accession"] == "NV0758437"
    assert rows[0]["has_fulltext"] == 1
    assert rows[0]["status"] == "cataloged"
    
    # doc 932729
    assert rows[1]["doc_id"] == "932729"
    assert rows[1]["accession"] == "NV0339713"
    assert rows[1]["has_fulltext"] == 0

@patch("httpx.Client.post")
@patch("httpx.Client.get")
def test_harvester_fetch_success(mock_get, mock_post, clean_db):
    import hashlib
    from palimpsest.harvester import fetch
    
    # Pre-populate cataloged doc
    conn = clean_db
    with conn:
        conn.execute(
            "INSERT INTO documents (doc_id, accession, has_fulltext, source_url, status) VALUES ('16007515', 'NV0758437', 1, 'https://www.osti.gov/opennet/servlets/purl/16007515.pdf', 'cataloged')"
        )
        
    # Mock GET for PDF download
    mock_resp_get = MagicMock()
    mock_resp_get.status_code = 200
    mock_resp_get.content = b"%PDF-1.4 mock content"
    mock_get.return_value = mock_resp_get
    
    # Mock POST for broker enqueue
    mock_resp_post = MagicMock()
    mock_resp_post.status_code = 200
    mock_post.return_value = mock_resp_post
    
    fetch(limit=5)
    
    # Assert PDF file is written
    cfg = load()
    pdf_path = cfg.storage_root / "raw" / "16007515.pdf"
    assert pdf_path.exists()
    assert pdf_path.read_bytes() == b"%PDF-1.4 mock content"
    
    # Assert db is updated
    cur = conn.execute("SELECT status, sha256, local_path FROM documents WHERE doc_id='16007515'")
    row = cur.fetchone()
    assert row["status"] == "fetched"
    assert row["sha256"] == hashlib.sha256(b"%PDF-1.4 mock content").hexdigest()
    assert row["local_path"] == str(pdf_path)
    
    # Assert broker enqueue called
    mock_post.assert_called_once()
    assert "/enqueue" in mock_post.call_args[0][0]

@patch("httpx.Client.post")
@patch("httpx.Client.get")
def test_harvester_fetch_idempotent(mock_get, mock_post, clean_db):
    import hashlib
    from palimpsest.harvester import fetch
    
    # Pre-populate cataloged doc
    conn = clean_db
    with conn:
        conn.execute(
            "INSERT INTO documents (doc_id, accession, has_fulltext, source_url, status) VALUES ('16007515', 'NV0758437', 1, 'https://www.osti.gov/opennet/servlets/purl/16007515.pdf', 'cataloged')"
        )
        
    cfg = load()
    pdf_path = cfg.storage_root / "raw" / "16007515.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 mock content")
    
    # Mock POST for broker enqueue
    mock_resp_post = MagicMock()
    mock_resp_post.status_code = 200
    mock_post.return_value = mock_resp_post
    
    # Run fetch: should not call GET since file exists
    fetch(limit=5)
    
    mock_get.assert_not_called()
    mock_post.assert_called_once()
    
    # Check db is updated
    cur = conn.execute("SELECT status, sha256 FROM documents WHERE doc_id='16007515'")
    row = cur.fetchone()
    assert row["status"] == "fetched"
    assert row["sha256"] == hashlib.sha256(b"%PDF-1.4 mock content").hexdigest()


