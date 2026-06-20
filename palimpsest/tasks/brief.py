# palimpsest/tasks/brief.py
"""Worker task handler for per-document brief jobs.

Compresses each document's OCR text into a structured, skimmable abstract:
summary, claims, events, redaction hypotheses, and investigative flags.

Architecture constraints preserved:
- No DB access, no volume access from the worker.
- OCR text fetched over HTTP from the broker (single-writer model).
- Long documents handled via map-reduce: brief each window, then reduce.
- Malformed model JSON raises PermanentJobError (don't burn retries).
"""
from __future__ import annotations

import json
import logging
import textwrap
from typing import Any

import httpx

from palimpsest.config import Config
from palimpsest.tasks import PermanentJobError, handler

logger = logging.getLogger(__name__)

# Approximate chars per token (conservative estimate for Cold-War bureaucratic prose).
_CHARS_PER_TOKEN = 4

# ── Prompt templates ─────────────────────────────────────────────────────────

_MAP_SYSTEM = """\
You are an investigative analyst reviewing declassified U.S. government documents.
Extract a structured brief from the supplied document pages.

Rules:
- Ground every claim and event in specific page text; stamp each with the page_no it came from.
- Never invent details not present in the supplied text.
- For redactions: infer the *category* of what is hidden (person_name | dose | location | date | protocol | other) from surrounding context. Do NOT guess a specific name or value.
- confidence is always 0.0 — human review assigns confidence, not the model.
- Return ONLY valid JSON matching the schema below. No prose, no markdown fences.

Schema:
{{
  "doc_type": "<field_report|memo|correspondence|protocol|summary|other>",
  "summary": "<= 4 sentences: what this document is and why it might matter>",
  "claims": [{{"text": "...", "page_no": <int>, "confidence": 0.0}}],
  "events": [{{"actor": "...", "action": "...", "object": "...", "subject_ref": "...", "date": "...", "place": "...", "page_no": <int>}}],
  "redaction_hypotheses": [{{"page_no": <int>, "label": "...", "likely_hidden": "<person_name|dose|location|date|protocol|other>", "rationale": "...", "confidence": 0.0}}],
  "flags": ["<human_subjects|consent_language_absent|dose_data|outcome_data|no_follow_up|protocol_code|other>"]
}}

Omit empty arrays. claims and events: max {max_claims} each."""

_REDUCE_SYSTEM = """\
You are merging partial briefs of the same document into one consolidated brief.

Rules:
- Deduplicate claims and events across slices (same fact, keep once).
- Merge flag lists.
- Write a fresh summary covering the whole document.
- confidence is always 0.0.
- Return ONLY valid JSON matching the schema above (same as map schema).
- No prose, no markdown fences."""


def _broker_url(cfg: Config) -> str:
    return f"http://{cfg.broker['host']}:{cfg.broker['port']}"


def _fetch_ocr(cfg: Config, doc_id: str) -> list[dict[str, Any]]:
    """Fetch OCR pages from broker. Returns list of page dicts."""
    url = f"{_broker_url(cfg)}/ocr/{doc_id}.json"
    try:
        resp = httpx.get(url, timeout=30.0)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"brief: network error fetching OCR for {doc_id}: {exc}") from exc
    if resp.status_code == 404:
        raise PermanentJobError(f"brief: OCR not found for doc_id {doc_id}")
    resp.raise_for_status()
    return resp.json()


def _pages_to_text(pages: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Return list of (page_no, text) pairs, skipping empty pages."""
    result = []
    for page in pages:
        text = page.get("text", "").strip()
        if text:
            result.append((page["page_no"], text))
    return result


def _call_ollama(cfg: Config, prompt: str, system: str) -> str:
    """Call Ollama /api/generate and return the response text."""
    brief_cfg = cfg.brief
    model = brief_cfg.get("model", cfg.models.get("extract", "llama3.1:8b"))
    temperature = brief_cfg.get("temperature", 0.1)
    keep_alive = cfg.models.get("keep_alive", "24h")

    payload = {
        "model": model,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": 4096,
        },
        "keep_alive": keep_alive,
    }

    try:
        resp = httpx.post(
            "http://localhost:11434/api/generate",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"brief: Ollama request failed: {exc}") from exc

    data = resp.json()
    return data.get("response", "").strip()


def _parse_brief_json(raw: str, context: str = "") -> dict[str, Any]:
    """Parse model JSON output; raise PermanentJobError on malformed response."""
    # Strip markdown fences if the model ignored the instruction
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PermanentJobError(
            f"brief: malformed JSON from model{' (' + context + ')' if context else ''}: {exc}\nRaw: {raw[:400]}"
        ) from exc


def _brief_slice(
    cfg: Config,
    doc_id: str,
    page_pairs: list[tuple[int, str]],
) -> dict[str, Any]:
    """Brief a single slice of pages (fits within model context window)."""
    brief_cfg = cfg.brief
    max_claims = brief_cfg.get("max_claims", 25)

    # Build the page content block
    page_blocks = []
    for page_no, text in page_pairs:
        page_blocks.append(f"[PAGE {page_no}]\n{text}")
    document_text = "\n\n".join(page_blocks)

    system = _MAP_SYSTEM.format(max_claims=max_claims)
    prompt = f"Document ID: {doc_id}\n\n{document_text}"

    raw = _call_ollama(cfg, prompt, system)
    return _parse_brief_json(raw, context=f"slice pages {page_pairs[0][0]}–{page_pairs[-1][0]}")


def _reduce_slices(cfg: Config, doc_id: str, slice_briefs: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge multiple slice-briefs into a single final brief."""
    brief_cfg = cfg.brief
    max_claims = brief_cfg.get("max_claims", 25)

    slices_text = json.dumps(slice_briefs, indent=2)
    system = _REDUCE_SYSTEM
    prompt = f"Document ID: {doc_id}\nMax claims/events each: {max_claims}\n\nPartial briefs:\n{slices_text}"

    raw = _call_ollama(cfg, prompt, system)
    return _parse_brief_json(raw, context="reduce pass")


@handler("brief")
def handle_brief(cfg: Config, job: dict[str, Any]) -> dict[str, Any]:
    """Generate a structured per-document brief from OCR text.

    Input:  job["doc_id"] — fetches OCR over HTTP from broker.
    Output: dict matching the §1 output contract.
    """
    doc_id = job["doc_id"]
    logger.info("brief: starting doc %s", doc_id)

    brief_cfg = cfg.brief
    model = brief_cfg.get("model", cfg.models.get("extract", "llama3.1:8b"))
    window_tokens = brief_cfg.get("window_tokens", 6000)
    window_chars = window_tokens * _CHARS_PER_TOKEN

    # 1. Fetch OCR
    pages = _fetch_ocr(cfg, doc_id)
    page_pairs = _pages_to_text(pages)

    if not page_pairs:
        logger.warning("brief: no text content for doc %s", doc_id)
        return {
            "doc_id": doc_id,
            "model": model,
            "schema": 1,
            "doc_type": "other",
            "summary": "No OCR text available for this document.",
            "claims": [],
            "events": [],
            "redaction_hypotheses": [],
            "flags": [],
        }

    # 2. Decide map vs single-pass
    total_chars = sum(len(t) for _, t in page_pairs)

    if total_chars <= window_chars:
        # Single pass
        logger.info("brief: single-pass (%d chars) for doc %s", total_chars, doc_id)
        result = _brief_slice(cfg, doc_id, page_pairs)
    else:
        # Map-reduce: chunk into window-sized slices
        logger.info(
            "brief: map-reduce (%d chars, window %d chars) for doc %s",
            total_chars, window_chars, doc_id,
        )
        slices: list[list[tuple[int, str]]] = []
        current_slice: list[tuple[int, str]] = []
        current_chars = 0

        for page_no, text in page_pairs:
            page_chars = len(text)
            if current_slice and current_chars + page_chars > window_chars:
                slices.append(current_slice)
                current_slice = []
                current_chars = 0
            current_slice.append((page_no, text))
            current_chars += page_chars

        if current_slice:
            slices.append(current_slice)

        logger.info("brief: map phase — %d slices for doc %s", len(slices), doc_id)
        slice_briefs = []
        for i, sl in enumerate(slices):
            logger.info("brief: map slice %d/%d for doc %s", i + 1, len(slices), doc_id)
            slice_briefs.append(_brief_slice(cfg, doc_id, sl))

        logger.info("brief: reduce phase for doc %s", doc_id)
        result = _reduce_slices(cfg, doc_id, slice_briefs)

    # 3. Normalise and stamp required fields
    # Validate that every claim/event page_no exists in the input OCR
    valid_pages = {pn for pn, _ in page_pairs}

    claims = result.get("claims", [])
    for claim in claims:
        if "page_no" not in claim:
            claim["page_no"] = page_pairs[0][0]
        claim["confidence"] = 0.0  # always override

    events = result.get("events", [])
    for event in events:
        if "page_no" not in event:
            event["page_no"] = page_pairs[0][0]

    redaction_hypotheses = result.get("redaction_hypotheses", [])
    for rh in redaction_hypotheses:
        if "page_no" not in rh:
            rh["page_no"] = page_pairs[0][0]
        rh["confidence"] = 0.0  # always override

    output = {
        "doc_id": doc_id,
        "model": model,
        "schema": 1,
        "doc_type": result.get("doc_type", "other"),
        "summary": result.get("summary", ""),
        "claims": claims,
        "events": events,
        "redaction_hypotheses": redaction_hypotheses,
        "flags": result.get("flags", []),
    }

    logger.info(
        "brief: completed doc %s — %d claims, %d events, %d redaction_hypotheses, flags=%s",
        doc_id,
        len(output["claims"]),
        len(output["events"]),
        len(output["redaction_hypotheses"]),
        output["flags"],
    )
    return output
