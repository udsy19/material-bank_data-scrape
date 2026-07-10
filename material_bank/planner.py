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
    from .enrich import seed_enrich_jobs   # late import: enrich pulls in fetch

    from .resolve import assign_variant_groups, audit_variant_groups
    from .taxonomy import classify_all

    conn = db.connect(str(db_path or db.DEFAULT_DB_PATH), check_same_thread=False)
    db.migrate(conn)
    classify_all(conn)          # canonical taxonomy before scoring
    summary = score_all(conn)
    variants = assign_variant_groups(conn)  # group SKUs into design families
    from .image_colour import eval_colour

    audit = audit_variant_groups(conn)      # free QA of grouping quality
    colour = eval_colour(conn)              # pixel-colour accuracy vs text ground truth
    conn.executemany("INSERT INTO metrics (captured_at, scope, key, value) VALUES (?,?,?,?)",
                     [(db.now_iso(), "global", "suspect_variant_groups", audit["suspect_count"]),
                      (db.now_iso(), "global", "colour_accuracy", colour["same_family_accuracy"])])
    conn.commit()
    snapshot_rows = snapshot_metrics(conn)
    enrich_jobs = seed_enrich_jobs(conn)    # measured gaps -> queued work
    report = quality_report(conn)
    conn.close()
    return {"scored": summary["scored"], "publish_ready": report["publish_ready"],
            "median_completeness": report["median_completeness"],
            "tiers": report["tiers"], "snapshot_rows": snapshot_rows,
            "enrich_jobs_seeded": enrich_jobs,
            "variant_groups": variants["groups"],
            "grouped_products": variants["grouped_products"],
            "suspect_variant_groups": audit["suspect_count"],
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
