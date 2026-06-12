# TASK-10 — Phase-1 Verification Run (the kill-or-scale evidence)

**This task is operations, not code (one small script). It produces the evidence for the plan §8 decision. Human (Eric) is in the loop at three marked points.**

## Depends on
Everything (TASK-00 … TASK-09 done; slice chosen by human from TASK-00b facet info; OSTI bulk-terms email sent/answered per HUMAN_DO_THIS.md).

## Deliverable
`reports/phase1-verification.md` + `palimpsest/preflight.py` (the checks script).

## Sequence

### 0. Preflight (`python -m palimpsest.preflight`) — write this script
Checks, each printed PASS/FAIL: config loads; storage root mounted + writable + ≥ 200GB free; DB migrated at current schema_version; broker `/status` reachable; M4 worker heartbeat seen < 5 min; Ollama models respond on M4 (and M5 if docked) with warm latency < 3s; Vision OCR available on ≥ 1 node; spaCy model loadable; FAISS index loadable-or-absent-cleanly. Exit nonzero on any FAIL.

### 1. Pilot: 50 docs end-to-end  **[HUMAN GATE 1: green-light slice + pilot]**
```
harvester catalog --query <slice> --limit 50
harvester fetch --limit 50
# wait for queue drain (broker /status)
indexer build && indexer gapjoin && indexer stats
```
Record in the report: per-stage timing (docs/hour OCR on M4, with/without M5), error counts by type, redactions found per doc, entities per doc, OCR-source split (osti/vision/tesseract). **Sanity thresholds:** if < 20% of pilot docs have ≥ 1 redaction marker, or entity extraction is obviously garbage on manual inspection of 5 docs, STOP — fix detection before scaling to the slice. (A redaction-poor pilot may also mean the slice is wrong — flag, don't auto-proceed.)

### 2. Full slice (~2–5K docs)  **[HUMAN GATE 2: confirm timing projection acceptable]**
Same commands, no --limit caps. Expect (plan §3): OCR dominates, 7–14h single-Mac; M5 drains extraction overnight when docked. Monitor via broker /status; the run must survive M5 disappearing (verify in report: note at least one M5 undock/redock and that its leased jobs were reaped + re-leased).

### 3. Gap join + review  **[HUMAN GATE 3: the actual verification]**
```
indexer gapjoin && indexer stats
python -m palimpsest.review gaps
```
Human opens BOTH purl source pages for each high-score candidate and judges. Success = ≥ 1 `verified` gap candidate: a specific redaction in doc A whose content is established by doc B, both pages cited.

### 4. Report (`reports/phase1-verification.md`)
Sections: Pipeline stats (every number from step 1–2); Gap-join yield (candidates by decile, by method); Verified findings (each: claim, both citations, reviewer, screenshots/page refs) — **persons masked per the §8 rule even here unless approved**; Rejected-candidate analysis (top 10 near-misses and failure reasons); Kill-or-scale recommendation with reasoning.

## Acceptance
- [ ] preflight.py exists, all checks PASS pasted.
- [ ] Report exists with all five sections populated from a real run.
- [ ] ≥ 1 verified gap OR a substantive zero-yield analysis (which detection stage starved the join: no redactions found? no entities? no cross-doc anchor overlap? candidates all sub-threshold?). "It didn't work" without localization = task not done.

## Out of scope
Scaling to 500K, the other five finding-types, Lane A orchestrator, publishing anything externally.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
