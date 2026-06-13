# palimpsest/scorers/type_f.py
"""Type f scorer — document series suppression gap.

Extracted from palimpsest/indexer.py::run_series_join().
See specs/FINDING-TYPES.md §Type f for the detector and corroboration rule.

Scoring formula (per missing accession number):
    Both flanking docs reference the missing accession: 0.90
    One flanking doc references it:                     0.70
    No flanking reference:                              0.50 (below threshold, not stored)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate
from palimpsest.tasks.features import normalize

logger = logging.getLogger(__name__)


class TypeFScorer:
    """Type f: detect gaps in document series (missing accession numbers)."""

    type_key = "type_f"
    candidates_table = "series_gap_candidates"

    def top(self, conn: sqlite3.Connection, limit: int = 20) -> list[Candidate]:
        """Return the top-scoring series gap candidates from the DB."""
        rows = conn.execute("""
            SELECT series_prefix, missing_number, missing_accession,
                   flanking_doc_id, ref_entity_id, score, status
            FROM series_gap_candidates
            ORDER BY score DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results: list[Candidate] = []
        for row in rows:
            eids = [row["ref_entity_id"]] if row["ref_entity_id"] else []
            results.append(Candidate(
                type_key=self.type_key,
                score=row["score"],
                doc_ids=[row["flanking_doc_id"]] if row["flanking_doc_id"] else [],
                page_refs=[f"missing accession {row['missing_accession']}"],
                summary=f"Series gap: {row['missing_accession']} missing from {row['series_prefix']}* series (score={row['score']:.2f})",
                entity_ids=eids,
            ))
        return results

    def run(self, conn: sqlite3.Connection, cfg: Config) -> list[Candidate]:
        # Fetch all accessions
        cur = conn.execute(
            "SELECT doc_id, accession FROM documents WHERE accession IS NOT NULL AND accession != ''"
        )
        rows = cur.fetchall()

        # Group by numeric prefix
        groups: dict[str, list] = defaultdict(list)
        for row in rows:
            doc_id = row["doc_id"]
            acc = row["accession"]
            m = re.match(r'^([^\d]+)(\d+)$', acc.strip())
            if m:
                prefix = m.group(1)
                digits_str = m.group(2)
                num = int(digits_str)
                padding_len = len(digits_str)
                groups[prefix].append((num, doc_id, padding_len, acc))

        inserted = 0
        candidates: list[Candidate] = []

        for prefix, accs in groups.items():
            if not accs:
                continue
            accs.sort(key=lambda x: x[0])
            present_set = {x[0] for x in accs}
            num_map = {num: (doc_id, acc, pad_len) for num, doc_id, pad_len, acc in accs}

            min_num = min(present_set)
            max_num = max(present_set)
            total_range = max_num - min_num + 1
            if total_range <= 1:
                continue

            missing_count = total_range - len(present_set)
            gap_ratio = missing_count / total_range
            if gap_ratio <= 0.20:
                continue

            default_pad = accs[0][2]

            for num in range(min_num + 1, max_num):
                if num in present_set:
                    continue

                missing_acc = f"{prefix}{num:0{default_pad}d}"
                norm_missing_acc = normalize("seq_ref", missing_acc)

                doc_id_prev = num_map.get(num - 1, [None])[0] if (num - 1) in present_set else None
                doc_id_next = num_map.get(num + 1, [None])[0] if (num + 1) in present_set else None

                ref_prev = False
                entity_id_prev = None
                if doc_id_prev:
                    ent_row = conn.execute(
                        "SELECT entity_id FROM entities WHERE doc_id = ? AND kind = 'seq_ref' AND norm = ? LIMIT 1",
                        (doc_id_prev, norm_missing_acc),
                    ).fetchone()
                    if ent_row:
                        ref_prev = True
                        entity_id_prev = ent_row["entity_id"]

                ref_next = False
                entity_id_next = None
                if doc_id_next:
                    ent_row = conn.execute(
                        "SELECT entity_id FROM entities WHERE doc_id = ? AND kind = 'seq_ref' AND norm = ? LIMIT 1",
                        (doc_id_next, norm_missing_acc),
                    ).fetchone()
                    if ent_row:
                        ref_next = True
                        entity_id_next = ent_row["entity_id"]

                if ref_prev and ref_next:
                    score = 0.90
                elif ref_prev or ref_next:
                    score = 0.70
                else:
                    score = 0.50

                if score >= 0.65:
                    # Use prev as the primary flanking ref for DB storage
                    flanking_doc_id = doc_id_prev if ref_prev else doc_id_next
                    ref_entity_id = entity_id_prev if ref_prev else entity_id_next
                    # Only include docs that actually have the cross-reference
                    candidate_doc_ids = []
                    if ref_prev and doc_id_prev:
                        candidate_doc_ids.append(doc_id_prev)
                    if ref_next and doc_id_next:
                        candidate_doc_ids.append(doc_id_next)

                    with conn:
                        conn.execute("""
                            INSERT INTO series_gap_candidates
                              (series_prefix, missing_number, missing_accession, flanking_doc_id, ref_entity_id, score, status)
                            VALUES (?, ?, ?, ?, ?, ?, 'candidate')
                            ON CONFLICT(missing_accession) DO UPDATE SET
                              score = excluded.score,
                              flanking_doc_id = excluded.flanking_doc_id,
                              ref_entity_id = excluded.ref_entity_id
                        """, (prefix, num, missing_acc, flanking_doc_id, ref_entity_id, score))
                        if conn.execute("SELECT changes()").fetchone()[0]:
                            inserted += 1
                            eids = [ref_entity_id] if ref_entity_id else []
                            candidates.append(Candidate(
                                type_key=self.type_key,
                                score=score,
                                doc_ids=candidate_doc_ids,
                                page_refs=[f"missing accession {missing_acc}"],
                                summary=f"Series gap: {missing_acc} missing from {prefix}* series (score={score:.2f})",
                                entity_ids=eids,
                            ))

        logger.info(f"TypeFScorer: {inserted} new series gap candidates.")
        return candidates
