# palimpsest/scorers/type_c.py
"""Type c scorer — anonymous identity linkage via batched vector embeddings.

Extracted from palimpsest/indexer.py::run_identity_link() and _edit_distance().
See specs/FINDING-TYPES.md §Type c for the detector and corroboration rule.

Scoring strategy:
    - Build a composite profile string per entity (type, org context, dosage context, year).
    - Embed all unique profile strings in batches of 100 via an injectable embed_fn.
    - Shard comparisons by decade (±1 decade window) for efficiency.
    - Score pairs by cosine similarity; insert pairs above threshold.

IDENTITY GATE: Results are stored in identity_link_candidates but only surfaced
via `review links` after the named person entity has status='approved' AND
living_status='deceased_historical' in review_queue.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from typing import Callable

from palimpsest.config import Config
from palimpsest.scorers.base import Candidate

logger = logging.getLogger(__name__)


def _edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings (case-insensitive).

    Args:
        a: First string.
        b: Second string.

    Returns:
        Integer edit distance.
    """
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


def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python, no numpy).

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine similarity in [-1.0, 1.0], or 0.0 if either vector is zero.
    """
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _profile(
    kind_label: str,
    org_norms: list[str],
    dosage_norms: list[str],
    doc_year: int | None,
) -> str:
    """Build a composite profile string for embedding.

    Args:
        kind_label: Human-readable entity type label.
        org_norms: Normalized organisation names on the same page.
        dosage_norms: Normalized dosage values on the same page.
        doc_year: Document year, or None if unknown.

    Returns:
        A single space-joined sentence string.
    """
    parts = [f"Entity type: {kind_label}."]
    if org_norms:
        parts.append(f"Organization: {', '.join(org_norms)}.")
    if dosage_norms:
        parts.append(f"Dosage context: {', '.join(dosage_norms)}.")
    if doc_year is not None:
        parts.append(f"Year: {doc_year}.")
    return " ".join(parts)


def _make_ollama_embed(model: str) -> Callable[[list[str]], list[list[float]]]:
    """Create an embed function that calls the local Ollama /api/embed endpoint.

    Args:
        model: Ollama model name to use for embeddings.

    Returns:
        A callable that takes a list of strings and returns a list of float vectors.
    """
    import httpx

    client = httpx.Client(timeout=60.0)

    def _embed(texts: list[str]) -> list[list[float]]:
        resp = client.post(
            "http://localhost:11434/api/embed",
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]

    return _embed


class TypeCScorer:
    """Type c: link anonymous subject_ref pages to named-person pages via vector similarity."""

    type_key = "type_c"
    candidates_table = "identity_link_candidates"

    def __init__(
        self,
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        """Initialise the scorer.

        Args:
            embed_fn: Optional embedding function. If None, a default Ollama
                client is constructed at run() time using cfg.embed["model"].
        """
        self._embed_fn = embed_fn

    def top(
        self,
        conn: sqlite3.Connection,
        limit: int = 20,
        doc_ids: list[str] | None = None,
    ) -> list[Candidate]:
        """Return the top-scoring identity-link candidates from the DB.

        Args:
            conn: Open SQLite connection with row_factory set.
            limit: Maximum number of results to return.
            doc_ids: If provided, filter to candidates referencing these doc IDs.

        Returns:
            List of Candidate objects ordered by score descending.
        """
        if doc_ids is not None and not doc_ids:
            return []

        base = """
            SELECT subject_doc_id, subject_page, subject_ref,
                   named_doc_id, named_page, named_entity_id, score
            FROM identity_link_candidates
        """

        if doc_ids is None:
            query = base + " ORDER BY score DESC LIMIT ?"
            rows = conn.execute(query, [limit]).fetchall()
        else:
            chunk_size = 400
            all_rows = []
            for i in range(0, len(doc_ids), chunk_size):
                chunk = doc_ids[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                query = (
                    base
                    + f" WHERE (subject_doc_id IN ({placeholders})"
                    f" OR named_doc_id IN ({placeholders}))"
                    + " ORDER BY score DESC LIMIT ?"
                )
                params = chunk + chunk + [limit]
                all_rows.extend(conn.execute(query, params).fetchall())

            seen: set[tuple] = set()
            rows = []
            for row in sorted(all_rows, key=lambda r: r["score"], reverse=True):
                k = (
                    row["subject_doc_id"],
                    row["subject_page"],
                    row["subject_ref"],
                    row["named_doc_id"],
                    row["named_entity_id"],
                )
                if k not in seen:
                    seen.add(k)
                    rows.append(row)
                    if len(rows) >= limit:
                        break

        results: list[Candidate] = []
        for row in rows:
            eids = [row["named_entity_id"]] if row["named_entity_id"] else []
            results.append(
                Candidate(
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
                )
            )
        return results

    def run(self, conn: sqlite3.Connection, cfg: Config) -> list[Candidate]:
        """Execute the Type-C scorer and persist candidates.

        Args:
            conn: Open SQLite connection with row_factory set.
            cfg: Application config (uses cfg.gapjoin["score_threshold"] and
                 cfg.embed["model"] when embed_fn is None).

        Returns:
            List of Candidate objects for newly inserted rows.
        """
        threshold = float(cfg.gapjoin.get("score_threshold", 0.65))

        embed_fn = self._embed_fn
        if embed_fn is None:
            embed_fn = _make_ollama_embed(cfg.embed["model"])

        # ------------------------------------------------------------------
        # 1. Two aggregation queries — no per-entity round-trips
        # ------------------------------------------------------------------
        subject_rows = conn.execute("""
            SELECT
                e.entity_id, e.doc_id, e.page_no, e.text AS subj_ref,
                d.year AS doc_year,
                COALESCE((SELECT JSON_GROUP_ARRAY(norm) FROM entities
                          WHERE doc_id=e.doc_id AND page_no=e.page_no AND kind='org'), '[]') AS org_norms,
                COALESCE((SELECT JSON_GROUP_ARRAY(norm) FROM entities
                          WHERE doc_id=e.doc_id AND page_no=e.page_no AND kind='dosage'), '[]') AS dosage_norms
            FROM entities e
            JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.kind = 'subject_ref'
              AND EXISTS (SELECT 1 FROM entities e2
                          WHERE e2.doc_id=e.doc_id AND e2.page_no=e.page_no
                            AND e2.kind IN ('org','dosage','date'))
        """).fetchall()

        if not subject_rows:
            logger.info("TypeCScorer: no qualifying subject pages — skipping.")
            return []

        logger.info("TypeCScorer: %d subject page(s) to match.", len(subject_rows))

        person_rows = conn.execute("""
            SELECT
                e.entity_id, e.doc_id, e.page_no, e.text AS person_text,
                d.year AS doc_year,
                COALESCE((SELECT JSON_GROUP_ARRAY(norm) FROM entities
                          WHERE doc_id=e.doc_id AND page_no=e.page_no AND kind='org'), '[]') AS org_norms,
                COALESCE((SELECT JSON_GROUP_ARRAY(norm) FROM entities
                          WHERE doc_id=e.doc_id AND page_no=e.page_no AND kind='dosage'), '[]') AS dosage_norms
            FROM entities e
            JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.kind = 'person'
        """).fetchall()

        if not person_rows:
            logger.info("TypeCScorer: no named-person entities — skipping.")
            return []

        # ------------------------------------------------------------------
        # 2. Build profile strings and dedup cache
        # ------------------------------------------------------------------
        # dict: profile_string -> vector (filled after embedding)
        profile_cache: dict[str, list[float]] = {}

        def _row_profile(row: sqlite3.Row, kind_label: str) -> str:
            org_norms: list[str] = json.loads(row["org_norms"])
            dosage_norms: list[str] = json.loads(row["dosage_norms"])
            doc_year: int | None = row["doc_year"]
            return _profile(kind_label, org_norms, dosage_norms, doc_year)

        # Collect all unique profile strings
        subj_profiles: list[str] = []
        for row in subject_rows:
            p = _row_profile(row, "subject reference")
            subj_profiles.append(p)
            profile_cache.setdefault(p, [])

        person_profiles: list[str] = []
        for row in person_rows:
            p = _row_profile(row, "named person")
            person_profiles.append(p)
            profile_cache.setdefault(p, [])

        # ------------------------------------------------------------------
        # 3. Batched embedding (chunk=100), dedup ensures each string once
        # ------------------------------------------------------------------
        unique_strings = list(profile_cache.keys())
        for i in range(0, len(unique_strings), 100):
            batch = unique_strings[i : i + 100]
            vecs = embed_fn(batch)
            for s, v in zip(batch, vecs):
                profile_cache[s] = v

        # Attach vectors back to rows, with explicit int|None for doc_year
        subjects_with_vecs: list[dict] = []
        for row, prof in zip(subject_rows, subj_profiles):
            year_raw = row["doc_year"]
            subjects_with_vecs.append({
                "entity_id": row["entity_id"],
                "doc_id": row["doc_id"],
                "page_no": row["page_no"],
                "subj_ref": row["subj_ref"],
                "doc_year": int(year_raw) if year_raw is not None else None,
                "vec": profile_cache[prof],
            })

        persons_with_vecs: list[dict] = []
        for row, prof in zip(person_rows, person_profiles):
            year_raw = row["doc_year"]
            persons_with_vecs.append({
                "entity_id": row["entity_id"],
                "doc_id": row["doc_id"],
                "page_no": row["page_no"],
                "doc_year": int(year_raw) if year_raw is not None else None,
                "vec": profile_cache[prof],
            })

        # ------------------------------------------------------------------
        # 4. Decade-sharded cosine comparison
        # ------------------------------------------------------------------
        def _decade(row: dict) -> int | None:
            y: int | None = row["doc_year"]
            if y is None:
                return None
            return (y // 10) * 10

        subjects_by_decade: dict[int | None, list[dict]] = defaultdict(list)
        for row in subjects_with_vecs:
            subjects_by_decade[_decade(row)].append(row)

        persons_by_decade: dict[int | None, list[dict]] = defaultdict(list)
        for row in persons_with_vecs:
            persons_by_decade[_decade(row)].append(row)

        inserted = 0
        candidates: list[Candidate] = []

        for subj_decade, subj_list in subjects_by_decade.items():
            # Collect comparison persons from decade-1, decade, decade+1 (or all if None)
            if subj_decade is None:
                cand_persons = [p for ps in persons_by_decade.values() for p in ps]
            else:
                cand_persons = []
                for d in (subj_decade - 10, subj_decade, subj_decade + 10):
                    cand_persons.extend(persons_by_decade.get(d, []))

            for subj in subj_list:
                for person in cand_persons:
                    # Skip same-page pairs
                    if subj["doc_id"] == person["doc_id"] and subj["page_no"] == person["page_no"]:
                        continue

                    score = _cosine(subj["vec"], person["vec"])
                    if score < threshold:
                        continue

                    with conn:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO identity_link_candidates
                              (subject_doc_id, subject_page, subject_ref,
                               named_doc_id, named_page, named_entity_id, score)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                subj["doc_id"],
                                subj["page_no"],
                                subj["subj_ref"],
                                person["doc_id"],
                                person["page_no"],
                                person["entity_id"],
                                score,
                            ),
                        )
                        if conn.execute("SELECT changes()").fetchone()[0]:
                            inserted += 1
                            candidates.append(
                                Candidate(
                                    type_key="type_c",
                                    score=score,
                                    doc_ids=[subj["doc_id"], person["doc_id"]],
                                    page_refs=[
                                        f"{subj['doc_id']} p.{subj['page_no']}",
                                        f"{person['doc_id']} p.{person['page_no']}",
                                    ],
                                    summary=(
                                        f"Subject ref '{subj['subj_ref']}' linked to person entity "
                                        f"{person['entity_id']} (score={score:.2f})"
                                    ),
                                    entity_ids=[person["entity_id"]],
                                )
                            )

        logger.info("TypeCScorer: %d new candidate(s) (threshold=%s).", inserted, threshold)
        return candidates
