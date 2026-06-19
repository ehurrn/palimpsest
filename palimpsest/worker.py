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
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(log_file)
    ]
)

cfg = load()
should_exit = False
broker_backoff = 5.0

def signal_handler(signum, frame):
    global should_exit
    logging.info(f"Received signal {signum}. Exiting cleanly after current job...")
    should_exit = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def warm_model(model_name: str, embed: bool = False):
    url = "http://localhost:11434/api/embeddings" if embed else "http://localhost:11434/api/generate"
    payload = {
        "model": model_name,
        "keep_alive": cfg.models["keep_alive"]
    }
    if embed:
        payload["input"] = "warmup"
    else:
        payload["prompt"] = ""
        payload["stream"] = False
        
    start_time = time.time()
    try:
        # Use a short timeout so we don't hang if Ollama is not running during testing
        resp = httpx.post(url, json=payload, timeout=2.0)
        duration = time.time() - start_time
        logging.info(f"Warmed model {model_name} in {duration:.2f}s (status {resp.status_code})")
    except Exception as e:
        logging.warning(f"Could not warm model {model_name}: {e}")

def warm_all_models(capabilities: list[str]):
    logging.info("Warming up models...")
    warmed = set()
    for cap in capabilities:
        if cap == "classify" and "classify" not in warmed:
            warm_model(cfg.models["classify"])
            warmed.add("classify")
        elif cap == "extract" and "extract" not in warmed:
            warm_model(cfg.models["extract"])
            warmed.add("extract")
        elif cap == "embed" and "embed" not in warmed:
            warm_model(cfg.embed["model"], embed=True)
            warmed.add("embed")

def heartbeat_loop(worker_id: str, job_id: int, stop_evt: threading.Event, lost_evt: threading.Event):
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    client = httpx.Client()
    interval = cfg.broker["heartbeat_seconds"]
    
    while not stop_evt.wait(interval):
        try:
            resp = client.post(
                f"{broker_url}/heartbeat",
                json={"worker_id": worker_id, "job_ids": [job_id]},
                timeout=5.0
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
    global should_exit, broker_backoff
    
    capabilities = cfg.nodes.get(node_name)
    if capabilities is None:
        logging.critical(f"Unknown node: {node_name}")
        sys.exit(2)
        
    logging.info(f"Starting worker daemon for {node_name} with capabilities: {capabilities}")
    
    # Initial model warming
    warm_all_models(capabilities)
    last_warm_time = time.time()
    
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    client = httpx.Client()
    
    while not should_exit:
        # Re-ping models every 5 minutes
        now_time = time.time()
        if now_time - last_warm_time > 300:
            warm_all_models(capabilities)
            last_warm_time = now_time
            
        try:
            # Lease a job
            resp = client.post(
                f"{broker_url}/lease",
                json={
                    "worker_id": node_name,
                    "capabilities": capabilities,
                    "max_jobs": 1
                },
                timeout=10.0
            )
            
            # Reset backoff on success
            broker_backoff = 5.0
            
            if resp.status_code != 200:
                logging.error(f"Lease request returned status {resp.status_code}")
                if once:
                    break
                time.sleep(10)
                continue
                
            jobs = resp.json().get("jobs", [])
            if not jobs:
                if once:
                    break
                # Sleep jittered 10s
                time.sleep(10 + random.uniform(-1, 1))
                continue
                
            job = jobs[0]
            job_id = job["job_id"]
            job_type = job["type"]
            doc_id = job["doc_id"]
            
            logging.info(f"Leased job {job_id} ({job_type}) for doc {doc_id}")
            start_time = time.time()
            
            # Setup heartbeat events and thread
            stop_evt = threading.Event()
            lost_evt = threading.Event()
            hb_thread = threading.Thread(
                target=heartbeat_loop,
                args=(node_name, job_id, stop_evt, lost_evt),
                daemon=True
            )
            hb_thread.start()
            
            # Find and execute handler
            handler_func = HANDLERS.get(job_type)
            if not handler_func:
                logging.error(f"No handler registered for job type: {job_type}")
                client.post(
                    f"{broker_url}/fail",
                    json={
                        "worker_id": node_name,
                        "job_id": job_id,
                        "error": f"No handler registered for {job_type}",
                        "retryable": False
                    },
                    timeout=10.0,
                )
                stop_evt.set()
                hb_thread.join()
                if once:
                    break
                continue
                
            try:
                # Execute handler
                result = handler_func(cfg, job)
                duration = time.time() - start_time
                
                # Check if job was lost during execution
                if lost_evt.is_set():
                    logging.warning(f"Discarding result for job {job_id} since it was lost.")
                else:
                    client.post(
                        f"{broker_url}/complete",
                        json={
                            "worker_id": node_name,
                            "job_id": job_id,
                            "result": result
                        },
                        timeout=10.0,
                    )
                    logging.info(f"Completed job {job_id} ({job_type}) for doc {doc_id} in {duration:.2f}s")
            except PermanentJobError as e:
                logging.error(f"Permanent handler error on job {job_id}: {e}")
                client.post(
                    f"{broker_url}/fail",
                    json={
                        "worker_id": node_name,
                        "job_id": job_id,
                        "error": str(e),
                        "retryable": False
                    },
                    timeout=10.0,
                )
            except Exception as e:
                logging.error(f"Handler error on job {job_id}: {e}")
                client.post(
                    f"{broker_url}/fail",
                    json={
                        "worker_id": node_name,
                        "job_id": job_id,
                        "error": str(e),
                        "retryable": True
                    },
                    timeout=10.0,
                )
                
            stop_evt.set()
            hb_thread.join()
            
            if once:
                break
                
        except Exception as e:
            logging.error(f"Broker connection error: {e}. Backing off for {broker_backoff}s...")
            if once:
                break
            time.sleep(broker_backoff)
            broker_backoff = min(broker_backoff * 2.0, 60.0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Palimpsest Worker Daemon")
    parser.add_argument("--node", type=str, required=True, help="Name of the node (e.g. m4, gonktop)")
    args = parser.parse_args()
    
    run_worker(args.node)
