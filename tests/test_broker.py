import os
import pytest
import json
import time
from pathlib import Path

# Setup temp config before importing broker
@pytest.fixture(scope="module", autouse=True)
def setup_config(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("broker_test_root")
    # Make directories that the broker/file endpoint needs
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    
    config_content = f"""
    [storage]
    root = "{tmp_path}"
    [db]
    path = "{{storage.root}}/db/palimpsest.db"
    [broker]
    host = "localhost"
    port = 8077
    lease_ttl_seconds = 2
    heartbeat_seconds = 1
    max_attempts = 2
    [mcp]
    port = 8078
    [harvest]
    base_url = "https://www.osti.gov/opennet"
    rate_limit_rps = 1.0
    backoff_initial_s = 5
    backoff_max_s = 300
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

# Now import TestClient and app
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    from palimpsest.broker import app
    # Clear tables before each test to have isolation
    from palimpsest.config import load
    from palimpsest.db import connect
    cfg = load()
    conn = connect(cfg)
    with conn:
        conn.execute("DELETE FROM jobs;")
        conn.execute("DELETE FROM pages;")
        conn.execute("DELETE FROM redactions;")
        conn.execute("DELETE FROM entities;")
        conn.execute("DELETE FROM chunks;")
        conn.execute("DELETE FROM documents;")
    return TestClient(app)

def test_enqueue_dedupe(client):
    # Enqueue first time
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["state"] == "pending"
    assert data.get("deduped") is not True
    job_id = data["job_id"]
    
    # Enqueue second time - should deduplicate
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["state"] == "pending"
    assert data.get("deduped") is True
    
    # Change status of job to failed manually in database to test reset
    from palimpsest.config import load
    from palimpsest.db import connect
    cfg = load()
    conn = connect(cfg)
    with conn:
        conn.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (job_id,))
        
    # Enqueue third time - should reset to pending
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["state"] == "pending"
    assert data.get("deduped") is True

def test_lease_capabilities(client):
    # Enqueue job
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    assert resp.status_code == 200
    
    # Lease with mismatching capability
    resp = client.post("/lease", json={"worker_id": "m4", "capabilities": ["embed"], "max_jobs": 1})
    assert resp.status_code == 200
    assert len(resp.json()["jobs"]) == 0
    
    # Lease with matching capability
    resp = client.post("/lease", json={"worker_id": "m4", "capabilities": ["ocr"], "max_jobs": 1})
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["doc_id"] == "111"
    assert jobs[0]["type"] == "ocr"

def test_double_lease(client):
    # Enqueue job
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    assert resp.status_code == 200
    
    # Lease first time
    resp = client.post("/lease", json={"worker_id": "m4", "capabilities": ["ocr"], "max_jobs": 1})
    assert resp.status_code == 200
    assert len(resp.json()["jobs"]) == 1
    
    # Lease second time - should be empty
    resp = client.post("/lease", json={"worker_id": "gonktop", "capabilities": ["ocr"], "max_jobs": 1})
    assert resp.status_code == 200
    assert len(resp.json()["jobs"]) == 0

def test_complete_non_owner(client):
    # Enqueue and lease
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    job_id = resp.json()["job_id"]
    client.post("/lease", json={"worker_id": "m4", "capabilities": ["ocr"], "max_jobs": 1})
    
    # Complete with different worker
    resp = client.post("/complete", json={"worker_id": "gonktop", "job_id": job_id, "result": []})
    assert resp.status_code == 409

def test_fail_max_attempts(client):
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    job_id = resp.json()["job_id"]
    
    # Lease 1 (attempts=1)
    client.post("/lease", json={"worker_id": "m4", "capabilities": ["ocr"], "max_jobs": 1})
    # Fail 1 (retryable=True) -> goes back to pending
    resp = client.post("/fail", json={"worker_id": "m4", "job_id": job_id, "error": "err1", "retryable": True})
    assert resp.status_code == 200
    
    # Lease 2 (attempts=2)
    client.post("/lease", json={"worker_id": "m4", "capabilities": ["ocr"], "max_jobs": 1})
    # Fail 2 (retryable=True) -> attempts(2) >= max_attempts(2) -> goes to dead
    resp = client.post("/fail", json={"worker_id": "m4", "job_id": job_id, "error": "err2", "retryable": True})
    assert resp.status_code == 200
    
    # Verify job is dead
    from palimpsest.config import load
    from palimpsest.db import connect
    cfg = load()
    conn = connect(cfg)
    cur = conn.execute("SELECT state, error FROM jobs WHERE job_id=?", (job_id,))
    row = cur.fetchone()
    assert row["state"] == "dead"
    assert "err2" in row["error"]

def test_reaper(client):
    # Enqueue
    resp = client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    job_id = resp.json()["job_id"]
    
    # Lease
    client.post("/lease", json={"worker_id": "m4", "capabilities": ["ocr"], "max_jobs": 1})
    
    # Run reaper synchronously (we call the endpoint /reap or trigger function directly)
    # Let's verify reaper endpoint or helper function works
    from palimpsest.broker import reap_leases
    
    # Immediately after lease, shouldn't reap (TTL is 2s)
    reap_leases()
    
    from palimpsest.config import load
    from palimpsest.db import connect
    cfg = load()
    conn = connect(cfg)
    cur = conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,))
    assert cur.fetchone()["state"] == "leased"
    
    # Wait 3 seconds so lease expires
    time.sleep(3.0)
    reap_leases()
    
    cur = conn.execute("SELECT state, attempts FROM jobs WHERE job_id=?", (job_id,))
    row = cur.fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 1 # Retains attempts count

def test_file_path_traversal(client):
    # Traversal resolves to a non-existent path outside the route prefix
    resp = client.get("/file/../../etc/passwd")
    assert resp.status_code in (400, 422, 404)
    
    # Invalid ID type (contains non-digits)
    resp = client.get("/file/abc.pdf")
    assert resp.status_code == 400
    
    resp = client.get("/file/12345.pdf")
    assert resp.status_code == 404 # Valid ID, but file doesn't exist

def test_ocr_result_handling(client):
    # Enqueue
    client.post("/enqueue", json={"type": "ocr", "doc_id": "111", "priority": 5, "payload": {}})
    
    # Lease
    lease_resp = client.post("/lease", json={"worker_id": "m4", "capabilities": ["ocr"], "max_jobs": 1})
    job_id = lease_resp.json()["jobs"][0]["job_id"]
    
    # Seed document entry in database first
    from palimpsest.config import load
    from palimpsest.db import connect
    cfg = load()
    conn = connect(cfg)
    with conn:
        conn.execute("INSERT INTO documents (doc_id, status) VALUES ('111', 'cataloged');")
        
    ocr_result = [
        {
            "page_no": 1,
            "width": 612.0,
            "height": 792.0,
            "ocr_source": "vision",
            "lines": [
                {"text": "CONFIDENTIAL DOCUMENT", "bbox": [0.1, 0.1, 0.5, 0.15], "conf": 0.95}
            ],
            "text": "CONFIDENTIAL DOCUMENT"
        }
    ]
    
    resp = client.post("/complete", json={
        "worker_id": "m4",
        "job_id": job_id,
        "result": ocr_result
    })
    assert resp.status_code == 200
    
    # Check JSON file written
    ocr_file = cfg.storage_root / "ocr" / "111.json"
    assert ocr_file.exists()
    with open(ocr_file) as f:
        saved_data = json.load(f)
    assert saved_data[0]["text"] == "CONFIDENTIAL DOCUMENT"
    
    # Check database pages row inserted
    cur = conn.execute("SELECT doc_id, page_no, text FROM pages WHERE doc_id='111'")
    page = cur.fetchone()
    assert page is not None
    assert page["page_no"] == 1
    assert page["text"] == "CONFIDENTIAL DOCUMENT"
    
    # Check documents table updated
    cur = conn.execute("SELECT status, page_count FROM documents WHERE doc_id='111'")
    doc = cur.fetchone()
    assert doc["status"] == "ocr_done"
    assert doc["page_count"] == 1
    
    # Check features job enqueued automatically (chaining)
    cur = conn.execute("SELECT state FROM jobs WHERE type='features' AND doc_id='111'")
    job = cur.fetchone()
    assert job is not None
    assert job["state"] == "pending"
    
    # Test GET /ocr/{doc_id}.json endpoint
    resp = client.get("/ocr/111.json")
    assert resp.status_code == 200
    assert resp.json()[0]["text"] == "CONFIDENTIAL DOCUMENT"
    
    resp = client.get("/ocr/abc.json")
    assert resp.status_code == 400
    
    resp = client.get("/ocr/999.json")
    assert resp.status_code == 404

