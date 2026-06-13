"""Dependency-free calibration statistics: PAV isotonic regression + Wilson LB."""
from __future__ import annotations

import math


def fit_isotonic(points: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators isotonic regression.

    Input: (score, label in {0,1}). Output: a non-decreasing step function as a
    list of (x_right, value) blocks sorted by x_right. Use with predict_isotonic.
    """
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[0])
    # each block: [sum_y, count, value, x_right]
    blocks: list[list[float]] = []
    for x, y in pts:
        blocks.append([float(y), 1.0, float(y), float(x)])
        while len(blocks) >= 2 and blocks[-2][2] > blocks[-1][2]:
            s2, c2, _v2, xr2 = blocks.pop()
            s1, c1, _v1, _xr1 = blocks.pop()
            s, c = s1 + s2, c1 + c2
            blocks.append([s, c, s / c, xr2])
    return [(b[3], b[2]) for b in blocks]


def predict_isotonic(curve: list[tuple[float, float]], score: float) -> float:
    """Calibrated probability for *score* from a fitted curve (clipped at ends)."""
    if not curve:
        return 0.0
    for x_right, value in curve:
        if score <= x_right:
            return value
    return curve[-1][1]


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a binomial proportion."""
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)
