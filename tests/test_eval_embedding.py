import numpy as np
from palimpsest.eval.embedding import deterministic_embed


class _Cfg:
    embed = {"dim": 768}


def _cos(a, b):
    a, b = np.array(a), np.array(b)
    return float(a @ b)  # inputs are unit vectors


def test_deterministic_same_text_same_vector():
    v1 = deterministic_embed(_Cfg(), "oak ridge 1957 dosimetry")
    v2 = deterministic_embed(_Cfg(), "oak ridge 1957 dosimetry")
    assert v1 == v2
    assert len(v1) == 768
    assert abs(np.linalg.norm(v1) - 1.0) < 1e-5


def test_overlap_more_similar_than_disjoint():
    base = deterministic_embed(_Cfg(), "the subject received fifteen rem at oak ridge")
    overlap = deterministic_embed(_Cfg(), "subject received rem oak ridge report")
    disjoint = deterministic_embed(_Cfg(), "zebra umbrella xylophone quartz")
    assert _cos(base, overlap) > _cos(base, disjoint)


def test_empty_text_is_unit_vector():
    v = deterministic_embed(_Cfg(), "")
    assert abs(np.linalg.norm(v) - 1.0) < 1e-5
