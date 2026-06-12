# TASK-07 — Embeddings, Index, and the Redaction-Gap Join

**Read `specs/00-ARCHITECTURE.md` §3 ([embed], [gapjoin]), §5 (chunks, gap_candidates), §7. This is the task that produces the Phase-1 result; the scoring must be implemented exactly as specified — it is deliberately deterministic and logged.**

Two deliverables, two processes: the embed HANDLER (runs on M4) and the INDEXER/gap-join (runs on gonktop).

## Depends on
TASK-04 (handler), TASK-02 (broker persistence incl. pending_embeddings.jsonl), TASK-05/06 outputs present for a slice.

## Deliverables
```
palimpsest/tasks/embed.py      # @handler('embed'), runs on M4
palimpsest/indexer.py          # gonktop-resident: FAISS build + gap join CLI
tests/test_embed.py
tests/test_gapjoin.py
```
New deps: `faiss-cpu`, `httpx`, `numpy`.

## Spec — embed handler (M4)

1. Get OCR JSON: `GET {broker}/ocr/{doc_id}.json`.
2. Chunk each page's `text`: windows of `cfg.embed["chunk_chars"]` (800) with `chunk_overlap` (150); never split mid-word (break at last whitespace before limit); record `page_no, char_start, char_end`.
3. Embed each chunk: `POST http://localhost:11434/api/embeddings {"model": cfg.embed["model"], "prompt": chunk_text, "keep_alive": cfg.models["keep_alive"]}` → 768-float vector. Batch sequentially; log chunks/sec.
4. Return `{"chunks": [{page_no, char_start, char_end, text, embedding}]}` (broker persists rows + appends vectors to `pending_embeddings.jsonl`).
Empty page text → no chunks (fine). Ollama connection refused → retryable failure.

## Spec — indexer (gonktop)

### CLI
```
python -m palimpsest.indexer build      # fold pending_embeddings.jsonl into FAISS
python -m palimpsest.indexer gapjoin    # run the join, write gap_candidates
python -m palimpsest.indexer stats
```

### build
- Load/create `{root}/index/faiss.idx`: `IndexIDMap2(IndexFlatIP(768))`.
- Read `pending_embeddings.jsonl`; L2-normalize each vector (so IP = cosine); `add_with_ids(vec, chunk_id)`; on success truncate the jsonl (atomic: process to .done rename). Re-running with empty pending = no-op.
- Mark affected documents `status='indexed'`.

### gapjoin — the algorithm (implement exactly)
For every `redactions` row `r` not yet joined (track via a `gapjoin_runs` bookkeeping table you add — `(redaction_id, run_at)`):

1. **Context.** `ctx = r.context_before + " " + r.context_after`. If `len(ctx.strip()) < 40`: skip (record as skipped — un-contextualized boxes are noise).
2. **Anchors.** `A` = set of entity `norm`s from `entities` on the same page as `r` whose line bbox falls within the context window (same ±2-line band). Persons, dates, dosages, protocol_codes, locations — all kinds.
3. **Slot kind guess.** From label/kind heuristic: `(b)(6)`/`(b)(7)` ⇒ expect person; `deleted_text`/`black_box`/`(b)(1)` ⇒ no expectation (kind_prior applies only when an expectation exists).
4. **Candidate generation** (union, dedupe by entity_id):
   - *Anchor route:* entities in OTHER documents (`doc_id != r.doc_id`) whose page contains ≥ 2 members of `A` (join `entities` on `norm` within same `(doc_id, page_no)`).
   - *Embedding route:* embed `ctx` (call Ollama on gonktop or via an `embed` job if no local model — Phase 1: gonktop runs nomic-embed under CPU, acceptable for query-time volume), search FAISS top `cfg.gapjoin["topk_embedding_candidates"]` (50) chunks, exclude chunks from `r.doc_id`, take entities whose `(doc_id, page_no)` and char span fall inside a hit chunk.
5. **Score** each candidate entity `e`:
   - `score_cosine` = max cosine between ctx embedding and any hit chunk containing `e` (anchor-route-only candidates: compute cosine against the chunk covering `e`'s span; if no chunk, 0).
   - `score_anchor` = `|A ∩ anchors_on_e's_page| / max(|A|, 1)` (cap 1.0).
   - `score_kind` = 1.0 if slot expectation exists and `e.kind` matches; 0.5 if no expectation; 0.0 on mismatch.
   - `score = w_cosine*score_cosine + w_anchor*score_anchor + w_kind*score_kind` (weights from config).
6. Persist every candidate with `score >= cfg.gapjoin["score_threshold"]` to `gap_candidates` (components logged in their columns, `method` per route(s), `status='candidate'`). Dedupe: `(redaction_id, clear_entity_id)` unique — add the constraint in a migration (bump schema_version to 2).
7. **Auto-flag for HITL:** any candidate whose clear entity `kind='person'` ⇒ insert `review_queue` row, reason `'person in gap candidate #<gap_id>'` (status pending).

`stats`: counts — redactions total/joined/skipped, candidates by score decile, by method, review_queue pending.

## Acceptance (paste output)
```
python -m pytest tests/test_embed.py tests/test_gapjoin.py -q
```
test_embed: chunker boundaries (no mid-word splits, overlap correct, exact-length edge cases), empty page, Ollama mock returns vector → result shape.
test_gapjoin (synthetic mini-corpus inserted directly into a tmp DB — 3 fake docs: doc A has redaction with anchors {“oak ridge”, “1957-03-02”}, doc B clear page shares both anchors + a dosage entity, doc C shares nothing): anchor route finds doc B not doc C; scoring components match hand-computed values to 1e-6; threshold filters; same-doc exclusion; person candidate creates review_queue row; rerun = no duplicate candidates; short-context skip.

Integration (manual, after a real slice is processed): `indexer build && indexer gapjoin && indexer stats` — paste stats.

## Out of scope
MCP exposure, verification UI, LLM extraction, tuning weights (human does that against real candidates).

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
