#!/usr/bin/env python3
"""Find each enabled outlet's Google News sitemap and write the catch-up list.

Reads the outlet's robots.txt, and keeps a sitemap only if robots.txt lists it,
allows this crawler to read it, and it holds news entries with titles.

    uv run python scripts/find_news_sitemaps.py
"""

from __future__ import annotations

import asyncio
import csv
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from monitor.ingest.feeds import (
    parse_news_sitemap,
    same_site,
    sitemap_children,
)
from monitor.ingest.sources import Source, load_sources
from monitor.settings import load_settings

OUTPUT = Path("config/news_sitemaps.csv")
SITEMAP_LINE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")
CONCURRENCY = 16
TIMEOUT_SECONDS = 12.0
AGENT_TOKEN = "ElectionNightMonitor"


async def entries_in(client: httpx.AsyncClient, url: str) -> int:
    response = await client.get(url)
    if response.status_code != 200:
        return 0
    children = sitemap_children(response.content)
    if not children:
        return len(parse_news_sitemap(response.content).entries)
    for child in children[:3]:
        if not same_site(url, child):
            continue
        inner = await client.get(child)
        found = len(parse_news_sitemap(inner.content).entries)
        if found:
            return found
    return 0


async def find(
    client: httpx.AsyncClient, source: Source
) -> tuple[Source, str, int] | None:
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
            found = await entries_in(client, url)
            if found:
                return source, url, found
    except (httpx.HTTPError, ValueError):
        return None
    return None


def write_list(found: list[tuple[Source, str, int]]) -> None:
    today = datetime.now(UTC).date().isoformat()
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["source_id", "url", "entries_seen", "checked_on"])
        for source, url, entries in found:
            writer.writerow([source.source_id, url, entries, today])


async def main() -> None:
    settings = load_settings()
    sources = [s for s in load_sources(settings) if s.enabled and s.feed_urls]
    limit = asyncio.Semaphore(CONCURRENCY)

    async def bounded(
        client: httpx.AsyncClient, source: Source
    ) -> tuple[Source, str, int] | None:
        async with limit:
            return await find(client, source)

    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": settings.poll.user_agent},
    ) as client:
        results = await asyncio.gather(*(bounded(client, s) for s in sources))
    found = sorted((r for r in results if r), key=lambda row: row[0].source_id)
    write_list(found)
    print(f"{len(found)} of {len(sources)} outlets; wrote {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
