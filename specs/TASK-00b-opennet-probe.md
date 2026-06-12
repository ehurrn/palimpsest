# TASK-00b — OpenNet Mechanics Probe (NO BULK FETCHING)

**Read `specs/00-ARCHITECTURE.md` §11 (worker rules) before starting.**

## Objective
Verify, with at most ~20 polite HTTP requests total, the real mechanics of DoE OSTI OpenNet search and document retrieval. Output `specs/CONFIRMED-OPENNET.md`. TASK-03 (harvester) is BLOCKED until this file exists.

## Hard constraints
- ≤ 20 requests total, ≥ 2 seconds apart, User-Agent `palimpsest-research/0.1 (contact: j.eric.herren@gmail.com)`.
- This is a probe, not a crawl. Download at most 2 sample PDFs.
- Add to `~/dev/HUMAN_DO_THIS.md`: "Email opennet@osti.gov requesting bulk-download terms for NV* accession research; mention rate limits we should honor." (Human action; do not send anything yourself.)

## Questions to answer (template for CONFIRMED-OPENNET.md)

```markdown
# CONFIRMED: OpenNet retrieval mechanics (probed YYYY-MM-DD)

## Search
- Advanced-search URL + exact query params to filter accession number prefix 'NV':
- Params for date-range / collection facets observed:
- Pagination: param name, page size, max result cap (state observed evidence):
- Result format: HTML to scrape, or JSON/API endpoint discovered? (prefer API;
  check for an OSTI API, e.g. any /api/ or XML/JSON export links on results):

## Document retrieval
- purl pattern verified? `https://www.osti.gov/opennet/servlets/purl/{id}.pdf`
  → tested with doc_id ___ , HTTP status ___ , content-type ___ :
- Where doc_id comes from in a search result (field/attribute):
- Metadata available per result (title, accession, year, fulltext flag?):

## Full-text availability
- For 5 sampled NV records: does OSTI serve a text layer / OCR already?
  (per-doc: yes/no, how detected — embedded PDF text vs image-only):

## Failure modes observed
- Rate-limit / robots.txt / terms-of-use notes found:

## Harvester corrections
- Changes TASK-03 must make vs. its spec as written:
```

## Method
1. Fetch and READ `https://www.osti.gov/opennet/` robots.txt and any terms/AUP page first; record constraints.
2. Use the advanced-search UI once in a browser-equivalent fetch, capture the resulting query string.
3. Pick 2 doc ids from results; test the purl pattern; for one downloaded PDF, check for embedded text (`pdftotext` output length > 0 ⇒ text layer exists).
4. Cite the actual URLs used for every claim.

## Acceptance
- [ ] `CONFIRMED-OPENNET.md` exists, all sections filled or marked UNKNOWN.
- [ ] Total request count stated and ≤ 20.
- [ ] HUMAN_DO_THIS.md contains the bulk-terms email item.

## Out of scope
Bulk downloading. Writing harvester code. Choosing the Phase-1 slice (that decision needs the facet info this task produces, but is made by the human/manager).

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
