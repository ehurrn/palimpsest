# palimpsest/scorers/base.py
"""Base types for the Palimpsest scorer registry.

Every scorer must implement the Scorer protocol so the Lane A orchestrator
can invoke any type uniformly:

    scorer = SCORERS["type_e"]()
    scorer.run(conn, cfg)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from palimpsest.config import Config


@dataclass
class Candidate:
    """A de-redaction candidate produced by a scorer.

    Fields
    ------
    type_key   : Scorer type label, e.g. "type_a".
    score      : Confidence in [0.0, 1.0].
    doc_ids    : Document accession IDs that support this candidate.
    page_refs  : Human-readable page citations, e.g. ["NV0001234 p.3"].
    summary    : One-line description of the finding.
    entity_ids : Database entity_id values related to this candidate (may be empty).
    """

    type_key: str
    score: float
    doc_ids: list[str]
    page_refs: list[str]
    summary: str
    entity_ids: list[int] = field(default_factory=list)


@runtime_checkable
class Scorer(Protocol):
    """Protocol that every scorer class must satisfy."""

    #: Name of the SQLite table this scorer writes its candidates to.
    #: The orchestrator reads it to count candidates and report progress.
    candidates_table: str

    def run(self, conn: sqlite3.Connection, cfg: Config) -> list[Candidate]:
        """Execute the scorer and return a (possibly empty) list of Candidates.

        Implementations are responsible for writing their results to the
        appropriate candidate table in *conn* AND returning the same data as
        Candidate objects for in-process consumers.
        """
        ...

    def top(self, conn: sqlite3.Connection, limit: int) -> list[Candidate]:
        """Return the top-scoring existing candidates from *candidates_table*.

        Reads previously persisted rows (highest score first) and reconstructs
        them as Candidate objects for the orchestrator's investigate command.
        """
        ...
