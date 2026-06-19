# palimpsest/scorers/type_d.py
"""Type d scorer — outcome suppression gap.

Extracted from palimpsest/indexer.py::run_outcome_gap().
See specs/FINDING-TYPES.md §Type d for the detector and corroboration rule.

Scoring formula (per protocol_code norm):
    base = 0.70  (initiation doc found, no outcome doc)
    +0.15 if initiation doc has a future_ref outcome_ref entity
    +0.10 if current_year > start_year + 5  (overdue per IRB norms)
    capped at 0.95
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from collections import defaultdict

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)


class TypeDScorer:
    """Type d: protocol_code in initiation doc with no outcome doc found."""

    type_key = "type_d"
    candidates_table = "outcome_gap_candidates"

    def top(self, conn: sqlite3.Connection, limit: int = 20, doc_ids: list[str] | None = None) -> list[Candidate]:
        """Return the top-scoring outcome gap candidates from the DB."""
        if doc_ids is not None and not doc_ids:
            return []

        base = """
            SELECT protocol_code, initiation_doc_id, start_year,
                   future_ref_entity_id, score
            FROM outcome_gap_candidates
        """

        if doc_ids is None:
            query = base + " ORDER BY score DESC LIMIT ?"
            rows = conn.execute(query, [limit]).fetchall()
        else:
            chunk_size = 900
            all_rows = []
            for i in range(0, len(doc_ids), chunk_size):
                chunk = doc_ids[i:i+chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                query = base + f" WHERE initiation_doc_id IN ({placeholders}) ORDER BY score DESC LIMIT ?"
                params = chunk + [limit]
                all_rows.extend(conn.execute(query, params).fetchall())

            seen = set()
            rows = []
            for row in sorted(all_rows, key=lambda r: r["score"], reverse=True):
                k = (row["protocol_code"], row["initiation_doc_id"])
                if k not in seen:
                    seen.add(k)
                    rows.append(row)
                    if len(rows) >= limit:
                        break

        results: list[Candidate] = []
        for row in rows:
            eids = [row["future_ref_entity_id"]] if row["future_ref_entity_id"] else []
            results.append(Candidate(
                type_key=self.type_key,
                score=row["score"],
                doc_ids=[row["initiation_doc_id"]],
                page_refs=[row["initiation_doc_id"]],
                summary=f"Protocol {row['protocol_code']!r}: initiation doc found, no outcome doc (score={row['score']:.2f})",
                entity_ids=eids,
            ))
        return results

    def run(self, conn: sqlite3.Connection, cfg: Config) -> list[Candidate]:
        threshold = float(cfg.gapjoin.get("score_threshold", 0.65))
        current_year = int(datetime.datetime.now().year)

        rows = conn.execute("""
            SELECT e.norm AS pc_norm, e.doc_id, d.year AS doc_year
            FROM entities e
            JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.kind = 'protocol_code'
            GROUP BY e.norm, e.doc_id
        """).fetchall()

        if not rows:
            logger.info("TypeDScorer: no protocol_code entities found — skipping.")
            return []

        pc_docs: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            pc_docs[str(row["pc_norm"])].append({
                "doc_id": str(row["doc_id"]),
                "doc_year": row["doc_year"],
            })

        inserted = 0
        candidates: list[Candidate] = []

        for pc_norm, doc_entries in pc_docs.items():
            initiation_entries = []
            for entry in doc_entries:
                date_check = conn.execute(
                    "SELECT 1 FROM entities WHERE doc_id = ? AND kind = 'date' LIMIT 1",
                    (entry["doc_id"],),
                ).fetchone()
                if date_check:
                    initiation_entries.append(entry)

            if not initiation_entries:
                continue

            outcome_docs = conn.execute("""
                SELECT COUNT(*) FROM entities e2
                WHERE e2.kind = 'outcome_ref'
                  AND e2.norm LIKE 'outcome_ind:%'
                  AND e2.doc_id IN (
                      SELECT doc_id FROM entities
                      WHERE kind = 'protocol_code' AND norm = ?
                  )
            """, (pc_norm,)).fetchone()

            if outcome_docs is not None and outcome_docs[0] > 0:
                continue

            for entry in initiation_entries:
                doc_id = entry["doc_id"]
                start_year = entry["doc_year"]

                future_ref_row = conn.execute("""
                    SELECT entity_id FROM entities
                    WHERE doc_id = ? AND kind = 'outcome_ref' AND norm LIKE 'future_ref:%'
                    LIMIT 1
                """, (doc_id,)).fetchone()
                future_ref_entity_id = future_ref_row["entity_id"] if future_ref_row else None

                score = 0.70
                if future_ref_entity_id is not None:
                    score += 0.15
                if start_year is not None and current_year > start_year + 5:
                    score += 0.10
                score = min(score, 0.95)

                if score >= threshold:
                    with conn:
                        conn.execute("""
                            INSERT OR IGNORE INTO outcome_gap_candidates
                              (protocol_code, initiation_doc_id, start_year, future_ref_entity_id, score)
                            VALUES (?, ?, ?, ?, ?)
                        """, (pc_norm, doc_id, start_year, future_ref_entity_id, score))
                        if conn.execute("SELECT changes()").fetchone()[0]:
                            inserted += 1
                            eids = [future_ref_entity_id] if future_ref_entity_id else []
                            candidates.append(Candidate(
                                type_key="type_d",
                                score=score,
                                doc_ids=[doc_id],
                                page_refs=[doc_id],
                                summary=f"Protocol {pc_norm!r}: initiation doc found, no outcome doc (score={score:.2f})",
                                entity_ids=eids,
                            ))

        logger.info(f"TypeDScorer: {inserted} candidate(s) (threshold={threshold}).")
        return candidates
