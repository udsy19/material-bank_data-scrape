"""The planner — the flywheel's brain (v0).

Runs every sweep: re-score the whole catalog, persist the scorecard snapshot,
and report the worst gaps. As enrichment stages land (Phase B+), this is where
measured gaps turn into prioritized `pipeline_jobs` — for now it makes quality
measurable and the publish gate current, which is what every later stage keys on.
"""

from __future__ import annotations

from pathlib import Path

from . import db
from .quality import quality_report, score_all, snapshot_metrics


def run_planner(db_path: str | Path | None = None) -> dict:
    conn = db.connect(str(db_path or db.DEFAULT_DB_PATH), check_same_thread=False)
    db.migrate(conn)
    summary = score_all(conn)
    snapshot_rows = snapshot_metrics(conn)
    report = quality_report(conn)
    conn.close()
    return {"scored": summary["scored"], "publish_ready": report["publish_ready"],
            "median_completeness": report["median_completeness"],
            "tiers": report["tiers"], "snapshot_rows": snapshot_rows,
            "worst_categories": report["worst_categories"][:5]}


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="mb-planner")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)
    rep = run_planner(args.db)
    print(json.dumps(rep, default=str), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
