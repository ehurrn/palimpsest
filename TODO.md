# Palimpsest TODO

## Infrastructure and Fixes
- [ ] Repair Ollama on the local M4 machine to resolve the missing `llama-server` binary and restore local embedding capability.
- [ ] Re-enable the `embed` capability for `m4` in config.toml.

## Phase 2 Scaling
- [ ] Generalize the pipeline to handle the other five finding-types.
- [ ] Configure the orchestrator agent lane (Lane A) on the mesh broker (192.168.0.58:8766).
- [ ] Scale the harvester to retrieve the remaining documents in the NV* accession series.
