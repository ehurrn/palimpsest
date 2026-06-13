"""Pure grading oracle for eval predictions.

`grade` is type-agnostic: given the known correct value (or None when no correct
answer exists) and a list of (score, value) predictions for one case, it returns
one Result per prediction (TP/FP), plus an FN when a positive case surfaced no
correct prediction, or a single TN when a negative case surfaced nothing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Result:
    label: str            # "TP" | "FP" | "FN" | "TN"
    raw_score: float | None
    predicted: str | None


def grade(answer: str | None, preds: list[tuple[float, str]]) -> list[Result]:
    out: list[Result] = []
    if answer is None:
        if not preds:
            out.append(Result("TN", None, None))
        else:
            out.extend(Result("FP", s, v) for s, v in preds)
        return out

    if not preds:
        out.append(Result("FN", None, None))
        return out

    matched = False
    for s, v in preds:
        if v == answer:
            out.append(Result("TP", s, v))
            matched = True
        else:
            out.append(Result("FP", s, v))
    if not matched:
        out.append(Result("FN", None, None))
    return out
