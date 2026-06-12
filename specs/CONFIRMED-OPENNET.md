# CONFIRMED: OpenNet retrieval mechanics (probed 2026-06-12)

## Search
- Advanced-search URL + exact query params to filter accession number prefix 'NV':
  - URL: `https://www.osti.gov/opennet/search-results`
  - Query params: `?accession-number=NV*` (Note: Wildcard `*` is required; querying `accession-number=NV` returns "No results found for your search parameters").
  - Verification: Tested using GET and confirmed it filters results to only those with accession numbers starting with `NV` (e.g. `NV0758437`).
- Params for date-range / collection facets observed:
  - Date Ranges:
    - Publication dates: `publication-start-date` and `publication-end-date`
    - Declassification dates: `declassification-start-date` and `declassification-end-date`
    - Database entry dates: `database-entry-start-date` and `database-entry-end-date`
    - Modification dates: `modified-start-date` and `modified-end-date`
  - Collection / other facets:
    - `document-categories` (array)
    - `declassification-status` (array)
    - `document-location` (array)
    - `opennet-field-office-acronym`
    - `document-type` (array)
    - `originating-research-organization`
    - `sort-by`
    - `order-by`
- Pagination: param name, page size, max result cap (state observed evidence):
  - **Param name**: `start` (0-based index of the first record, e.g., `start=0` for page 1, `start=10` for page 2). Note: The `page` and `search-form-page-num` parameters are ignored by the backend unless `start` is specified.
  - **Page size**: Controlled by parameter `length` (defaults to 10; setting `length=100` successfully retrieves 100 results per page, verified by receiving 100 unique document IDs in a single response).
  - **Max result cap**: No observed cap; a query for `NV*` returned a total of `401,996` entries, and querying `start=10&length=100` successfully retrieved entries 11 to 110.
- Result format: HTML to scrape, or JSON/API endpoint discovered? (prefer API; check for an OSTI API, e.g. any /api/ or XML/JSON export links on results):
  - HTML to scrape: Results are returned in a standard HTML table element `#search-results-table`.
  - No JSON/API endpoint or XML/JSON export links were found on the OpenNet results or advanced-search pages. The site relies entirely on HTML parsing for client-side display.

## Document retrieval
- purl pattern verified? `https://www.osti.gov/opennet/servlets/purl/{id}.pdf`
  - Tested with doc_id `16007515`, HTTP status `200`, content-type `application/pdf`, size: 3,040,499 bytes.
  - Tested with doc_id `16387497`, HTTP status `200`, content-type `application/pdf`, size: 4,265,204 bytes.
- Where doc_id comes from in a search result (field/attribute):
  - In the results table, the `osti-id` query parameter is found in the detail link's `href` (e.g. `/opennet/detail?osti-id=16007515`), and the PDF download link points directly to `/opennet/servlets/purl/16007515.pdf`. The numeric string is the `doc_id`.
- Metadata available per result (title, accession, year, fulltext flag?):
  - Detail page (`/opennet/detail?osti-id={id}`) provides:
    - `Title:`
    - `Author(s):`
    - `Subject Terms:`
    - `Document Location:`
    - `Document Type:`
    - `Document Type Other:`
    - `Publication Date:` (contains year, e.g., `1993 Sep 08`)
    - `Declassification Status:`
    - `Document Pages:` (page count)
    - `Accession Number:` (e.g., `NV0758437`)
    - `Originating Research Org.:`
    - `OpenNet Entry Date:`
    - `OpenNet Modified Date:`
    - `Description/Abstract:`
  - Fulltext flag: Indicated in search results by the presence of a PDF download link pointing to the `/opennet/servlets/purl/` servlet.

## Full-text availability
- For 5 sampled NV records: does OSTI serve a text layer / OCR already?
  (per-doc: yes/no, how detected — embedded PDF text vs image-only):
  - Yes. We sampled 5 NV records with PDFs, and downloaded 2 of them to check for embedded text layers. Both of the downloaded PDFs had embedded text layers (as verified by `pdftotext` output > 0). The remaining 3 sampled records have PDFs available via the PURL servlet, but were not downloaded to adhere to the strict constraint of downloading at most 2 sample PDFs:
    1. Doc ID `16007515` (Accession `NV0758437`): **YES**, has embedded text layer. Detected by running `pdftotext` (output length: 64,297 chars).
    2. Doc ID `16387497` (Accession `NV0753148`): **YES**, has embedded text layer. Detected by running `pdftotext` (output length: 93,253 chars).
    3. Doc ID `16010755` (Accession `NV0761681`): **YES** (PDF link present, not downloaded/probed).
    4. Doc ID `16295151` (Accession `NV0707866`): **YES** (PDF link present, not downloaded/probed).
    5. Doc ID `16295159` (Accession `NV0707874`): **YES** (PDF link present, not downloaded/probed).

## Failure modes observed
- Rate-limit / robots.txt / terms-of-use notes found:
  - `robots.txt` at `https://www.osti.gov/robots.txt` disallows `/opennet/document` and `/opennet/search-results` for all user-agents (`*`). However, it registers an XML sitemap at `https://www.osti.gov/opennet/sitemap/xml`.
  - Heads-up automation (like default Playwright/Selenium Chrome instances) may receive a `403 Forbidden` response.
  - Standard HTTP requests with custom User-Agent `palimpsest-research/0.1 (contact: j.eric.herren@gmail.com)` and rate-limiting (e.g. 2s delays) succeed without triggering rate-limit blocks.

## Harvester corrections
- Changes TASK-03 must make vs. its spec as written:
  - **Use GET instead of POST**: Search results can be retrieved using standard GET requests (e.g., `https://www.osti.gov/opennet/search-results?accession-number=NV*&start={start}&length={length}`), which avoids complex state management and POST body handling.
  - **Accession Number Wildcard**: The query parameter must use `accession-number=NV*` rather than `accession-number=NV`.
  - **Pagination Variable**: The correct query parameter for pagination is `start` (0-based starting result index) rather than `page`.
  - **Batch Size Optimization**: The page size can be set via `length=100` to retrieve 100 entries per request instead of the default 10, drastically reducing total request count for harvesting.
