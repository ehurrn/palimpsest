import os
from unittest.mock import MagicMock, patch

import pytest

from palimpsest.tasks import HANDLERS, PermanentJobError, handler
from palimpsest.worker import run_worker


# Dummy tasks for testing. Handlers receive optional lost_evt/shutdown_event
# control events as keyword args (see palimpsest.tasks signature); the dummies
# ignore them via **kwargs.
@handler("dummy_ok")
def dummy_ok_handler(cfg, job, **kwargs):
    return {"status": "ok"}


@handler("dummy_fail")
def dummy_fail_handler(cfg, job, **kwargs):
    raise Exception("Normal failure")


@handler("dummy_permanent_fail")
def dummy_permanent_fail_handler(cfg, job, **kwargs):
    raise PermanentJobError("Permanent failure")


@pytest.fixture(scope="module", autouse=True)
def setup_config(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("worker_test_root")
    config_content = f"""
    [storage]
    root = "{tmp_path}"
    [db]
    path = "{{storage.root}}/db/palimpsest.db"
    [broker]
    host = "localhost"
    port = 8077
    lease_ttl_seconds = 900
    heartbeat_seconds = 1
    max_attempts = 3
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
    m4 = ["dummy_ok", "dummy_fail", "dummy_permanent_fail"]
    [orchestrator]
    heartbeat_interval_secs = 900
    """
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(config_content)
    os.environ["PALIMPSEST_CONFIG"] = str(cfg_file)
    yield cfg_file


def test_registry():
    assert "dummy_ok" in HANDLERS
    assert "dummy_fail" in HANDLERS
    assert "dummy_permanent_fail" in HANDLERS


@patch("httpx.Client.post")
@patch("httpx.Client.get")
def test_worker_lease_and_complete(mock_get, mock_post, setup_config):
    # Mock model warming re-ping
    mock_warm_resp = MagicMock()
    mock_warm_resp.status_code = 200
    mock_post.return_value = mock_warm_resp

    # Mock /lease response
    mock_lease_resp = MagicMock()
    mock_lease_resp.status_code = 200
    mock_lease_resp.json.return_value = {
        "jobs": [{"job_id": 42, "type": "dummy_ok", "doc_id": "100", "payload": {}}]
    }

    # Mock /complete response
    mock_complete_resp = MagicMock()
    mock_complete_resp.status_code = 200
    mock_complete_resp.json.return_value = {"ok": True}

    # Define side effects for client.post:
    # First call: warming (we will mock client.post to return 200 for model warming)
    # Next call: /lease
    # Next call: /complete
    def post_side_effect(url, **kwargs):
        if "/lease" in url:
            return mock_lease_resp
        elif "/complete" in url:
            return mock_complete_resp
        else:
            return mock_warm_resp

    mock_post.side_effect = post_side_effect

    run_worker("m4", once=True)

    # Assert complete was posted
    complete_called = False
    for call in mock_post.call_args_list:
        url = call[0][0]
        if "/complete" in url:
            complete_called = True
            payload = call[1]["json"]
            assert payload["job_id"] == 42
            assert payload["worker_id"] == "m4"
            assert payload["result"] == {"status": "ok"}
    assert complete_called


@patch("httpx.Client.post")
@patch("httpx.Client.get")
def test_worker_handler_fail_retryable(mock_get, mock_post, setup_config):
    mock_warm_resp = MagicMock()
    mock_warm_resp.status_code = 200

    mock_lease_resp = MagicMock()
    mock_lease_resp.status_code = 200
    mock_lease_resp.json.return_value = {
        "jobs": [{"job_id": 43, "type": "dummy_fail", "doc_id": "100", "payload": {}}]
    }

    mock_fail_resp = MagicMock()
    mock_fail_resp.status_code = 200
    mock_fail_resp.json.return_value = {"status": "recorded"}

    def post_side_effect(url, **kwargs):
        if "/lease" in url:
            return mock_lease_resp
        elif "/fail" in url:
            return mock_fail_resp
        else:
            return mock_warm_resp

    mock_post.side_effect = post_side_effect

    run_worker("m4", once=True)

    # Assert fail was posted with retryable=True
    fail_called = False
    for call in mock_post.call_args_list:
        url = call[0][0]
        if "/fail" in url:
            fail_called = True
            payload = call[1]["json"]
            assert payload["job_id"] == 43
            assert payload["worker_id"] == "m4"
            assert payload["retryable"] is True
            assert "Normal failure" in payload["error"]
    assert fail_called


@patch("httpx.Client.post")
@patch("httpx.Client.get")
def test_worker_handler_fail_permanent(mock_get, mock_post, setup_config):
    mock_warm_resp = MagicMock()
    mock_warm_resp.status_code = 200

    mock_lease_resp = MagicMock()
    mock_lease_resp.status_code = 200
    mock_lease_resp.json.return_value = {
        "jobs": [{"job_id": 44, "type": "dummy_permanent_fail", "doc_id": "100", "payload": {}}]
    }

    mock_fail_resp = MagicMock()
    mock_fail_resp.status_code = 200
    mock_fail_resp.json.return_value = {"status": "recorded"}

    def post_side_effect(url, **kwargs):
        if "/lease" in url:
            return mock_lease_resp
        elif "/fail" in url:
            return mock_fail_resp
        else:
            return mock_warm_resp

    mock_post.side_effect = post_side_effect

    run_worker("m4", once=True)

    # Assert fail was posted with retryable=False
    fail_called = False
    for call in mock_post.call_args_list:
        url = call[0][0]
        if "/fail" in url:
            fail_called = True
            payload = call[1]["json"]
            assert payload["job_id"] == 44
            assert payload["worker_id"] == "m4"
            assert payload["retryable"] is False
            assert "Permanent failure" in payload["error"]
    assert fail_called
