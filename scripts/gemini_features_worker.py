"""Gemini-powered features extraction worker for palimpsest.

Each thread leases BATCH_SIZE jobs, packs all their OCR text into one big
Gemini prompt, gets a single JSON response keyed by doc_id, then completes
every job in the batch. This maximises context utilisation while keeping
concurrent subprocess count low enough not to freeze the machine.

GEMINI_API_KEY must be set in the environment (or in ~/.zprofile).

Usage:
    uv run python scripts/gemini_features_worker.py [--concurrency 4] [--batch-size 15]
"""

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from palimpsest.config import load as load_config  # noqa: E402

AGY_BIN = "/Users/herren/.local/bin/agy"
MODEL = "gemini-3.1-flash-lite"
WORKER_ID = "gemini-features"

MAX_CHARS_PER_PAGE = 4000  # per-page cap; keeps very long pages from dominating
MAX_PROMPT_CHARS = 120_000  # hard cap per Gemini call; oversized batches are split
# Max pages to include for a single doc — prevents a 200-page doc from filling the context alone
_MAX_PAGES_PER_DOC = (MAX_PROMPT_CHARS - 2000) // (MAX_CHARS_PER_PAGE + 30)  # ~28 pages

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a named-entity extractor for declassified US nuclear-test documents
from the OSTI OpenNet archive (NV-series, 1945-1995).

You will receive multiple documents separated by markers. For EACH document,
extract all named entities and detected redactions.

Return ONLY a valid JSON object — no markdown, no commentary:

{{
  "documents": [
    {{
      "doc_id": "<doc_id>",
      "entities": [
        {{
          "page_no": <int>,
          "kind": "<kind>",
          "text": "<exact text>",
          "norm": "<normalized>",
          "char_start": null,
          "char_end": null,
          "bbox": [null, null, null, null]
        }}
      ],
      "redactions": [
        {{
          "page_no": <int>,
          "kind": "text",
          "bbox": [null, null, null, null],
          "label": "<matched pattern>",
          "context_before": "<≤100 chars>",
          "context_after": "<≤100 chars>"
        }}
      ]
    }}
  ]
}}

Entity kinds and norms:
- person      : human names. norm = lowercase "first last"; strip titles; flip "LAST, FIRST".
- date        : dates/ranges. norm = YYYY-MM-DD, YYYY-MM, or YYYY.
- dosage      : radiation doses. norm = "<number> <unit>" lc. Units: r,rad,rem,mrem,roentgen,uCi,mCi,curies.
- protocol_code: CAL-123, CHI-45, HP-6. norm = uppercase PREFIX-NUMBER.
- location    : places, test sites. norm = lowercase.
- org         : organizations, agencies, labs. norm = lowercase.
- reg_cite    : CFR/USC citations, Common Rule, Belmont Report, Declaration of Helsinki, Nuremberg Code.
               norm = "45 CFR 46" canonical form.
- seq_ref     : NV1234567, NV-123, Report No. 456. norm = uppercase.
- subject_ref : subject codes/IDs (not names). norm = lowercase.
- outcome_ref : outcomes, mortality, survival, pending reports.
               norm = "outcome_ind:<text>" or "future_ref:<text>".

Redactions: flag [DELETED], [REDACTED], DELETED, SANITIZED patterns with ≤100 chars context each side.
Extract only what is present. Do not invent entities.
"""

_DOC_SEPARATOR = "=== DOCUMENT doc_id={doc_id} ==="
_DOC_END = "=== END DOCUMENT ==="


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def call_gemini(prompt: str) -> str:
    r = subprocess.run(
        [AGY_BIN, "-p", prompt, "--model", MODEL],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"agy exit {r.returncode}: {r.stderr[:400]}")
    return r.stdout.strip()


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in response: {text[:300]}")
    return json.loads(text[start : end + 1])


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(t: str) -> str:
    """Strip control characters that cause Gemini CLI (Node.js) encoding crashes."""
    return _CONTROL_CHARS_RE.sub(" ", t)


def page_text(page: dict) -> str:
    t = sanitize_text(page.get("text", ""))
    if len(t) > MAX_CHARS_PER_PAGE:
        t = t[:MAX_CHARS_PER_PAGE] + "\n[...truncated...]"
    return t


def build_batch_prompt(batch: list[tuple[str, list[dict]]]) -> str:
    """Build one prompt containing all docs in the batch."""
    parts = [_SYSTEM, ""]
    for doc_id, pages in batch:
        parts.append(_DOC_SEPARATOR.format(doc_id=doc_id))
        for pg in pages:
            parts.append(f"--- page {pg.get('page_no', '?')} ---")
            parts.append(page_text(pg))
        parts.append(_DOC_END)
        parts.append("")
    return "\n".join(parts)


def split_by_prompt_size(
    batch: list[tuple[str, list[dict]]],
) -> list[list[tuple[str, list[dict]]]]:
    """Split a batch into sub-batches each under MAX_PROMPT_CHARS.

    Oversized single documents (more pages than fit in MAX_PROMPT_CHARS) are
    truncated to _MAX_PAGES_PER_DOC so the Gemini call never exceeds the
    character budget regardless of how many pages the doc has.
    """
    system_len = len(_SYSTEM)
    chunks: list[list[tuple[str, list[dict]]]] = []
    current: list[tuple[str, list[dict]]] = []
    current_len = system_len

    for doc_id, pages in batch:
        # Truncate very long docs so a single doc can never blow the prompt budget.
        if len(pages) > _MAX_PAGES_PER_DOC:
            pages = pages[:_MAX_PAGES_PER_DOC]

        doc_chars = sum(min(len(p.get("text", "")), MAX_CHARS_PER_PAGE) for p in pages)
        doc_chars += len(doc_id) + 60  # separators

        if current and current_len + doc_chars > MAX_PROMPT_CHARS:
            chunks.append(current)
            current = []
            current_len = system_len

        current.append((doc_id, pages))
        current_len += doc_chars

    if current:
        chunks.append(current)

    return chunks or [[]]


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


def process_batch(
    http: httpx.Client,
    broker_url: str,
    jobs: list[dict],
    dry_run: bool,
    tag: str,
) -> tuple[int, int]:
    """Fetch OCR for all jobs, call Gemini once, complete all. Returns (ok, failed)."""
    # Fetch OCR for every job in the batch
    batch: list[tuple[str, list[dict]]] = []
    job_map: dict[str, dict] = {}  # doc_id → job
    for job in jobs:
        doc_id = job["doc_id"]
        job_map[doc_id] = job
        try:
            r = http.get(f"{broker_url}/ocr/{doc_id}.json", timeout=30)
        except httpx.RequestError as exc:
            print(f"{tag}OCR fetch error {doc_id}: {exc}")
            _fail_job(http, broker_url, job)
            continue
        if r.status_code != 200:
            print(f"{tag}OCR missing {doc_id}: HTTP {r.status_code}")
            _fail_job(http, broker_url, job)
            continue
        pages = r.json()
        if isinstance(pages, list):
            batch.append((doc_id, pages))

    if not batch:
        return 0, len(jobs)

    total_pages = sum(len(p) for _, p in batch)
    print(f"{tag}{len(batch)} docs, {total_pages} pages total")

    if dry_run:
        for doc_id, pages in batch:
            print(f"{tag}  DRY RUN [{doc_id}] {len(pages)}p")
        return len(batch), 0

    # Split into sub-batches that each fit within MAX_PROMPT_CHARS
    sub_batches = split_by_prompt_size(batch)

    results: dict[str, dict] = {}
    for sub in sub_batches:
        prompt = build_batch_prompt(sub)
        print(f"{tag}Gemini call — {len(sub)} docs, {len(prompt):,} chars")
        try:
            raw = call_gemini(prompt)
            data = extract_json(raw)
        except Exception as exc:
            print(f"{tag}Gemini error: {exc}")
            for doc_id, _ in sub:
                _fail_job(http, broker_url, job_map[doc_id])
            continue

        for entry in data.get("documents", []):
            did = str(entry.get("doc_id", ""))
            if did:
                results[did] = entry

    ok = failed = 0
    for doc_id, _ in batch:
        job = job_map[doc_id]
        result = results.get(doc_id, {"entities": [], "redactions": []})
        ents = len(result.get("entities", []))
        reds = len(result.get("redactions", []))
        try:
            resp = http.post(
                f"{broker_url}/complete",
                json={
                    "job_id": job["job_id"],
                    "worker_id": WORKER_ID,
                    "result": {
                        "entities": result.get("entities", []),
                        "redactions": result.get("redactions", []),
                    },
                },
                timeout=30,
            )
            if resp.status_code == 200:
                print(f"{tag}  ✓ [{doc_id}] {ents}e {reds}r")
                ok += 1
            else:
                print(f"{tag}  ✗ [{doc_id}] complete {resp.status_code}")
                failed += 1
        except httpx.RequestError as exc:
            print(f"{tag}  ✗ [{doc_id}] complete error: {exc}")
            failed += 1

    return ok, failed


def _fail_job(http: httpx.Client, broker_url: str, job: dict) -> None:
    try:
        http.post(
            f"{broker_url}/fail",
            json={
                "job_id": job["job_id"],
                "worker_id": WORKER_ID,
                "error": "gemini extraction failed",
                "retryable": True,
            },
            timeout=10,
        )
    except httpx.RequestError:
        pass


# ---------------------------------------------------------------------------
# Thread loop
# ---------------------------------------------------------------------------


def thread_loop(
    thread_id: int,
    broker_url: str,
    batch_size: int,
    dry_run: bool,
    loop: bool,
) -> int:
    http = httpx.Client(timeout=60.0)
    tag = f"[t{thread_id:02d}] "
    total_ok = 0

    while True:
        try:
            r = http.post(
                f"{broker_url}/lease",
                json={"worker_id": WORKER_ID, "capabilities": ["features"], "max_jobs": batch_size},
                timeout=10,
            )
        except httpx.ConnectError as exc:
            print(f"{tag}broker unreachable: {exc} — 15s")
            if not loop:
                return total_ok
            time.sleep(15)
            continue
        except httpx.RequestError as exc:
            print(f"{tag}lease error: {exc} — 5s")
            if not loop:
                return total_ok
            time.sleep(5)
            continue

        if r.status_code != 200:
            print(f"{tag}lease HTTP {r.status_code}")
            if not loop:
                return total_ok
            time.sleep(5)
            continue

        jobs = r.json().get("jobs", [])
        if not jobs:
            if not loop:
                return total_ok
            time.sleep(10)
            continue

        ok, _ = process_batch(http, broker_url, jobs, dry_run, tag)
        total_ok += ok

    return total_ok  # unreachable in loop mode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--concurrency", type=int, default=4, help="Parallel Gemini subprocesses (default 4)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=15, help="Docs per Gemini call (default 15)"
    )
    parser.add_argument("--loop", action="store_true", help="Keep polling when queue is empty")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"

    print(
        f"Gemini features worker — broker {broker_url} | model {MODEL} | "
        f"concurrency {args.concurrency} | batch {args.batch_size} docs/call"
    )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                thread_loop, i, broker_url, args.batch_size, args.dry_run, args.loop
            ): i
            for i in range(args.concurrency)
        }
        total = 0
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                n = fut.result()
                total += n
                print(f"Thread {tid:02d} done — {n} completed")
            except Exception as exc:
                print(f"Thread {tid:02d} crashed: {exc}")

    print(f"All threads finished. Total completed: {total}")


if __name__ == "__main__":
    main()
