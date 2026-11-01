"""Fetch full article text for relevant items, politely."""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from monitor.disk import DiskGuard
from monitor.ingest.feeds import canonicalize_url
from monitor.ingest.sources import Source
from monitor.lake import utcnow
from monitor.logs import get
from monitor.parsers.base import (
    RELEVANT_SQL,
    Item,
    ParserOutput,
    needs_parser_sql,
)
from monitor.settings import Settings

log = get(__name__)

MAX_TEXT_CHARS = 20_000
MAX_PAGE_BYTES = 5_000_000
PAYWALL_STATUSES = {401, 402, 403}
SHORT_TEXT_CHARS = 400
ROBOTS_TTL_SECONDS = 6 * 3600
BLOCK_AFTER_REFUSALS = 3
BLOCK_SECONDS = 3600
CANONICAL_RE = re.compile(
    r"<link[^>]+rel=[\"']canonical[\"'][^>]*href=[\"']([^\"']+)[\"']", re.I
)
AGENT_TOKEN_RE = re.compile(r"^[^/\s]+")


@dataclass
class RobotsEntry:
    parser: RobotFileParser
    fetched_at: float


def parse_robots(status: int, body: str) -> RobotFileParser:
    """Apply the robots.txt rules for a fetch result.

    A 4xx other than 401 or 403 means no rules exist. Anything else that is
    not a 200 means the rules are unknown, so everything is disallowed.
    """
    parser = RobotFileParser()
    if status == 200:
        parser.parse(body.splitlines())
    elif 400 <= status < 500 and status not in {401, 403}:
        parser.allow_all = True  # type: ignore[attr-defined]
    else:
        parser.disallow_all = True  # type: ignore[attr-defined]
    return parser


def canonical_from_html(page: str) -> str | None:
    match = CANONICAL_RE.search(page[:200_000])
    return canonicalize_url(match.group(1)) if match else None


def extract_text(page: str, url: str) -> str:
    text = trafilatura.extract(
        page,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return (text or "")[:MAX_TEXT_CHARS]


def is_paywalled(status: int, text: str, source: Source | None) -> bool:
    if status in PAYWALL_STATUSES:
        return True
    metered = source is not None and source.paywall != "none"
    return metered and len(text) < SHORT_TEXT_CHARS


class FetchTextParser:
    """One fetch per domain every few seconds, honoring robots.txt."""

    name = "fetch_text"
    version = "1"
    batch_size = 1
    max_concurrency = 16

    def __init__(
        self,
        settings: Settings,
        sources: dict[str, Source],
        guard: DiskGuard,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.guard = guard
        self.interval = settings.nlp.fetch_domain_interval_seconds
        self.agent = settings.poll.user_agent
        token = AGENT_TOKEN_RE.match(self.agent)
        self.agent_token = token.group(0) if token else self.agent
        self.client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(settings.nlp.fetch_timeout_seconds),
            headers={"User-Agent": self.agent},
            limits=httpx.Limits(max_connections=32),
        )
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_fetch: dict[str, float] = {}
        self.robots: dict[str, RobotsEntry] = {}
        self.refusals: dict[str, int] = defaultdict(int)
        self.blocked_until: dict[str, float] = {}
        threshold = settings.nlp.relevance_threshold
        self.input_query = needs_parser_sql(
            self.name,
            self.version,
            RELEVANT_SQL.format(threshold=threshold),
        )

    async def _pace(self, domain: str) -> None:
        """Hold the domain's lock until its next request may go out."""
        wait = (
            self.last_fetch.get(domain, 0) + self.interval - time.monotonic()
        )
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_fetch[domain] = time.monotonic()

    async def _robots(self, url: str, domain: str) -> RobotFileParser:
        entry = self.robots.get(domain)
        now = time.monotonic()
        if entry and now - entry.fetched_at < ROBOTS_TTL_SECONDS:
            return entry.parser
        parts = urlsplit(url)
        try:
            await self._pace(domain)
            response = await self.client.get(
                f"{parts.scheme}://{parts.netloc}/robots.txt"
            )
            parser = parse_robots(response.status_code, response.text)
        except Exception as error:
            log.debug(
                "robots_failed", domain=domain, error=type(error).__name__
            )
            parser = parse_robots(599, "")
        self.robots[domain] = RobotsEntry(parser, now)
        return parser

    def _note_refusal(self, domain: str, status: int) -> None:
        """Back off a domain that keeps refusing."""
        if status in {401, 403, 429, 451}:
            self.refusals[domain] += 1
            if self.refusals[domain] >= BLOCK_AFTER_REFUSALS:
                self.blocked_until[domain] = time.monotonic() + BLOCK_SECONDS
                self.refusals[domain] = 0
                log.info("domain_backoff", domain=domain, status=status)
        else:
            self.refusals[domain] = 0

    async def _fetch(
        self, item: Item, domain: str
    ) -> tuple[int | None, str | None, str]:
        """Returns (status, page, skip reason)."""
        async with self.locks[domain]:
            if time.monotonic() < self.blocked_until.get(domain, 0):
                return None, None, "domain backing off"
            robots = await self._robots(item.url, domain)
            if not robots.can_fetch(self.agent_token, item.url):
                return None, None, "robots.txt disallows"
            await self._pace(domain)
            response = await self.client.get(item.url)
            self._note_refusal(domain, response.status_code)
        if len(response.content) > MAX_PAGE_BYTES:
            return response.status_code, None, "page too large"
        return response.status_code, response.text, ""

    async def run(self, batch: list[Item]) -> ParserOutput:
        output = ParserOutput()
        for item in batch:
            await self._one(output, item)
        return output

    async def _one(self, output: ParserOutput, item: Item) -> None:
        source = self.sources.get(item.source_id)
        if source is not None and source.paywall == "hard":
            output.mark(item.item_id, "skipped", "hard paywall")
            return
        if not self.guard.full_text_allowed():
            output.mark(item.item_id, "skipped", "disk guard")
            return
        domain = urlsplit(item.url).hostname or ""
        try:
            status, page, reason = await self._fetch(item, domain)
        except Exception as error:
            output.mark(item.item_id, "skipped", type(error).__name__)
            return
        if reason and status is None:
            output.mark(item.item_id, "skipped", reason)
            return
        text = ""
        canonical = None
        if page and status == 200:
            text = await asyncio.to_thread(extract_text, page, item.url)
            canonical = canonical_from_html(page)
        item.text = text or None
        row: dict[str, Any] = {
            "item_id": item.item_id,
            "fetched_at": utcnow(),
            "http_status": status,
            "text": text,
            "n_chars": len(text),
            "extractor": "trafilatura",
            "paywalled": is_paywalled(status or 0, text, source),
            "canonical_url": canonical,
        }
        output.add("raw.article_text", row)
