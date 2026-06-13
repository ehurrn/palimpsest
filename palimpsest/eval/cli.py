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


def _cmd_calibrate(args):
    import sqlite3
    from palimpsest.eval.calibrate import build_artifact, write_artifact
    cfg = load(args.config)
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    run_id = args.run if args.run is not None else conn.execute(
        "SELECT MAX(run_id) FROM eval_runs").fetchone()[0]
    if run_id is None:
        raise SystemExit("no eval runs found — run `palimpsest-eval run` first")
    artifact = build_artifact(conn, run_id, cfg)
    conn.close()
    path = write_artifact(cfg, artifact)
    print(f"calibrated run_id={run_id} → {path}")
    for tk, t in artifact["types"].items():
        print(f"  {tk:8} threshold={t['threshold']} n={t['n']} ({t['reason']})")


def _cmd_report(args):
    import json
    import sqlite3
    from pathlib import Path
    from palimpsest.eval.metrics import render_report
    cfg = load(args.config)
    ev = make_eval_config(cfg)
    conn = sqlite3.connect(ev.db_path)
    run_id = args.run if args.run is not None else conn.execute(
        "SELECT MAX(run_id) FROM eval_runs").fetchone()[0]
    if run_id is None:
        raise SystemExit("no eval runs found")
    artifact = None
    apath = Path(cfg.eval.get("artifact_path", ""))
    if apath and apath.exists():
        artifact = json.loads(apath.read_text())
    text = render_report(conn, run_id, cfg, artifact)
    conn.close()
    out = Path(args.out) if args.out else Path(f"reports/eval-report-{run_id}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out}")


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

    c = sub.add_parser("calibrate", help="fit per-type thresholds → calibration.json")
    c.add_argument("--config", default="config.toml")
    c.add_argument("--run", type=int, default=None, help="run_id (default: latest)")
    c.set_defaults(func=_cmd_calibrate)

    rep = sub.add_parser("report", help="render a markdown metrics report")
    rep.add_argument("--config", default="config.toml")
    rep.add_argument("--run", type=int, default=None)
    rep.add_argument("--out", default=None)
    rep.set_defaults(func=_cmd_report)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
