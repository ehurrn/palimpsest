import sqlite3
import textwrap

from palimpsest.config import load
from palimpsest.eval.isolation import make_eval_config
from palimpsest.eval.runner import run_eval

# Assuming type_d is similar to c but for a different purpose
# I'll need to update generator and runner to support it, but
# let's first establish the test structure.

def _cfg(tmp_path):
    # This matches the structure in test_eval_runner.py
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


def test_runner_type_d_execution(tmp_path):
    cfg = _cfg(tmp_path)
    # This should fail if type_d is not supported by the runner
    # The goal is to see it fail, then implement support
    run_eval(cfg, n_per_kind=3, seed=1, types=("type_d",))
    
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    
    # Verify results exist
    count = conn.execute("SELECT COUNT(*) FROM eval_results WHERE type_key='type_d'").fetchone()[0]
    assert count >= 1
    conn.close()
