import sqlite3
import textwrap

import faiss
import pytest

from palimpsest.config import load
from palimpsest.eval.isolation import make_eval_config, fresh_eval_db, write_index


def _cfg(tmp_path):
    toml = textwrap.dedent(f"""
    [storage]
    root = "{tmp_path}"
    [db]
    path = "{{storage.root}}/db/palimpsest.db"
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
    [models]
    keep_alive="24h"
    [nodes]
    [eval]
    eval_db_path="{{storage.root}}/eval/eval.db"
    artifact_path="{{storage.root}}/eval/calibration.json"
    """)
    p = tmp_path / "config.toml"
    p.write_text(toml)
    return load(p)


def test_make_eval_config_repoints(tmp_path):
    cfg = _cfg(tmp_path)
    ev = make_eval_config(cfg)
    assert ev.db_path == (tmp_path / "eval" / "eval.db")
    assert ev.storage_root == (tmp_path / "eval")
    # production paths unchanged on the original
    assert cfg.db_path == (tmp_path / "db" / "palimpsest.db")


def test_make_eval_config_rejects_collision(tmp_path):
    cfg = _cfg(tmp_path)
    bad = cfg.eval.copy()
    bad["eval_db_path"] = str(cfg.db_path)
    import dataclasses
    cfg2 = dataclasses.replace(cfg, eval=bad)
    with pytest.raises(ValueError):
        make_eval_config(cfg2)


def test_fresh_eval_db_migrates_isolated(tmp_path):
    cfg = _cfg(tmp_path)
    ev = make_eval_config(cfg)
    fresh_eval_db(ev)
    assert ev.db_path.exists()
    assert not cfg.db_path.exists()  # production DB never created
    conn = sqlite3.connect(ev.db_path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 7
    conn.close()


def test_write_index_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    ev = make_eval_config(cfg)
    out = write_index(ev, {500: [1.0] + [0.0] * 767, 600: [0.0, 1.0] + [0.0] * 766})
    assert out.exists()
    idx = faiss.read_index(str(out))
    assert idx.ntotal == 2
    rec = idx.reconstruct(500)
    assert abs(float(rec[0]) - 1.0) < 1e-5
