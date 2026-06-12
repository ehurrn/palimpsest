# TASK-00 — ml-pipeline Reconnaissance (NO CODE)

**Read `specs/00-ARCHITECTURE.md` §11 (worker rules) before starting.**

## Objective
Read `~/dev/ml-pipeline` and produce `specs/RECON-ML-PIPELINE.md` answering the questions below. This task writes ONE markdown file. Do not modify ml-pipeline. Do not write any Python.

## Depends on
Nothing. Run first or in parallel with TASK-00b / TASK-01.

## Deliverable
`~/dev/palimpsest/specs/RECON-ML-PIPELINE.md` using exactly this template:

```markdown
# RECON: ml-pipeline → Palimpsest reuse surface

## 1. Transport
- Mechanism (sockets/HTTP/other):
- Message format (with one verbatim example message):
- Broker stateful? (what state, where persisted):

## 2. send_message tool
- Exact signature (copy from source, cite file:line):
- Node registration flow:
- Response routing back to caller:

## 3. Node registry / config
- File(s) where nodes + models are declared (paths):
- Can a NON-mesh process read this config safely? (yes/no + why):
- How M5 intermittent availability is signaled:

## 4. Model lifecycle
- How Ollama models launch / stay warm:
- Daemon long-lived? Restart behavior:
- Is direct Ollama API (http://node:11434) usable outside send_message? (verify a
  config or code reference proving the port/binding, cite it):

## 5. Health / retry
- Behavior when a node drops mid-task (cite code):

## 6. Reuse verdict
| Capability | ml-pipeline provides | Palimpsest must build |
|---|---|---|
| node availability | ... | ... |
| node/model config | ... | ... |
| job persistence   | ... | ... |
| retry/idempotency | ... | ... |
| file transfer     | ... | ... |

## 7. Plan corrections
Bullet list: every assumption in palimpsest-phase1-plan.md or
specs/00-ARCHITECTURE.md this recon contradicts, with the correction.
```

## Method
1. `ls -R ~/dev/ml-pipeline` (skip venvs, node_modules, .git).
2. Read README/docs first, then entry points, then config files.
3. Every claim in the template must cite `file:line` or a config key. No claim from memory.
4. If an answer cannot be determined from the repo, write `UNKNOWN — needs human` in that slot; do not guess.

## Acceptance
- [ ] `RECON-ML-PIPELINE.md` exists, every template section filled (or marked UNKNOWN).
- [ ] ≥ 1 verbatim code/config citation per section.
- [ ] Section 7 explicitly confirms or denies: (a) node config readable by non-mesh process, (b) direct Ollama API reachable on M4/M5, (c) availability signal reusable by Lane B.

## Out of scope
Any modification to ml-pipeline. Any Palimpsest code. Benchmarking.

**Blocked?** Write the blocker to `~/dev/HUMAN_DO_THIS.md`, move on.
