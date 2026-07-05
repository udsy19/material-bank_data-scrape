"""In-process vector index inside catalog.db — one index, three uses
(Explore back-match / Specify retrieval / novelty gate).

Deviation from the locked stack (flagged): the plan names sqlite-vec (vec0),
but this platform's sqlite3 build cannot load extensions and no pysqlite3 wheel
exists for it. Vectors are stored as a normalized float32 BLOB and searched by
numpy cosine — milliseconds at catalog scale (4k, and fine to ~1e5). Everything
sits behind :class:`VectorStore` so a sqlite-vec backend drops in unchanged on
an extension-capable sqlite build.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

import numpy as np

from .db import now_iso


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def normalize(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class VectorStore(Protocol):
    def upsert(self, product_id: int, kind: str, vector: np.ndarray, model: str) -> None: ...
    def search(self, query: np.ndarray, *, kind: str, k: int = 10) -> list[tuple[int, float]]: ...
    def count(self, kind: str) -> int: ...


class NumpyVectorStore:
    """BLOB-backed store with brute-force cosine search over catalog.db."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, product_id: int, kind: str, vector: np.ndarray, model: str) -> None:
        v = normalize(vector)
        self.conn.execute(
            """
            INSERT INTO embeddings (product_id, kind, model, dim, vector, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(product_id, kind) DO UPDATE SET
                model=excluded.model, dim=excluded.dim, vector=excluded.vector,
                created_at=excluded.created_at
            """,
            (product_id, kind, model, int(v.shape[0]), to_blob(v), now_iso()),
        )
        self.conn.commit()

    def upsert_many(self, rows: list[tuple[int, np.ndarray]], *, kind: str, model: str) -> int:
        payload = [
            (pid, kind, model, int(normalize(v).shape[0]), to_blob(normalize(v)), now_iso())
            for pid, v in rows
        ]
        self.conn.executemany(
            """
            INSERT INTO embeddings (product_id, kind, model, dim, vector, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(product_id, kind) DO UPDATE SET
                model=excluded.model, dim=excluded.dim, vector=excluded.vector,
                created_at=excluded.created_at
            """,
            payload,
        )
        self.conn.commit()
        return len(payload)

    def _load_matrix(self, kind: str) -> tuple[np.ndarray, np.ndarray]:
        ids, vecs = [], []
        for r in self.conn.execute(
            "SELECT product_id, vector FROM embeddings WHERE kind=?", (kind,)
        ):
            ids.append(r[0])
            vecs.append(from_blob(r[1]))
        if not ids:
            return np.array([], dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
        return np.array(ids, dtype=np.int64), np.vstack(vecs)

    def search(self, query: np.ndarray, *, kind: str, k: int = 10) -> list[tuple[int, float]]:
        ids, mat = self._load_matrix(kind)
        if ids.size == 0:
            return []
        q = normalize(query)
        sims = mat @ q            # rows already normalized => dot = cosine
        top = np.argsort(-sims)[:k]
        return [(int(ids[i]), float(sims[i])) for i in top]

    def count(self, kind: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE kind=?", (kind,)
        ).fetchone()[0]

    def embedded_ids(self, kind: str) -> set[int]:
        return {r[0] for r in self.conn.execute(
            "SELECT product_id FROM embeddings WHERE kind=?", (kind,))}
