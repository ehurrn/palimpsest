# Offline Development Handoff

This document is the briefing for local Ollama models continuing Palimpsest
development when cloud session limits are reached. Models run on the **MacBook
Pro (m4)**; the broker and production DB live on **gonktop** (192.168.0.58).

---

## 1. Environment

| Machine | Role | Ollama URL |
|---------|------|------------|
| MacBook Pro (m4) | Development, local worker | http://localhost:11434 |
| gonktop (192.168.0.58) | Broker (8077), DB, primary worker | http://192.168.0.58:11434 |

**Python runtime** — always use `uv run` (never `python` or `python3`):
```bash
uv run python -m palimpsest.db migrate
uv run pytest -x -q
uv run ruff check palimpsest/
uv run ty check palimpsest/<file>.py
```

**Git sync**: `git pull origin main` / `git push origin main`
Remote: `git@github.com:ehurrn/palimpsest.git`
`config.toml` is gitignored — each host keeps its own (never overwrite).

---

## 2. Local Models (MacBook)

| Model | Role |
|-------|------|
| `qwen3.6-heretic-27b:latest` | Lead architect / code generation |
| `gemma-4-12b-it-abliterated:latest` | Instruction-following, code review |
| `granite-4.1-8b-claude-opus-thinking:latest` | Step-by-step planning, debugging |
| `lfm2-8b-qwen3.6-distill:latest` | Fast edits, unit test writing |
| `nomic-embed-text:latest` | Chunk embeddings (used by pipeline) |

Check Ollama is live and models are warm before starting:
```bash
ollama ps                          # what's loaded in GPU memory
ollama list                        # all installed models
curl -s http://localhost:11434/api/tags | python3 -m json.tool
```

---

## 3. Current State

- **Phase 2 active.** Phase 1 proved cross-document de-redaction (Common Rule
  §46/§219 violations). Phase 2 generalises to six finding-types.
- **Identity gate is enforced.** All person entities default to
  `potentially_living`. No name is surfaced without HITL approval or the
  document-age heuristic (doc age > 75 years OR doc_year − birth_year > 100).
- **Schema**: v3 in production — `regulation_citations` + `violation_candidates`
  tables live. Run `uv run python -m palimpsest.db migrate` if starting fresh.
- **Worker on gonktop** is running, draining features jobs (755 queued when
  last checked). Worker command:
  ```bash
  ssh herren@192.168.0.58 'tail -20 /tmp/palimpsest-worker.log'
  ```

---

## 4. Before Starting Work

Read these in order:
1. `WORK-LOG.md` — chronological log; write an entry when you start and finish.
2. `TODO.md` — next prioritised tasks.
3. `palimpsest-phase2-plan.md` — Phase 2 roadmap.
4. `specs/FINDING-TYPES.md` — six finding-type taxonomy (a–f) with detectors,
   corroboration rules, and identity-gate requirements.

**WORK-LOG protocol** — agy (Gemini CLI) also writes to this file. Always read
it before claiming a task to avoid duplication.

---

## 5. Safety Invariants (never violate)

1. **Provenance**: every de-redaction cites the source doc_id + page for both
   the redacted passage and the corroborating passage.
2. **Identity gate**: no plaintext person name in output unless
   `living_status = 'deceased_historical'` AND `status = 'approved'` in
   `review_queue`, OR the heuristic subcommand clears it.
3. **No DB access off-gonktop**: workers never open the SQLite file directly;
   all mutations go through the broker at 192.168.0.58:8077.

---

## 6. Completed (do not redo)

- Task 1–10: full pipeline (harvester → OCR → features → embed → gapjoin → review CLI → MCP server → preflight).
- Phase 2 safety revert: bulk-approval of 5 291 persons and 1 474 gaps undone; identity gate re-enforced.
- Heuristic gate: birth-year/doc-date heuristic added as `review heuristic` subcommand.
- **Type e** (regulatory-violation): `reg_cite` entity kind, 7 regex patterns,
  `normalize_reg_cite()`, `run_violation_join()` + `violationjoin` CLI in
  `indexer.py`, schema v3 migration, 5 tests. 74 tests total, all green.

---

## 7. Next Tasks (priority order)

### Type f — Document-Series Suppression

1. Add `seq_ref` entity kind in `palimpsest/tasks/features.py`:
   - Patterns: `\bNV[-\s]?\d{7}\b`, `\bReport\s+No\.?\s*\d+\b`
2. Add `run_series_gap()` in `palimpsest/indexer.py` + `seriesgap` subcommand:
   - Query catalog for known accession ranges; flag gaps > 20%.
   - Require ≥1 flanking document (N−1 or N+1) to reference the missing number.
3. Tests in `tests/test_series.py`. Run full suite — all must pass.
4. `uv run ruff check` + `uv run ty check` on edited files — zero errors.

### Type b — Undisclosed Radiation Dosage

1. Add `subject_ref` entity kind in `features.py`:
   - Pattern: `\b(?:Subject|Patient|Case|Individual)\s+[A-Z0-9]+\b`
2. Add proximity dosage scorer: co-occurrence of `dosage` + `subject_ref` on
   same page within `redaction_context_chars` of a `black_box` or
   `deleted_text` redaction → type-b candidate.
3. Tests in `tests/test_dosage.py`.

### After each task

```bash
uv run pytest -x -q          # all tests green
git add -p                   # stage relevant changes only
git commit                   # follow existing commit style
git push origin main
# update WORK-LOG.md
```
