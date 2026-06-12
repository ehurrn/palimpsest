# Palimpsest — Phase 1 Build Plan

*Anchor document for the Cowork Project. Recovering text from beneath the redactions.*

**Target corpus:** DoE OSTI OpenNet, NV* accession series (Nevada Test Site, ~500K docs)
**Phase 1 goal:** Produce **one provable de-redaction** on a bounded slice — a name/dosage/outcome blacked out in document A, found in the clear in document B, with both source pages shown. If the slice yields zero, we reassess before scaling. That single result validates (or kills) the entire premise cheaply.

---

## 0. The build decision

**Extend `ml-pipeline`; do not replace it.** It already provides the validated, benchmarked multi-node transport (broker on gonktop, per-node bridge daemons, `send_message` tool-call routing). Rebuilding that is the one thing we must not do.

**But split the workload into two lanes:**

- **Lane A — Orchestration (existing mesh).** Routing decisions, light agentic extraction, human-in-the-loop prompts, "ask M5 to summarize this curated doc." Uses `ml-pipeline`'s broker + `send_message` as-is.
- **Lane B — Bulk grind (new, Palimpsest).** OCR, embeddings, NER, per-doc structured extraction over tens of thousands of documents. A persistent SQLite-backed job queue on gonktop + long-lived warm-model worker daemons on the Macs. Reuses the mesh's node-availability signal but **not** its chat-tool path — wrong abstraction for batch throughput.

Rationale: the mesh's 3B–8B Ollama models are an orchestration/light-extraction tier. OCR and embeddings aren't LLM-token workloads at all, and 500K-doc batch processing needs idempotency/retry/persistence that a chat broker handles poorly. Same cluster, same node-role logic, separate lane.

---

## 1. Cowork Step 0 — Reconnaissance (do this BEFORE writing any code)

This plan was written against the benchmark doc, not the source. First task in Cowork is to read `~/dev/ml-pipeline` and produce a one-page reconcile note answering:

- **Transport:** sockets? HTTP? message format? Is the broker stateful?
- **`send_message` tool:** exact signature, how a node registers, how responses route back.
- **Node registry / config:** where nodes + their models are declared; how availability (esp. M5 intermittent) is signaled.
- **Model lifecycle:** how Ollama models are launched and kept warm; is the daemon long-lived?
- **Health/retry:** what happens when a node drops mid-task.
- **Reuse surface:** what can Lane B borrow directly (node registry, availability, health) vs. what it needs of its own (job persistence, retry, idempotency).

Output: a short "ml-pipeline provides X / Palimpsest must add Y" diff. Adjust this plan against reality, then build.

**Assumptions in this plan to verify against the real repo:** broker is reusable as a node-availability source; node config can be read by a non-mesh process; models can be invoked outside the `send_message` path (direct Ollama API on each node) for batch extraction.

---

## 2. Node → workload map (grounded in your benchmark)

| Node | Always-on | Lane A (mesh agent) | Lane B (Palimpsest grind) | Extraction model |
|------|:---------:|---------------------|---------------------------|------------------|
| **gonktop** (Xeon, 128GB, CPU-only) | ✅ | qwen2.5:3b — routing/orchestration (~18 tok/s) | **Brain:** store, in-RAM index, job broker, MCP server. *No heavy LLM grind* (8B = ~70s/doc, never). | — |
| **M4 Mini** (16GB, Metal, SSD) | ✅ | qwen2.5:3b warm (~41 tok/s) | **Always-on grind horse:** OCR, embeddings, light classification | qwen2.5:3b (classification only) |
| **M5 Pro** (24GB, Metal) | ❌ intermittent | llama3.1:8b (~55 tok/s) | **Heavy extraction:** drains queue when docked | llama3.1:8b |

Keep models warm — your benchmark shows cold-start is 10–19s (SSD load) but warm eval is sub-2s. Lane B workers are long-lived daemons, not per-job processes.

---

## 3. The funnel, with real timing

Confirms last turn's mandate: triage cheap → filter → LLM-extract the subset → frontier-synthesize the curated few. Numbers, grounded:

- **OCR (the real phase-1 time sink):** Apple Vision (`VNRecognizeTextRequest`) ~0.3–0.7s/page on Metal. A 5,000-doc slice × ~15 pages ≈ 75K pages ≈ 7–14h single-stream; split across both Macs, half that. Prefer OSTI's existing OCR layer where it's not garbage; re-OCR only the bad/missing.
- **Embeddings:** an embed model (bge-small / nomic) does hundreds of chunks/sec on Metal. 5K docs ≈ minutes. Not a bottleneck.
- **LLM extraction (subset only):** ~600 output tokens/doc structured extraction → M5 llama3.1:8b ≈ **11s/doc**, M4 qwen2.5:3b ≈ 15s/doc (classification-grade), gonktop ≈ 70s/doc (don't). A 5K-doc filtered subset on M5 ≈ ~15h overnight batch. A 50K subset ≈ ~6 days — which is exactly why **phase 1 takes a small slice**, not all of NV.

**Phase 1 slice:** one bounded NV sub-collection, target ~2,000–5,000 docs. Pick by test series or year range in Cowork after we see what the advanced-search facets allow.

---

## 4. Phase 1 spear-tip — redaction-gap detection

The most tractable, most verifiable, most distinctive of the six finding-types (de-redaction by corroboration; covers categories *a* and *f*). It doesn't require an LLM to "understand" the physics, and every result is self-proving: two source pages.

**Build tasks (Cowork):**

1. **Harvester** (`palimpsest/harvester.py`) — query OpenNet advanced search for the NV slice, paginate the result set, extract document IDs, download PDFs via the purl pattern `osti.gov/opennet/servlets/purl/{id}.pdf`. Throttle hard (1–2 req/s + backoff); OSTI's Acceptable Use Policy will block aggressive crawling — email opennet@osti.gov to request bulk terms. Persist catalog rows (id, accession, title, has_fulltext, local_path).
2. **Ingest** (`palimpsest/ingest/`) — Vision OCR (+ Tesseract fallback for Linux-side); **redaction-mark detection** (exemption stamps `(b)(1)` etc., "[deleted]"/"DELETED", black-box image regions) emitted as structured features with page + bbox; **entity extraction** — names, dates, dosages, locations, and the patient/protocol codes (CAL/CHI/HP series) via spaCy NER + targeted regex.
3. **Index** (`palimpsest/index.py`) — embeddings + FAISS (resident in gonktop RAM), full-text index, entity store. **The redaction-gap join:** for each entity that appears *adjacent to a redaction marker* in some document, search for the same entity/context appearing *in the clear* elsewhere in the corpus. Rank candidate gaps by contextual similarity + corroboration strength.
4. **MCP server** (`palimpsest/server.py`, `palimpsest_mcp`, FastMCP, streamable HTTP on gonktop) — tools: `palimpsest_find_redaction_gaps`, `palimpsest_search`, `palimpsest_get_document`, `palimpsest_get_entity`, `palimpsest_queue_status`. Read-only annotations. Structured output.
5. **Investigator skill** (`skills/palimpsest-investigator/SKILL.md`) — the methodology that drives an investigation through the MCP tools and **enforces the provenance invariant**.

---

## 5. Non-negotiable gates (build in from line one)

- **Provenance invariant:** no synthesized claim exists in any output without a citation to a specific document ID + page. A finding *is* its source pages. An uncited claim is discarded, not surfaced.
- **Identity human-in-the-loop gate:** categories *a* and *e* surface real medical-subject data. "The government withheld that experiment X happened" is always publishable. "Here is a possibly-living individual's medical record" requires a human approval step before it lands in any output, with a deceased-historical vs. potentially-living flag on every person entity. Designed in once, then it's automatic.

---

## 6. Where it lives

`~/dev/palimpsest/`, sibling to `~/dev/ml-pipeline`, importing/reading ml-pipeline's node registry where Step 0 confirms it's safe to. **Storage:** gonktop's 30GB internal cannot hold a multi-hundred-GB corpus + indexes — decide in Cowork between (a) external SSD on gonktop as canonical store (cleaner, self-contained brain) vs. (b) corpus on M4's Thunderbolt SSD with gonktop mmap-ing it. Recommend (a). Settle before harvesting; painful to move later.

---

## 7. Open questions to resolve in Cowork

1. **ml-pipeline internals** — the Step 0 reconcile (Section 1).
2. **OpenNet mechanics** — exact advanced-search query/params for `NV*` accession; how reliably full-text is present for NV docs vs. metadata-only; pagination/result-cap behavior.
3. **Storage target** — Section 6 decision.
4. **Embedding model** — bge-small vs. nomic-embed vs. other; must run well on Metal.
5. **Slice selection** — which NV sub-collection is the ~2–5K-doc phase-1 target.

---

## 8. Sequence

1. Step 0 recon → reconcile note.
2. Storage + slice decisions.
3. Harvester → pull the slice.
4. Ingest daemon (M4) → OCR + redaction-marks + entities into the store.
5. Index + redaction-gap join (gonktop).
6. MCP server + investigator skill.
7. Drive `find_redaction_gaps`; verify ≥1 provable de-redaction against source pages.
8. **Kill-or-scale decision.** A verifiable hit → generalize to the other five finding-types and add the orchestrator. Zero hits → reassess detection approach before spending more.

The orchestrator is deliberately last. We don't coordinate a workload whose unit-shape we haven't validated.
