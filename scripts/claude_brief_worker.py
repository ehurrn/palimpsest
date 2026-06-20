"""Claude-powered brief generation worker for palimpsest.

Leases brief jobs from the broker, fetches OCR via HTTP, and calls
Claude Sonnet 4.6 via agy to generate structured per-document abstracts.

Claude's 200k context means single-pass for virtually all docs.
Map-reduce only for true behemoths (>800k chars).

Usage:
    uv run python scripts/claude_brief_worker.py [--concurrency 2]
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
MODEL = "Claude Sonnet 4.6 (Thinking)"
WORKER_ID = "claude-brief"

# Claude 200k context — use large windows
MAX_CHARS_PER_CALL = 600_000   # ~150k tokens, leaves room for prompt + output
REDUCE_THRESHOLD = MAX_CHARS_PER_CALL

_SYSTEM = """\
You are an investigative analyst reviewing declassified U.S. government documents \
from the OSTI OpenNet nuclear archive (NV-series, 1945-1995).

Extract a structured brief from the supplied document pages.

Rules:
- Ground every claim and event in specific page text; stamp each with the page_no.
- Never invent details not present in the supplied text.
- For redactions: infer only the *category* of what is hidden \
(person_name | dose | location | date | protocol | other) from surrounding context. \
Do NOT guess a specific name or value.
- confidence is always 0.0 — human review assigns confidence, not the model.
- Return ONLY valid JSON. No prose, no markdown fences, no commentary.

Schema:
{
  "doc_type": "<field_report|memo|correspondence|protocol|summary|other>",
  "summary": "<= 4 sentences: what this document is and why it might matter>",
  "claims": [{"text": "...", "page_no": <int>, "confidence": 0.0}],
  "events": [{"actor": "...", "action": "...", "object": "...", "subject_ref": "...", "date": "...", "place": "...", "page_no": <int>}],
  "redaction_hypotheses": [{"page_no": <int>, "label": "...", "likely_hidden": "<person_name|dose|location|date|protocol|other>", "rationale": "...", "confidence": 0.0}],
  "flags": ["<human_subjects|consent_language_absent|dose_data|outcome_data|no_follow_up|protocol_code|other>"]
}

Omit empty arrays. Maximum 25 claims and 25 events."""

_REDUCE_SYSTEM = """\
You are merging partial briefs of the same document into one consolidated brief.

Rules:
- Deduplicate claims and events across slices (same fact, keep once).
- Merge flag lists (union).
- Write a fresh summary covering the whole document.
- confidence is always 0.0.
- Return ONLY valid JSON matching the same schema. No prose, no markdown fences."""


def call_claude(prompt: str, system: str) -> str:
    full_prompt = f"<system>\n{system}\n</system>\n\n{prompt}"
    r = subprocess.run(
        [AGY_BIN, "-p", full_prompt, "--model", MODEL],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(f"agy exit {r.returncode}: {r.stderr[:400]}")
    out = r.stdout.strip()
    if not out:
        raise RuntimeError("agy returned empty — quota likely exhausted")
    return out


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in response: {text[:300]}")
    return json.loads(text[start : end + 1])


def pages_to_block(pages: list[dict]) -> str:
    parts = []
    for p in pages:
        text = p.get("text", "").strip()
        if text:
            parts.append(f"[PAGE {p['page_no']}]\n{text}")
    return "\n\n".join(parts)


def brief_single(doc_id: str, pages: list[dict]) -> dict:
    block = pages_to_block(pages)
    prompt = f"Document ID: {doc_id}\n\n{block}"
    raw = call_claude(prompt, _SYSTEM)
    return extract_json(raw)


def brief_mapreduce(doc_id: str, pages: list[dict], tag: str) -> dict:
    # Chunk pages into slices that fit the context
    slices, current, current_chars = [], [], 0
    for p in pages:
        chars = len(p.get("text", ""))
        if current and current_chars + chars > MAX_CHARS_PER_CALL:
            slices.append(current)
            current, current_chars = [], 0
        current.append(p)
        current_chars += chars
    if current:
        slices.append(current)

    print(f"{tag}map-reduce: {len(slices)} slices")
    slice_briefs = []
    for i, sl in enumerate(slices):
        print(f"{tag}  map slice {i+1}/{len(slices)}")
        slice_briefs.append(brief_single(doc_id, sl))

    print(f"{tag}  reduce pass")
    prompt = f"Document ID: {doc_id}\n\nPartial briefs:\n{json.dumps(slice_briefs, indent=2)}"
    raw = call_claude(prompt, _REDUCE_SYSTEM)
    return extract_json(raw)


def normalise(result: dict, doc_id: str, pages: list[dict]) -> dict:
    first_page = pages[0]["page_no"] if pages else 1
    for claim in result.get("claims", []):
        claim.setdefault("page_no", first_page)
        claim["confidence"] = 0.0
    for event in result.get("events", []):
        event.setdefault("page_no", first_page)
    for rh in result.get("redaction_hypotheses", []):
        rh.setdefault("page_no", first_page)
        rh["confidence"] = 0.0
    return {
        "doc_id": doc_id,
        "model": MODEL,
        "schema": 1,
        "doc_type": result.get("doc_type", "other"),
        "summary": result.get("summary", ""),
        "claims": result.get("claims", []),
        "events": result.get("events", []),
        "redaction_hypotheses": result.get("redaction_hypotheses", []),
        "flags": result.get("flags", []),
    }


def _fail_job(http: httpx.Client, broker_url: str, job: dict, error: str, retryable: bool = True) -> None:
    try:
        http.post(
            f"{broker_url}/fail",
            json={"job_id": job["job_id"], "worker_id": WORKER_ID, "error": error, "retryable": retryable},
            timeout=10,
        )
    except httpx.RequestError:
        pass


def thread_loop(thread_id: int, broker_url: str, loop: bool) -> int:
    http = httpx.Client(timeout=60.0)
    tag = f"[claude-brief t{thread_id:02d}] "
    total_ok = 0

    while True:
        try:
            r = http.post(
                f"{broker_url}/lease",
                json={"worker_id": WORKER_ID, "capabilities": ["brief"], "max_jobs": 1},
                timeout=10,
            )
        except httpx.RequestError as exc:
            print(f"{tag}broker error: {exc} — 15s")
            if not loop:
                return total_ok
            time.sleep(15)
            continue

        if r.status_code != 200:
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

        job = jobs[0]
        doc_id = job["doc_id"]
        print(f"{tag}leased brief job for doc {doc_id}")

        # Fetch OCR
        try:
            ocr_r = http.get(f"{broker_url}/ocr/{doc_id}.json", timeout=30)
        except httpx.RequestError as exc:
            _fail_job(http, broker_url, job, f"OCR fetch error: {exc}")
            continue
        if ocr_r.status_code == 404:
            _fail_job(http, broker_url, job, "OCR not found", retryable=False)
            continue
        if ocr_r.status_code != 200:
            _fail_job(http, broker_url, job, f"OCR HTTP {ocr_r.status_code}")
            continue

        pages = [p for p in ocr_r.json() if p.get("text", "").strip()]
        if not pages:
            # Complete with empty brief — no text to analyse
            http.post(f"{broker_url}/complete", json={
                "job_id": job["job_id"], "worker_id": WORKER_ID,
                "result": {"doc_id": doc_id, "model": MODEL, "schema": 1,
                           "doc_type": "other", "summary": "No OCR text.", "claims": [],
                           "events": [], "redaction_hypotheses": [], "flags": []},
            }, timeout=30)
            continue

        total_chars = sum(len(p.get("text", "")) for p in pages)
        print(f"{tag}doc {doc_id}: {len(pages)} pages, {total_chars:,} chars")

        try:
            if total_chars <= REDUCE_THRESHOLD:
                result = brief_single(doc_id, pages)
            else:
                result = brief_mapreduce(doc_id, pages, tag)
            output = normalise(result, doc_id, pages)
        except RuntimeError as exc:
            print(f"{tag}Claude error: {exc}")
            _fail_job(http, broker_url, job, str(exc), retryable=True)
            continue
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"{tag}JSON parse error: {exc}")
            _fail_job(http, broker_url, job, str(exc), retryable=False)
            continue

        try:
            resp = http.post(f"{broker_url}/complete", json={
                "job_id": job["job_id"], "worker_id": WORKER_ID, "result": output,
            }, timeout=30)
            if resp.status_code == 200:
                print(f"{tag}✓ {doc_id} — {len(output['claims'])}c {len(output['events'])}e {len(output['redaction_hypotheses'])}r")
                total_ok += 1
            else:
                print(f"{tag}complete {resp.status_code}")
        except httpx.RequestError as exc:
            print(f"{tag}complete error: {exc}")

    return total_ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    print(f"Claude brief worker — broker {broker_url} | model {MODEL} | concurrency {args.concurrency}")

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(thread_loop, i, broker_url, args.loop): i
            for i in range(args.concurrency)
        }
        total = 0
        for fut in as_completed(futures):
            try:
                total += fut.result()
            except Exception as exc:
                print(f"Thread crashed: {exc}")

    print(f"Total completed: {total}")


if __name__ == "__main__":
    main()
