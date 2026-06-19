"""Gemini-powered features extraction worker for palimpsest.

Leases 'features' jobs from the broker, fetches the OCR JSON, sends the full
document text to Gemini CLI for entity/redaction extraction, and completes the
job via broker.

GEMINI_API_KEY must be set in the environment (or in ~/.zprofile).

Usage:
    uv run python scripts/gemini_features_worker.py [--loop] [--dry-run] [--max-jobs N]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))
from palimpsest.config import load as load_config  # noqa: E402

GEMINI_BIN = "/opt/homebrew/bin/gemini"
# 4 M context + 4 M TPM — ideal for bulk NER over long documents
MODEL = "gemini-3.1-flash-lite-preview"
WORKER_ID = "gemini-features"

MAX_CHARS_PER_PAGE = 6000   # truncate runaway pages; keeps prompt sane
MAX_PAGES_PER_CALL = 60     # pages per Gemini call; at 4M context this is plenty

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
    """Invoke the Gemini CLI and return stdout (the model's response)."""
    result = subprocess.run(
        [GEMINI_BIN, "-p", prompt, "-m", MODEL],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    if result.returncode != 0:
        stderr_snippet = result.stderr[:400] if result.stderr else "(no stderr)"
        raise RuntimeError(f"gemini exited {result.returncode}: {stderr_snippet}")
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
    """Format OCR pages into a readable block for the prompt."""
    parts = []
    for page in pages:
        page_no = page.get("page_no", "?")
        text = page.get("text", "")
        if len(text) > MAX_CHARS_PER_PAGE:
            text = text[:MAX_CHARS_PER_PAGE] + f"\n[...page {page_no} truncated...]"
        parts.append(f"=== PAGE {page_no} ===\n{text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def process_doc(
    http: httpx.Client,
    broker_url: str,
    doc_id: str,
    job_id: int,
    env: dict[str, str],
    dry_run: bool,
) -> bool:
    """Fetch OCR, run Gemini extraction, complete the broker job."""
    ocr_resp = http.get(f"{broker_url}/ocr/{doc_id}.json", timeout=30)
    if ocr_resp.status_code != 200:
        print(f"  [{doc_id}] Cannot fetch OCR JSON: HTTP {ocr_resp.status_code}")
        return False

    pages = ocr_resp.json()
    if not isinstance(pages, list):
        print(f"  [{doc_id}] OCR JSON is not a list")
        return False

    print(f"  [{doc_id}] {len(pages)} OCR pages")

    if dry_run:
        print(f"  [{doc_id}] DRY RUN — would call Gemini and complete job {job_id}")
        return True

    all_entities: list[dict] = []
    all_redactions: list[dict] = []

    for chunk_start in range(0, len(pages), MAX_PAGES_PER_CALL):
        chunk = pages[chunk_start : chunk_start + MAX_PAGES_PER_CALL]
        pages_text = build_pages_text(chunk)
        prompt = _PROMPT_TEMPLATE.format(doc_id=doc_id, pages_text=pages_text)

        p_start = chunk[0].get("page_no", chunk_start + 1)
        p_end = chunk[-1].get("page_no", chunk_start + len(chunk))
        print(
            f"  [{doc_id}] Gemini call — pages {p_start}–{p_end}, "
            f"prompt {len(prompt):,} chars"
        )

        try:
            raw = call_gemini(prompt, env)
            data = extract_json(raw)
        except Exception as exc:
            print(f"  [{doc_id}] Gemini error on chunk {chunk_start}: {exc}")
            return False

        all_entities.extend(data.get("entities", []))
        all_redactions.extend(data.get("redactions", []))

    print(
        f"  [{doc_id}] {len(all_entities)} entities, "
        f"{len(all_redactions)} redactions — completing..."
    )

    resp = http.post(
        f"{broker_url}/complete",
        json={
            "job_id": job_id,
            "worker_id": WORKER_ID,
            "result": {"entities": all_entities, "redactions": all_redactions},
        },
        timeout=30,
    )
    if resp.status_code == 200:
        print(f"  [{doc_id}] Done")
        return True
    print(f"  [{doc_id}] Complete failed: {resp.status_code} {resp.text[:200]}")
    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini features extraction worker")
    parser.add_argument(
        "--loop", action="store_true", help="Keep polling until queue is empty"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Lease and inspect jobs without processing"
    )
    parser.add_argument(
        "--max-jobs", type=int, default=1, help="Jobs to lease per poll (default 1)"
    )
    args = parser.parse_args()

    cfg = load_config()
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    env = get_gemini_env()

    if not env.get("GEMINI_API_KEY"):
        print(
            "ERROR: GEMINI_API_KEY not found. Add it to ~/.zprofile.", file=sys.stderr
        )
        sys.exit(1)

    http = httpx.Client(timeout=60.0)
    print(f"Gemini features worker started — broker {broker_url}, model {MODEL}")

    while True:
        try:
            lease_resp = http.post(
                f"{broker_url}/lease",
                json={
                    "worker_id": WORKER_ID,
                    "capabilities": ["features"],
                    "max_jobs": args.max_jobs,
                },
                timeout=10,
            )
        except httpx.ConnectError as exc:
            print(f"Broker unreachable: {exc} — retrying in 15s")
            if not args.loop:
                break
            time.sleep(15)
            continue
        except httpx.RequestError as exc:
            print(f"Lease request error: {exc} — retrying in 5s")
            if not args.loop:
                break
            time.sleep(5)
            continue

        if lease_resp.status_code != 200:
            print(f"Lease error: HTTP {lease_resp.status_code}")
            if not args.loop:
                break
            time.sleep(5)
            continue

        jobs = lease_resp.json().get("jobs", [])
        if not jobs:
            print("No features jobs pending.")
            if not args.loop:
                break
            time.sleep(10)
            continue

        for job in jobs:
            job_id = job["job_id"]
            doc_id = job["doc_id"]
            print(f"\nLeased job {job_id} — doc {doc_id}")
            ok = process_doc(http, broker_url, doc_id, job_id, env, args.dry_run)
            if not ok and not args.dry_run:
                http.post(
                    f"{broker_url}/fail",
                    json={
                        "job_id": job_id,
                        "worker_id": WORKER_ID,
                        "error": "gemini extraction failed",
                        "retryable": True,
                    },
                    timeout=10,
                )

        if not args.loop:
            break


if __name__ == "__main__":
    main()
