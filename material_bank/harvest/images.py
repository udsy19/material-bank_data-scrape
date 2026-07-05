"""Image-embedding backfill — the Explore back-match bridge.

For each product without an ``image`` vector: resolve its image_url (from the
row, or by re-parsing its PDP if the row predates image_url capture), download
the image, encode it with the same shared-space model, and store the vector.
Resumable (skips products already image-embedded); failures are quarantined.

Image fetches hit images.* while PDP re-parses hit www.* — different hosts, so
the ~2s/host politeness budgets interleave rather than stack.
"""

from __future__ import annotations

import io
import sqlite3

from .. import db
from ..fetch import Fetcher
from . import orientbell as ob


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
) -> dict:
    from PIL import Image  # deferred so importing this module stays light

    pdp_fetcher = pdp_fetcher or Fetcher()
    image_fetcher = image_fetcher or Fetcher(raw_dir=None)  # don't archive images

    done = set() if force else store.embedded_ids("image")
    rows = [r for r in conn.execute("SELECT id, image_url FROM products ORDER BY id")
            if r["id"] not in done]
    if limit is not None:
        rows = rows[:limit]

    stats = {"targets": len(rows), "embedded": 0, "no_image_url": 0, "quarantined": 0}
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


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from ..embeddings import MarqoEmbedder
    from ..vectorstore import NumpyVectorStore

    ap = argparse.ArgumentParser(prog="mb-embed-images")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.migrate(conn)
    store = NumpyVectorStore(conn)
    embedder = MarqoEmbedder()

    def prog(pid, url, stats):
        if stats["embedded"] % 25 == 0:
            print(f"  embedded={stats['embedded']} quarantined={stats['quarantined']} "
                  f"no_url={stats['no_image_url']} / {stats['targets']}", file=sys.stderr)

    stats = embed_images(conn, embedder, store, limit=args.limit, force=args.force, on_item=prog)
    print(f"\nimage embed stats: {stats}; image vectors: {store.count('image')}", file=sys.stderr)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
