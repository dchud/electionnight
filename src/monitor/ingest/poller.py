"""Tiered feed polling with conditional GET and per-domain limits."""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import duckdb
import httpx

from monitor.archive import ArchiveWriter
from monitor.ingest.feeds import FeedEntry, parse_feed
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, hash_id
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

MAX_BACKOFF_MULTIPLIER = 8


@dataclass
class FeedState:
    """Per-feed polling state, kept in memory and restored from the lake."""

    source: Source
    url: str
    etag: str | None = None
    last_modified: str | None = None
    failures: int = 0
    circuit_open_until: float = 0.0
    last_success_at: datetime | None = None
    last_error: str | None = None
    n_new_total: int = 0

    @property
    def is_open(self) -> bool:
        return self.failures > 0 and monotonic() < self.circuit_open_until


def item_row(
    source: Source, feed_url: str, entry: FeedEntry, now: datetime
) -> dict[str, Any]:
    return {
        "item_id": hash_id(entry.canonical_url),
        "source_id": source.source_id,
        "feed_url": feed_url,
        "url": entry.url,
        "canonical_url": entry.canonical_url,
        "guid": entry.guid,
        "title": entry.title,
        "summary": entry.summary,
        "author": entry.author,
        "published_at": entry.published_at,
        "first_seen_at": now,
        "language": entry.language or source.language,
        "feed_categories": entry.categories,
        "raw_entry": entry.raw,
    }


def next_delay(settings: Settings, state: FeedState) -> float:
    """Seconds until the next poll: tier cadence, jitter, failure backoff."""
    poll = settings.poll
    index = min(state.source.poll_tier, len(poll.tier_seconds)) - 1
    delay = float(poll.tier_seconds[index])
    if state.failures:
        multiplier = min(2**state.failures, MAX_BACKOFF_MULTIPLIER)
        delay *= multiplier
    if state.failures >= poll.circuit_failures:
        delay = float(poll.circuit_retry_seconds)
    jitter = delay * poll.jitter_pct / 100
    return max(1.0, delay + random.uniform(-jitter, jitter))


def load_seen(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("SELECT item_id FROM lake.raw.items").fetchall()
    return {row[0] for row in rows}


def load_validators(
    conn: duckdb.DuckDBPyConnection,
) -> dict[str, tuple[str | None, str | None]]:
    """Latest ETag and Last-Modified per feed, so restarts stay conditional."""
    rows = conn.execute(
        """
        SELECT feed_url, etag, last_modified FROM lake.raw.fetches
        WHERE http_status = 200 AND (etag IS NOT NULL
                                     OR last_modified IS NOT NULL)
        QUALIFY row_number() OVER (
            PARTITION BY feed_url ORDER BY started_at DESC) = 1
        """
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def load_sightings(
    conn: duckdb.DuckDBPyConnection,
) -> dict[tuple[str, str], str]:
    """Latest title hash per item and feed."""
    rows = conn.execute(
        """
        SELECT item_id, feed_url, title_hash FROM lake.raw.sightings
        QUALIFY row_number() OVER (
            PARTITION BY item_id, feed_url ORDER BY seen_at DESC) = 1
        """
    ).fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


class Poller:
    """Polls every enabled feed on its tier and hands new items onward."""

    def __init__(
        self,
        settings: Settings,
        writer: BatchWriter,
        sources: list[Source],
        seen: set[str],
        validators: dict[str, tuple[str | None, str | None]] | None = None,
        on_new_items: Callable[[list[dict[str, Any]]], None] | None = None,
        client: httpx.AsyncClient | None = None,
        archive: ArchiveWriter | None = None,
        sightings: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.settings = settings
        self.archive = archive
        self.sightings = sightings if sightings is not None else {}
        self.writer = writer
        self.seen = seen
        self.on_new_items = on_new_items
        self.client: httpx.AsyncClient = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(settings.poll.timeout_seconds),
            headers={"User-Agent": settings.poll.user_agent},
            limits=httpx.Limits(max_connections=64),
        )
        self.paused_tiers: set[int] = set()
        self.domain_limits: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.poll.per_domain_concurrency)
        )
        validators = validators or {}
        self.feeds: list[FeedState] = []
        for source in sources:
            if not source.enabled:
                continue
            for url in source.feed_urls:
                etag, modified = validators.get(url, (None, None))
                self.feeds.append(FeedState(source, url, etag, modified))

    async def poll_once(self, state: FeedState) -> None:
        started = datetime.now(UTC)
        clock = monotonic()
        headers: dict[str, str] = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        domain = urlsplit(state.url).hostname or ""
        status: int | None = None
        error: str | None = None
        n_entries = 0
        n_new = 0
        new_rows: list[dict[str, Any]] = []
        details: dict[str, Any] | None = None
        try:
            async with self.domain_limits[domain]:
                response = await self.client.get(state.url, headers=headers)
            status = response.status_code
            if status == 200:
                state.etag = response.headers.get("etag")
                state.last_modified = response.headers.get("last-modified")
                feed = await asyncio.to_thread(parse_feed, response.content)
                n_entries = len(feed.entries)
                now = datetime.now(UTC)
                sighted = []
                for entry in feed.entries:
                    row = item_row(state.source, state.url, entry, now)
                    item_id = row["item_id"]
                    title_hash = hash_id(entry.title)
                    key = (item_id, state.url)
                    if self.sightings.get(key) != title_hash:
                        self.sightings[key] = title_hash
                        sighted.append(
                            {
                                "item_id": item_id,
                                "source_id": state.source.source_id,
                                "feed_url": state.url,
                                "seen_at": now,
                                "title_hash": title_hash,
                            }
                        )
                    if item_id not in self.seen:
                        self.seen.add(item_id)
                        new_rows.append(row)
                self.writer.add("raw.sightings", sighted)
                index_row = None
                if self.archive is not None:
                    index_row = self.archive.add(
                        "feeds",
                        state.url,
                        state.source.source_id,
                        response.content,
                        {"http_status": status},
                        skip_unchanged=True,
                    )
                    if index_row:
                        self.writer.add("raw.archive_index", [index_row])
                details = {
                    "n_bytes": len(response.content),
                    "bozo": feed.bozo,
                    "content_type": response.headers.get("content-type"),
                    "unchanged": self.archive is not None
                    and index_row is None,
                }
                n_new = len(new_rows)
                if n_entries == 0:
                    error = "no entries"
            elif status != 304:
                error = f"HTTP {status}"
        except Exception as exc:
            error = type(exc).__name__
        self._record(state, status, error, n_new)
        fetch_id = hash_id(state.url, started.isoformat())
        if details is not None:
            self.writer.add(
                "raw.fetch_details", [{"fetch_id": fetch_id, **details}]
            )
        self.writer.add(
            "raw.fetches",
            [
                {
                    "fetch_id": fetch_id,
                    "source_id": state.source.source_id,
                    "feed_url": state.url,
                    "started_at": started,
                    "duration_ms": int((monotonic() - clock) * 1000),
                    "http_status": status,
                    "not_modified": status == 304,
                    "n_entries": n_entries,
                    "n_new": n_new,
                    "error": error,
                    "etag": state.etag,
                    "last_modified": state.last_modified,
                }
            ],
        )
        if new_rows:
            self.writer.add("raw.items", new_rows)
            if self.on_new_items:
                self.on_new_items(new_rows)

    def _record(
        self,
        state: FeedState,
        status: int | None,
        error: str | None,
        n_new: int,
    ) -> None:
        if error is None:
            state.failures = 0
            state.circuit_open_until = 0.0
            state.last_error = None
            state.last_success_at = datetime.now(UTC)
            state.n_new_total += n_new
            return
        state.failures += 1
        state.last_error = error
        limit = self.settings.poll.circuit_failures
        if state.failures >= limit:
            retry = self.settings.poll.circuit_retry_seconds
            state.circuit_open_until = monotonic() + retry
            if state.failures == limit:
                log.warning(
                    "feed_circuit_open",
                    source_id=state.source.source_id,
                    feed_url=state.url,
                    error=error,
                    status=status,
                )

    async def run_feed(self, state: FeedState) -> None:
        # Spread the first poll across the tier so startup is not a burst.
        index = min(state.source.poll_tier, 3) - 1
        tier = self.settings.poll.tier_seconds[index]
        await asyncio.sleep(random.uniform(0, min(tier, 60)))
        while True:
            if state.source.poll_tier not in self.paused_tiers:
                await self.poll_once(state)
            await asyncio.sleep(next_delay(self.settings, state))

    async def run(self) -> None:
        try:
            await asyncio.gather(
                *(self.run_feed(state) for state in self.feeds)
            )
        finally:
            await self.client.aclose()

    def details(self) -> list[dict[str, Any]]:
        """Per-feed state for the status page."""
        return [
            {
                "source_id": feed.source.source_id,
                "feed_url": feed.url,
                "failures": feed.failures,
                "open": feed.is_open,
                "last_error": feed.last_error,
                "last_success_at": feed.last_success_at,
                "n_new": feed.n_new_total,
            }
            for feed in self.feeds
        ]

    def health(self) -> dict[str, int]:
        healthy = sum(1 for f in self.feeds if f.failures == 0)
        return {"healthy": healthy, "total": len(self.feeds)}
