# Palimpsest eval report — run 1

Generated: 2026-06-13T17:22:03.382507+00:00
Embedding: embed=palimpsest.scorers.type_a.get_ollama_embedding
Corpus hash: 69fc04b14ae2ad821b308f3e6b65c7cb7966664f73bb0182598bcc998a4f61b0   Scorer SHA: d211ac4d510452d46a50e38782530003ff2ceb53

## Per-type metrics

| type | TP | FP | FN | TN | precision | recall | specificity | gate threshold |
|------|----|----|----|----|-----------|--------|-------------|----------------|
| type_a | 50 | 303 | 0 | 50 | 0.142 | 1.000 | 0.142 | None |
| type_b | 50 | 474 | 0 | 50 | 0.095 | 1.000 | 0.095 | None |
| type_c | 2000 | 4000 | 0 | 0 | 0.333 | 1.000 | 0.000 | None |

## Reliability (score band → empirical correctness)

- type_a: [0.653-0.693]→0.0 (n=53), [0.815-0.856]→0.167 (n=300)
- type_b: [0.651-0.716]→0.0 (n=124), [0.848-0.914]→0.0 (n=300), [0.914-0.979]→0.5 (n=100)
- type_c: [0.7-0.76]→0.328 (n=5800), [0.94-1.0]→0.5 (n=200)

## ⚠ Validity disclosure (required)

Precision here is measured on SYNTHETIC cases whose answer is recoverable by construction. It is an UPPER BOUND on real-world precision, not an estimate of it. Real anchor cases included: 0.
