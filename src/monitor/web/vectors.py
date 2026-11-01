"""Item and storyline vectors in memory, for search and nearest neighbors.

Vectors are read through the read model's read-only connection into numpy
matrices and refreshed incrementally when the lake has a new snapshot. The
embedding model loads on the first query. Everything here blocks, so callers
run it off the event loop: FastAPI runs a plain `def` route in a thread.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb
import numpy as np

from monitor.logs import get
from monitor.settings import Settings
from monitor.web.readmodel import VECTOR_DIM, WATERMARK_OVERLAP, ReadModel

log = get(__name__)

GROWTH = 1.5
MIN_CAPACITY = 1024
MAX_QUERY_CHARS = 500


def unit_rows(matrix: np.ndarray) -> np.ndarray:
    """Scale each row to length 1, leaving all-zero rows at zero."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class VectorIndex:
    """Unit vectors keyed by id, with brute-force cosine ranking.

    Readers take a view of the matrix without a lock. New rows go past the end
    of any view already taken, and growing replaces the array rather than
    resizing it, so a search in progress keeps a consistent matrix.
    """

    def __init__(self, dim: int = VECTOR_DIM) -> None:
        self.dim = dim
        self._matrix = np.zeros((0, dim), dtype=np.float32)
        self._count = 0
        self._ids: list[str] = []
        self._rows: dict[str, int] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return self._count

    def upsert(self, ids: list[str], vectors: np.ndarray) -> None:
        """Replace known ids in place and append the rest."""
        if not ids:
            return
        vectors = unit_rows(vectors.astype(np.float32))
        with self._lock:
            fresh = sum(1 for key in set(ids) if key not in self._rows)
            needed = self._count + fresh
            if needed > self._matrix.shape[0]:
                capacity = max(needed, int(self._count * GROWTH), MIN_CAPACITY)
                grown = np.zeros((capacity, self.dim), dtype=np.float32)
                grown[: self._count] = self._matrix[: self._count]
                self._matrix = grown
            for position, key in enumerate(ids):
                row = self._rows.get(key)
                if row is None:
                    row = self._count
                    self._matrix[row] = vectors[position]
                    self._ids.append(key)
                    self._count += 1
                    self._rows[key] = row
                else:
                    self._matrix[row] = vectors[position]

    def vector(self, key: str) -> np.ndarray | None:
        row = self._rows.get(key)
        return None if row is None else self._matrix[row].copy()

    def rank(
        self, query: np.ndarray, eligible: Iterable[str], limit: int
    ) -> list[tuple[str, float]]:
        """The `limit` eligible ids most similar to a unit query vector."""
        rows = np.fromiter(
            (row for row in map(self._rows.get, eligible) if row is not None),
            dtype=np.int64,
        )
        if rows.size == 0 or limit <= 0:
            return []
        matrix = self._matrix[: self._count]
        scores = (matrix @ query)[rows]
        limit = min(limit, rows.size)
        top = np.argpartition(-scores, limit - 1)[:limit]
        ordered = top[np.argsort(-scores[top], kind="stable")]
        return [(self._ids[rows[i]], float(scores[i])) for i in ordered]


def _arrow_to_matrix(table: Any) -> tuple[list[str], np.ndarray]:
    """Ids and vectors from an Arrow table of `id` and a float list `vec`."""
    if table.num_rows == 0:
        return [], np.zeros((0, VECTOR_DIM), dtype=np.float32)
    ids = table.column("id").to_pylist()
    flat = table.column("vec").combine_chunks().flatten()
    matrix = flat.to_numpy(zero_copy_only=False).astype(np.float32)
    return ids, matrix.reshape(len(ids), VECTOR_DIM)


class SearchUnavailable(RuntimeError):
    """The embedding model could not be loaded."""


def load_cached_model(settings: Settings) -> Any:
    """Load the model from the cache the pipeline filled, with no download."""
    from monitor.parsers.embed import load_embedding_model

    os.environ["HF_HUB_OFFLINE"] = "1"
    return load_embedding_model(settings)


@dataclass
class Watermark:
    snapshot_id: int = -1
    read_at: datetime | None = None


class Vectors:
    """The item and storyline indexes, and the query embedder."""

    def __init__(
        self,
        settings: Settings,
        model: ReadModel,
        load_model: Callable[[Settings], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.model = model
        self.items = VectorIndex()
        self.storylines = VectorIndex()
        self._load_model = load_model or load_cached_model
        self._embedder: Any = None
        self._embedder_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._watermark = Watermark()

    def refresh(self) -> None:
        """Read vectors committed since the last refresh.

        A failed read leaves the indexes as they are, and the next call
        retries from the same watermark.
        """
        with self._refresh_lock:
            snapshot_id = self.model.snapshot.snapshot_id
            if snapshot_id == self._watermark.snapshot_id:
                return
            now = datetime.now(UTC)
            read_at = self._watermark.read_at
            since = (
                datetime(2000, 1, 1, tzinfo=UTC)
                if read_at is None
                else read_at - WATERMARK_OVERLAP
            )
            try:
                items = self.model.embeddings_since(
                    since, self.settings.nlp.embed_model
                )
                centroids = self.model.centroids_since(since)
            except duckdb.Error as error:
                log.warning("vector_refresh_failed", error=str(error)[:300])
                return
            self.items.upsert(*_arrow_to_matrix(items))
            self.storylines.upsert(*_arrow_to_matrix(centroids))
            self._watermark = Watermark(snapshot_id, now)

    def _embedding_model(self) -> Any:
        if self._embedder is None:
            with self._embedder_lock:
                if self._embedder is None:
                    try:
                        self._embedder = self._load_model(self.settings)
                    except Exception as error:
                        log.warning(
                            "embedding_model_failed", error=str(error)[:300]
                        )
                        raise SearchUnavailable(str(error)) from error
        return self._embedder

    def embed_query(self, text: str) -> np.ndarray:
        """A unit vector for a search query, with the model's query prefix."""
        from monitor.parsers.embed import QUERY_PREFIX

        model = self._embedding_model()
        prompt = f"{QUERY_PREFIX}{text[:MAX_QUERY_CHARS]}"
        with self._embedder_lock:
            vectors = list(model.embed([prompt]))
        matrix = np.asarray(vectors, dtype=np.float32)
        return unit_rows(matrix)[0]
