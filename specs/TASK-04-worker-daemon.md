# TASK-04 — Worker Daemon (runs on M4 / M5)

**Read `specs/00-ARCHITECTURE.md` §1, §3 ([nodes], [models]), §9. Iron rule: this process NEVER opens the database or the storage volume. HTTP to the broker only.**

## Objective
`palimpsest/worker.py`: a long-lived daemon that leases jobs from the broker, dispatches them to task handlers (`palimpsest/tasks/*` — built in TASK-05/06/07), heartbeats, reports results, and keeps Ollama models warm.

## Depends on
TASK-01, TASK-02. Task handlers arrive later; the daemon ships with a registry so handlers plug in without modifying worker.py.

## Deliverables
```
palimpsest/worker.py
palimpsest/tasks/__init__.py     # handler registry
tests/test_worker.py
```
New dep: `httpx`.

## Spec

### Handler contract (tasks/__init__.py)
```python
HANDLERS: dict[str, Callable[[Config, dict], dict]] = {}

def handler(job_type: str):
    """Decorator: @handler('ocr') registers fn(cfg, job) -> result_dict."""
```
A handler receives the job dict (`job_id`, `doc_id`, `payload`) and returns the result dict the broker persists (per TASK-02 §Result handling). Handlers fetch the PDF themselves via `GET {broker}/file/{doc_id}.pdf` to a local tmp dir, and clean up after.

### Daemon loop
```
python -m palimpsest.worker --node m4
```
1. Resolve capabilities from `cfg.nodes[node]`. Unknown node ⇒ exit 2 with message.
2. Warm-up: for each Ollama model this node's capabilities imply (`classify`→models.classify, `extract`→models.extract, `embed`→embed.model), call `POST http://localhost:11434/api/generate` (or `/api/embeddings` for embed) with empty-ish prompt and `"keep_alive": cfg.models["keep_alive"]`. Log per-model warm-up time.
3. Loop: `POST /lease (max_jobs=1)` → if empty, sleep 10s (jittered), repeat.
4. While a job runs: background thread heartbeats every `cfg.broker["heartbeat_seconds"]`. If heartbeat reports the job in `lost`: kill/abandon local work, discard result.
5. Handler success ⇒ `POST /complete`; handler exception ⇒ `POST /fail` with `retryable=True` unless the exception is `PermanentJobError` (define in tasks/__init__.py).
6. Every 5 min (even when idle): re-ping models with `keep_alive` to hold them resident.
7. SIGTERM/SIGINT: finish or fail the current job, then exit cleanly.
8. Broker unreachable: log, backoff 5s→60s, keep trying forever (M5 docks/undocks; the daemon must survive broker restarts and network blips without human attention).

### Logging
One line per lifecycle event: leased/completed/failed with job_id, doc_id, type, duration. Local log file `~/palimpsest-worker.log` + stderr.

### launchd (deliver as files, do not install)
`deploy/com.palimpsest.worker.plist` — KeepAlive=true, RunAtLoad=true, runs `python -m palimpsest.worker --node <NODE>`; README comment explaining `launchctl load` per node.

## Acceptance (paste output)
With broker from TASK-02 running locally and a dummy handler registered in the test:
```
python -m palimpsest.worker --node m4   # logs warm-up attempt, polls, idles cleanly
```
tests/test_worker.py (mock broker with httpx MockTransport): leases respect capability list; heartbeat thread extends during a slow handler; handler exception → /fail retryable; PermanentJobError → /fail retryable=false; `lost` job abandoned (result never posted); broker-down → retry loop with backoff (mock clock); SIGTERM mid-job fails the job then exits.

## Out of scope
Actual OCR/feature/embed logic (TASK-05/06/07). Installing launchd jobs. Touching the DB.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
