"""Image-embedding backfill — the Explore back-match bridge.

For each product without an ``image`` vector: resolve its image_url (from the
row, or by re-parsing its PDP if the row predates image_url capture), download
the image, encode it with the same shared-space model, and store the vector.
Resumable (skips products already image-embedded); failures are quarantined.

THROUGHPUT, measured on the production box (2 vCPU, no GPU, 2026-07-16):

  * Encoding tops out at ~2.6 img/s at batch=8. Batch=1 gives 1.5, batch=32
    gives 2.2 (cache pressure) — the ceiling is the 2 cores, not the batch, so
    ~245k images is ~27h of pure compute. That is the floor for this box.
  * Fetching, left alone, is FAR worse: Fetcher spaces requests ~2s/host, and
    53% of our images sit on cdn.shopify.com — 72 hours for that host alone,
    ~136h overall. Fetching would dominate encoding 3:1.

The 2s budget protects small ORIGIN servers from being hammered, which is
right. But CDNs are not origins: cdn.shopify.com, CloudFront, ImageKit and
Azure blob exist to serve static assets at scale, and spacing requests to them
buys politeness nobody asked for at a cost of days. So hosts are split — CDNs
fetch concurrently with no spacing, origins keep the full budget and are
sharded so no two workers ever touch the same origin at once. Fetching then
lands well under the ~27h compute floor and stops being the constraint.
"""

from __future__ import annotations

import io
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from .. import db
from ..fetch import Fetcher
from . import orientbell as ob

# Hosts built to serve static assets at scale. Endswith-matched, so a
# sub-subdomain of a CDN still counts.
_CDN_SUFFIXES = (
    "cloudfront.net",
    "akamaized.net",
    "akamai.net",
    "fastly.net",
    "cloudinary.com",
    "imagekit.io",
    "blob.core.windows.net",
    "amazonaws.com",
    "shopifycdn.com",
    "cdn.shopify.com",
    "googleusercontent.com",
)

# Conventional asset-host subdomains: a host calling itself cdn./media./assets./
# static./img. is telling us it serves files, which is what we are asking for.
_CDN_PREFIXES = (
    "cdn.",
    "cdn2.",
    "media.",
    "assets.",
    "static.",
    "img.",
    "images.",
    "ik.",
)

EMBED_BATCH = 8  # measured optimum on 2 vCPU; larger is slower, not faster

# Deliberately small. Measured on this box: a single fetch thread pulls ~45
# img/s from a CDN, while the encoder tops out at ~3.3 img/s — fetching only
# ever needs to beat encoding, and one thread beats it 14x over. Every extra
# worker just decodes and downscales (both CPU-bound) on the same 2 cores the
# encoder needs, so 8 workers measured SLOWER end-to-end than 2. Threads here
# buy latency hiding, not throughput; two is enough to hide it.
CDN_WORKERS = 2
ORIGIN_WORKERS = 1
_QUEUE_DEPTH = 32

# Decoded images are handed between threads, and supplier images are big: 2048x
# 2048 RGB is 12.6MB each in memory. At depth 64 that is ~800MB of bitmaps in
# the queue alone, which OOM-killed the run on this 3.3GB box within 31s.
#
# The model preprocesses to 224x224 anyway, so carrying full resolution across
# the queue buys nothing. Downscale in the worker, right after decode: memory
# per item drops ~100x and the encoder sees exactly what it would have seen.
# Kept above 224 so the model's own resize/crop still has pixels to work with.
_MAX_EDGE = 336

# store.upsert() commits per call — one fsync per image. catalog.db is written
# concurrently by the harvesters (see db.run_locked), so each of those commits
# also queues behind the write lock; measured, that alone dragged the backfill
# to ~16s/image, five times slower than the model. upsert_many() takes one
# commit per batch, so writes stop being the bottleneck at 245k rows.
# Kept larger than EMBED_BATCH: encoding wants small batches (cache), writing
# wants big ones (fsync amortization). They are different problems.
WRITE_BATCH = 128


def _is_cdn(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return host.endswith(_CDN_SUFFIXES) or host.startswith(_CDN_PREFIXES)


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "?").lower()


def _resolve_image_url(conn: sqlite3.Connection, pdp_fetcher: Fetcher,
                       product_id: int, image_url: str | None) -> str | None:
    if image_url:
        return image_url
    # Row predates image_url capture — re-parse the PDP (its source_url) for it.
    row = conn.execute(
        "SELECT source_url FROM price_observation WHERE product_id=? LIMIT 1", (product_id,)
    ).fetchone()
    if not row or not row["source_url"]:
        return None
    r = pdp_fetcher.get(row["source_url"])
    if not r.ok:
        return None
    parsed = ob.parse_pdp(r.text, row["source_url"])
    if not parsed:
        return None
    url = parsed[0].image_url
    if url:
        conn.execute("UPDATE products SET image_url=? WHERE id=?", (url, product_id))
        conn.commit()
    return url


def embed_images(
    conn: sqlite3.Connection,
    embedder,
    store,
    *,
    pdp_fetcher: Fetcher | None = None,
    image_fetcher: Fetcher | None = None,
    limit: int | None = None,
    force: bool = False,
    on_item=None,
    batch_size: int = EMBED_BATCH,
    concurrent: bool = True,
) -> dict:
    """Backfill image vectors.

    ``concurrent=False`` keeps the original serial path — used by the tests,
    and by anything passing an injected fetcher it wants full control over.
    """
    from PIL import Image  # deferred so importing this module stays light

    pdp_fetcher = pdp_fetcher or Fetcher()

    done = set() if force else store.embedded_ids("image")
    rows = [r for r in conn.execute("SELECT id, image_url FROM products ORDER BY id")
            if r["id"] not in done]
    if limit is not None:
        rows = rows[:limit]

    stats = {"targets": len(rows), "embedded": 0, "no_image_url": 0, "quarantined": 0}

    if not concurrent or image_fetcher is not None:
        return _embed_serial(conn, embedder, store, rows, stats,
                             pdp_fetcher, image_fetcher or Fetcher(raw_dir=None),
                             on_item, Image)

    return _embed_concurrent(conn, embedder, store, rows, stats,
                             pdp_fetcher, on_item, Image, batch_size)


def _embed_serial(conn, embedder, store, rows, stats, pdp_fetcher, image_fetcher,
                  on_item, Image) -> dict:
    """The original one-at-a-time path. Correct, slow, and easy to reason about."""
    for row in rows:
        pid = row["id"]
        url = _resolve_image_url(conn, pdp_fetcher, pid, row["image_url"])
        if not url:
            stats["no_image_url"] += 1
            continue
        resp = image_fetcher.get(url)
        if not resp.ok or not resp.content:
            db.quarantine(conn, stage="image", source_url=url,
                          reason=f"image fetch {resp.status_code or resp.error}", raw_ref=None)
            stats["quarantined"] += 1
            continue
        try:
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            vec = embedder.encode_image([img])[0]
        except Exception as exc:
            db.quarantine(conn, stage="image", source_url=url,
                          reason=f"decode/encode {type(exc).__name__}: {exc}", raw_ref=None)
            stats["quarantined"] += 1
            continue
        store.upsert(pid, "image", vec, embedder.model_id)
        stats["embedded"] += 1
        if on_item:
            on_item(pid, url, stats)
    return stats


def _embed_concurrent(conn, embedder, store, rows, stats, pdp_fetcher,
                      on_item, Image, batch_size) -> dict:
    """Fetch in parallel, encode in batches, write from one thread.

    Only the fetch+decode fan out. Every sqlite touch (upsert, quarantine) and
    every encode stays on the calling thread: sqlite connections are not
    thread-safe, and the model is the serial bottleneck anyway, so there is
    nothing to win by sharing either.

    Rows whose image_url is missing (~0.5%) fall back to the serial path at the
    end — resolving them re-parses a PDP and writes to sqlite, which a worker
    thread must not do.
    """
    have_url = [r for r in rows if r["image_url"]]
    need_resolve = [r for r in rows if not r["image_url"]]

    # Shard ORIGIN hosts across workers so a host is only ever fetched by one
    # thread — that is what keeps the 2s politeness budget meaningful. CDNs are
    # exempt and share a pool with no spacing.
    cdn_rows = [r for r in have_url if _is_cdn(r["image_url"])]
    origin_rows = [r for r in have_url if not _is_cdn(r["image_url"])]

    origin_shards: list[list] = [[] for _ in range(ORIGIN_WORKERS)]
    for r in origin_rows:
        origin_shards[hash(_host_of(r["image_url"])) % ORIGIN_WORKERS].append(r)

    cdn_shards: list[list] = [[] for _ in range(CDN_WORKERS)]
    for i, r in enumerate(cdn_rows):
        cdn_shards[i % CDN_WORKERS].append(r)

    out: queue.Queue = queue.Queue(maxsize=_QUEUE_DEPTH)
    sentinel = object()
    n_workers = len(origin_shards) + len(cdn_shards)

    def worker(shard, *, cdn: bool):
        # Each worker owns its Fetcher: _last (the per-host clock) is instance
        # state, so a shared one would race.
        f = Fetcher(raw_dir=None, min_interval=0.0 if cdn else 2.0)
        for row in shard:
            url = row["image_url"]
            try:
                resp = f.get(url)
                if not resp.ok or not resp.content:
                    out.put((row["id"], url, None,
                             f"image fetch {resp.status_code or resp.error}"))
                    continue
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                # Shrink before queueing — see _MAX_EDGE. thumbnail() is
                # in-place, keeps aspect, and never upscales.
                img.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.BILINEAR)
                out.put((row["id"], url, img, None))
            except Exception as exc:
                out.put((row["id"], url, None, f"decode {type(exc).__name__}: {exc}"))
        out.put(sentinel)

    threads = [threading.Thread(target=worker, args=(s,), kwargs={"cdn": False}, daemon=True)
               for s in origin_shards if s]
    threads += [threading.Thread(target=worker, args=(s,), kwargs={"cdn": True}, daemon=True)
                for s in cdn_shards if s]
    n_workers = len(threads)
    for t in threads:
        t.start()

    pending: list = []      # decoded images awaiting encode
    write_buf: list = []    # (pid, vector) awaiting a batched write
    finished = 0

    def drain_writes(force: bool = False):
        if not write_buf or (not force and len(write_buf) < WRITE_BATCH):
            return
        store.upsert_many(write_buf, kind="image", model=embedder.model_id)
        write_buf.clear()

    def flush():
        if not pending:
            return
        try:
            vecs = embedder.encode_image([p[2] for p in pending])
        except Exception:
            # A bad image in the batch must not lose the good ones — retry this
            # batch one at a time and quarantine only what actually fails.
            for pid, url, img in pending:
                try:
                    v = embedder.encode_image([img])[0]
                except Exception as inner:
                    db.quarantine(conn, stage="image", source_url=url,
                                  reason=f"encode {type(inner).__name__}: {inner}", raw_ref=None)
                    stats["quarantined"] += 1
                    continue
                write_buf.append((pid, v))
                stats["embedded"] += 1
            pending.clear()
            drain_writes()
            return
        for (pid, url, _img), vec in zip(pending, vecs):
            write_buf.append((pid, vec))
            stats["embedded"] += 1
            if on_item:
                on_item(pid, url, stats)
        pending.clear()
        drain_writes()

    while finished < n_workers:
        item = out.get()
        if item is sentinel:
            finished += 1
            continue
        pid, url, img, err = item
        if err:
            db.quarantine(conn, stage="image", source_url=url, reason=err, raw_ref=None)
            stats["quarantined"] += 1
            continue
        pending.append((pid, url, img))
        if len(pending) >= batch_size:
            flush()
    flush()
    drain_writes(force=True)

    # The stragglers with no image_url — serial, because resolving one writes.
    if need_resolve:
        _embed_serial(conn, embedder, store, need_resolve, stats,
                      pdp_fetcher, Fetcher(raw_dir=None), on_item, Image)

    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from ..embeddings import MarqoEmbedder
    from ..vectorstore import NumpyVectorStore

    ap = argparse.ArgumentParser(prog="mb-embed-images")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--serial", action="store_true", help="disable the concurrent fetch path")
    ap.add_argument("--batch", type=int, default=EMBED_BATCH)
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    store = NumpyVectorStore(conn)
    embedder = MarqoEmbedder()

    import time
    t0 = time.time()

    def prog(pid, url, stats):
        if stats["embedded"] % 100 == 0:
            done = stats["embedded"]
            rate = done / max(time.time() - t0, 1e-6)
            left = stats["targets"] - done
            eta = left / rate / 3600 if rate else 0
            print(f"  embedded={done} quarantined={stats['quarantined']} "
                  f"no_url={stats['no_image_url']} / {stats['targets']} "
                  f"| {rate:.2f} img/s | ETA {eta:.1f}h", file=sys.stderr, flush=True)

    stats = embed_images(conn, embedder, store, limit=args.limit, force=args.force,
                         on_item=prog, batch_size=args.batch, concurrent=not args.serial)
    print(f"\nimage embed stats: {stats}; image vectors: {store.count('image')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
