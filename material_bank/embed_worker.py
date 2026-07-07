"""Continuous embedding worker — the consumer half of the pipeline.

Runs as its own process alongside the harvest workers. Loads the model once,
then loops: embed every product that isn't yet in the text index, rebuild FTS
when new rows land, sleep, repeat. Fully resumable and idempotent, so it simply
keeps up with whatever the harvesters produce. Text-only (fast, keeps pace);
image embedding is a separate slower backfill.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import db
from .embeddings import embed_catalog_text
from .vectorstore import NumpyVectorStore


def _default_embedder():
    from .embeddings import MarqoEmbedder
    return MarqoEmbedder()


def run_embed_worker(
    db_path: str | Path | None = None,
    *,
    poll_interval: float = 30.0,
    max_passes: int | None = None,
    batch_size: int = 128,
    embedder_factory=_default_embedder,
    sleep=time.sleep,
    on_pass=None,
) -> dict:
    """Loop embedding new products until stopped (or max_passes reached).

    Returns cumulative stats. The model is loaded once and reused every pass.
    """
    db_path = str(db_path or db.DEFAULT_DB_PATH)
    conn = db.connect(db_path, check_same_thread=False)
    store = NumpyVectorStore(conn)
    embedder = embedder_factory()

    total, passes, idle = 0, 0, 0
    while max_passes is None or passes < max_passes:
        passes += 1
        store._invalidate("text")  # a harvester may have added rows since last pass
        n = embed_catalog_text(conn, embedder, store, batch_size=batch_size)["embedded"]
        total += n
        if n:
            db.rebuild_fts(conn)
            idle = 0
        else:
            idle += 1
        if on_pass:
            on_pass(passes, n, store.count("text"))
        if max_passes is not None and passes >= max_passes:
            break
        sleep(poll_interval)

    conn.close()
    return {"passes": passes, "embedded_total": total, "idle_passes": idle}


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="mb-embed-worker")
    ap.add_argument("--poll", type=float, default=30.0, help="seconds between passes")
    ap.add_argument("--passes", type=int, default=None, help="stop after N passes (default: forever)")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    def prog(p, n, total):
        print(f"  pass {p}: +{n} embedded, {total} total vectors", file=sys.stderr, flush=True)

    print(f"embed worker: poll={args.poll}s (consumer running alongside harvest)", file=sys.stderr)
    rep = run_embed_worker(args.db, poll_interval=args.poll, max_passes=args.passes, on_pass=prog)
    print(f"embed worker exit: {rep}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
