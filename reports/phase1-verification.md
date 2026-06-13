# Phase-1 Verification Report

> **Status**: _In progress — populate each section after each human gate._

---

## 0. Preflight

Run on gonktop before beginning:

```
python -m palimpsest.preflight
```

Paste full output here once run:

```
[paste preflight output]
```

---

## 1. Pilot Run — 50 Documents

**Slice chosen**: `[e.g., NV* 1960–1965, keyword: dosimetry]`  
**Human gate 1 sign-off**: `[initials + date]`

### Commands run

```bash
python -m palimpsest.harvester catalog --query "[query]" --limit 50
python -m palimpsest.harvester fetch --limit 50
# wait for queue drain — watch: curl http://gonktop.local:8077/status
python -m palimpsest.indexer build
python -m palimpsest.indexer gapjoin
python -m palimpsest.indexer stats
```

### Per-stage timings

| Stage | Docs | Duration | Rate | Notes |
|-------|------|----------|------|-------|
| Catalog | — | — | — | — |
| Fetch | — | — | — | — |
| OCR (M4) | — | — | docs/hr | osti/vision/tesseract split: — |
| Features | — | — | — | — |
| Embed | — | — | — | — |
| Gapjoin | — | — | — | — |

### Sanity check

- Docs with ≥ 1 redaction marker: **—** / 50 (_≥ 20% required_)
- Manual inspection of 5 docs:
  1. `[doc_id]` — [notes]
  2. `[doc_id]` — [notes]
  3. `[doc_id]` — [notes]
  4. `[doc_id]` — [notes]
  5. `[doc_id]` — [notes]
- Error counts by type: _none_ / `[paste from broker /status]`

**Pilot decision**: `[GO / STOP — reason if stopping]`

---

## 2. Full Slice Run

**Human gate 2 sign-off (timing projection acceptable)**: `[initials + date]`

Estimated based on pilot rate: `[N]` docs × `[rate]` docs/hr = ~`[hours]` h OCR.

```bash
python -m palimpsest.harvester catalog --query "[query]"
python -m palimpsest.harvester fetch
# monitor: curl http://gonktop.local:8077/status
python -m palimpsest.indexer build
python -m palimpsest.indexer gapjoin
python -m palimpsest.indexer stats
```

### Pipeline stats (full slice)

| Metric | Value |
|--------|-------|
| Total docs cataloged | — |
| Total docs fetched | — |
| Total pages OCR'd | — |
| OCR source split (osti/vision/tesseract) | — |
| Total redactions found | — |
| Total entities found | — |
| Total gap candidates | — |
| M5 undock/redock events observed | — |
| Jobs reaped + re-leased after M5 undock | — |
| Total errors (dead jobs) | — |

### Gap-join yield

| Score decile | Count | Method breakdown |
|-------------|-------|-----------------|
| 0.9–1.0 | — | — |
| 0.8–0.9 | — | — |
| 0.7–0.8 | — | — |
| 0.65–0.7 | — | — |

---

## 3. Gap Candidate Review

**Human gate 3 sign-off**: `[initials + date]`

```bash
python -m palimpsest.indexer stats
python -m palimpsest.review gaps
```

### Verified Findings

> Persons are masked per Architecture §8 unless approved via `python -m palimpsest.review people`.

#### Finding 1 _(if any)_

**Claim**: `[e.g., The blacked-out dosage on doc A page N is "15 rem" — established by the unredacted version of the same table in doc B.]`

**Confidence**: High / Medium

**Reasoning**: _Sentence 1 describing the positive match evidence. Sentence 2 describing why alternative interpretations are implausible._

| Doc ID | Page No | Accession | Title | PURL | Role |
|--------|---------|-----------|-------|------|------|
| — | — | — | — | — | Redacted Source |
| — | — | — | — | — | Corroborating Source |

**Reviewed by**: `[initials]` on `[date]`

---

## 4. Rejected Candidate Analysis (Top Near-Misses)

| Rank | Gap ID | Score | Failure Reason |
|------|--------|-------|---------------|
| 1 | — | — | — |
| 2 | — | — | — |
| 3 | — | — | — |
| 4 | — | — | — |
| 5 | — | — | — |
| 6 | — | — | — |
| 7 | — | — | — |
| 8 | — | — | — |
| 9 | — | — | — |
| 10 | — | — | — |

If zero gap candidates survived:

- **Detection stage that starved the join**: `[no redactions found / no entities / no cross-doc anchor overlap / all candidates sub-threshold]`
- **Root cause analysis**: _[description]_

---

## 5. Kill-or-Scale Recommendation

**Recommendation**: `[SCALE TO FULL CORPUS / KILL — fix X before retry / PAUSE — needs human decision on Y]`

**Reasoning**:

_[2–4 sentences. Reference specific numbers from sections 1–4. State what evidence confirms Phase-1 success or what specifically failed and what would be required to proceed.]_
