# WORK-LOG.md

- 2026-06-19: Started project initialization.
- 2026-06-19: Verifying `gapjoin` functionality.
- 2026-06-19: Running sharding indexer tests.
- 2026-06-19: Starting rewrite of TypeCScorer.run() with batched vector embeddings + decade sharding (palimpsest/scorers/type_c.py, tests/test_identity.py).
- 2026-06-19: Completed rewrite — TypeCScorer uses injectable embed_fn, two SQL aggregation queries, profile string dedup cache, chunk-100 embedding, pure-Python cosine, decade-shard comparison. Tests: 10/10 pass, ruff + ty clean.
