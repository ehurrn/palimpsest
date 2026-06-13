# Palimpsest — Phase 2 Build Plan

*Successor to `palimpsest-phase1-plan.md`. Phase 1 proved the premise; Phase 2 scales it without breaking the gates that make it publishable.*

**Target corpus:** DoE OSTI OpenNet, full NV* accession series (Nevada Test Site, ~500K docs)
**Phase 1 outcome:** **SCALE.** The redaction-gap pipeline produced two provable, fully-cited de-redactions (Common Rule §219/§46 citations) from a 536-doc slice, with 1,246 gap candidates and 0 dead jobs once the cluster was tuned. See `reports/phase1-verification.md`.
**Phase 2 goal:** Generalize from one finding-type to all six, stand up the orchestrator lane, and scale the harvester to the full NV* series — while *re-establishing* the identity HITL gate that bulk-approval has currently bypassed.

---

## 0. Carry-over before any scaling

These block a clean Phase 2 start and are listed in `TODO.md` / `HUMAN_DO_THIS.md`. Resolve first.

- **[BLOCKER — safety] Reinstate the identity HITL gate.** The current working tree records a *bulk approval of all 5,258 person entities* and *bulk verification of all 1,474 gap candidates*. This directly violates Architecture Iron Rule #3 ("No person surfaced without HITL approval") and the Phase 1 plan's non-negotiable identity gate. The two *verified* Phase 1 findings are regulatory-citation text (safe to publish), but a blanket unmask of every person — including any flagged `potentially_living` — must not flow into any output. **Action:** revert the bulk approval, re-segregate `potentially_living` from `deceased_historical`, and re-run review per-entity (or at minimum re-mask everything not individually signed off). No Phase 2 output ships until this is restored. *(See §5.)*
- **[infra] Repair Ollama on M4.** Missing `llama-server` binary / local 500 on embed. Restore local embedding so `embed` can be re-enabled for `m4` in `config.toml` instead of leaning on M5 + gonktop.
- **[infra] OCR coverage on gonktop.** Tesseract was the Linux-side fallback; confirm it is installed everywhere a worker may run so OCR jobs don't go dead at scale.
- **[human] Bulk-download terms.** Email opennet@osti.gov for NV* bulk-research terms and the rate limits to honor *before* pulling the full series. Already in `HUMAN_DO_THIS.md`.

---

## 1. The six finding-types

Phase 1 built only the spear-tip: **de-redaction by corroboration** (covers categories *a* + *f*), because it is the most verifiable and needs no LLM to "understand" the physics. Phase 2 generalizes the same funnel (triage → filter → extract → corroborate, every claim self-proving via source pages) to the remaining finding-types.

For each new finding-type, the work is: define its **detector** (what marks/entities/patterns flag a candidate), its **corroboration rule** (what counts as a second independent citation), and its **scoring**, then plug it into the existing `features` → `indexer`/gapjoin path. Reuse the schema; do **not** fork the pipeline per type. Where a type surfaces medical-subject or personal data (notably category *e*), the identity gate in §5 is mandatory before any surfacing.

> **Note:** `specs/00-ARCHITECTURE.md` enumerates the finding-type taxonomy referenced as a/e/f. Before building, write a short `specs/FINDING-TYPES.md` pinning down all six with one detector + one corroboration rule each, so the generalization is grounded rather than invented.

---

## 2. Orchestrator lane (Lane A)

Phase 1 deliberately deferred the orchestrator — "we don't coordinate a workload whose unit-shape we haven't validated." It is now validated.

- Configure the orchestrator agent lane on the mesh broker (`192.168.0.58:8766` per `TODO.md`), reusing `ml-pipeline`'s `send_message` routing as-is for routing decisions, light agentic extraction, and HITL prompts.
- Keep Lane A (chat/orchestration) and Lane B (batch grind, the Palimpsest broker on `:8077`) strictly separate, as the Phase 1 plan mandates. Lane A drives investigations through the read-only MCP server (`:8078`); it never writes the DB.
- The `palimpsest-investigator` skill (already built, TASK-09) is the methodology Lane A runs.

---

## 3. Harvester scaling

- Scale `harvester.py` from the ~1,000-doc pilot catalog to the full NV* accession series. Keep the hard throttle (1–2 req/s + backoff on 429/503) and idempotent downloads (disk + SHA-256).
- Gate the first full pull on the bulk-terms reply from OSTI (§0).
- Storage: the Phase 1 verification ran against `~289 GB` free; the full series is multi-hundred-GB with indexes. Confirm the canonical store (external SSD on gonktop, per Phase 1 §6 recommendation) has headroom before harvesting, and that FAISS index growth fits gonktop RAM — shard the index if not.

---

## 4. Throughput at corpus scale

Phase 1 timings extrapolate roughly linearly: OCR ~1,110 docs/hr on the Macs, embed/features/gapjoin far faster. The real cost is LLM extraction on the filtered subset (M5 ≈ 11s/doc). At 50K-doc subsets this is multi-day overnight batch — keep the funnel aggressive (filter hard before extract) and keep M5 draining when docked. Re-measure on the first 5K full-series batch before committing to a full-corpus run; don't trust linear extrapolation past one order of magnitude.

---

## 5. Non-negotiable gates (unchanged from Phase 1, re-asserted)

- **Provenance invariant.** No claim without a document-ID + page citation. A finding *is* its source pages.
- **Identity HITL gate.** Every person entity carries `deceased_historical` vs `potentially_living`. Only individually-approved `deceased_historical` persons may be surfaced unmasked. **Bulk approval defeats this gate and is prohibited** — Phase 2 reinstates per-entity review (§0). "The government withheld that X happened" is always publishable; "here is a possibly-living individual's record" is not, absent sign-off.
- **Read-only MCP.** The server (`:8078`) never mutates. Only `review.py` (local CLI on gonktop) writes approvals, and the broker owns the DB.

---

## 6. Sequence

1. **Carry-over (§0):** reinstate identity gate → repair M4 Ollama / OCR coverage → confirm bulk-download terms.
2. **Pin the taxonomy:** write `specs/FINDING-TYPES.md` (all six, detector + corroboration each).
3. **Generalize detectors:** add the next finding-type to `features` + gapjoin; verify ≥1 provable, fully-cited hit per type on a slice before moving to the next. One type at a time.
4. **Stand up Lane A:** orchestrator on the mesh, driving the investigator skill through the MCP server.
5. **Scale harvester:** full NV* series pull (gated on OSTI terms + storage headroom).
6. **Re-measure throughput** on the first 5K full-series batch; adjust node/workload map.
7. **Per-type kill-or-scale:** a verifiable hit → keep; zero hits for a type → reassess that detector before spending more. Same cheap-validation discipline as Phase 1.

---

## 7. Open questions to resolve in Cowork

1. **Finding-type taxonomy** — exact definition + corroboration rule for the five not yet built.
2. **Identity-gate remediation** — revert-and-re-review vs. re-mask-then-review; who signs off `deceased_historical` at scale (5,258+ persons is not hand-reviewable one-by-one — need a defensible bulk *rule*, e.g. document date + birth-year heuristic, not a blanket approve).
3. **Index sharding** — does the full-corpus FAISS index still fit gonktop RAM, or shard by sub-collection?
4. **Mesh integration depth** — how much of `ml-pipeline`'s registry Lane A reuses vs. duplicates.
5. **Corpus boundary** — full NV* (~500K) in one pass, or sub-collection at a time keyed to a finding-type?
