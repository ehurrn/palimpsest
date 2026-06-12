# TASK-08 — MCP Server (read-only, gonktop)

**Read `specs/00-ARCHITECTURE.md` §5, §8 (HITL MASKING — every tool here must apply it). Iron rules: read-only (no tool mutates state), no finding without two citations.**

## Objective
`palimpsest/server.py`: FastMCP server, streamable HTTP transport, port `cfg.mcp["port"]` (8078), exposing the corpus and gap candidates to investigating agents.

## Depends on
TASK-01, TASK-07 (populated schema). Runs on gonktop; opens the DB read-only (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`).

## Deliverables
```
palimpsest/server.py
tests/test_server.py
```
New dep: `mcp` (the official Python SDK; `FastMCP` import). Pin the version you install in pyproject.

## Spec

### Masking helper (write first, use everywhere)
```python
def mask_person(entity_row, conn) -> str:
    """Return entity text iff living_status='deceased_historical' AND an
    approved review_queue row exists; else f'PERSON-{entity_id:04d}'."""
```
Applied to every person entity in every tool response, including inside text snippets: when returning page/context text that contains a non-approved person entity's span, replace that span with the pseudonym. There is NO parameter on any tool to disable masking.

### Citation type (used in every response that asserts anything)
```json
{"doc_id": "16007132", "page_no": 3,
 "source_url": "https://www.osti.gov/opennet/servlets/purl/16007132.pdf",
 "title": "...", "accession": "NV0123456"}
```

### Tools (all return structured JSON; all errors as {"error": "..."} not exceptions)

`palimpsest_find_redaction_gaps(min_score: float = 0.65, status: str = "candidate", kind: str | None = None, limit: int = 20)`
→ list of:
```json
{"gap_id": 9, "score": 0.81,
 "score_components": {"cosine": 0.84, "anchor": 0.67, "kind": 1.0},
 "method": "both", "status": "candidate",
 "redaction": {"kind": "exemption_stamp", "label": "(b)(6)",
   "context_before": "...", "context_after": "...",
   "citation": {…doc A, page…}},
 "clear_entity": {"kind": "person", "text": "PERSON-0042",   // masked!
   "context": "±300 chars around the entity, masked",
   "citation": {…doc B, page…}},
 "requires_review": true}      // true when the clear entity is a masked person
```
Ordered by score desc. BOTH citations always present (the schema guarantees it; the tool composes it).

`palimpsest_search(query: str, limit: int = 10)` — embed query (same model/route as indexer), FAISS top-k, return chunks: `{text (masked), score, citation}`.

`palimpsest_get_document(doc_id: str, page_no: int | None = None)` — metadata + status + page text (masked) for one page or all; includes that doc's redactions and entities (persons masked).

`palimpsest_get_entity(norm: str, kind: str | None = None, limit: int = 50)` — every occurrence of an entity across the corpus: `{kind, text (masked if person), citation, char_start, char_end}`. This is the manual-corroboration workhorse.

`palimpsest_queue_status()` — same payload as broker GET /status (call the broker over HTTP; if broker down, return `{"error": "broker unreachable"}`), plus document counts by status and gapjoin stats.

`palimpsest_review_queue(limit: int = 50)` — pending review_queue items: `{review_id, entity_id, pseudonym, reason, gap_id if parseable}`. NOTE: read-only listing. Approval happens ONLY in the review CLI (TASK-09).

### Server
```
python -m palimpsest.server --config config.toml
```
FastMCP streamable-http on `0.0.0.0:{cfg.mcp["port"]}`. Tool docstrings: one line each, stating masking behavior where relevant. Log every tool call (name, args, row count returned).

## Acceptance (paste output)
```
python -m pytest tests/test_server.py -q
python -m palimpsest.server &   # then exercise ≥2 tools via an MCP client or curl to the endpoint; paste responses
```
Tests (tmp DB seeded with fixtures incl. an unapproved person, an approved deceased person, a gap candidate): mask_person both branches; masking applied inside snippet text (span replacement verified); find_redaction_gaps returns both citations and respects min_score; get_entity masks persons but not dosages; no tool accepts any write; DB opened mode=ro (attempt a write through the server's connection in a test → fails).

## Out of scope
Approvals/mutations, the investigator skill, auth (LAN-only Phase 1), Lane A mesh integration.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
