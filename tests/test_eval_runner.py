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
    [orchestrator]
    heartbeat_interval_secs = 900
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
