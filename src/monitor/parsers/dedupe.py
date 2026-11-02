"""Near-duplicate grouping, for syndicated copies of a story."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import xxhash

from monitor.lake import hash_id
from monitor.orchestrator import Database
from monitor.parsers.base import Item, ParserOutput, needs_parser_sql
from monitor.parsers.classify_a import STATE_NAMES
from monitor.parsers.embed import EMBED_DIM
from monitor.settings import Settings

# A colon starts a headline's second half, not an outlet's name. French also
# puts a space before it.
SUFFIX_RE = re.compile(r"\s+[-–—|]\s+([^-–—|:]{2,40})$")
WORD_RE = re.compile(r"\w+", re.UNICODE)
CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")
INITIAL_CAPACITY = 4096
MAX_OUTLET_WORDS = 4
MIN_WORDS_BEFORE_SUFFIX = 3
# Four words make seven tokens, as do eight Chinese characters. Shorter titles
# such as "Live updates" repeat across unrelated stories.
MIN_TITLE_TOKENS = 7
OUTLET_CONNECTORS = {"of", "the", "and", "de", "la", "le", "el", "du", "des"}
# With a state's name, these words make a headline's clause, not an outlet.
RACE_WORDS = {
    "senate",
    "governor",
    "house",
    "race",
    "results",
    "election",
    "elections",
    "polls",
    "votes",
    "county",
    "district",
    "live",
    "updates",
}


def _is_outlet_name(suffix: str) -> bool:
    """True for capitalized words, such as "AP News" or "Texas Tribune".

    A clause such as "Ossoff wins", "Georgia", or "Arizona Senate" is part of
    the headline.
    """
    words = suffix.split()
    if len(words) > MAX_OUTLET_WORDS:
        return False
    lowered = suffix.lower()
    for name in STATE_NAMES:
        lowered = lowered.replace(name, " ")
    if lowered != suffix.lower() and set(lowered.split()) <= RACE_WORDS:
        return False
    return all(
        word[0].isdigit()
        or word[0].isupper()
        or word.lower() in OUTLET_CONNECTORS
        for word in words
    )


def normalize_title(title: str) -> str:
    """Lowercase, remove punctuation and a trailing outlet name."""
    trimmed = title.strip()
    match = SUFFIX_RE.search(trimmed)
    if match and _is_outlet_name(match.group(1)):
        before = trimmed[: match.start()]
        if len(WORD_RE.findall(before)) >= MIN_WORDS_BEFORE_SUFFIX:
            trimmed = before
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


def title_hash_of(title: str) -> int | None:
    """A title's hash, or None when it is too short to identify a story."""
    normalized = normalize_title(title)
    if len(title_tokens(normalized)) < MIN_TITLE_TOKENS:
        return None
    return simhash64(normalized)


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
        self.titled = np.zeros(INITIAL_CAPACITY, dtype=bool)
        self.relevant = np.zeros(INITIAL_CAPACITY, dtype=bool)
        self.vectors = np.zeros(
            (INITIAL_CAPACITY, EMBED_DIM), dtype=np.float32
        )
        self.times = np.zeros(INITIAL_CAPACITY, dtype=np.float64)
        self.days: list[str] = []
        self.groups: list[str] = []
        self.states: list[frozenset[str]] = []
        self.positions: dict[str, int] = {}
        self.last_nearest: tuple[str | None, float | None, int | None] = (
            None,
            None,
            None,
        )

    def _grow(self) -> None:
        capacity = len(self.hashes) * 2
        self.hashes = np.resize(self.hashes, capacity)
        self.titled = np.resize(self.titled, capacity)
        self.relevant = np.resize(self.relevant, capacity)
        vectors = np.zeros((capacity, EMBED_DIM), dtype=np.float32)
        vectors[: self.size] = self.vectors[: self.size]
        self.vectors = vectors
        self.times = np.resize(self.times, capacity)

    def add(
        self,
        title_hash: int | None,
        vector: np.ndarray,
        seen_at: datetime,
        day: str,
        group_id: str,
        item_id: str | None = None,
        states: Iterable[str] = (),
        relevant: bool = False,
    ) -> None:
        """Add an item, or replace it if this index already holds it."""
        position = self.positions.get(item_id) if item_id else None
        if position is not None:
            self._write(position, title_hash, vector, seen_at, relevant)
            self.days[position] = day
            self.groups[position] = group_id
            self.states[position] = frozenset(states)
            return
        if self.size == len(self.hashes):
            self._grow()
        self._write(self.size, title_hash, vector, seen_at, relevant)
        self.days.append(day)
        self.groups.append(group_id)
        self.states.append(frozenset(states))
        if item_id:
            self.positions[item_id] = self.size
        self.size += 1

    def _write(
        self,
        position: int,
        title_hash: int | None,
        vector: np.ndarray,
        seen_at: datetime,
        relevant: bool,
    ) -> None:
        self.hashes[position] = title_hash or 0
        self.titled[position] = title_hash is not None
        self.relevant[position] = relevant
        self.vectors[position] = vector
        self.times[position] = seen_at.timestamp()

    def _same_place(self, index: int, states: frozenset[str]) -> bool:
        """False only when both items name states and share none."""
        other = self.states[index]
        return not states or not other or bool(states & other)

    def find(
        self,
        title_hash: int | None,
        vector: np.ndarray,
        seen_at: datetime,
        day: str,
        item_id: str | None = None,
        states: Iterable[str] = (),
        relevant: bool = False,
    ) -> Match | None:
        """The item's duplicate group, if it has one.

        An item already in the index is being run again. It never matches
        itself, and it matches only items seen before it, so re-running every
        item leaves each group's first item canonical. A relevant item joins
        only groups started by a relevant item, so the canonical copy of a
        relevant story is always relevant.
        """
        self.last_nearest = (None, None, None)
        if self.size == 0:
            return None
        now = seen_at.timestamp()
        span = self.window.total_seconds()
        in_window = np.abs(self.times[: self.size] - now) <= span
        own = self.positions.get(item_id) if item_id else None
        if own is not None:
            in_window[own] = False
        if relevant:
            in_window &= self.relevant[: self.size]
        distances = np.bitwise_count(
            self.hashes[: self.size] ^ np.uint64(title_hash or 0)
        )
        scores = self.vectors[: self.size] @ vector
        if in_window.any():
            windowed = np.flatnonzero(in_window)
            top = windowed[np.argmax(scores[windowed])]
            self.last_nearest = (
                self.groups[top],
                float(scores[top]),
                int(distances[windowed].min()),
            )
        if own is not None:
            same_poll = self.times[: self.size] == now
            same_poll[own:] = False
            in_window &= (self.times[: self.size] < now) | same_poll
        place = frozenset(states)
        if title_hash is not None:
            # A near-identical title can be a template with only the state
            # changed, so it needs a shared place. An identical title from
            # places that share no state also needs matching text: a copy of
            # one story matches, and a generic title used by two outlets does
            # not. A story with no state names no other place, so the title
            # alone settles it, even when one copy has no summary to embed.
            close = [
                index
                for index in np.flatnonzero(
                    in_window
                    & self.titled[: self.size]
                    & (distances <= self.max_distance)
                )
                if self._same_place(int(index), place)
                or (distances[index] == 0 and scores[index] >= self.min_cosine)
            ]
            if close:
                best = min(close, key=lambda index: distances[index])
                return Match(self.groups[best], "title_hash", 1.0)
        candidates = np.flatnonzero(in_window & (scores >= self.min_cosine))
        same_day = [
            i
            for i in candidates
            if self.days[i] == day and self._same_place(int(i), place)
        ]
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
                   coalesce(i.published_at, i.first_seen_at), d.dup_group_id,
                   d.item_id, coalesce(c.states, []),
                   coalesce(c.election_relevant
                            AND c.relevance_conf >= $threshold, FALSE)
            FROM lake.nlp.dup_groups d
            JOIN lake.raw.items i USING (item_id)
            JOIN lake.nlp.embeddings e USING (item_id)
            LEFT JOIN (
                SELECT item_id, states, election_relevant, relevance_conf
                FROM lake.nlp.classify_a
                QUALIFY row_number() OVER (
                    PARTITION BY item_id ORDER BY created_at DESC) = 1
            ) c USING (item_id)
            WHERE i.first_seen_at >= $since
            QUALIFY row_number() OVER (
                PARTITION BY d.item_id ORDER BY d.created_at DESC) = 1
            """,
            {
                "since": since,
                "threshold": self.settings.nlp.relevance_threshold,
            },
        )
        for row in rows:
            title, vec, seen_at, published, group, item_id, states, keep = row
            self.index.add(
                title_hash_of(title or ""),
                np.array(vec, dtype=np.float32),
                seen_at,
                published.strftime("%Y-%m-%d"),
                group,
                item_id,
                states,
                keep,
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
            title_hash = title_hash_of(item.title)
            seen_at = item.first_seen_at or datetime.now(UTC)
            day = published_day(item)
            threshold = self.settings.nlp.relevance_threshold
            relevant = item.relevant and item.relevance_conf >= threshold
            match = self.index.find(
                title_hash,
                item.vec,
                seen_at,
                day,
                item.item_id,
                item.states,
                relevant,
            )
            group_id = (
                match.group_id if match else hash_id("dup", item.item_id)
            )
            item.is_canonical = match is None
            nearest, cosine, distance = self.index.last_nearest
            self.index.add(
                title_hash,
                item.vec,
                seen_at,
                day,
                group_id,
                item.item_id,
                item.states,
                relevant,
            )
            output.add(
                "nlp.cluster_decisions",
                {
                    "item_id": item.item_id,
                    "stage": "dedupe",
                    "decision": "duplicate" if match else "new",
                    "method": match.method if match else None,
                    "nearest_id": nearest,
                    "nearest_similarity": cosine,
                    "threshold": self.index.min_cosine,
                    "simhash_distance": distance,
                    "parser": self.name,
                    "parser_version": self.version,
                },
            )
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
