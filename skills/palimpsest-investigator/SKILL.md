---
name: palimpsest-investigator
description: Triggers on requests to investigate redactions, find de-redactions, or answer questions about the palimpsest OpenNet corpus.
---

Every claim you output must carry (doc_id, page_no, purl) for EVERY document it rests on. A de-redaction claim requires TWO citations: the redacted page and the clear page. If you cannot cite it, you do not say it. Findings without citations must be discarded, not hedged.

## Identity Rule
Pseudonyms (in the format `PERSON-NNNN` where NNNN is the entity ID) are a strict security barrier. You must never attempt to "work around" them, infer their real identities, or reconstruct them from context. If a human reviewer needs to review a name disclosure, flag the entity for review and instruct the human user to run:
```bash
python -m palimpsest.review people
```

## Methodology Loop
To systematically discover and verify de-redactions in the database, execute the following steps in order:

1. **Queue Status**: Call the `palimpsest_queue_status` tool to verify the state of the harvesting, OCR, indexing, and review queues.
2. **Find Redaction Gaps**: Call `palimpsest_find_redaction_gaps`. 
   - Start with `min_score` set to `0.75`.
   - If too few candidates are returned, lower `min_score` stepwise down to a minimum of `0.65`.
3. **Analyze Candidate Pages**: For each candidate redaction gap:
   - Call `palimpsest_get_document` for both the redacted document ID and the corroborating clear document ID.
   - Read the context around the redacted slot and the clear text entity.
   - Critically judge if the clear text plausibly fills the redacted slot based on:
     - Entity kind (e.g., name, dosage, date).
     - Text span length and spacing.
     - Grammatical and syntactical fit in the redacted sentence context.
4. **Corroborate Entity**: Call `palimpsest_get_entity` on the normalized entity name to trace other occurrences across the corpus. Check for any conflicting data or context that might invalidate the match.
5. **Write Up Findings**: Filter out all candidates that do not survive all validation checks. For each remaining high-confidence match, document:
   - The specific de-redaction claim (e.g., entity value, context).
   - Redacted-page citation (document ID, page number, PURL).
   - Clear-page citation (document ID, page number, PURL).
   - Confidence level (High or Medium).
   - A concise reasoning summary of exactly two sentences.

## Negative Results
Negative results are valuable. If no candidates survive your validation checks, report the strongest rejected candidates and details on why they failed (e.g., span length mismatch, syntactical context mismatch, contradictory dates). This information is crucial for driving project scale-up or kill decisions.

## Output Format
All verified findings must be written to a markdown report located at `findings/YYYY-MM-DD-<slug>.md` (e.g., `findings/2026-06-12-nevada-test-site-dosages.md`). The report must include a Citations table in this format:

| Doc ID | Page No | Accession | Title | PURL | Role |
|--------|---------|-----------|-------|------|------|
| [Redacted Doc ID] | [Page No] | [Accession] | [Title] | [PURL] | Redacted Source |
| [Clear Doc ID] | [Page No] | [Accession] | [Title] | [PURL] | Corroborating Source |
