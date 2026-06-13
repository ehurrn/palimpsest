import sqlite3

from palimpsest.db import migrate
from palimpsest.eval.metrics import confusion, rates, render_report


class DummyConfig:
    def __init__(self, tmp_path):
        self.storage_root = tmp_path
        self.db_path = tmp_path / "db" / "palimpsest.db"
        self.eval = {"target_precision": 0.9}


def _seed(conn):
    conn.execute("INSERT INTO eval_runs (run_id, started_at, corpus_hash, notes) "
                 "VALUES (1,'now','abc','embed=palimpsest.eval.embedding.deterministic_embed')")
    conn.execute("INSERT INTO eval_cases (case_id, run_id, type_key, case_kind, spec, truth) "
                 "VALUES (1,1,'type_a','positive','{}','{}')")
    rows = [("type_a", 0.9, "TP"), ("type_a", 0.5, "FP"),
            ("type_a", None, "FN"), ("type_a", None, "TN")]
    for tk, s, lbl in rows:
        conn.execute("INSERT INTO eval_results (run_id, case_id, type_key, raw_score, label) "
                     "VALUES (1,1,?,?,?)", (tk, s, lbl))
    conn.commit()


def test_confusion_and_rates(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    _seed(conn)
    c = confusion(conn, 1, "type_a")
    assert c == {"TP": 1, "FP": 1, "FN": 1, "TN": 1}
    prec, rec, spec = rates(c)
    assert prec == 0.5 and rec == 0.5 and spec == 0.5
    conn.close()


def test_report_has_disclosure_and_stub_flag(tmp_path):
    cfg = DummyConfig(tmp_path)
    migrate(cfg)
    conn = sqlite3.connect(cfg.db_path)
    _seed(conn)
    text = render_report(conn, 1, cfg, artifact=None)
    assert "type_a" in text
    assert "precision" in text.lower()
    assert "upper bound" in text.lower()            # mandatory disclosure present
    assert "PLUMBING-ONLY" in text                  # stub detected from notes
    conn.close()
