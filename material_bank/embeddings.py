"""Provider-agnostic embedder + catalog embedding pipeline.

Mirrors the provider-agnostic pattern of routers/render.py: one interface,
swappable backend. The locked backend is Marqo/marqo-ecommerce-embeddings-B
(Apache-2.0, 768-dim) via open_clip — text and image share one space, so a
text query and an image back-match hit the same index.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

import numpy as np

MODEL_NAME = "hf-hub:Marqo/marqo-ecommerce-embeddings-B"
EMBED_DIM = 768


class Embedder(Protocol):
    model_id: str
    def encode_text(self, texts: list[str]) -> np.ndarray: ...
    def encode_image(self, images: list) -> np.ndarray: ...  # list[PIL.Image]


class MarqoEmbedder:
    """marqo-ecommerce-B via open_clip. Model loads lazily (first encode)."""

    model_id = MODEL_NAME

    def __init__(self, name: str = MODEL_NAME):
        self._name = name
        self._model = None
        self._preprocess = None
        self._tokenizer = None

    def _ensure(self):
        if self._model is None:
            import open_clip  # heavy import, deferred so tests stay light
            import torch

            self._torch = torch
            model, _, preprocess = open_clip.create_model_and_transforms(self._name)
            model.eval()
            self._model = model
            self._preprocess = preprocess
            self._tokenizer = open_clip.get_tokenizer(self._name)

    def encode_text(self, texts: list[str]) -> np.ndarray:
        self._ensure()
        with self._torch.no_grad():
            feats = self._model.encode_text(self._tokenizer(texts))
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy().astype(np.float32)

    def encode_image(self, images: list) -> np.ndarray:
        self._ensure()
        batch = self._torch.stack([self._preprocess(im) for im in images])
        with self._torch.no_grad():
            feats = self._model.encode_image(batch)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy().astype(np.float32)


class FakeEmbedder:
    """Deterministic hashing embedder for offline tests (no torch/model)."""

    model_id = "fake-embedder"

    def __init__(self, dim: int = EMBED_DIM):
        self.dim = dim

    def _vec(self, seed_text: str) -> np.ndarray:
        h = abs(hash(seed_text)) % (2**32)
        rng = np.random.default_rng(h)
        v = rng.standard_normal(self.dim).astype(np.float32)
        return v / np.linalg.norm(v)

    def encode_text(self, texts: list[str]) -> np.ndarray:
        return np.vstack([self._vec("t:" + t) for t in texts])

    def encode_image(self, images: list) -> np.ndarray:
        return np.vstack([self._vec("i:" + str(im)) for im in images])


def product_text(row: sqlite3.Row) -> str:
    """The text we embed for a product (title + salient specs)."""
    parts = [row["title"] or "", row["category"] or ""]
    if row["size_mm"]:
        parts.append(f"size {row['size_mm']}")
    if row["finish"]:
        parts.append(f"{row['finish']} finish")
    return ". ".join(p for p in parts if p).strip()


def embed_catalog_text(
    conn: sqlite3.Connection,
    embedder: Embedder,
    store,
    *,
    batch_size: int = 64,
    force: bool = False,
    on_batch=None,
) -> dict:
    """Embed every product's text into the shared space. Resumable (skips
    already-embedded unless force)."""
    rows = list(conn.execute("SELECT * FROM products ORDER BY id"))
    if not force:
        done = store.embedded_ids("text")
        rows = [r for r in rows if r["id"] not in done]

    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        vecs = embedder.encode_text([product_text(r) for r in batch])
        store.upsert_many([(r["id"], vecs[j]) for j, r in enumerate(batch)],
                          kind="text", model=embedder.model_id)
        total += len(batch)
        if on_batch:
            on_batch(total, len(rows))
    return {"embedded": total, "skipped_existing": 0 if force else None}
