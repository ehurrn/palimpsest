# Palimpsest Phase 4 — Evaluation & Trust Gate: Implementation Plan

> **For agentic workers (agy / gemini):** execute the TASK packets below **one at
> a time, in order**. Each packet is self-contained, test-driven, and ends in
> acceptance commands with expected output. Do not start a packet until the
> previous one's tests are green. Commit after each packet (the packet says when
> and with what message). Append start/finish to `WORK-LOG.md`; write any blocker
> to `HUMAN_DO_THIS.md` and move on. Steps use `- [ ]` checkboxes for tracking.

**Goal:** Add a precision-first trust gate to Palimpsest: a synthetic evaluation
harness that calibrates each finding-type's score into a confidence, and an
enforcement layer that withholds findings below a calibrated precision bar from
the MCP/report surface (Iron Rule #4).

**Architecture:** A new `palimpsest/eval/` package generates synthetic
documents with known answers (types a/b/c, including negative-control and decoy
cases), runs the **unmodified** production scorers against an **isolated** eval
database, grades the output, fits a per-type calibration (PAV isotonic +
Wilson-lower-bound threshold), and enforces a confidence tier at
`server.py`'s surfacing boundary. Type e is deferred; types d/f are out of scope.

**Tech stack:** Python 3.13, `sqlite3`, `faiss`, `numpy`, `pytest`, `ruff`, `uv`.
**No new third-party dependencies** — PAV isotonic and the Wilson interval are
hand-rolled (~30 lines) rather than pulling in scikit-learn.

**Design (source of truth):** `specs/EVAL-TRUST-GATE.md`. If a packet ever
contradicts it, that file wins.

---

## Pre-flight (do before TASK-11)

1. Clear any stale git lock on the host: `rm -f .git/HEAD.lock` (a sandbox left
   one behind; harmless to remove on the Mac).
2. Commit the current working-tree baseline (the code-review fixes:
   `load_approved_person_ids` N+1 fix, scorer `top()`/`candidates_table`). The
   plan is grounded in these versions — `TASK-18` assumes `server.py` already has
   `load_approved_person_ids`. A clean checkout without them will not match.
3. Confirm green start state: `uv run pytest -q` (≈150 passing).

## Tasks (in dependency order)

| # | Packet | Builds | Depends on |
|---|--------|--------|------------|
| 1 | `TASK-11-eval-schema-config.md` | schema v7 (eval tables + gate columns) + `[eval]` config | — |
| 2 | `TASK-12-eval-isolation-embedding.md` | isolated eval DB, deterministic lexical embedding, synthetic FAISS index | 11 |
| 3 | `TASK-13-eval-generator-ab-oracle.md` | type a/b case generator + pure grading oracle | 12 |
| 4 | `TASK-14-eval-generator-c.md` | type c generator (decoy + answer-absent) | 13 |
| 5 | `TASK-15-eval-runner-cli.md` | runner (cases→DB→real scorers→grade) + `palimpsest-eval` CLI | 13, 14 |
| 6 | `TASK-16-eval-calibration.md` | PAV isotonic + Wilson threshold + `calibration.json` + `calibrate` CLI | 15 |
| 7 | `TASK-17-eval-metrics-report.md` | per-type metrics + markdown report (mandatory disclosure) + `report` CLI | 16 |
| 8 | `TASK-18-trust-gate-enforcement.md` | gate + `server.py` enforcement (Iron Rule #4) | 16 |
| 9 | `TASK-19-eval-verification-run.md` | end-to-end acceptance gate | all |

## Per-packet protocol

- Read the whole packet before writing code.
- Follow its TDD steps exactly — including the "run the test, verify it FAILS"
  steps. Do not write implementation before its failing test.
- Use the exact file paths, code, and commit messages given. The code blocks are
  complete; do not substitute placeholders.
- After the packet's own commit, run the global gate before the next packet:
  `uv run pytest -q && uv run ruff check palimpsest tests`. Both must be clean.
- Append a start line and a finish line to `WORK-LOG.md` for each packet.

## Definition of done

TASK-11…19 committed; full suite green; `ruff` clean; `palimpsest-eval
run | calibrate | report` works end to end on the lexical stub; the trust gate
returns surfaceable-only by default in `server.py`. The **only** remaining item
is REAL calibration, which is blocked on Ollama (`nomic-embed-text` on the M4)
and tracked in `HUMAN_DO_THIS.md` — stub thresholds are PLUMBING-ONLY and must
not be used to surface real findings.

## Important guardrails (don't "simplify" these away)

- The synthetic generators use **per-case-unique anchors** (a/b) and **years
  spaced ≥3 apart** (c). All cases share one eval DB and the scorers match across
  every document in it; collapsing to fixed anchors/years makes cases cross-link
  and corrupts grading. (See the notes in TASK-13 / TASK-14.)
- The trust gate is **additive to the identity gate**. It runs after masking and
  can only drop or annotate a finding — never unmask a person.
- Run the real scorers unchanged. The harness never re-implements scoring.
