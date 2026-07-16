"""Image preparation for multimodal enrichment: fetch → resize ≤384px → cache.

Gemini bills an image at a flat 258 tokens only when BOTH dimensions are ≤384px
(larger images tile at 258 tokens/tile, and Google's tiling math is inconsistent
enough that 384 is the only *guaranteed* single-tile size). So we resize every
product image to ≤384px once and content-address the result on disk — a re-run
never re-downloads or re-processes. A material swatch at 384px is plenty for
colour/pattern/surface-look. Dead URLs return None → the caller falls back to a
text-only prompt with a no_image flag (graceful degradation, never a hard fail).
"""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path

from PIL import Image

MAX_DIM = 384
# Hard source-size ceiling. Above this we SKIP the image (return None -> text-only
# enrichment) rather than decode it: an 83MP source decodes to ~250MB of raw RGB,
# and 8 parallel prep workers each holding one OOM-killed the drain on the 8GB box.
# 24MP is still generous for a 384px thumbnail (6000×4000); anything larger is a
# scan/bomb not worth the RAM (non-JPEG can't be draft-downscaled, so it decodes in
# full). PIL's own limit stays as a backstop for the absurd.
MAX_SRC_PIXELS = 24_000_000
Image.MAX_IMAGE_PIXELS = 64_000_000
_CACHE = Path(__file__).resolve().parent.parent / "data" / "img_cache"


def make_fetch(timeout: float = 20.0):
    """Fetch closure with a chosen timeout. Enrichment keeps the patient 20s
    default; the serving path (dashboard image proxy) passes a short one so a
    dead origin can't pin a browser image lane for 20s on first encounter."""
    def _fetch(url: str) -> bytes | None:
        try:
            from curl_cffi import requests
            r = requests.get(url, impersonate="chrome131", timeout=timeout)
            if r.status_code and 200 <= r.status_code < 300 and r.content:
                return r.content
        except Exception:
            pass
        return None
    return _fetch


_default_fetch = make_fetch()


def _resize(raw: bytes) -> bytes | None:
    """Resize to fit MAX_DIM (both dims ≤384) and re-encode JPEG. None if not an image.

    draft() BEFORE decode is load-bearing: it tells the JPEG decoder to emit at a
    reduced DCT scale (½, ¼, ⅛), so a 6000×4000 source never fully materialises as
    ~72 MB of raw RGB. Without it, 24 parallel prep workers each held a full-size
    decode and OOM-killed the drain on the 8 GB box (marqo already resident)."""
    try:
        im = Image.open(io.BytesIO(raw))
        if (im.width or 0) * (im.height or 0) > MAX_SRC_PIXELS:
            return None                            # pathological source: skip -> text-only
        im.draft("RGB", (MAX_DIM, MAX_DIM))        # cheap on JPEG, a no-op otherwise
        im = im.convert("RGB")
    except Exception:
        return None
    im.thumbnail((MAX_DIM, MAX_DIM))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def prepare_image(url: str, *, fetch=_default_fetch, cache_dir: Path | None = None) -> bytes | None:
    """Return ≤384px JPEG bytes for a product image URL, cached on disk. None if
    the URL is dead or not an image (caller degrades to text-only)."""
    if not url:
        return None
    cdir = cache_dir or _CACHE
    cdir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = cdir / f"{key}.jpg"
    miss = cdir / f"{key}.miss"
    if path.exists():
        return path.read_bytes()
    if miss.exists():                       # remembered dead URL — don't refetch every run
        return None
    resized = _resize(fetch(url) or b"")
    if resized is None:
        miss.write_bytes(b"")
        return None
    path.write_bytes(resized)
    return resized


def as_inline_data(jpeg: bytes) -> dict:
    """Gemini inline_data part for a JPEG image."""
    return {"inline_data": {"mime_type": "image/jpeg",
                            "data": base64.b64encode(jpeg).decode("ascii")}}
