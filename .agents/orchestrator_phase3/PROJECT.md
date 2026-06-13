# Project: Palimpsest Phase 3 Part 1

## Architecture
We are extracting finding-type scorers from `palimpsest/indexer.py` into a new package `palimpsest/scorers`.
The package structure:
- `palimpsest/scorers/__init__.py`: imports and registers scorers.
- `palimpsest/scorers/base.py`: Candidate dataclass and Scorer protocol.
- `palimpsest/scorers/type_d.py`: TypeDScorer (Outcome Suppression).
- `palimpsest/scorers/type_e.py`: TypeEScorer (Regulatory Violation).
- `palimpsest/scorers/type_f.py`: TypeFScorer (Series Suppression).

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | R1. Scorer Registry Base | Create base.py and skeleton __init__.py, write tests | None | DONE |
| 2 | R2. Type e Scorer | Extract run_violation_join to type_e.py, write tests | M1 | DONE |
| 3 | R3. Type f Scorer | Extract run_series_join to type_f.py, write tests | M1 | DONE |
| 4 | R4. Type d Scorer | Extract run_outcome_gap to type_d.py, write tests | M1 | IN_PROGRESS (b1a0d411-d1ff-401d-8608-e65c23a5eb83) |
| 5 | Verification | Run all tests and ensure 100% pass, log in WORK-LOG.md | M1, M2, M3, M4 | PLANNED |

## Interface Contracts
### Candidate
- type_key: str
- score: float
- doc_ids: list[str]
- page_refs: list[str]
- summary: str
- entity_ids: list[int]

### Scorer Protocol
- type_key: str
- candidates_table: str
- run(conn, config) -> list[Candidate]
- top(conn, limit) -> list[Candidate]
