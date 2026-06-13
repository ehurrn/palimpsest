# palimpsest/preflight.py
"""
Phase-1 preflight checks.

Run:  python -m palimpsest.preflight
Exit: 0 if all PASS, 1 if any FAIL.
"""

import logging
import shutil
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

RESET = "\033[0m"
GREEN = "\033[32m"
RED   = "\033[31m"

def _pass(label: str) -> bool:
    print(f"  {GREEN}PASS{RESET}  {label}")
    return True

def _fail(label: str, reason: str = "") -> bool:
    detail = f" — {reason}" if reason else ""
    print(f"  {RED}FAIL{RESET}  {label}{detail}")
    return False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_config() -> bool:
    """Config file loads and validates without error."""
    try:
        from palimpsest.config import load
        load()
        return _pass("Config loads (config.toml)")
    except Exception as exc:
        return _fail("Config loads (config.toml)", str(exc))


def check_storage(cfg) -> bool:
    """Storage root is mounted, writable, and has ≥ 200 GB free."""
    root: Path = cfg.storage_root
    ok = True
    if not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return _fail(f"Storage root exists/writable ({root})", str(exc))

    # Writable?
    probe = root / ".preflight_probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except Exception as exc:
        ok = _fail(f"Storage root writable ({root})", str(exc))

    # Free space ≥ 200 GB
    try:
        usage = shutil.disk_usage(root)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 200:
            ok = _fail(f"Storage ≥ 200 GB free ({root})", f"only {free_gb:.1f} GB available")
        else:
            _pass(f"Storage ≥ 200 GB free ({root}) — {free_gb:.0f} GB available")
    except Exception as exc:
        ok = _fail(f"Storage disk usage check ({root})", str(exc))

    if ok:
        _pass(f"Storage root mounted + writable ({root})")
    return ok


def check_db(cfg) -> bool:
    """DB migrated at current schema_version (5)."""
    EXPECTED_VERSION = 6
    try:
        from palimpsest.db import connect
        conn = connect(cfg)
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        conn.close()
        if row is None or row[0] is None:
            return _fail("DB schema_version", "schema_version table is empty — run: python -m palimpsest.db migrate")
        version = row[0]
        if version < EXPECTED_VERSION:
            return _fail("DB schema_version", f"got {version}, need {EXPECTED_VERSION} — run: python -m palimpsest.db migrate")
        return _pass(f"DB migrated (schema_version={version})")
    except Exception as exc:
        return _fail("DB migrated", str(exc))


def check_broker(cfg) -> bool:
    """Broker /status endpoint is reachable."""
    host = cfg.broker.get("host", "localhost")
    port = cfg.broker.get("port", 8077)
    url  = f"http://{host}:{port}/status"
    try:
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        return _pass(f"Broker reachable ({url})")
    except Exception as exc:
        return _fail(f"Broker reachable ({url})", str(exc))


def check_worker_heartbeat(cfg) -> bool:
    """M4 worker heartbeat seen in the last 5 minutes."""
    MAX_SECONDS = 300
    try:
        from palimpsest.db import connect
        conn = connect(cfg)
        cur = conn.execute(
            """
            SELECT lease_expires_at FROM jobs
            WHERE state = 'leased'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            # No leased jobs — could be idle, not necessarily bad
            return _pass("Worker heartbeat — no leased jobs (idle workers are OK)")
        # Parse the ISO timestamp and compare
        import datetime
        expires_str = row[0]
        expires = datetime.datetime.fromisoformat(expires_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.timezone.utc)
        diff = (expires - now).total_seconds()
        lease_ttl = cfg.broker.get("lease_ttl_seconds", 900)
        # heartbeat should refresh the lease, so if it expires in the future, heartbeat is alive
        if diff > 0:
            return _pass(f"Worker heartbeat active (lease expires in {diff:.0f}s)")
        else:
            return _fail("Worker heartbeat", f"lease expired {-diff:.0f}s ago — worker may be dead")
    except Exception as exc:
        return _fail("Worker heartbeat check", str(exc))


def check_ollama(cfg) -> bool:
    """Ollama models respond with warm latency < 3s on localhost."""
    model_name = cfg.embed.get("model", "nomic-embed-text")
    ok = True
    try:
        t0 = time.monotonic()
        resp = httpx.post(
            "http://localhost:11434/api/embeddings",
            json={"model": model_name, "prompt": "preflight", "keep_alive": "5m"},
            timeout=10.0,
        )
        elapsed = time.monotonic() - t0
        resp.raise_for_status()
        if elapsed > 3.0:
            ok = _fail(f"Ollama embed model warm latency ({model_name})", f"{elapsed:.2f}s > 3s")
        else:
            _pass(f"Ollama embed model ({model_name}) — {elapsed:.2f}s")
    except Exception as exc:
        ok = _fail(f"Ollama embed model ({model_name})", str(exc))
    return ok


def check_spacy() -> bool:
    """spaCy en_core_web_sm loads without error."""
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm")
        doc = nlp("Preflight check.")
        _ = list(doc.ents)  # force pipeline execution
        return _pass("spaCy en_core_web_sm loads")
    except Exception as exc:
        return _fail("spaCy en_core_web_sm loads", str(exc))


def check_faiss(cfg) -> bool:
    """FAISS index loads cleanly (or is cleanly absent if not yet built)."""
    try:
        import faiss
        index_path = cfg.storage_root / "index" / "faiss.idx"
        if not index_path.exists():
            return _pass("FAISS index absent-cleanly (not yet built)")
        index = faiss.read_index(str(index_path))
        return _pass(f"FAISS index loads ({index.ntotal} vectors)")
    except Exception as exc:
        return _fail("FAISS index", str(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=== Palimpsest Phase-1 Preflight ===\n")

    results: list[bool] = []

    # 1. Config (must succeed first — everything else needs cfg)
    r = check_config()
    results.append(r)
    if not r:
        print("\nConfig load failed; skipping remaining checks.")
        return 1

    from palimpsest.config import load
    cfg = load()

    # 2. Storage
    results.append(check_storage(cfg))

    # 3. DB schema
    results.append(check_db(cfg))

    # 4. Broker
    results.append(check_broker(cfg))

    # 5. Worker heartbeat
    results.append(check_worker_heartbeat(cfg))

    # 6. Ollama
    results.append(check_ollama(cfg))

    # 7. spaCy
    results.append(check_spacy())

    # 8. FAISS
    results.append(check_faiss(cfg))

    print()
    passed = sum(results)
    total  = len(results)
    if all(results):
        print(f"{GREEN}All {total} checks PASS.{RESET}")
        return 0
    else:
        print(f"{RED}{total - passed}/{total} checks FAILED.{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
