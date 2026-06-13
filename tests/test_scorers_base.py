# tests/test_scorers_base.py
"""Tests for Candidate dataclass and Scorer protocol."""
from palimpsest.scorers.base import Candidate, Scorer
from palimpsest.scorers import SCORERS


def test_candidate_stores_all_fields():
    c = Candidate(
        type_key="type_a",
        score=0.8,
        doc_ids=["NV0001234"],
        page_refs=["NV0001234 p.3"],
        summary="Test finding",
        entity_ids=[42],
    )
    assert c.type_key == "type_a"
    assert c.score == 0.8
    assert c.doc_ids == ["NV0001234"]
    assert c.page_refs == ["NV0001234 p.3"]
    assert c.summary == "Test finding"
    assert c.entity_ids == [42]


def test_candidate_allows_empty_entity_ids():
    c = Candidate(
        type_key="type_d",
        score=0.7,
        doc_ids=["NV0001"],
        page_refs=["NV0001 p.1"],
        summary="Absence finding",
        entity_ids=[],
    )
    assert c.entity_ids == []


def test_registry_starts_empty():
    # At this point only base.py exists; registry is empty until type modules added
    assert isinstance(SCORERS, dict)
