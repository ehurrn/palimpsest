# TASK-20 — Document Brief + Triage (Intelligence Layer)

**Status:** spec / not yet implemented.
**Depends on:** OCR (TASK-05) populated for the target slice. Independent of the gapjoin / finding scorers.
**Authority:** `specs/00-ARCHITECTURE.md` wins on every shared interface. Everything here is *additive* and obeys the existing conventions — single-writer broker, config-driven tunables, idempotent `(type, doc_id)` jobs, workers never touch the DB or the storage volume.

---

## 0. Why this exists

Two problems, one mechanism.

1. **Human bandwidth.** Nobody will read the raw corpus. A per-document *brief* compresses each PDF into a dense, skimmable, structured abstract — so a person grasps the corpus in an afternoon, and every smarter feature downstream reasons over briefs instead of OCR sludge.
2. **Machine comprehension.** Today's pipeline extracts *nouns* (`entities`). Briefs add *predicates* — claims and events — plus a first cut at what each redaction is hiding. That structured layer is the substrate for multi-hop correlation later.

A brief is an **investigative aid, not a finding.** It never bypasses Iron Rule 2 (no finding without two citations) or the §8 masking rule. It feeds the existing finding pipeline and the triage view; it does not surface conclusions on its own.

---

## 1. New job type: `brief`

Same shape as every other handler: `@handler("brief") def handle_brief(cfg, job) -> dict`, registered by importing `palimpsest.tasks.brief` in `palimpsest/tasks/__init__.py`.

**Input.** The worker pulls OCR text over HTTP from the broker (`GET {broker}/ocr/{doc_id}.json`), exactly as the OCR handler pulls the PDF. No DB access, no volume access — single-writer model preserved.

**Compute.** One Ollama `/api/generate` call per document (model + params from the new `[brief]` config section). If the doc exceeds the model window, **map-reduce**: brief each `window_tokens` slice, then one reduce pass merges the slice-briefs into the final object. This is the answer to "models have context windows" — you never feed the corpus to a model, and you never feed an over-long doc in one shot.

**Output (the result dict persisted by `results.py`):**

```json
{
  "doc_id": "16007132",
  "model": "llama3.1:8b",
  "schema": 1,
  "doc_type": "field_report",
  "summary": "<=4 sentences, plain English: what this document is and why it might matter.",
  "claims": [
    {"text": "Subjects at the test site received whole-body exposure.",
     "page_no": 3, "confidence": 0.0}
  ],
  "events": [
    {"actor": "...", "action": "administered dose", "object": "15 rem",
     "subject_ref": "Subject 3", "date": "1953-07", "place": "NTS", "page_no": 3}
  ],
  "redaction_hypotheses": [
    {"page_no": 3, "label": "(b)(1)",
     "likely_hidden": "person_name|dose|location|date|protocol|other",
     "rationale": "follows 'subject' and precedes a dose value", "confidence": 0.0}
  ],
  "flags": ["human_subjects", "consent_language_absent"]
}
```

**Prompt rules (hard):** ground every claim/event in page text; stamp each with the `page_no` it came from; never invent specifics not on the page. For redactions, infer the *category* behind the mark from surrounding context — **not** a specific name. Specific reconstruction stays in the gapjoin pipeline under the two-citation rule; the brief only says "a person's name probably sat here."

---

## 2. Config (`config.toml`)

```toml
[brief]
model = "llama3.1:8b"     # reuse [models].extract, or point at a bigger/remote model
window_tokens = 6000      # map step size; reduce pass merges slice-briefs
max_claims = 25
max_events = 25
temperature = 0.1
```

Add `brief: Dict[str, Any]` to the `Config` dataclass and to `load()` in `config.py`. Keep it **optional** — `data.get("brief", {...defaults})` — so existing `config.toml` files don't break (do not add to `required_sections`).

---

## 3. Routing

Brief wants the strongest available model, so give the `brief` capability to the `extract` node:

```toml
[nodes]
m4 = ["ocr", "embed", "classify"]
m5 = ["extract", "ocr", "brief"]   # llama3.1:8b
```

The broker leases a job only to a worker whose capabilities include the job's `type`, so this is the whole routing change. If m5's intermittency hurts throughput, add `"brief"` to `m4` and set `[brief].model = "qwen2.5:3b"` — lower quality, always-on. **Config decides; no code change.**

> Open decision for Eric: local-only (llama3.1:8b / qwen2.5:3b) keeps the box air-gapped but caps comprehension quality on dense Cold-War bureaucratic prose. Pointing `[brief].model` at a frontier API would sharply raise brief quality and is cheap (one small call per doc, briefs are short) — but breaks local-only. The handler should treat the model as a config-supplied endpoint so this stays a one-line switch.

---

## 4. Persistence (`results.py` + schema v4)

Migration v4 in `db.py` — same idempotent, version-gated pattern as v2/v3:

```sql
CREATE TABLE IF NOT EXISTS briefs (
  doc_id          TEXT PRIMARY KEY REFERENCES documents(doc_id),
  model           TEXT,
  doc_type        TEXT,
  summary         TEXT,
  claims_json     TEXT,    -- claims array, verbatim
  events_json     TEXT,    -- events array
  redactions_json TEXT,    -- redaction_hypotheses array
  flags_json      TEXT,
  interest_score  REAL,    -- filled by triage (§6), NULL until then
  novelty_score   REAL,    -- filled by triage (§6), NULL until then
  created_at      TEXT
);
```

Add `process_brief(conn, cfg, doc_id, result, now)` to `RESULT_PROCESSORS`: write `{root}/briefs/{doc_id}.json` (tmp-rename like `process_ocr`), then `INSERT OR REPLACE` one `briefs` row. Leave the two score columns NULL. Brief is **terminal** — it enqueues no follow-on.

---

## 5. Chaining + backfill

- **New docs:** in `process_features`, enqueue `brief` alongside `embed`. (Brief only needs OCR text, but gating on `features_done` means entities already exist for later prompt enrichment and avoids racing OCR.)
- **Existing corpus:** one-shot `python -m palimpsest.orchestrator enqueue-brief [--status indexed]` inserts a pending `brief` job per doc already past OCR. Idempotent via `UNIQUE(type, doc_id)`.

---

## 6. Triage CLI: `python -m palimpsest.triage`

Read-only, runs on gonktop. Two scores, both written back to `briefs`:

- **`novelty_score`** — cheap, unsupervised, no labels. Embed each brief's `summary` (reuse the embed endpoint), compute mean cosine distance to its *k* nearest brief-neighbors. High distance = anomalous document = worth a human glance. This finds the interesting 2% without anyone reading anything.
- **`interest_score`** — optional, one `classify`-model pass scoring 0–1 against a fixed rubric (human-subject exposure with consent language absent; outcome promised but no follow-up doc; redaction adjacent to a dose or person). Rubric text lives in config.

`triage` prints the top-N briefs by `max(interest, novelty)` — a table of `doc_id, year, doc_type, score, one-line summary, top flag`. `triage --doc {id}` dumps one full brief. **This is the fruitfulness answer:** skim 30 ranked one-liners instead of 500 PDFs, and decide whether to keep going.

---

## 7. Acceptance tests (`tests/test_brief.py`, mock Ollama like the embed/extract tests)

1. `handle_brief` returns the §1 contract shape; every `claim`/`event` carries a `page_no` that exists in the input OCR; malformed model JSON raises `PermanentJobError` (don't burn retries on an unparseable response).
2. `process_brief` writes the JSON file and upserts exactly one `briefs` row; re-running `(brief, doc_id)` overwrites, never duplicates.
3. Migration v4 is idempotent: `migrate` twice on a v3 DB → `briefs` exists, no error.
4. `triage` orders by `max(interest, novelty)` and honors `--limit`.

**Prove on a real slice before scaling.** Run `brief` over ~30 already-indexed docs. Eyeball 5 briefs against their PDFs for hallucination. Run `triage`; check whether the top-ranked handful are actually the interesting ones. Faithful briefs + signal-bearing ranking → the layer earns its keep. Hallucinated briefs or noise ranking → fix the prompt or swap the model before anything is built on top. (Phase-1 discipline: prove ≥1 unit of value before moving on.)

---

## 8. Deliberately out of scope

Multi-hop agentic retrieval and full generative redaction reconstruction build *on* this layer — not in this packet. Briefs + triage first: cheapest path to "is there anything here," and the structured substrate everything smarter reasons over.
