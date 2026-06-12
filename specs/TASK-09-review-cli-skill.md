# TASK-09 — HITL Review CLI + Investigator Skill

**Read `specs/00-ARCHITECTURE.md` §8 (masking rule) and DESIGN-REVIEW.md F5/F6. The review CLI is the ONLY write path for identity disclosure. It is deliberately not an MCP tool.**

## Objective
Two deliverables: (1) `palimpsest/review.py`, a local CLI on gonktop for human review of person-identity disclosures and gap-candidate verification; (2) `skills/palimpsest-investigator/SKILL.md`, the methodology document that drives an investigating agent through the MCP tools.

## Depends on
TASK-07 (review_queue populated), TASK-08 (tools the skill references).

## Deliverables
```
palimpsest/review.py
skills/palimpsest-investigator/SKILL.md
tests/test_review.py
```

## Spec — review.py (plain stdlib CLI, no TUI frameworks)

```
python -m palimpsest.review people            # interactive queue
python -m palimpsest.review people --list     # non-interactive dump
python -m palimpsest.review gaps              # verify/reject gap candidates
python -m palimpsest.review audit             # decision log
```

### `people` (interactive)
For each pending review_queue item show: pseudonym, entity text (REAL name — this is the human's screen, the one place it appears pre-approval), kind, all occurrences with doc/page citations + purl URLs, the gap candidate context that triggered review, and any date entities co-occurring on those pages (helps the living/deceased judgment). Prompt:
```
[a]pprove as deceased_historical / [d]eny / [s]kip / [q]uit:
```
- approve ⇒ `entities.living_status='deceased_historical'` for ALL entity rows sharing that `norm` (not just this occurrence) + review_queue `approved`, `decided_by` (prompt for initials once per session), `decided_at`.
- deny ⇒ review_queue `denied`; living_status set to `potentially_living` for that norm. Denied stays masked forever unless re-queued manually.
- Every decision appended to `{root}/db/review_audit.jsonl`: `{review_id, norm_hash (sha256 of norm, not the norm itself), decision, decided_by, decided_at}`.

### `gaps`
For each `gap_candidates` row status `candidate` ordered by score desc: show score components, redaction context + citation, clear-entity context + citation (REAL text — human screen), purl URLs for BOTH pages. Prompt `[v]erify / [r]eject / [s]kip / [q]uit` → status `verified`/`rejected` + reviewed_by/at/notes (optional note prompt). **A `verified` gap with both purls opened and confirmed by the human IS the Phase-1 success condition.**

### Writes
Local DB, WAL, short transactions (per 00-ARCHITECTURE §1 amended rule — review.py runs only on gonktop).

## Spec — SKILL.md
Frontmatter: `name: palimpsest-investigator`, description triggering on "investigate redactions / find de-redactions / palimpsest corpus questions". Body must contain, in this order:

1. **Provenance invariant (verbatim, top of file):** "Every claim you output must carry (doc_id, page_no, purl) for EVERY document it rests on. A de-redaction claim requires TWO citations: the redacted page and the clear page. If you cannot cite it, you do not say it. Findings without citations must be discarded, not hedged."
2. **Identity rule:** pseudonyms (PERSON-NNNN) are never to be 'worked around'; never attempt to infer or reconstruct a masked identity from context; flag wanted disclosures via the review queue and tell the human to run `python -m palimpsest.review people`.
3. **Methodology loop:** queue_status → find_redaction_gaps (start min_score 0.75, lower stepwise to 0.65) → for each candidate: get_document both pages, read context, judge whether the clear text plausibly fills the redaction slot (kind, span length, syntax) → corroborate with get_entity across corpus → write up only candidates surviving all checks, each as: claim, redacted-page citation, clear-page citation, confidence (high/medium), reasoning in two sentences.
4. **Negative results are results:** if nothing survives, report the strongest rejected candidates and WHY they failed — that drives the kill-or-scale decision.
5. **Output format:** a findings file `findings/YYYY-MM-DD-<slug>.md` with a Citations table.

## Acceptance (paste output)
```
python -m pytest tests/test_review.py -q
```
Tests (seeded tmp DB, monkeypatched stdin): approve propagates living_status to all rows with same norm; deny → potentially_living; audit line written with hashed norm (assert real norm NOT in audit file); gap verify sets status+reviewer; quit mid-queue leaves remaining pending.
Manual: run `review people` against a seeded DB, paste a session transcript (fake data).

## Out of scope
Auto-classification of living status (Phase 2, if ever), MCP write access, publishing/report generation beyond the skill's findings format.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
