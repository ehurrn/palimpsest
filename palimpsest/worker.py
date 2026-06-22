# palimpsest/worker.py
import argparse
import logging
import random
import signal
import sys
import threading
import time
from pathlib import Path

import httpx

from palimpsest.config import load
from palimpsest.tasks import HANDLERS, PermanentJobError

# Configure Logging
log_file = Path.home() / "palimpsest-worker.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file)],
)

cfg = load()
should_exit = False
broker_backoff = 5.0
_current_job_id: int | None = None
_current_worker_id: str | None = None
_job_lock = threading.Lock()
shutdown_event = threading.Event()


def signal_handler(signum, frame):
    global should_exit
    logging.info(f"Received signal {signum}. Exiting cleanly after current job...")
    should_exit = True
    shutdown_event.set()

    with _job_lock:
        job_id = _current_job_id
        worker_id = _current_worker_id

    if job_id is not None and worker_id is not None:
        broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
        try:
            httpx.post(
                f"{broker_url}/release",
                json={"worker_id": worker_id, "job_id": job_id},
                timeout=5.0,
            )
            logging.info(f"Released job {job_id} back to queue on shutdown.")
        except Exception as e:
            logging.warning(f"Could not release job {job_id} on shutdown: {e}")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def _post_with_retry(
    client: httpx.Client,
    url: str,
    json_body: dict,
    max_retries: int = 5,
) -> None:
    """POST json_body to url, retrying on network errors with exponential backoff.

    Only retries on httpx.RequestError (connection failures, timeouts, etc.).
    HTTP 4xx/5xx responses from the server are not retried — they indicate a
    logic problem (e.g. ownership mismatch) that retrying cannot fix.
    """
    for attempt in range(max_retries):
        try:
            client.post(url, json=json_body, timeout=10.0)
            return
        except httpx.RequestError as exc:
            if attempt == max_retries - 1:
                logging.error(f"Failed to POST {url} after {max_retries} attempts: {exc}")
            else:
                sleep_secs = 2**attempt
                logging.warning(
                    f"POST {url} attempt {attempt + 1} failed ({exc}); retrying in {sleep_secs}s..."
                )
                time.sleep(sleep_secs)


def warm_model(client: httpx.Client, model_name: str, embed: bool = False):
    """Ping ollama to keep a model resident, reusing the caller's connection pool.

    Args:
        client: Shared httpx.Client so repeated warmups reuse TCP connections
            instead of building and tearing down a pool on every call.
        model_name: Ollama model tag to warm.
        embed: Hit the embeddings endpoint instead of generate when True.
    """
    url = (
        "http://localhost:11434/api/embeddings" if embed else "http://localhost:11434/api/generate"
    )
    payload: dict[str, object] = {"model": model_name, "keep_alive": cfg.models["keep_alive"]}
    if embed:
        payload["input"] = "warmup"
    else:
        payload["prompt"] = ""
        payload["stream"] = False

    start_time = time.time()
    try:
        resp = client.post(url, json=payload, timeout=30.0)
        duration = time.time() - start_time
        logging.info(f"Warmed model {model_name} in {duration:.2f}s (status {resp.status_code})")
    except Exception as e:
        logging.warning(f"Could not warm model {model_name}: {e}")


def warm_all_models(client: httpx.Client, capabilities: list[str]):
    logging.info("Warming up models...")
    warmed = set()
    for cap in capabilities:
        if cap == "classify" and "classify" not in warmed:
            warm_model(client, cfg.models["classify"])
            warmed.add("classify")
        elif cap == "extract" and "extract" not in warmed:
            warm_model(client, cfg.models["extract"])
            warmed.add("extract")
        elif cap == "embed" and "embed" not in warmed:
            warm_model(client, cfg.embed["model"], embed=True)
            warmed.add("embed")


def heartbeat_loop(
    worker_id: str, job_id: int, stop_evt: threading.Event, lost_evt: threading.Event
):
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    interval = cfg.broker["heartbeat_seconds"]

    with httpx.Client() as client:
        while not stop_evt.wait(interval):
            try:
                resp = client.post(
                    f"{broker_url}/heartbeat",
                    json={"worker_id": worker_id, "job_ids": [job_id]},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    lost = resp.json().get("lost", [])
                    if job_id in lost:
                        logging.warning(f"Job {job_id} reported as lost by broker. Aborting.")
                        lost_evt.set()
                        break
            except Exception as e:
                logging.error(f"Heartbeat failed for job {job_id}: {e}")


def run_worker(node_name: str, once: bool = False):
    global should_exit, broker_backoff, _current_job_id, _current_worker_id

    capabilities = cfg.nodes.get(node_name)
    if capabilities is None:
        logging.critical(f"Unknown node: {node_name}")
        sys.exit(2)

    logging.info(f"Starting worker daemon for {node_name} with capabilities: {capabilities}")

    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"

    with httpx.Client() as client:
        warm_all_models(client, capabilities)
        last_warm_time = time.time()

        while not should_exit:
            now_time = time.time()
            if now_time - last_warm_time > 300:
                warm_all_models(client, capabilities)
                last_warm_time = now_time

            try:
                resp = client.post(
                    f"{broker_url}/lease",
                    json={"worker_id": node_name, "capabilities": capabilities, "max_jobs": 1},
                    timeout=10.0,
                )

                broker_backoff = 5.0

                if resp.status_code != 200:
                    logging.error(f"Lease request returned status {resp.status_code}")
                    if once:
                        break
                    shutdown_event.wait(timeout=10)
                    if shutdown_event.is_set():
                        break
                    continue

                jobs = resp.json().get("jobs", [])
                if not jobs:
                    if once:
                        break
                    shutdown_event.wait(timeout=10 + random.uniform(-1, 1))
                    if shutdown_event.is_set():
                        break
                    continue

                job = jobs[0]
                job_id = job["job_id"]
                job_type = job["type"]
                doc_id = job["doc_id"]

                logging.info(f"Leased job {job_id} ({job_type}) for doc {doc_id}")
                start_time = time.time()

                with _job_lock:
                    _current_job_id = job_id
                    _current_worker_id = node_name

                stop_evt = threading.Event()
                lost_evt = threading.Event()
                hb_thread = threading.Thread(
                    target=heartbeat_loop, args=(node_name, job_id, stop_evt, lost_evt), daemon=True
                )
                hb_thread.start()

                handler_func = HANDLERS.get(job_type)
                if not handler_func:
                    logging.error(f"No handler registered for job type: {job_type}")
                    _post_with_retry(
                        client,
                        f"{broker_url}/fail",
                        {
                            "worker_id": node_name,
                            "job_id": job_id,
                            "error": f"No handler registered for {job_type}",
                            "retryable": False,
                        },
                    )
                    with _job_lock:
                        _current_job_id = None
                        _current_worker_id = None
                    stop_evt.set()
                    hb_thread.join()
                    if once:
                        break
                    continue

                try:
                    result = handler_func(
                        cfg, job, lost_evt=lost_evt, shutdown_event=shutdown_event
                    )
                    duration = time.time() - start_time

                    if lost_evt.is_set():
                        logging.warning(f"Discarding result for job {job_id} since it was lost.")
                    else:
                        _post_with_retry(
                            client,
                            f"{broker_url}/complete",
                            {
                                "worker_id": node_name,
                                "job_id": job_id,
                                "result": result,
                            },
                        )
                        logging.info(
                            f"Completed job {job_id} ({job_type}) for doc {doc_id} in {duration:.2f}s"
                        )
                except PermanentJobError as e:
                    logging.error(f"Permanent handler error on job {job_id}: {e}")
                    _post_with_retry(
                        client,
                        f"{broker_url}/fail",
                        {
                            "worker_id": node_name,
                            "job_id": job_id,
                            "error": str(e),
                            "retryable": False,
                        },
                    )
                except Exception as e:
                    logging.error(f"Handler error on job {job_id}: {e}")
                    _post_with_retry(
                        client,
                        f"{broker_url}/fail",
                        {
                            "worker_id": node_name,
                            "job_id": job_id,
                            "error": str(e),
                            "retryable": True,
                        },
                    )

                with _job_lock:
                    _current_job_id = None
                    _current_worker_id = None
                stop_evt.set()
                hb_thread.join()

                if once:
                    break

            except Exception as e:
                logging.error(f"Broker connection error: {e}. Backing off for {broker_backoff}s...")
                if once:
                    break
                shutdown_event.wait(timeout=broker_backoff)
                broker_backoff = min(broker_backoff * 2.0, 60.0)
                if shutdown_event.is_set():
                    break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Palimpsest Worker Daemon")
    parser.add_argument(
        "--node", type=str, required=True, help="Name of the node (e.g. m4, gonktop)"
    )
    args = parser.parse_args()

    run_worker(args.node)
