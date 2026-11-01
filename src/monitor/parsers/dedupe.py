"""Near-duplicate grouping, for syndicated copies of a story."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import xxhash

from monitor.lake import hash_id
from monitor.orchestrator import Database
from monitor.parsers.base import Item, ParserOutput, needs_parser_sql
from monitor.parsers.embed import EMBED_DIM
from monitor.settings import Settings

SUFFIX_RE = re.compile(r"\s+[-–—|:]\s+[^-–—|:]{2,40}$")
WORD_RE = re.compile(r"\w+", re.UNICODE)
CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")
INITIAL_CAPACITY = 4096


def normalize_title(title: str) -> str:
    """Lowercase, remove punctuation and a trailing outlet name."""
    trimmed = SUFFIX_RE.sub("", title.strip())
    return " ".join(WORD_RE.findall(trimmed.lower()))


def title_tokens(normalized: str) -> list[str]:
    """Words and word pairs; character pairs for CJK text."""
    if CJK_RE.search(normalized):
        compact = normalized.replace(" ", "")
        return [compact[i : i + 2] for i in range(len(compact) - 1)] or [
            compact
        ]
    words = normalized.split()
    return words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


def simhash64(normalized: str) -> int:
    weights = np.zeros(64, dtype=np.int64)
    for token in title_tokens(normalized):
        value = xxhash.xxh64(token.encode("utf-8")).intdigest()
        for bit in range(64):
            weights[bit] += 1 if (value >> bit) & 1 else -1
    result = 0
    for bit in range(64):
        if weights[bit] > 0:
            result |= 1 << bit
    return result


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass
class Match:
    group_id: str
    method: str
    similarity: float


class DedupeIndex:
    """The recent window of items, in arrays for fast comparison."""

    def __init__(self, settings: Settings) -> None:
        nlp = settings.nlp
        self.window = timedelta(hours=nlp.dedupe_window_hours)
        self.max_distance = nlp.dedupe_simhash_distance
        self.min_cosine = nlp.dedupe_cosine
        self.size = 0
        self.hashes = np.zeros(INITIAL_CAPACITY, dtype=np.uint64)
        self.vectors = np.zeros(
            (INITIAL_CAPACITY, EMBED_DIM), dtype=np.float32
        )
        self.times = np.zeros(INITIAL_CAPACITY, dtype=np.float64)
        self.days: list[str] = []
        self.groups: list[str] = []

    def _grow(self) -> None:
        capacity = len(self.hashes) * 2
        self.hashes = np.resize(self.hashes, capacity)
        vectors = np.zeros((capacity, EMBED_DIM), dtype=np.float32)
        vectors[: self.size] = self.vectors[: self.size]
        self.vectors = vectors
        self.times = np.resize(self.times, capacity)

    def add(
        self,
        title_hash: int,
        vector: np.ndarray,
        seen_at: datetime,
        day: str,
        group_id: str,
    ) -> None:
        if self.size == len(self.hashes):
            self._grow()
        self.hashes[self.size] = title_hash
        self.vectors[self.size] = vector
        self.times[self.size] = seen_at.timestamp()
        self.days.append(day)
        self.groups.append(group_id)
        self.size += 1

    def find(
        self,
        title_hash: int,
        vector: np.ndarray,
        seen_at: datetime,
        day: str,
    ) -> Match | None:
        if self.size == 0:
            return None
        now = seen_at.timestamp()
        span = self.window.total_seconds()
        in_window = np.abs(self.times[: self.size] - now) <= span
        distances = np.bitwise_count(
            self.hashes[: self.size] ^ np.uint64(title_hash)
        )
        close = np.flatnonzero(in_window & (distances <= self.max_distance))
        if close.size:
            best = close[np.argmin(distances[close])]
            return Match(self.groups[best], "title_hash", 1.0)
        scores = self.vectors[: self.size] @ vector
        candidates = np.flatnonzero(in_window & (scores >= self.min_cosine))
        same_day = [i for i in candidates if self.days[i] == day]
        if same_day:
            best = max(same_day, key=lambda i: scores[i])
            return Match(self.groups[best], "embedding", float(scores[best]))
        return None


def published_day(item: Item) -> str:
    when = item.published_at or item.first_seen_at
    return when.strftime("%Y-%m-%d") if when else ""


class DedupeParser:
    name = "dedupe"
    version = "1"
    batch_size = 64
    max_concurrency = 1
    input_query = needs_parser_sql(
        name,
        version,
        "i.item_id IN (SELECT item_id FROM lake.nlp.embeddings)",
    )

    def __init__(
        self,
        settings: Settings,
        db: Database,
        index: DedupeIndex | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.index = index or DedupeIndex(settings)
        self.loaded = index is not None

    async def _load_window(self) -> None:
        since = datetime.now(UTC) - self.index.window
        rows = await self.db.fetch(
            """
            SELECT i.title, e.vec, i.first_seen_at,
                   coalesce(i.published_at, i.first_seen_at), d.dup_group_id
            FROM lake.nlp.dup_groups d
            JOIN lake.raw.items i USING (item_id)
            JOIN lake.nlp.embeddings e USING (item_id)
            WHERE i.first_seen_at >= $since
            QUALIFY row_number() OVER (
                PARTITION BY d.item_id ORDER BY d.created_at DESC) = 1
            """,
            {"since": since},
        )
        for title, vec, seen_at, published, group_id in rows:
            self.index.add(
                simhash64(normalize_title(title or "")),
                np.array(vec, dtype=np.float32),
                seen_at,
                published.strftime("%Y-%m-%d"),
                group_id,
            )
        self.loaded = True

    async def _attach_vectors(self, batch: list[Item]) -> None:
        missing = [item for item in batch if item.vec is None]
        if not missing:
            return
        rows = await self.db.fetch(
            "SELECT item_id, vec FROM lake.nlp.embeddings "
            "WHERE item_id IN (SELECT unnest($ids))",
            {"ids": [item.item_id for item in missing]},
        )
        vectors = {row[0]: row[1] for row in rows}
        for item in missing:
            if item.item_id in vectors:
                item.vec = np.array(vectors[item.item_id], dtype=np.float32)

    async def run(self, batch: list[Item]) -> ParserOutput:
        if not self.loaded:
            await self._load_window()
        await self._attach_vectors(batch)
        output = ParserOutput()
        for item in sorted(
            batch, key=lambda i: i.first_seen_at or datetime.now(UTC)
        ):
            if item.vec is None:
                output.mark(item.item_id, "retry", "embedding not committed")
                continue
            title_hash = simhash64(normalize_title(item.title))
            seen_at = item.first_seen_at or datetime.now(UTC)
            day = published_day(item)
            match = self.index.find(title_hash, item.vec, seen_at, day)
            group_id = (
                match.group_id if match else hash_id("dup", item.item_id)
            )
            item.is_canonical = match is None
            self.index.add(title_hash, item.vec, seen_at, day, group_id)
            output.add(
                "nlp.dup_groups",
                {
                    "item_id": item.item_id,
                    "dup_group_id": group_id,
                    "is_canonical": match is None,
                    "method": match.method if match else "none",
                    "similarity": match.similarity if match else None,
                    "parser": self.name,
                    "parser_version": self.version,
                },
            )
        return output
