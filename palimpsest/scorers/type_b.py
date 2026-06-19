# palimpsest/scorers/type_b.py
"""Type b scorer — undisclosed radiation dosage.

Type b candidates live in the same gap_candidates table as Type a.
The scoring logic is identical (run_gapjoin handles dosage proximity internally).
TypeBScorer delegates run() to TypeAScorer and filters top() to dosage-kind rows only.

See specs/FINDING-TYPES.md §Type b for the detector and corroboration rule.
"""
from __future__ import annotations
import logging
import sqlite3
from typing import Callable, List

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate
from palimpsest.scorers.type_a import TypeAScorer

logger = logging.getLogger(__name__)


class TypeBScorer:
    type_key = "type_b"
    candidates_table = "gap_candidates"   # shared with TypeA

    def __init__(self, embed_fn: Callable[[Config, str], List[float]] | None = None):
        self._type_a = TypeAScorer(embed_fn=embed_fn)

    def run(self, conn: sqlite3.Connection, config: Config) -> list[Candidate]:
        """Delegate to TypeAScorer.run() — dosage scoring is embedded there.

        Returns only the Candidate objects from this run that are dosage-kind,
        so the caller can distinguish Type b insertions from Type a insertions.
        """
        all_inserted = self._type_a.run(conn, config)
        type_b = [c for c in all_inserted if "dosage" in c.summary.lower()]
        logger.info(
            "TypeBScorer.run() complete: %d dosage candidates in this run.", len(type_b)
        )
        return type_b

    def top(self, conn: sqlite3.Connection, limit: int = 20, doc_ids: list[str] | None = None) -> list[Candidate]:
        """Return top-N dosage-kind gap candidates ordered by score DESC."""
        if doc_ids is not None and not doc_ids:
            return []

        base = (
            "SELECT gc.gap_id, gc.redaction_id, gc.clear_entity_id, gc.score, "
            "gc.method, r.doc_id AS red_doc_id, r.page_no AS red_page_no, "
            "e.doc_id AS ent_doc_id, e.page_no AS ent_page_no, "
            "e.kind, e.norm "
            "FROM gap_candidates gc "
            "JOIN redactions r ON gc.redaction_id = r.redaction_id "
            "JOIN entities e ON gc.clear_entity_id = e.entity_id "
            "WHERE gc.status = 'candidate' AND e.kind = 'dosage' "
        )

        if doc_ids is None:
            base += "ORDER BY gc.score DESC LIMIT ?"
            rows = conn.execute(base, [limit]).fetchall()
            return [self._row_to_candidate(row) for row in rows]

        chunk_size = 400
        all_rows = []
        for i in range(0, len(doc_ids), chunk_size):
            chunk = doc_ids[i:i+chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            query = base + f"AND (r.doc_id IN ({placeholders}) OR e.doc_id IN ({placeholders})) "
            query += "ORDER BY gc.score DESC LIMIT ?"
            params = chunk + chunk + [limit]
            all_rows.extend(conn.execute(query, params).fetchall())

        seen = set()
        deduped = []
        for row in sorted(all_rows, key=lambda r: r["score"], reverse=True):
            if row["gap_id"] not in seen:
                seen.add(row["gap_id"])
                deduped.append(row)
                if len(deduped) >= limit:
                    break

        return [self._row_to_candidate(row) for row in deduped]

    def _row_to_candidate(self, row: sqlite3.Row) -> Candidate:
        return Candidate(
            type_key=self.type_key,
            score=float(row["score"]),
            doc_ids=[row["red_doc_id"], row["ent_doc_id"]],
            page_refs=[
                f"{row['red_doc_id']} p.{row['red_page_no']}",
                f"{row['ent_doc_id']} p.{row['ent_page_no']}",
            ],
            summary=(
                f"Dosage gap: redaction in {row['red_doc_id']} p.{row['red_page_no']} "
                f"→ dosage '{row['norm']}' in "
                f"{row['ent_doc_id']} p.{row['ent_page_no']}, "
                f"score={row['score']:.2f}"
            ),
            entity_ids=[row["clear_entity_id"]],
        )
