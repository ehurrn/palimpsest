# Offline Development Handoff & Instructions

This document directs offline coding models (running locally via Ollama) on how to resume and continue development on the Palimpsest project when session limits are reached.

## 1. Local Model Mapping & Capabilities

Map these specific locally-installed models to the following development roles:

- **Lead Architect / Code Generator / Reasoning Agent**:
  - `qwen3.6-heretic-27b:latest` (Best overall coding and structural reasoning)
  - `gemma-4-12b-it-abliterated:latest` (Highly capable instruction-following and coding)
  - `granite-4.1-8b-claude-opus-thinking:latest` (Best for step-by-step logic planning and debugging)
- **Fast Coding / Unit Test Helper**:
  - `lfm2-8b-qwen3.6-distill:latest` (Fast turnaround on code edits and test writing)
  - `l3.2-rogue-dark-horror-7b:latest` (Lightweight syntax completions)
- **Text Embeddings**:
  - `nomic-embed-text:latest` (Target model for chunk indexing)

---

## 2. Project Context & Current State

The goal of Palimpsest is systematic redacted text recovery from the DoE OSTI OpenNet database (NV* accession series).

### Active Status:
- Phase 1 completed successfully, proving cross-document de-redaction (Common Rule citations).
- Phase 2 is currently active.
- The primary production database resides on **gonktop** at `/home/herren/palimpsest-data/db/palimpsest.db`.
- The identity safety gate has been reverted (pending reviews reinstated, bulk-approvals undone) to enforce Iron Rule #3 (no unmasking without individual HITL or heuristic verification).

### Blocker Status:
- **Local M4 Ollama**: Currently returning `500 Internal Server Error` on embeddings. The Ollama process is running in memory, but `Ollama.app` has been deleted from the filesystem, preventing the launch of `llama-server`.
- **OCR Fallback**: Tesseract is installed and verified on `gonktop` but missing on the local machine.

---

## 3. Active Handoff Files

Before starting work, the offline agent must read:
1. `WORK-LOG.md` — Current chronological progress.
2. `TODO.md` — Next tasks.
3. `palimpsest-phase2-plan.md` — Phase 2 roadmap.
4. `.agents/explorer_r1_r2/handoff.md` — Environment diagnostics and database metrics.
5. `specs/FINDING-TYPES.md` — The newly drafted specification of the six finding-types.

---

## 4. Safety Invariants to Enforce

1. **Provenance Invariant**: No de-redaction is valid without explicit citation to a Document ID and page number for both the redacted source and the clear corroborating source.
2. **Identity HITL Gate**: Do not output plaintext names of potentially living subjects. All person entities default to `potentially_living` and must be pseudonymized as `PERSON-XXXX` unless individually verified or meeting the document-age heuristic (doc age > 75 years, or doc year - birth year > 100 years).

---

## 5. Completed Tasks

- **Task 1: M4 Ollama Reinstallation & Setup**: Clean installation of `ollama-app` completed via Homebrew cask. Zombie memory processes killed.
  - *Note*: Ensure the Ollama GUI app is launched, models are warmed up, and re-enable the `embed` capability for `m4` in `config.toml` once embedding requests succeed.
- **Task 2: Heuristic Safety Gate & Regulatory-Violation Scorer (Type e)**:
  - Birth-year/document-date safety heuristic implemented and added as `heuristic` subcommand to `review.py` (verified by `tests/test_review.py:test_heuristic_classification`).
  - Regulatory-violation citation detector and database schema v3 migration completed (verified by `tests/test_violation.py:test_violation_join`).
  - All 74 unit tests passing.

---

## 6. Next Execution Tasks (Todo for Offline Models)

Direct the local Ollama model to resume with these tasks:

### Task 1: Implement Document-Series Suppression Detector (Type f)
1. **Define Regex Patterns**: Add sequence-number regex patterns in `palimpsest/tasks/features.py` as entity kind `seq_ref` to match accession number or sequence patterns (e.g., `NV\d{7}`, `NV-\d+`, `Report No. \d+`).
2. **Implement Gap Analyzer**: In `palimpsest/indexer.py` (or a new module/subcommand), write an analyzer that scans the cataloged sequences for missing numbers (gaps > 20%), verifies if flanking documents reference the missing number, and inserts them as Type-f gap candidates.
3. **Write Unit Tests**: Add tests in `tests/test_series.py` verifying gap detection, flanking references, and candidate scoring. Ensure all tests run and pass.

### Task 2: Implement Undisclosed Radiation Dosage (Type b)
1. **Identify Subject References**: Add subject-reference pattern regexes (e.g., `Subject [A-Z\d]+` or `Patient [A-Z\d]+`) in `features.py` as entity kind `subject_ref`.
2. **Implement Proximity Dosage Scorer**: In the gapjoin scorer, write logic to match dosage values with subjects in other documents based on close-proximity event details.
3. **Write Unit Tests**: Add tests in `tests/test_dosage.py` and run the suite.
