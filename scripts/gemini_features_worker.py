"""Gemini-powered features extraction worker for palimpsest.

Runs N concurrent threads, each independently leasing a features job,
calling Gemini CLI for NER, and completing the job via the broker.

GEMINI_API_KEY must be set in the environment (or in ~/.zprofile).

Usage:
    uv run python scripts/gemini_features_worker.py [--concurrency 20] [--dry-run]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from palimpsest.config import load as load_config  # noqa: E402

GEMINI_BIN = "/opt/homebrew/bin/gemini"
MODEL = "gemini-3.1-flash-lite-preview"  # 4M ctx, 4M TPM — ideal for bulk NER
WORKER_ID = "gemini-features"

MAX_CHARS_PER_PAGE = 6000

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are a named-entity extractor for declassified US nuclear-test documents
from the OSTI OpenNet archive (NV-series, 1945-1995).

Extract every entity that appears in the OCR text below.
Return ONLY a valid JSON object — no markdown fences, no commentary, nothing else.

Required structure:
{{
  "entities": [
    {{
      "page_no": <integer>,
      "kind": "<kind>",
      "text": "<exact text as found in document>",
      "norm": "<normalized form>",
      "char_start": null,
      "char_end": null,
      "bbox": [null, null, null, null]
    }}
  ],
  "redactions": [
    {{
      "page_no": <integer>,
      "kind": "text",
      "bbox": [null, null, null, null],
      "label": "<matched pattern>",
      "context_before": "<up to 100 chars before>",
      "context_after": "<up to 100 chars after>"
    }}
  ]
}}

Entity kinds and normalization rules:
- person     : human names only (not orgs, not single-letter abbreviations).
               norm = lowercase "firstname lastname"; strip titles (Dr., Mr.,
               Col., Gen., Capt., Prof.). "LAST, FIRST" → "first last".
- date       : dates and date ranges.
               norm = YYYY-MM-DD, YYYY-MM, or YYYY.
- dosage     : radiation doses.
               norm = "<number> <unit>" lowercase.
               Units: r, rad, rem, mrem, roentgen, uCi, mCi, curies.
- protocol_code : codes like CAL-123, CHI-45, HP-6.
               norm = uppercase "PREFIX-NUMBER".
- location   : geographic places, test sites, cities, states.
               norm = lowercase.
- org        : organizations, agencies, labs, hospitals, universities.
               norm = lowercase.
- reg_cite   : regulatory citations.
               Examples: "45 CFR 46", "45 USC 1234", "Common Rule",
               "Belmont Report", "Declaration of Helsinki", "Nuremberg Code".
               norm = "45 CFR 46" canonical form.
- seq_ref    : document sequence IDs like NV1234567, NV-123, "Report No. 456".
               norm = uppercase.
- subject_ref: experimental subjects referenced by code or ID (not names).
               norm = lowercase.
- outcome_ref: study outcomes, mortality data, survival rates, pending reports.
               norm = "outcome_ind:<text>" or "future_ref:<text>" (future_ref
               if the outcome is pending/planned).

For redactions: detect text patterns like [DELETED], [REDACTED], DELETED,
SANITIZED in the OCR text and list them as redaction entries with surrounding
context (up to 100 chars before and after).

Do not invent entities. Only extract what is present in the text.

--- DOCUMENT TEXT (doc_id={doc_id}) ---

{pages_text}
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_gemini_env() -> dict[str, str]:
    """Return os.environ with GEMINI_API_KEY loaded from ~/.zprofile if absent."""
    env = os.environ.copy()
    if not env.get("GEMINI_API_KEY"):
        zprofile = Path.home() / ".zprofile"
        if zprofile.exists():
            result = subprocess.run(
                f"source {zprofile} && printf '%s' \"$GEMINI_API_KEY\"",
                shell=True,
                capture_output=True,
                text=True,
                executable="/bin/zsh",
            )
            key = result.stdout.strip()
            if key:
                env["GEMINI_API_KEY"] = key
    return env


def call_gemini(prompt: str, env: dict[str, str]) -> str:
    """Invoke the Gemini CLI and return stdout (the model response)."""
    result = subprocess.run(
        [GEMINI_BIN, "-p", prompt, "-m", MODEL],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    if result.returncode != 0:
        snippet = result.stderr[:400] if result.stderr else "(no stderr)"
        raise RuntimeError(f"gemini exited {result.returncode}: {snippet}")
    return result.stdout.strip()


def extract_json(text: str) -> dict:
    """Parse JSON from model output, tolerating optional markdown fences."""
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in response: {text[:300]}")
    return json.loads(text[start : end + 1])


def build_pages_text(pages: list[dict]) -> str:
    parts = []
    for page in pages:
        page_no = page.get("page_no", "?")
        text = page.get("text", "")
        if len(text) > MAX_CHARS_PER_PAGE:
            text = text[:MAX_CHARS_PER_PAGE] + f"\n[...page {page_no} truncated...]"
        parts.append(f"=== PAGE {page_no} ===\n{text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Per-job processing (runs in a thread)
# ---------------------------------------------------------------------------


def process_one(
    http: httpx.Client,
    broker_url: str,
    job: dict,
    env: dict[str, str],
    dry_run: bool,
    tag: str,
) -> bool:
    """Fetch OCR, run Gemini, complete job. Returns True on success."""
    job_id = job["job_id"]
    doc_id = job["doc_id"]

    try:
        ocr_resp = http.get(f"{broker_url}/ocr/{doc_id}.json", timeout=30)
    except httpx.RequestError as exc:
        print(f"{tag}[{doc_id}] OCR fetch error: {exc}")
        return False

    if ocr_resp.status_code != 200:
        print(f"{tag}[{doc_id}] OCR not found: HTTP {ocr_resp.status_code}")
        return False

    pages = ocr_resp.json()
    if not isinstance(pages, list):
        print(f"{tag}[{doc_id}] OCR JSON is not a list")
        return False

    if dry_run:
        print(f"{tag}[{doc_id}] DRY RUN — {len(pages)} pages, job {job_id}")
        return True

    all_entities: list[dict] = []
    all_redactions: list[dict] = []

    for chunk_start in range(0, len(pages), 60):
        chunk = pages[chunk_start : chunk_start + 60]
        prompt = _PROMPT_TEMPLATE.format(
            doc_id=doc_id, pages_text=build_pages_text(chunk)
        )
        try:
            raw = call_gemini(prompt, env)
            data = extract_json(raw)
        except Exception as exc:
            print(f"{tag}[{doc_id}] Gemini error: {exc}")
            return False

        all_entities.extend(data.get("entities", []))
        all_redactions.extend(data.get("redactions", []))

    print(
        f"{tag}[{doc_id}] {len(pages)}p → "
        f"{len(all_entities)} entities, {len(all_redactions)} redactions"
    )

    try:
        resp = http.post(
            f"{broker_url}/complete",
            json={
                "job_id": job_id,
                "worker_id": WORKER_ID,
                "result": {"entities": all_entities, "redactions": all_redactions},
            },
            timeout=30,
        )
    except httpx.RequestError as exc:
        print(f"{tag}[{doc_id}] complete request error: {exc}")
        return False

    if resp.status_code == 200:
        return True
    print(f"{tag}[{doc_id}] complete failed: {resp.status_code} {resp.text[:120]}")
    return False


# ---------------------------------------------------------------------------
# Thread worker loop
# ---------------------------------------------------------------------------


def thread_loop(
    thread_id: int,
    broker_url: str,
    env: dict[str, str],
    dry_run: bool,
    loop: bool,
) -> int:
    """Poll → lease → process → complete, indefinitely. Returns jobs completed."""
    http = httpx.Client(timeout=60.0)
    tag = f"[t{thread_id:02d}] "
    completed = 0

    while True:
        try:
            lease_resp = http.post(
                f"{broker_url}/lease",
                json={"worker_id": WORKER_ID, "capabilities": ["features"], "max_jobs": 1},
                timeout=10,
            )
        except httpx.ConnectError as exc:
            print(f"{tag}broker unreachable: {exc} — sleeping 15s")
            if not loop:
                return completed
            time.sleep(15)
            continue
        except httpx.RequestError as exc:
            print(f"{tag}lease error: {exc} — sleeping 5s")
            if not loop:
                return completed
            time.sleep(5)
            continue

        if lease_resp.status_code != 200:
            print(f"{tag}lease HTTP {lease_resp.status_code} — sleeping 5s")
            if not loop:
                return completed
            time.sleep(5)
            continue

        jobs = lease_resp.json().get("jobs", [])
        if not jobs:
            if not loop:
                return completed
            time.sleep(5)
            continue

        for job in jobs:
            ok = process_one(http, broker_url, job, env, dry_run, tag)
            if not ok and not dry_run:
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
            elif ok:
                completed += 1

    return completed  # unreachable in loop mode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini features extraction worker")
    parser.add_argument(
        "--concurrency", type=int, default=20,
        help="Parallel Gemini calls (default 20; stay under rate limit)",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Threads re-poll when queue is empty instead of exiting",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Lease and inspect jobs without calling Gemini",
    )
    args = parser.parse_args()

    cfg = load_config()
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    env = get_gemini_env()

    if not env.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found. Add it to ~/.zprofile.", file=sys.stderr)
        sys.exit(1)

    print(
        f"Gemini features worker — broker {broker_url}, model {MODEL}, "
        f"concurrency {args.concurrency}"
    )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(thread_loop, i, broker_url, env, args.dry_run, args.loop): i
            for i in range(args.concurrency)
        }
        total = 0
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                n = fut.result()
                total += n
                print(f"Thread {tid:02d} exited — {n} jobs completed")
            except Exception as exc:
                print(f"Thread {tid:02d} crashed: {exc}")

    print(f"Done. Total jobs completed: {total}")


if __name__ == "__main__":
    main()
