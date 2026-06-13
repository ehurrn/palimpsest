# TASK-15 — Eval runner + `palimpsest-eval` CLI

**Depends on:** TASK-12 (isolation/embedding), TASK-13 (a/b generator + oracle),
TASK-14 (c generator).
**Builds:** the runner that loads cases into the isolated eval DB, builds the
synthetic index, runs the **real** scorers, grades the output, and persists
`eval_runs` / `eval_cases` / `eval_results`. Plus a `palimpsest-eval` CLI.
**Source of truth:** `specs/EVAL-TRUST-GATE.md` §1, §2.

## Context you need (restated)

- `TypeAScorer(embed_fn=...)` and `TypeCScorer()` both expose
  `run(conn, cfg) -> list[Candidate]` and **write** their candidates to
  `gap_candidates` / `identity_link_candidates`. Type b cases are scored by
  `TypeAScorer` too (dosage handling is inside it). The runner injects the same
  `embed_fn` used to build the index, so the embedding route is consistent.
- The scorer reads the FAISS index from `cfg.storage_root/index/faiss.idx`
  (chunk ids == FAISS ids), so the runner must insert chunks with explicit
  `chunk_id` matching the vectors it indexes.
- Attribution at grade time uses the generator's doc-id convention: an a/b
  case's redaction lives in `"{case_uid}_A"`; a c case's subject lives in
  `"{case_uid}_S"`.
- `[project.scripts]` already exists in `pyproject.toml` (Phase 3 added CLI
  entry points). Add one line.

## Files

- Create: `palimpsest/eval/runner.py`
- Create: `palimpsest/eval/cli.py`
- Modify: `pyproject.toml` (`[project.scripts]`)
- Test: `tests/test_eval_runner.py`

---

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_eval_runner.py`:

```python
import sqlite3
import textwrap

from palimpsest.config import load
from palimpsest.eval.isolation import make_eval_config
from palimpsest.eval.runner import run_eval


def _cfg(tmp_path):
    toml = textwrap.dedent(f"""
    [storage]
    root="{tmp_path}"
    [db]
    path="{{storage.root}}/db/palimpsest.db"
    [broker]
    host="h"
    port=1
    [mcp]
    port=2
    [harvest]
    base_url="u"
    [ocr]
    engine_preference=["tesseract"]
    [features]
    redaction_context_chars=300
    [embed]
    model="nomic-embed-text"
    dim=768
    [gapjoin]
    score_threshold=0.65
    w_cosine=0.5
    w_anchor=0.3
    w_kind=0.2
    topk_embedding_candidates=50
    [models]
    keep_alive="24h"
    [nodes]
    [eval]
    default_seed=1
    eval_db_path="{{storage.root}}/eval/eval.db"
    artifact_path="{{storage.root}}/eval/calibration.json"
    """)
    p = tmp_path / "config.toml"
    p.write_text(toml)
    return load(p)


def _labels(conn, type_key=None):
    q = "SELECT label, COUNT(*) c FROM eval_results"
    args = ()
    if type_key:
        q += " WHERE type_key = ?"
        args = (type_key,)
    q += " GROUP BY label"
    return {r[0]: r[1] for r in conn.execute(q, args)}


def test_runner_recovers_positive_typea(tmp_path):
    cfg = _cfg(tmp_path)
    run_id = run_eval(cfg, n_per_kind=3, seed=1, types=("type_a",))
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    # the production DB was never created
    assert not cfg.db_path.exists()
    labels = _labels(conn, "type_a")
    assert labels.get("TP", 0) >= 1          # at least one positive recovered
    assert sum(labels.values()) >= 3         # results were written
    # run bookkeeping recorded
    assert conn.execute("SELECT COUNT(*) FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()[0] == 1
    conn.close()


def test_runner_typec_decoy_produces_fp(tmp_path):
    cfg = _cfg(tmp_path)
    run_eval(cfg, n_per_kind=3, seed=1, types=("type_c",))
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    labels = _labels(conn, "type_c")
    # decoy + answer-absent cases must generate false positives (the safety signal)
    assert labels.get("FP", 0) >= 1
    conn.close()
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_eval_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: palimpsest.eval.runner`.

- [ ] **Step 3: Implement the runner**

Create `palimpsest/eval/runner.py`:

```python
"""Eval runner: generate → isolated DB → real scorers → grade → persist."""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import subprocess

from palimpsest.config import Config
from palimpsest.eval.embedding import deterministic_embed
from palimpsest.eval.generators import (
    gen_type_a_cases, gen_type_b_cases, gen_type_c_cases,
)
from palimpsest.eval.isolation import make_eval_config, fresh_eval_db, write_index
from palimpsest.eval.oracle import grade
from palimpsest.scorers.type_a import TypeAScorer
from palimpsest.scorers.type_c import TypeCScorer


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def _corpus_hash(cases) -> str:
    blob = json.dumps(
        [(c.case_uid, c.type_key, c.case_kind, c.truth) for c in cases],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_cases(conn, eval_cfg, run_id, cases, embed_fn):
    chunk_vectors: dict[int, list[float]] = {}
    chunk_seq = 1
    case_ids: dict[str, int] = {}
    with conn:
        for case in cases:
            cur = conn.execute(
                "INSERT INTO eval_cases (run_id, type_key, case_kind, spec, truth) "
                "VALUES (?,?,?,?,?)",
                (run_id, case.type_key, case.case_kind,
                 json.dumps({"case_uid": case.case_uid}), json.dumps(case.truth)),
            )
            case_ids[case.case_uid] = cur.lastrowid
            seen = set()
            for doc in case.docs:
                if doc.doc_id in seen:
                    continue
                seen.add(doc.doc_id)
                conn.execute(
                    "INSERT INTO documents (doc_id, year, status) VALUES (?,?, 'indexed')",
                    (doc.doc_id, doc.year),
                )
            for page in case.pages:
                conn.execute(
                    "INSERT INTO pages (doc_id, page_no, text) VALUES (?,?,?)",
                    (page.doc_id, page.page_no, page.text),
                )
                for e in page.entities:
                    conn.execute(
                        "INSERT INTO entities "
                        "(doc_id,page_no,kind,text,norm,char_start,char_end) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (page.doc_id, page.page_no, e["kind"], e["text"],
                         e["norm"], e["char_start"], e["char_end"]),
                    )
                cid = chunk_seq
                chunk_seq += 1
                conn.execute(
                    "INSERT INTO chunks (chunk_id, doc_id, page_no, char_start, char_end, text) "
                    "VALUES (?,?,?,?,?,?)",
                    (cid, page.doc_id, page.page_no, 0, len(page.text), page.text),
                )
                chunk_vectors[cid] = embed_fn(eval_cfg, page.text)
                if page.redaction:
                    r = page.redaction
                    conn.execute(
                        "INSERT INTO redactions "
                        "(doc_id,page_no,kind,label,context_before,context_after) "
                        "VALUES (?,?,?,?,?,?)",
                        (page.doc_id, page.page_no, r["kind"], r["label"],
                         r["context_before"], r["context_after"]),
                    )
    write_index(eval_cfg, chunk_vectors)
    return case_ids


def _grade_and_store(conn, run_id, cases, case_ids):
    with conn:
        for case in cases:
            cid = case_ids[case.case_uid]
            if case.type_key in ("type_a", "type_b"):
                rows = conn.execute(
                    "SELECT gc.score AS score, e.norm AS norm "
                    "FROM gap_candidates gc "
                    "JOIN redactions r ON gc.redaction_id = r.redaction_id "
                    "JOIN entities e ON gc.clear_entity_id = e.entity_id "
                    "WHERE r.doc_id = ?",
                    (f"{case.case_uid}_A",),
                ).fetchall()
                answer = case.truth["answer_norm"]
            else:
                rows = conn.execute(
                    "SELECT ilc.score AS score, e.norm AS norm "
                    "FROM identity_link_candidates ilc "
                    "JOIN entities e ON ilc.named_entity_id = e.entity_id "
                    "WHERE ilc.subject_doc_id = ?",
                    (f"{case.case_uid}_S",),
                ).fetchall()
                answer = case.truth["true_named_norm"]
            preds = [(float(r[0]), r[1]) for r in rows]
            for res in grade(answer, preds):
                conn.execute(
                    "INSERT INTO eval_results "
                    "(run_id, case_id, type_key, raw_score, score_components, predicted, label, confidence) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, cid, case.type_key, res.raw_score, None,
                     res.predicted, res.label, None),
                )


def run_eval(cfg: Config, *, embed_fn=None, n_per_kind: int = 5,
             seed: int | None = None,
             types: tuple[str, ...] = ("type_a", "type_b", "type_c")) -> int:
    """Run a full synthetic eval; return the run_id. Writes only to the eval DB."""
    seed = seed if seed is not None else int(cfg.eval.get("default_seed", 1337))
    embed_fn = embed_fn or deterministic_embed
    eval_cfg = make_eval_config(cfg)
    fresh_eval_db(eval_cfg)

    cases = []
    if "type_a" in types:
        cases += gen_type_a_cases(n_per_kind, seed)
    if "type_b" in types:
        cases += gen_type_b_cases(n_per_kind, seed)
    if "type_c" in types:
        cases += gen_type_c_cases(n_per_kind, seed)

    conn = sqlite3.connect(eval_cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO eval_runs (started_at, scorer_git_sha, corpus_hash, seed, config_snapshot) "
            "VALUES (?,?,?,?,?)",
            (now, _git_sha(), _corpus_hash(cases), seed, json.dumps(cfg.eval)),
        )
        conn.commit()
        run_id = cur.lastrowid

        case_ids = _load_cases(conn, eval_cfg, run_id, cases, embed_fn)

        if any(c.type_key in ("type_a", "type_b") for c in cases):
            TypeAScorer(embed_fn=embed_fn).run(conn, eval_cfg)
        if any(c.type_key == "type_c" for c in cases):
            TypeCScorer().run(conn, eval_cfg)

        _grade_and_store(conn, run_id, cases, case_ids)

        conn.execute(
            "UPDATE eval_runs SET finished_at=? WHERE run_id=?",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(), run_id),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()
```

- [ ] **Step 4: Run the end-to-end test, verify it passes**

Run: `uv run pytest tests/test_eval_runner.py -v`
Expected: PASS (both tests). If `test_runner_recovers_positive_typea` fails with
0 TP, confirm the redaction label is `(b)(6)` in the generator (TASK-13) — that
is what makes the person answer outrank the shared anchors.

- [ ] **Step 5: Implement the CLI**

Create `palimpsest/eval/cli.py`:

```python
"""`palimpsest-eval` — run the synthetic evaluation harness."""
from __future__ import annotations

import argparse
import sqlite3

from palimpsest.config import load
from palimpsest.eval.isolation import make_eval_config
from palimpsest.eval.runner import run_eval


def _cmd_run(args):
    cfg = load(args.config)
    embed_fn = None
    if args.real_embed:
        from palimpsest.scorers.type_a import get_ollama_embedding
        embed_fn = get_ollama_embedding
    run_id = run_eval(
        cfg, embed_fn=embed_fn, n_per_kind=args.n_per_kind,
        seed=args.seed, types=tuple(args.types.split(",")),
    )
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    rows = conn.execute(
        "SELECT type_key, label, COUNT(*) FROM eval_results WHERE run_id=? "
        "GROUP BY type_key, label ORDER BY type_key, label", (run_id,),
    ).fetchall()
    conn.close()
    embed_kind = "REAL(ollama)" if args.real_embed else "STUB(lexical — NOT valid precision)"
    print(f"run_id={run_id}  embed={embed_kind}")
    for type_key, label, n in rows:
        print(f"  {type_key:8} {label:3} {n}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="palimpsest-eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="generate cases, run scorers, grade, persist")
    r.add_argument("--config", default="config.toml")
    r.add_argument("--n-per-kind", type=int, default=5, dest="n_per_kind")
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--types", default="type_a,type_b,type_c")
    r.add_argument("--real-embed", action="store_true", dest="real_embed",
                   help="use the production Ollama embedder instead of the lexical stub")
    r.set_defaults(func=_cmd_run)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Register the entry point**

In `pyproject.toml`, add this line under the existing `[project.scripts]` table:

```toml
palimpsest-eval = "palimpsest.eval.cli:main"
```

- [ ] **Step 7: Full suite + lint + smoke + commit**

Run: `uv run pytest -q`
Run: `uv run ruff check palimpsest/eval/runner.py palimpsest/eval/cli.py tests/test_eval_runner.py`
Smoke (uses the lexical stub, so safe without Ollama):
Run: `uv run palimpsest-eval run --n-per-kind 2 --config config.toml`
Expected: prints `run_id=… embed=STUB(...)` then per-type label counts.

```bash
git add palimpsest/eval/runner.py palimpsest/eval/cli.py pyproject.toml tests/test_eval_runner.py
git commit -m "feat(eval): runner (cases→isolated DB→real scorers→grade) + palimpsest-eval CLI"
```

## Out of scope
- No calibration or thresholds yet (TASK-16). No report (TASK-17). No gate (TASK-18).
- The smoke run uses the lexical stub; numbers are plumbing-only. A real
  calibration run (`--real-embed`) requires Ollama, which is currently down on
  the M4 — if you need a real run and Ollama is unavailable, record it in
  `HUMAN_DO_THIS.md` and proceed with the stub for plumbing verification.

## Blocker protocol
Log start/finish in `~/dev/palimpsest/WORK-LOG.md`. Hard blocker →
`~/dev/palimpsest/HUMAN_DO_THIS.md`, stop, move on.
