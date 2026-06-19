# palimpsest/scorers/type_c.py
"""Type c scorer — anonymous identity linkage.

Extracted from palimpsest/indexer.py::run_identity_link() and _edit_distance().
See specs/FINDING-TYPES.md §Type c for the detector and corroboration rule.

Scoring formula (per subject/named-person pair):
    org_match      = 1.0 if edit_distance(org_a, org_b) <= 2 else 0.0
    date_proximity = max(0, 1 - abs(year_a - year_b) / 3.0)
    dosage_bonus   = 0.2 if both pages share a normalized dosage value else 0.0
    score          = org_match * 0.5 + date_proximity * 0.3 + dosage_bonus

IDENTITY GATE: Results are stored in identity_link_candidates but only surfaced
via `review links` after the named person entity has status='approved' AND
living_status='deceased_historical' in review_queue.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)


def _edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings (case-insensitive)."""
    a, b = a.lower(), b.lower()
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


class TypeCScorer:
    """Type c: link anonymous subject_ref pages to named-person pages."""

    type_key = "type_c"
    candidates_table = "identity_link_candidates"

    def top(self, conn: sqlite3.Connection, limit: int = 20, doc_ids: list[str] | None = None) -> list[Candidate]:
        """Return the top-scoring identity-link candidates from the DB."""
        query = """
            SELECT subject_doc_id, subject_page, subject_ref,
                   named_doc_id, named_page, named_entity_id, score
            FROM identity_link_candidates
        """
        params: list = []
        if doc_ids:
            placeholders = ",".join("?" for _ in doc_ids)
            query += f" WHERE (subject_doc_id IN ({placeholders}) OR named_doc_id IN ({placeholders}))"
            params.extend(doc_ids)
            params.extend(doc_ids)
        query += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        results: list[Candidate] = []
        for row in rows:
            eids = [row["named_entity_id"]] if row["named_entity_id"] else []
            results.append(Candidate(
                type_key=self.type_key,
                score=row["score"],
                doc_ids=[row["subject_doc_id"], row["named_doc_id"]],
                page_refs=[
                    f"{row['subject_doc_id']} p.{row['subject_page']}",
                    f"{row['named_doc_id']} p.{row['named_page']}",
                ],
                summary=(
                    f"Subject ref {row['subject_ref']!r} linked to person entity "
                    f"{row['named_entity_id']} (score={row['score']:.2f})"
                ),
                entity_ids=eids,
            ))
        return results

    def run(self, conn: sqlite3.Connection, cfg: Config) -> list[Candidate]:
        threshold = float(cfg.gapjoin.get("score_threshold", 0.65))

        # --- 1. Collect qualifying subject pages ---
        subject_rows = conn.execute("""
            SELECT DISTINCT e.doc_id, e.page_no, e.text AS subj_ref, e.norm AS subj_norm
            FROM entities e
            WHERE e.kind = 'subject_ref'
              AND EXISTS (
                  SELECT 1 FROM entities e2
                  WHERE e2.doc_id = e.doc_id AND e2.page_no = e.page_no
                    AND e2.kind IN ('org', 'date', 'dosage')
              )
        """).fetchall()

        if not subject_rows:
            logger.info("TypeCScorer: no qualifying subject pages — skipping.")
            return []

        logger.info(f"TypeCScorer: {len(subject_rows)} subject page(s) to match.")

        # --- 2. Collect named-person pages ---
        named_rows = conn.execute("""
            SELECT e.entity_id, e.doc_id, e.page_no, e.text, e.norm, d.year AS doc_year
            FROM entities e
            JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.kind = 'person'
        """).fetchall()

        if not named_rows:
            logger.info("TypeCScorer: no named-person entities — skipping.")
            return []

        # Pre-fetch all org and dosage attributes to avoid O(S × N) round-trips
        named_attrs_map: dict[tuple, list] = defaultdict(list)
        for r in conn.execute("""
            SELECT doc_id, page_no, kind, norm FROM entities
            WHERE kind IN ('org', 'dosage')
        """):
            named_attrs_map[(r["doc_id"], r["page_no"])].append(r)

        inserted = 0
        candidates: list[Candidate] = []

        for subj in subject_rows:
            s_doc = subj["doc_id"]
            s_page = subj["page_no"]
            s_ref = subj["subj_ref"]

            s_attrs = conn.execute("""
                SELECT kind, norm FROM entities
                WHERE doc_id = ? AND page_no = ? AND kind IN ('org', 'date', 'dosage')
            """, (s_doc, s_page)).fetchall()

            s_orgs = [r["norm"] for r in s_attrs if r["kind"] == "org"]
            s_years: list[int] = []
            for r in s_attrs:
                if r["kind"] == "date":
                    m = re.search(r'\b(\d{4})\b', r["norm"])
                    if m:
                        s_years.append(int(m.group(1)))
            s_dosages = {r["norm"] for r in s_attrs if r["kind"] == "dosage"}

            for named in named_rows:
                if named["doc_id"] == s_doc and named["page_no"] == s_page:
                    continue

                n_doc = named["doc_id"]
                n_page = named["page_no"]
                n_eid = named["entity_id"]
                n_year = named["doc_year"]

                n_attrs = named_attrs_map[(n_doc, n_page)]
                n_orgs = [r["norm"] for r in n_attrs if r["kind"] == "org"]
                n_dosages = {r["norm"] for r in n_attrs if r["kind"] == "dosage"}

                org_match = 0.0
                if s_orgs and n_orgs:
                    best_dist = min(
                        _edit_distance(so, no)
                        for so in s_orgs
                        for no in n_orgs
                    )
                    org_match = 1.0 if best_dist <= 2 else 0.0

                date_proximity = 0.0
                if s_years and n_year is not None:
                    min_gap = min(abs(sy - n_year) for sy in s_years)
                    date_proximity = max(0.0, 1.0 - min_gap / 3.0)

                dosage_bonus = 0.2 if (s_dosages & n_dosages) else 0.0

                score = org_match * 0.5 + date_proximity * 0.3 + dosage_bonus

                if score < threshold:
                    continue

                with conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO identity_link_candidates
                          (subject_doc_id, subject_page, subject_ref,
                           named_doc_id, named_page, named_entity_id,
                           org_match, date_proximity, dosage_bonus, score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        s_doc, s_page, s_ref,
                        n_doc, n_page, n_eid,
                        org_match, date_proximity, dosage_bonus, score,
                    ))
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        inserted += 1
                        candidates.append(Candidate(
                            type_key="type_c",
                            score=score,
                            doc_ids=[s_doc, n_doc],
                            page_refs=[f"{s_doc} p.{s_page}", f"{n_doc} p.{n_page}"],
                            summary=f"Subject ref '{s_ref}' linked to person entity {n_eid} (score={score:.2f})",
                            entity_ids=[n_eid],
                        ))

        logger.info(f"TypeCScorer: {inserted} new candidate(s) (threshold={threshold}).")
        return candidates
