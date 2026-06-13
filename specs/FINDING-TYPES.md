# Palimpsest — Finding-Type Taxonomy (Phase 2)

*Grounds the six finding-types referenced in the Phase 2 plan.  
Each entry specifies: what marks a candidate, what constitutes corroboration, and which Iron Rules apply.*

---

## Overview

All six types share the same pipeline skeleton: `features` detects candidates →
`indexer/gapjoin` scores them → `review.py` gates surfacing. The differences are
in the **detector** (what features.py extracts and flags), the **corroboration
rule** (what makes a candidate a finding), and whether the **identity HITL gate**
applies.

Types **a** and **f** are safe to publish without the identity gate (no PII).
Types **b**, **c**, **d**, **e** require individual `deceased_historical` approval
before any person is surfaced.

---

## Type a — Redacted-text corroboration

**What it is:** A passage redacted in document A can be proven from an unredacted
passage in document B, where both clearly describe the same event, measurement, or
decision.

**Detector:** `exemption_stamp` or `deleted_text` redaction marks (already
extracted by `features.py`). The surrounding `context_before` / `context_after`
window provides the embedding anchor.

**Corroboration rule:** The gapjoin score for a `clear_entity` from a *different
document* exceeds `score_threshold` AND at least one of the two scoring components
(cosine, anchor) independently exceeds 0.70. The entity must not come from the
same `doc_id` as the redaction.

**Status:** ✅ Built (Phase 1). Yielded two verified findings (Common Rule §219/§46
citations).

**Identity gate:** Not required (the finding is the text, not the person).

---

## Type b — Undisclosed radiation dosage

**What it is:** A document records that a human subject received a radiation dose
but the dose value itself is redacted (or the subject is anonymized). The actual
dose can be reconstructed from test-parameter records and proximity geometry in
other documents.

**Detector:** Co-occurrence of a `dosage` entity and a `person` entity (or an
anonymized subject reference pattern, e.g. "Subject 3", "Patient A") on the same
page, within `redaction_context_chars` of a `black_box` or `deleted_text`
redaction. Flag the (redaction, dosage, person/subject) triple as a type-b
candidate.

**Corroboration rule:** A separate document contains a `dosage` entity with the
same normalized value AND references the same test event (matched by date entity
or protocol_code entity). Score = cosine(redaction context, candidate page) ×
proximity score (dosage value match) × kind bonus (both `dosage`).

**New detector work needed:**
- Add subject-reference regex: `\b(Subject|Patient|Case|Individual)\s+[A-Z\d]+\b`
  to `features.py` as entity kind `subject_ref`.
- Add cross-document dosage deduplication to the gapjoin scorer.

**Identity gate:** Required if the subject is or may be a living person (see type c
for the identity linkage flow).

---

## Type c — Anonymous subject identity linkage

**What it is:** A subject anonymized in one document can be linked to a named
individual in another via non-identifying attributes (institution + year +
role + diagnosis pattern), without directly surfacing PII.

**Detector:** A `subject_ref` entity (from type b's regex) on a page that also
contains at least two of: an `org` entity (institution), a `date` entity (year),
a `dosage` entity (exposure record). Flag the page as a type-c candidate.

**Corroboration rule:** A second document contains a named `person` entity whose
`org` + `date` attributes match the anonymous subject's attributes (fuzzy match:
same org norm within edit distance 2, same year ± 2). The person must hold
`status = 'approved'` and `living_status = 'deceased_historical'` in `review_queue`
before the linkage is surfaced.

**New detector work needed:**
- `subject_ref` entity kind in `features.py` (shared with type b).
- Attribute-match scorer in `indexer.py` (org + date cosine, not text cosine).

**Identity gate:** Mandatory. No linkage surfaced until named person is
individually approved as `deceased_historical`.

---

## Type d — Outcome suppression gap

**What it is:** A cohort or experiment has documented initiation records but no
follow-up outcome records in the archive, where such records would be expected
under the applicable regulation (e.g., IRB requires outcome reporting). The
absence is the finding.

**Detector:** A `protocol_code` entity (e.g. `CAL-12`) that appears in an
experiment-initiation document but has no corresponding outcome document in the
catalog. Flag as a type-d candidate when a protocol_code appears in ≥1 document
with a start-date entity but in 0 documents with outcome-indicator terms
("results", "follow-up", "outcome", "mortality", "survival").

**Corroboration rule:** The initiating document explicitly references a future
report ("to be submitted", "annual report due", "follow-up study planned") AND
no document with the same protocol_code and an outcome-indicator appears in the
catalog within the expected timeframe (start year + 5 years, per IRB norms).

**New detector work needed:**
- Outcome-indicator phrase detection in `features.py` (new entity kind
  `outcome_ref` or a flag on the page).
- Cross-document protocol_code absence query in `indexer.py` (absence scoring,
  not similarity scoring — logically different from types a-c).
- New `indexer gaps` subcommand for type-d.

**Identity gate:** Not required for the absence finding itself. Required if
subject names are included in the initiation document.

---

## Type e — Regulatory-violation citation

**What it is:** A document describes a procedure that violated a regulation in
effect at the time, provable from the document date, the procedure description,
and the published regulation text.

**Detector:** A `date` entity (document or procedure date) + a regulatory
section reference (regex: `\b\d+\s*(?:CFR|U\.S\.C\.)\s*[§\s]*\d+[\.\d]*\b` or
`\b(?:Common Rule|Belmont|Helsinki)\b`) on the same page.

**Corroboration rule:** The document date predates (or the procedure violates)
the regulation's effective date OR the described procedure explicitly conflicts
with the regulation's text. The regulation text itself is the second citation
(stored as a reference document in a `regulation_citations` table, to be added
in Phase 2 schema migration).

**Status:** ✅ Partially built (Phase 1 found two Common Rule §219/§46
violations). Detector is implicit in the existing features pipeline. Phase 2
formalizes the regulation reference table and scoring.

**New detector work needed:**
- Regulation-reference regex entity kind `reg_cite` in `features.py`.
- `regulation_citations` reference table in `db.py` (schema migration v3):
  `(reg_id, citation, effective_date, text_snippet)`. Seed with Common Rule
  §46/§219, Belmont Report, Declaration of Helsinki.
- Violation scorer in `indexer.py`: date comparison + semantic similarity to
  regulation text snippet.

**Identity gate:** Not required (the finding is the procedure vs. the rule).

---

## Type f — Document-series suppression

**What it is:** A numbered document series has gaps — documents that should exist
by the series numbering convention are absent from the catalog entirely, suggesting
deliberate omission rather than non-production.

**Detector:** Accession number or internal sequence number patterns
(`NV\d{7}`, `NV-\d+`, `Report No. \d+`) extracted from document text. Flag
sequences with a gap ratio > 20% (≥1 missing number per 5 consecutive present
ones) as type-f candidates.

**Corroboration rule:** At least one flanking document (sequence N-1 or N+1)
explicitly references the missing document by number or title. Cross-reference
presence confirms the missing document was known to exist, not simply never
created.

**New detector work needed:**
- Sequence-number regex entity kind `seq_ref` in `features.py`.
- Series-gap analyzer in `indexer.py` (or a new `harvester.py` subcommand
  `gaps`): query catalog for known accession ranges, flag missing entries.

**Identity gate:** Not required.

---

## Build order for Phase 2

| Priority | Type | Gate | Shared infrastructure |
|----------|------|------|-----------------------|
| 1 | **e** (reg violation) | None | `reg_cite` entity + regulation table |
| 2 | **f** (series gap) | None | `seq_ref` entity + gap analyzer |
| 3 | **b** (dose record) | Identity if subject named | `subject_ref` entity |
| 4 | **d** (outcome gap) | None | `outcome_ref` + absence scorer |
| 5 | **c** (identity linkage) | Mandatory identity gate | Attribute matcher |

Types e and f are first because they require no identity gate and their detectors
are additive (new entity kinds dropped into the existing `features.py` → gapjoin
path). Prove ≥1 finding per type before moving on (Phase 1 discipline).

---

## Schema additions required (Phase 2)

```sql
-- Migration v3
CREATE TABLE regulation_citations (
  reg_id        INTEGER PRIMARY KEY,
  citation      TEXT NOT NULL,          -- e.g. '45 CFR 46'
  effective_date TEXT,                  -- ISO date
  text_snippet  TEXT                    -- key clause for semantic matching
);
```

New entity kinds: `subject_ref`, `outcome_ref`, `reg_cite`, `seq_ref`.
All handled by the existing `entities` table — no structural change, just new
`kind` values and new regex/spaCy patterns in `features.py`.
