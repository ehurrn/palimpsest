# tests/test_worker_release.py
"""Unit tests for worker graceful release on SIGTERM/SIGINT."""
import signal
from unittest.mock import MagicMock, patch


def test_signal_handler_calls_release_when_job_active():
    """When a job is in progress, signal_handler POSTs /release and sets should_exit."""
    import palimpsest.worker as worker_mod

    worker_mod._current_job_id = 42
    worker_mod._current_worker_id = "test-node"
    worker_mod.should_exit = False

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("palimpsest.worker.httpx.post", return_value=mock_resp) as mock_post:
        worker_mod.signal_handler(signal.SIGTERM, None)

    assert mock_post.called
    url = mock_post.call_args[0][0]
    assert "/release" in url
    body = mock_post.call_args[1]["json"]
    assert body["job_id"] == 42
    assert body["worker_id"] == "test-node"
    assert worker_mod.should_exit is True

    worker_mod._current_job_id = None
    worker_mod._current_worker_id = None
    worker_mod.should_exit = False


def test_signal_handler_does_not_call_release_when_idle():
    """When idle (_current_job_id is None), signal_handler only sets should_exit."""
    import palimpsest.worker as worker_mod

    worker_mod._current_job_id = None
    worker_mod._current_worker_id = None
    worker_mod.should_exit = False

    with patch("palimpsest.worker.httpx.post") as mock_post:
        worker_mod.signal_handler(signal.SIGTERM, None)

    assert not mock_post.called
    assert worker_mod.should_exit is True
    worker_mod.should_exit = False


def test_signal_handler_sets_should_exit_even_if_release_fails():
    """If /release raises (broker offline), should_exit is still set."""
    import palimpsest.worker as worker_mod

    worker_mod._current_job_id = 99
    worker_mod._current_worker_id = "test-node"
    worker_mod.should_exit = False

    with patch("palimpsest.worker.httpx.post", side_effect=Exception("connection refused")):
        worker_mod.signal_handler(signal.SIGTERM, None)

    assert worker_mod.should_exit is True

    worker_mod._current_job_id = None
    worker_mod._current_worker_id = None
    worker_mod.should_exit = False
