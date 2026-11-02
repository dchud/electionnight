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

from monitor.archive import ArchiveWriter
from monitor.disk import DiskGuard
from monitor.ingest.download import (
    BlockedAddress,
    BodyTooLarge,
    build_client,
    download,
)
from monitor.ingest.feeds import canonical_or_none
from monitor.ingest.network import NetworkGate
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
MAX_ROBOTS_BYTES = 1_000_000
PAYWALL_STATUSES = {401, 402, 403}
SHORT_TEXT_CHARS = 400
ROBOTS_TTL_SECONDS = 6 * 3600
# Rules not learned because of a timeout or server error are asked for again.
ROBOTS_RETRY_SECONDS = 300
BLOCK_AFTER_REFUSALS = 3
BLOCK_SECONDS = 3600
# Bounded, and stopped by `<`, so a hostile page cannot make this slow. The
# regex engine holds the GIL, so a slow match would stop the whole pipeline.
CANONICAL_RE = re.compile(
    r"<link\b[^<>]{0,500}?rel=[\"']canonical[\"']"
    r"[^<>]{0,500}?href=[\"']([^\"'<>]{1,2048})[\"']",
    re.I,
)
AGENT_TOKEN_RE = re.compile(r"^[^/\s]+")


class RobotsRefused(Exception):
    """robots.txt disallows the page a redirect leads to."""


@dataclass
class RobotsEntry:
    parser: RobotFileParser
    fetched_at: float
    ttl: float = ROBOTS_TTL_SECONDS


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


def canonical_from_html(page: str | bytes) -> str | None:
    if isinstance(page, bytes):
        page = page[:200_000].decode("latin-1")
    match = CANONICAL_RE.search(page[:200_000])
    return canonical_or_none(match.group(1)) if match else None


def page_from(response: httpx.Response) -> str | bytes:
    """Text when the headers name a charset, else bytes for the extractor.

    The extractor reads a charset declared inside the page, which httpx does
    not.
    """
    return response.text if response.charset_encoding else response.content


def extract_text(page: str | bytes, url: str) -> str:
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
        archive: ArchiveWriter | None = None,
        gate: NetworkGate | None = None,
    ) -> None:
        self.settings = settings
        self.gate = gate or NetworkGate(settings)
        self.archive = archive
        self.sources = sources
        self.guard = guard
        self.interval = settings.nlp.fetch_domain_interval_seconds
        self.agent = settings.poll.user_agent
        token = AGENT_TOKEN_RE.match(self.agent)
        self.agent_token = token.group(0) if token else self.agent
        self.client = client or build_client(
            settings, settings.nlp.fetch_timeout_seconds, 32
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

    @staticmethod
    def partition(item: Item) -> str:
        """The item's domain. A stage runs one item per domain at a time."""
        try:
            return urlsplit(item.url).hostname or ""
        except ValueError:
            return ""

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
        if entry and now - entry.fetched_at < entry.ttl:
            return entry.parser
        parts = urlsplit(url)
        ttl = float(ROBOTS_RETRY_SECONDS)
        try:
            await self._pace(domain)
            response = await download(
                self.client,
                f"{parts.scheme}://{parts.netloc}/robots.txt",
                MAX_ROBOTS_BYTES,
                self.settings.nlp.fetch_deadline_seconds,
                network_up=self.gate.reachable,
            )
            parser = parse_robots(response.status_code, response.text)
            if response.status_code < 500:
                ttl = float(ROBOTS_TTL_SECONDS)
        except BlockedAddress:
            raise
        except httpx.TransportError as error:
            # With no network, nothing is learned about this site's rules.
            if not await self.gate.reachable():
                raise
            log.debug(
                "robots_failed", domain=domain, error=type(error).__name__
            )
            parser = parse_robots(599, "")
        except Exception as error:
            log.debug(
                "robots_failed", domain=domain, error=type(error).__name__
            )
            parser = parse_robots(599, "")
        self.robots[domain] = RobotsEntry(parser, now, ttl)
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

    async def _check_hop(self, request: httpx.Request) -> None:
        """Apply robots.txt for the host a redirect actually reaches."""
        url = str(request.url)
        robots = await self._robots(url, request.url.host)
        if not robots.can_fetch(self.agent_token, url):
            raise RobotsRefused(url)

    async def _fetch(
        self, item: Item, domain: str
    ) -> tuple[int | None, str | bytes | None, str, bytes | None]:
        """Returns (status, page, skip reason, original bytes)."""
        async with self.locks[domain]:
            if time.monotonic() < self.blocked_until.get(domain, 0):
                return None, None, "domain backing off", None
            robots = await self._robots(item.url, domain)
            if not robots.can_fetch(self.agent_token, item.url):
                return None, None, "robots.txt disallows", None
            await self._pace(domain)
            try:
                response = await download(
                    self.client,
                    item.url,
                    MAX_PAGE_BYTES,
                    self.settings.nlp.fetch_deadline_seconds,
                    check_hop=self._check_hop,
                    network_up=self.gate.reachable,
                )
            except BodyTooLarge as error:
                self._note_refusal(domain, error.status_code)
                return error.status_code, None, "page too large", None
            except RobotsRefused:
                return None, None, "robots.txt disallows", None
            self._note_refusal(domain, response.status_code)
        return response.status_code, page_from(response), "", response.content

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
        domain = self.partition(item)
        while True:
            await self.gate.wait_online()
            try:
                status, page, reason, body = await self._fetch(item, domain)
                break
            except httpx.TransportError as error:
                # Offline, the item waits and keeps its chance at full text.
                if await self.gate.reachable():
                    output.mark(item.item_id, "skipped", type(error).__name__)
                    return
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
        item.paywalled = is_paywalled(status or 0, text, source)
        if self.archive is not None and body and status == 200:
            index_row = self.archive.add(
                "pages",
                item.item_id,
                item.source_id,
                body,
                {"url": item.url, "http_status": status},
            )
            if index_row:
                output.add("raw.archive_index", index_row)
        row: dict[str, Any] = {
            "item_id": item.item_id,
            "fetched_at": utcnow(),
            "http_status": status,
            "text": text,
            "n_chars": len(text),
            "extractor": "trafilatura",
            "paywalled": item.paywalled,
            "canonical_url": canonical,
        }
        output.add("raw.article_text", row)
