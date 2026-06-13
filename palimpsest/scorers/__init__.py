# palimpsest/scorers/__init__.py
"""Scorer registry.

Import all scorer classes here so callers only need::

    from palimpsest.scorers import SCORERS
    scorer = SCORERS["type_e"]()

Entries are added lazily to avoid circular imports — each type_*.py module
registers itself via _register() at module load time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from palimpsest.scorers.base import Scorer

# Registry populated by _register() calls at the bottom of this file.
SCORERS: dict[str, type[Scorer]] = {}


def _register(key: str, cls: type[Scorer]) -> None:
    SCORERS[key] = cls


# --- Register all scorers ---
from palimpsest.scorers.type_a import TypeAScorer  # noqa: E402
from palimpsest.scorers.type_b import TypeBScorer  # noqa: E402
from palimpsest.scorers.type_c import TypeCScorer  # noqa: E402
from palimpsest.scorers.type_d import TypeDScorer  # noqa: E402
from palimpsest.scorers.type_e import TypeEScorer  # noqa: E402
from palimpsest.scorers.type_f import TypeFScorer  # noqa: E402

_register("type_a", TypeAScorer)
_register("type_b", TypeBScorer)
_register("type_c", TypeCScorer)
_register("type_d", TypeDScorer)
_register("type_e", TypeEScorer)
_register("type_f", TypeFScorer)

__all__ = [
    "SCORERS",
    "TypeAScorer",
    "TypeBScorer",
    "TypeCScorer",
    "TypeDScorer",
    "TypeEScorer",
    "TypeFScorer",
]
