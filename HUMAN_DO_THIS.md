
## Ollama embed (M4) — needed for REAL eval calibration  [added 2026-06-13]
The Phase-4 eval trust-gate plan (specs/EVAL-TRUST-GATE.md, TASK-11..18) can be implemented
and unit-tested NOW using the deterministic lexical embedding stub. BUT producing VALID
precision numbers (and a real calibration.json) requires the production embedder
`nomic-embed-text` via Ollama on a worker node. Ollama is currently broken on the M4
(missing llama-server; see TODO.md).
ACTION: restore Ollama + `ollama pull nomic-embed-text` on the M4 (or another node), then run
`palimpsest-eval run --real-embed && palimpsest-eval calibrate`. Until then, gate thresholds
from stub runs are PLUMBING-ONLY and must NOT be used to surface real findings.
