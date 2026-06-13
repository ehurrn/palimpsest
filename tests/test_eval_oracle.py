from palimpsest.eval.oracle import grade, Result


def test_positive_correct_is_tp():
    out = grade("john smith", [(0.9, "john smith")])
    assert out == [Result("TP", 0.9, "john smith")]


def test_positive_wrong_is_fp_plus_fn():
    out = grade("john smith", [(0.8, "oak ridge")])
    labels = sorted(r.label for r in out)
    assert labels == ["FN", "FP"]


def test_negative_with_hits_all_fp():
    out = grade(None, [(0.7, "x"), (0.66, "y")])
    assert [r.label for r in out] == ["FP", "FP"]


def test_negative_empty_is_tn():
    assert grade(None, []) == [Result("TN", None, None)]


def test_positive_empty_is_fn():
    assert grade("a", []) == [Result("FN", None, None)]
