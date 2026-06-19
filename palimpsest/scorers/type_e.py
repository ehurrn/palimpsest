# palimpsest/scorers/type_e.py
"""Type e scorer — regulatory violation citation.

Extracted from palimpsest/indexer.py::run_violation_join().
See specs/FINDING-TYPES.md §Type e for the detector and corroboration rule.

Scoring formula (per reg_cite entity):
    Temporal violation (doc_year < reg effective_year): base = 0.70
    Otherwise:                                           base = 0.65
    +0.10 per additional corroborating reg_cite entity on the same page (cap 0.95)
"""
from __future__ import annotations

import logging
import sqlite3

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)


class TypeEScorer:
    """Type e: pages citing a regulation scored as violation candidates."""

    type_key = "type_e"
    candidates_table = "violation_candidates"

    def top(self, conn: sqlite3.Connection, limit: int = 20, doc_ids: list[str] | None = None) -> list[Candidate]:
        """Return the top-scoring violation candidates from the DB."""
        if doc_ids is not None and not doc_ids:
            return []

        base = """
            SELECT doc_id, page_no, reg_id, reg_cite_entity_id,
                   doc_year, violation_type, score
            FROM violation_candidates
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
                query = base + f" WHERE doc_id IN ({placeholders}) ORDER BY score DESC LIMIT ?"
                params = chunk + [limit]
                all_rows.extend(conn.execute(query, params).fetchall())

            seen = set()
            rows = []
            for row in sorted(all_rows, key=lambda r: r["score"], reverse=True):
                k = (row["doc_id"], row["page_no"], row["reg_cite_entity_id"])
                if k not in seen:
                    seen.add(k)
                    rows.append(row)
                    if len(rows) >= limit:
                        break

        results: list[Candidate] = []
        for row in rows:
            results.append(Candidate(
                type_key=self.type_key,
                score=row["score"],
                doc_ids=[row["doc_id"]],
                page_refs=[f"{row['doc_id']} p.{row['page_no']}"],
                summary=f"{row['violation_type']}: {row['doc_id']} p.{row['page_no']} (score={row['score']:.2f})",
                entity_ids=[row["reg_cite_entity_id"]] if row["reg_cite_entity_id"] else [],
            ))
        return results

    def run(self, conn: sqlite3.Connection, cfg: Config) -> list[Candidate]:
        threshold = float(cfg.gapjoin.get("score_threshold", 0.65))

        regs: dict[int, dict] = {
            int(row["reg_id"]): {
                "citation": str(row["citation"]),
                "effective_date": row["effective_date"],
                "effective_year": int(str(row["effective_date"])[:4]) if row["effective_date"] else None,
            }
            for row in conn.execute(
                "SELECT reg_id, citation, effective_date FROM regulation_citations"
            ).fetchall()
        }

        if not regs:
            logger.warning("TypeEScorer: no regulations seeded — run db.py migrate first.")
            return []

        rows = conn.execute("""
            SELECT e.entity_id, e.doc_id, e.page_no, e.norm, d.year AS doc_year
            FROM entities e
            JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.kind = 'reg_cite'
        """).fetchall()

        inserted = 0
        candidates: list[Candidate] = []

        for row in rows:
            entity_id = row["entity_id"]
            doc_id = row["doc_id"]
            page_no = row["page_no"]
            doc_year = row["doc_year"]
            cite_norm = row["norm"]

            matched_reg_id = None
            for reg_id, reg in regs.items():
                if reg["citation"].lower() in cite_norm.lower() or cite_norm.lower() in reg["citation"].lower():
                    matched_reg_id = reg_id
                    break

            if matched_reg_id is None:
                continue

            reg = regs[matched_reg_id]
            reg_year = reg["effective_year"]

            if doc_year and reg_year and doc_year < reg_year:
                base_score = 0.70
                violation_type = "pre_regulation"
            else:
                base_score = 0.65
                violation_type = "possible_violation"

            corroborating = conn.execute("""
                SELECT COUNT(*) FROM entities
                WHERE doc_id = ? AND page_no = ? AND kind = 'reg_cite' AND entity_id != ?
            """, (doc_id, page_no, entity_id)).fetchone()[0]
            score = min(base_score + corroborating * 0.10, 0.95)

            if score < threshold:
                continue

            with conn:
                conn.execute("""
                    INSERT OR IGNORE INTO violation_candidates
                      (doc_id, page_no, reg_id, reg_cite_entity_id, doc_year, violation_type, score, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate')
                """, (doc_id, page_no, matched_reg_id, entity_id, doc_year, violation_type, score))
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
                    candidates.append(Candidate(
                        type_key="type_e",
                        score=score,
                        doc_ids=[doc_id],
                        page_refs=[f"{doc_id} p.{page_no}"],
                        summary=f"{violation_type}: {cite_norm!r} on {doc_id} p.{page_no} (score={score:.2f})",
                        entity_ids=[entity_id],
                    ))

        logger.info(f"TypeEScorer: {inserted} new candidates (threshold={threshold}).")
        return candidates
