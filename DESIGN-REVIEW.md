# Palimpsest Phase 1 — Design Review

*Reviewer: Claude (Manager). Reviewed against: `palimpsest-phase1-plan.md`. Date: 2026-06-12.*

This review evaluates the Phase 1 plan as a system design and records the corrections and concretizations baked into the accompanying spec packets (`specs/`). Where the plan left a decision open, the specs lock a default and flag it; defaults are cheap to change before code exists and expensive after.

## Verdict

The plan is sound at the strategic level. The two-lane split (mesh for orchestration, dedicated batch lane for grind), the kill-or-scale gate on a single provable de-redaction, and the orchestrator-last sequencing are all correct calls. The funnel math is grounded in real benchmarks rather than wishes. The non-negotiable gates (provenance, identity HITL) are stated early, which is where they belong.

What the plan lacks is implementation-level precision — which is exactly what a less capable worker model needs and exactly what the spec packets supply. Below: the specific gaps found and how the specs resolve them.

## Findings and corrections

### F1 — SQLite over the network will corrupt. Single-writer broker required. (Critical)

The plan says "persistent SQLite-backed job queue on gonktop" with workers on M4/M5 draining it, but never says *how* remote workers touch the database. SQLite over NFS/SMB is a well-known corruption path (file locking is unreliable across network filesystems), and even a shared mount invites it.

**Resolution (specs/TASK-02):** Only one process ever opens the database: the **broker** on gonktop (WAL mode, single writer). Workers on M4/M5 interact exclusively through a small HTTP JSON API (`lease / heartbeat / complete / fail / enqueue / status`) and fetch PDFs from the broker's file endpoint. All artifact and DB writes happen broker-side. This also gives us idempotency and lease-expiry reassignment (handles M5's intermittency) for free in one place.

### F2 — Apple Vision OCR is not callable from plain Python

`VNRecognizeTextRequest` is a macOS framework API. A worker model told "use Apple Vision" will flounder. **Resolution (specs/TASK-05):** use the `ocrmac` PyPI package (pyobjc wrapper over Vision) as the primary path, `pytesseract` as the cross-platform fallback, selected by capability flag. Exact output JSON schema specified, including the coordinate-system normalization (Vision returns bottom-left-origin normalized coords; the spec mandates conversion to top-left-origin, normalized 0–1 — an off-by-orientation bug here would silently break redaction-box/text adjacency joins later).

### F3 — "Adjacent to a redaction marker" is undefined

The entire gap join hinges on this and the plan never defines it. **Resolution (specs/TASK-07):** adjacency = same line as marker, or within ±2 lines / ±300 characters of reading-order text, or (for black-box regions) text lines whose bboxes fall within a vertical band of 1.5× line-height around the box. Window sizes are config values, not magic numbers in code.

### F4 — Gap-join scoring needs a formula, not an intent

"Rank by contextual similarity + corroboration strength" is not implementable by a small model. **Resolution (specs/TASK-07):** explicit two-stage candidate generation (anchor-entity co-occurrence ∪ embedding top-k) and a fixed linear score: `0.5·cosine(context, candidate_chunk) + 0.3·anchor_overlap + 0.2·slot_kind_prior`, threshold 0.65, all weights in config. The formula is a starting point — the point is that it's deterministic, tunable, and logged per-candidate so a human can see *why* something ranked.

### F5 — Provenance must be structural, not behavioral

The plan states the invariant; specs enforce it in the schema: a `gap_candidates` row *cannot exist* without foreign keys to a specific redaction (doc A, page, bbox) and a specific clear-text entity occurrence (doc B, page, bbox). The MCP server composes citations from those joins; there is no code path that emits a finding without two source pages. An invariant a weak model can't violate beats one it's asked to remember.

### F6 — Identity HITL needs a mechanism, not a flag

**Resolution (specs/TASK-09):** every `person` entity carries `living_status ∈ {unknown, potentially_living, deceased_historical}`, default `unknown`, and `unknown` is treated as `potentially_living`. Any MCP output that would surface a person not marked `deceased_historical`-and-approved is masked to a stable pseudonym (`PERSON-0042`). Approvals happen via a local CLI (`palimpsest.review`) on gonktop — deliberately *not* exposed as an MCP write tool, so no agent can approve its own disclosure. The review queue is a table, the gate is a WHERE clause: automatic, as the plan demands.

### F7 — Black-box redactions are an image problem, not a text problem

Exemption stamps and "[deleted]" are regex-able from OCR text; solid black boxes are not. **Resolution (specs/TASK-06):** render page via PyMuPDF → OpenCV: threshold, rectangular contour detection, min-area and aspect filters, and the key disambiguator — a dark rectangle that *overlaps no recognized text* is a redaction candidate; one that does is probably a table rule or figure. Tunable thresholds in config; expect noise; the gap join tolerates false-positive boxes because they simply produce no corroborating match.

### F8 — Harvester resilience and OSTI etiquette

Plan covers throttling. Specs add: resumability (catalog row written before download; downloads idempotent by `doc_id`; SHA-256 recorded), exponential backoff honoring 429/503 + `Retry-After`, a hard kill-switch on repeated 403s (sign of a block — stop, don't fight), an honest User-Agent string with contact email, and a standing instruction to prefer OSTI's existing full-text/OCR where present (`has_fulltext`) before burning local OCR hours. The plan's "email opennet@osti.gov for bulk terms" becomes a `HUMAN_DO_THIS.md` item.

### F9 — OpenNet search mechanics are unverified

The plan's purl pattern and advanced-search parameters are assumptions. A worker model must not guess URLs into existence. **Resolution:** TASK-00b is a bounded probe task — manually fetch one known document, confirm the purl pattern, capture the real query-string parameters for `NV*` accession filtering and pagination behavior, and write them into `specs/CONFIRMED-OPENNET.md` before TASK-03 (harvester) is allowed to start. Same pattern as the ml-pipeline recon: verify, then build.

### F10 — Storage decision: locked to option (a)

Specs assume **external SSD on gonktop as canonical store**, mounted at a single configurable root (`storage.root` in `config.toml`); every other path derives from it. Rationale, as the plan suggested: self-contained brain, one backup target, no cross-machine mmap fragility, and the broker-mediated I/O model (F1) means the Macs never need the volume mounted at all. If recon overturns this, one config line changes, not the architecture.

### F11 — Embedding model: locked to `nomic-embed-text` via Ollama

Resolves open question 4. Reasons: Ollama is already deployed on every node (no new runtime), Metal-accelerated, 768-dim output, strong retrieval quality at this scale, 8K context tolerance for sloppy chunking. bge-small would also work; the tiebreaker is operational (one runtime to manage, and the worker daemon already speaks Ollama's API). FAISS `IndexFlatIP` on gonktop — at ~5K docs / ~200K chunks, brute-force inner product is milliseconds; no ANN index complexity warranted in Phase 1.

### F12 — Warm-model economics enforced mechanically

The plan's warm-model requirement becomes: workers set `keep_alive: "24h"` on every Ollama call and the daemon pings its model every 5 minutes. Cold-start (10–19s) only ever paid once per daemon lifetime.

## Risks accepted, explicitly

- **OCR quality ceiling.** 1950s-60s typewritten carbons + microfilm scans will defeat some pages regardless of engine. Mitigation is the funnel itself: garbage pages produce no entities, cost nothing downstream. Not solved, just bounded.
- **NER on period documents.** spaCy's stock model will miss period-specific entities; the regex layer (dosages, CAL/CHI/HP codes) carries the load for the entity kinds that matter most to the gap join. Acceptable for Phase 1; a fine-tune is a scale-phase question.
- **The slice may yield zero.** By design. The kill-gate is the feature.
- **Single broker = single point of failure.** Accepted: gonktop is always-on, the queue is durable SQLite, workers reconnect with backoff. HA is not a Phase 1 problem.

## What I'd revisit at scale (post-kill-gate)

Queue → something with real fan-out (or just keep SQLite — 500K docs is still fine for a single-writer broker, honestly); FAISS flat → IVF/HNSW; spaCy → fine-tuned NER; add the Lane A orchestrator; and revisit storage if the corpus outgrows one SSD.

## Spec-packet design notes (why they look the way they do)

Written for less capable / local worker models, therefore: every packet is self-contained (restates the contracts it touches rather than assuming the worker holds the architecture in head); every interface is given as literal signatures, schemas, and example payloads; every packet ends in **acceptance tests phrased as commands with expected output** — the worker doesn't decide what "done" means; ambiguity is converted to config keys with locked defaults; "out of scope" sections fence each task so workers don't wander; and the blocker protocol (stop → `HUMAN_DO_THIS.md` → next task) is restated in every packet because worker models don't reliably carry global instructions across contexts.

Build order and the dependency graph are in `specs/00-ARCHITECTURE.md`.
