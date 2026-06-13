from palimpsest.eval.stats import fit_isotonic, predict_isotonic, wilson_lower_bound


def test_isotonic_is_monotone():
    pts = [(0.1, 0), (0.2, 1), (0.3, 0), (0.4, 1), (0.5, 1)]
    curve = fit_isotonic(pts)
    ys = [y for _, y in curve]
    assert ys == sorted(ys)              # non-decreasing
    assert all(0.0 <= y <= 1.0 for y in ys)


def test_isotonic_predict_clips_and_steps():
    pts = [(0.2, 0), (0.4, 0), (0.6, 1), (0.8, 1)]
    curve = fit_isotonic(pts)
    assert predict_isotonic(curve, 0.0) <= predict_isotonic(curve, 1.0)
    assert 0.0 <= predict_isotonic(curve, 0.5) <= 1.0


def test_wilson_known_values():
    # 80/100 successes, z=1.96 → lower bound ≈ 0.7106
    lb = wilson_lower_bound(80, 100, 1.96)
    assert 0.70 < lb < 0.72
    assert wilson_lower_bound(0, 0, 1.96) == 0.0
    # lower bound is below the point estimate
    assert wilson_lower_bound(9, 10, 1.96) < 0.9
