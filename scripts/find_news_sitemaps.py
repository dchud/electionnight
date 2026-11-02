#!/usr/bin/env python3
"""Find each enabled outlet's Google News sitemap and write the catch-up list.

Reads the outlet's robots.txt, and keeps a sitemap only if robots.txt lists it,
allows this crawler to read it, and it holds news entries with titles. A
sitemap that several outlets list goes to the one whose language matches its
entries, and is left out if none does.

    uv run python scripts/find_news_sitemaps.py
"""

from __future__ import annotations

import asyncio
import csv
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from monitor.ingest.feeds import (
    ParsedFeed,
    main_language,
    parse_news_sitemap,
    same_site,
    sitemap_children,
)
from monitor.ingest.poller import MAX_SITEMAP_CHILDREN
from monitor.ingest.sources import Source, load_sources, outlet_for_language
from monitor.settings import load_settings

OUTPUT = Path("config/news_sitemaps.csv")
SITEMAP_LINE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")
CONCURRENCY = 16
RECENT = timedelta(days=7)
TIMEOUT_SECONDS = 12.0
AGENT_TOKEN = "ElectionNightMonitor"


@dataclass
class Found:
    source: Source
    url: str
    entries: int
    language: str


def recent_entries(content: bytes) -> ParsedFeed:
    """A sitemap's entries from the last week. An old one cannot help."""
    since = datetime.now(UTC) - RECENT
    feed = parse_news_sitemap(content)
    feed.entries = [
        entry
        for entry in feed.entries
        if entry.published_at is None or entry.published_at >= since
    ]
    return feed


async def entries_in(client: httpx.AsyncClient, url: str) -> tuple[int, str]:
    """How many entries a sitemap holds, and the language of most of them."""
    response = await client.get(url)
    if response.status_code != 200:
        return 0, ""
    children = sitemap_children(response.content)
    if not children:
        feed = recent_entries(response.content)
        return len(feed.entries), main_language(feed)
    listed = [child for child in children if same_site(url, child)]
    for child in listed[:MAX_SITEMAP_CHILDREN]:
        inner = await client.get(child)
        feed = recent_entries(inner.content)
        if feed.entries:
            return len(feed.entries), main_language(feed)
    return 0, ""


async def find(client: httpx.AsyncClient, source: Source) -> Found | None:
    parts = urlsplit(source.homepage or source.feed_urls[0])
    try:
        robots = await client.get(
            f"{parts.scheme}://{parts.netloc}/robots.txt"
        )
        if robots.status_code != 200:
            return None
        rules = RobotFileParser()
        rules.parse(robots.text.splitlines())
        for url in SITEMAP_LINE.findall(robots.text):
            if not url.startswith(("http://", "https://")):
                continue
            if "news" not in url.lower() or url.endswith(".gz"):
                continue
            if not rules.can_fetch(AGENT_TOKEN, url):
                continue
            entries, language = await entries_in(client, url)
            if entries:
                return Found(source, url, entries, language)
    except (httpx.HTTPError, ValueError):
        return None
    return None


def one_outlet_per_url(found: list[Found]) -> list[Found]:
    """One row per sitemap. A shared one goes to the outlet in its language."""
    by_url: dict[str, list[Found]] = {}
    for row in found:
        by_url.setdefault(row.url, []).append(row)
    kept = []
    for url, rows in by_url.items():
        if len(rows) == 1:
            kept.append(rows[0])
            continue
        language = rows[0].language
        outlet = outlet_for_language([row.source for row in rows], language)
        if outlet is None:
            print(f"left out {url}: no outlet here writes in {language!r}")
            continue
        kept.append(Found(outlet, url, rows[0].entries, language))
    return kept


def write_list(found: list[Found]) -> None:
    today = datetime.now(UTC).date().isoformat()
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["source_id", "url", "entries_seen", "checked_on"])
        for row in found:
            writer.writerow(
                [row.source.source_id, row.url, row.entries, today]
            )


async def main() -> None:
    settings = load_settings()
    sources = [s for s in load_sources(settings) if s.enabled and s.feed_urls]
    limit = asyncio.Semaphore(CONCURRENCY)

    async def bounded(
        client: httpx.AsyncClient, source: Source
    ) -> Found | None:
        async with limit:
            return await find(client, source)

    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": settings.poll.user_agent},
    ) as client:
        results = await asyncio.gather(*(bounded(client, s) for s in sources))
    found = sorted(
        one_outlet_per_url([r for r in results if r]),
        key=lambda row: row.source.source_id,
    )
    write_list(found)
    print(f"{len(found)} of {len(sources)} outlets; wrote {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
