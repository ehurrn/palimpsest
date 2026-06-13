# Palimpsest eval report — run 1

Generated: 2026-06-13T16:55:49.807226+00:00
Embedding: embed=palimpsest.eval.embedding.deterministic_embed
Corpus hash: 9b2ef95eea56043d6a77092bea48096b0491b96a4e8cb212777ddf67c69af726   Scorer SHA: 17d082856eb954b2647e59caf35f73f4fefcb1d5

## Per-type metrics

| type | TP | FP | FN | TN | precision | recall | specificity | gate threshold |
|------|----|----|----|----|-----------|--------|-------------|----------------|
| type_a | 30 | 150 | 0 | 30 | 0.167 | 1.000 | 0.167 | None |
| type_b | 30 | 210 | 0 | 30 | 0.125 | 1.000 | 0.125 | None |
| type_c | 720 | 1440 | 0 | 0 | 0.333 | 1.000 | 0.000 | None |

## Reliability (score band → empirical correctness)

- type_a: [0.852-0.853]→0.333 (n=90), [0.857-0.859]→0.0 (n=90)
- type_b: [0.81-0.838]→0.0 (n=84), [0.838-0.867]→0.0 (n=96), [0.895-0.923]→1.0 (n=6), [0.923-0.951]→0.444 (n=54)
- type_c: [0.7-0.76]→0.324 (n=2040), [0.94-1.0]→0.5 (n=120)

## ⚠ Validity disclosure (required)

Precision here is measured on SYNTHETIC cases whose answer is recoverable by construction. It is an UPPER BOUND on real-world precision, not an estimate of it. Real anchor cases included: 0.

**PLUMBING-ONLY**: this run used the deterministic lexical embedding stub, not the production model. Treat all precision/recall numbers as a pipeline smoke test, NOT a measurement of detector quality.
