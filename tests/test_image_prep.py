"""Image prep: resize to <=384px, content-addressed cache, dead-URL memo."""

import base64
import io

import numpy as np
from PIL import Image

from material_bank import image_prep as ip


def _png(w, h):
    b = io.BytesIO()
    Image.fromarray(np.full((h, w, 3), (200, 150, 100), "uint8"), "RGB").save(b, "PNG")
    return b.getvalue()


def test_resizes_to_max_dim_and_caches(tmp_path):
    calls = {"n": 0}
    def fetch(url):
        calls["n"] += 1
        return _png(1200, 800)                         # oversized source
    out = ip.prepare_image("https://img/x.png", fetch=fetch, cache_dir=tmp_path)
    im = Image.open(io.BytesIO(out))
    assert max(im.size) <= ip.MAX_DIM and im.format == "JPEG"
    # second call is served from cache — no refetch
    ip.prepare_image("https://img/x.png", fetch=fetch, cache_dir=tmp_path)
    assert calls["n"] == 1


def test_dead_url_returns_none_and_is_memoized(tmp_path):
    calls = {"n": 0}
    def dead(url):
        calls["n"] += 1
        return None
    assert ip.prepare_image("https://img/dead.png", fetch=dead, cache_dir=tmp_path) is None
    ip.prepare_image("https://img/dead.png", fetch=dead, cache_dir=tmp_path)
    assert calls["n"] == 1                              # memoized, not refetched


def test_non_image_bytes_return_none(tmp_path):
    assert ip.prepare_image("https://x/y", fetch=lambda u: b"not an image", cache_dir=tmp_path) is None


def test_as_inline_data_is_valid_base64_jpeg():
    part = ip.as_inline_data(_png(50, 50) and ip._resize(_png(50, 50)))
    assert part["inline_data"]["mime_type"] == "image/jpeg"
    assert base64.b64decode(part["inline_data"]["data"])[:2] == b"\xff\xd8"   # JPEG magic
