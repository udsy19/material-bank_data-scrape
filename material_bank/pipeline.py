"""End-to-end pipeline orchestrator with retry + self-healing.

Runs the stages in order, each durable and resumable:
  1. harvest  — queue-driven, per-supplier retry/backoff/dead-letter
  2. embed    — resumable text embedding of every un-embedded product
  3. index    — FTS5 rebuild so keyword search sees new rows
and returns a health report (queue counts + dead-letters) so failures surface.

Everything is idempotent: re-running skips completed work and re-attempts only
what failed. This is the loop cron drives (PIPELINE.md Stage 9).
"""

from __future__ import annotations

from pathlib import Path

from . import db, jobs
from .harvest import worker
from .vectorstore import NumpyVectorStore

DEFAULT_EXCLUDE = {"ikea.com", "lxhausys.com"}


def _default_embedder():
    from .embeddings import MarqoEmbedder
    return MarqoEmbedder()


def run_pipeline(
    db_path: str | Path | None = None,
    *,
    tiers: tuple[str, ...] = ("shopify", "woocommerce", "jsonld"),
    workers: int = 8,
    jsonld_limit: int | None = None,   # uncapped: collect every product
    min_interval: float = 2.0,
    exclude: set[str] | None = None,
    embed: bool = True,
    embedder_factory=_default_embedder,
    retry_dead_first: bool = True,
    on_job=None,
) -> dict:
    db_path = str(db_path or db.DEFAULT_DB_PATH)
    conn = db.connect(db_path, check_same_thread=False)
    db.migrate(conn)

    # Stage 1 — harvest (durable queue). Re-arm prior dead-letters for a retry.
    if retry_dead_first:
        jobs.retry_dead(conn, worker.STAGE)
    worker.seed_harvest_jobs(conn, tiers=tiers, exclude=exclude or DEFAULT_EXCLUDE)
    harvest_counts = worker.run_workers(
        db_path, workers=workers, jsonld_limit=jsonld_limit,
        min_interval=min_interval, on_job=on_job)

    report = {"harvest_jobs": harvest_counts,
              "dead_letters": jobs.dead_letters(conn, worker.STAGE)}

    # Stage 2 — embed (resumable). Wrapped so a model failure surfaces, not crashes.
    if embed:
        from .embeddings import embed_catalog_text
        store = NumpyVectorStore(conn)
        try:
            report["embed"] = embed_catalog_text(conn, embedder_factory(), store)
        except Exception as exc:
            report["embed_error"] = f"{type(exc).__name__}: {exc}"

    # Stage 3 — keyword index rebuild.
    report["fts_rows"] = db.rebuild_fts(conn)
    report["catalog"] = {
        "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "priced": conn.execute("SELECT COUNT(DISTINCT product_id) FROM price_observation").fetchone()[0],
        "suppliers": conn.execute("SELECT COUNT(DISTINCT supplier_domain) FROM products").fetchone()[0],
        "text_vectors": conn.execute("SELECT COUNT(*) FROM embeddings WHERE kind='text'").fetchone()[0],
    }
    conn.close()
    return report


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="mb-pipeline")
    ap.add_argument("--tiers", default="shopify,woocommerce,jsonld")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--jsonld-limit", type=int, default=0, help="0 = unlimited")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--exclude", default="ikea.com,lxhausys.com")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    def prog(target, status, result):
        p = result.get("products", 0) if isinstance(result, dict) else 0
        print(f"  {target:<24} {status:<8} products={p}", file=sys.stderr, flush=True)

    rep = run_pipeline(
        args.db, tiers=tuple(t.strip() for t in args.tiers.split(",") if t.strip()),
        workers=args.workers, jsonld_limit=(None if args.jsonld_limit <= 0 else args.jsonld_limit),
        exclude={d.strip() for d in args.exclude.split(",") if d.strip()},
        embed=not args.no_embed, on_job=prog)
    print("\n=== pipeline report ===", file=sys.stderr)
    print(json.dumps({k: v for k, v in rep.items() if k != "dead_letters"}, indent=2, default=str),
          file=sys.stderr)
    if rep["dead_letters"]:
        print(f"\n{len(rep['dead_letters'])} dead-letters:", file=sys.stderr)
        for d in rep["dead_letters"]:
            print(f"  {d['target']}: {(d['last_error'] or '')[:80]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
