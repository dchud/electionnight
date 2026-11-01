"""Find, validate, and report feeds for each seed outlet."""

from __future__ import annotations

import asyncio
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from monitor.ingest.feeds import ParsedFeed, newest_entry_at, parse_feed
from monitor.ingest.sources import (
    Source,
    load_sources,
    seed_to_source,
    write_sources,
)
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

MAX_FEEDS_PER_OUTLET = 3
MIN_ENTRIES = 5
FRESH_HOURS = 72
FRESH_HOURS_SMALL = 7 * 24
SMALL_SCOPES = {"local", "state", "analysis"}
GLOBAL_CONCURRENCY = 48

# Used for the report when `config/races.yaml` has not been built.
DEFAULT_BATTLEGROUND = (
    "GA MI NC ME NH OH TX IA AK NE MN AZ WI PA NV KS NY".split()
)
ALL_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA "
    "WA WV WI WY"
).split()
NON_ENGLISH = ("ar", "zh", "fr", "ru", "es")

SECTION_PATHS = ("/politics", "/elections", "/news/politics", "/politics/")

COMMON_PATHS = (
    "/category/politics/feed/",
    "/politics/feed/",
    "/tag/politics/feed/",
    "/news/politics/feed/",
    "/section/politics/feed/",
    "/tags/politics.rss",
    "/politics.rss",
    "/politics/index.rss",
    "/news/politics/index.rss",
    "/rss/politics",
    "/politics/rss",
    "/feeds/politics",
    "/arc/outboundfeeds/rss/category/politics/?outputType=xml",
    "/search/?f=rss&t=article&c=news/politics&l=50&s=start_time&sd=desc",
    "/feed/",
    "/feed",
    "/rss",
    "/rss/",
    "/rss.xml",
    "/feed.xml",
    "/index.rss",
    "/atom.xml",
    "/news.rss",
    "/feeds/latest",
    "/feeds/rss",
    "/arc/outboundfeeds/rss/?outputType=xml",
    "/search/?f=rss&t=article&l=50&s=start_time&sd=desc",
)

POLITICS_RE = re.compile(
    r"politic|election|campaign|congress|capitol|government|vote|"
    r"us-news|usa|/us\b|america|ameriques|estados|eeuu|\bworld\b|world|"
    r"international|mundo",
    re.IGNORECASE,
)
STRONG_RE = re.compile(r"politic|election|campaign", re.IGNORECASE)


class AlternateLinks(HTMLParser):
    """Collects RSS and Atom links from a page's `<head>`."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "link":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        rel = values.get("rel", "").lower()
        kind = values.get("type", "").lower()
        if "alternate" in rel and ("rss" in kind or "atom" in kind):
            if values.get("href"):
                self.links.append(values["href"])


def find_alternate_links(page: str, base_url: str) -> list[str]:
    parser = AlternateLinks()
    try:
        parser.feed(page)
    except (AssertionError, ValueError):
        return []
    return [urljoin(base_url, link) for link in parser.links]


def rank_feed(url: str) -> int:
    """Lower sorts first: politics and elections feeds before general ones."""
    if STRONG_RE.search(url):
        return 0
    if POLITICS_RE.search(url):
        return 1
    return 2


def validate_feed(
    feed: ParsedFeed, scope: str, now: datetime
) -> tuple[bool, str]:
    """Apply the entry-count and freshness rules. Returns (ok, reason)."""
    count = len(feed.entries)
    if count < MIN_ENTRIES:
        return False, f"only {count} entries"
    newest = newest_entry_at(feed)
    if newest is None:
        return True, "ok (entries have no dates)"
    limit = FRESH_HOURS_SMALL if scope in SMALL_SCOPES else FRESH_HOURS
    age = now - newest
    if age > timedelta(hours=limit):
        return False, f"newest entry is {age.days} days old"
    return True, "ok"


@dataclass
class OutletResult:
    source: Source
    good: list[tuple[str, int]] = field(default_factory=list)
    tried: dict[str, str] = field(default_factory=dict)


class Discoverer:
    def __init__(
        self,
        settings: Settings,
        patterns: dict[str, list[str]],
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.patterns = patterns
        self.client = client
        self.global_limit = asyncio.Semaphore(GLOBAL_CONCURRENCY)
        self.domain_limits: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.poll.per_domain_concurrency)
        )

    async def get(self, url: str) -> httpx.Response | None:
        domain = urlsplit(url).hostname or ""
        async with self.global_limit, self.domain_limits[domain]:
            try:
                return await self.client.get(url)
            except Exception as error:
                log.debug("fetch_failed", url=url, error=type(error).__name__)
                return None

    async def try_feed(
        self, url: str, result: OutletResult, now: datetime
    ) -> None:
        if url in result.tried:
            return
        result.tried[url] = "pending"
        response = await self.get(url)
        if response is None:
            result.tried[url] = "connection failed"
            return
        if response.status_code != 200:
            result.tried[url] = f"HTTP {response.status_code}"
            return
        final = str(response.url)
        try:
            feed = parse_feed(response.content)
        except Exception as error:
            result.tried[url] = type(error).__name__
            return
        ok, reason = validate_feed(feed, result.source.scope, now)
        result.tried[url] = reason
        if ok and final not in {u for u, _ in result.good}:
            result.good.append((final, len(feed.entries)))

    async def discover_links(self, homepage: str) -> list[str]:
        pages = [homepage] + [
            urljoin(homepage.rstrip("/") + "/", path.lstrip("/"))
            for path in SECTION_PATHS
        ]
        links: list[str] = []
        for page in pages:
            response = await self.get(page)
            if response is not None and response.status_code == 200:
                links.extend(find_alternate_links(response.text, str(page)))
        unique = list(dict.fromkeys(links))
        return sorted(unique, key=rank_feed)

    async def outlet(self, source: Source, now: datetime) -> OutletResult:
        result = OutletResult(source=source)
        host = (urlsplit(source.homepage).hostname or "").removeprefix("www.")
        key = f"{host}{urlsplit(source.homepage).path}".rstrip("/")
        known = self.patterns.get(key) or self.patterns.get(host) or []
        await asyncio.gather(
            *(self.try_feed(url, result, now) for url in known)
        )
        if not any(rank_feed(url) == 0 for url, _ in result.good):
            links = await self.discover_links(source.homepage)
            await asyncio.gather(
                *(self.try_feed(url, result, now) for url in links[:8])
            )
        if not result.good or not any(
            rank_feed(url) == 0 for url, _ in result.good
        ):
            base = source.homepage.rstrip("/")
            origin = "{0.scheme}://{0.netloc}".format(urlsplit(base))
            for bases in (base, origin):
                for path in COMMON_PATHS:
                    if len(result.good) >= MAX_FEEDS_PER_OUTLET:
                        break
                    await self.try_feed(bases + path, result, now)
                if result.good:
                    break
        result.good.sort(key=lambda pair: rank_feed(pair[0]))
        result.good = result.good[:MAX_FEEDS_PER_OUTLET]
        return result


def read_seed(path: Path) -> list[Source]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [seed_to_source(row) for row in csv.DictReader(handle)]


def load_patterns(settings: Settings) -> dict[str, list[str]]:
    path = settings.paths.config / "feed_patterns.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def merge_existing(fresh: Source, existing: Source | None) -> Source:
    """Keep hand-edited fields when a source is rediscovered."""
    if existing is None:
        return fresh
    fresh.poll_tier = existing.poll_tier
    fresh.notes = existing.notes or fresh.notes
    fresh.lean_label = existing.lean_label
    fresh.lean_source = existing.lean_source
    fresh.lean_source_url = existing.lean_source_url
    fresh.lean_retrieved_on = existing.lean_retrieved_on
    fresh.reliability_note = existing.reliability_note
    fresh.ownership = existing.ownership
    return fresh


def battleground_states(config_dir: Path) -> list[str]:
    """Battleground states from the race watchlist."""
    path = config_dir / "races.yaml"
    if not path.exists():
        return DEFAULT_BATTLEGROUND
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("battleground_states") or DEFAULT_BATTLEGROUND


def coverage_counts(sources: list[Source]) -> dict[str, Counter]:
    """Enabled outlet counts by state, by language, and in total."""
    enabled = [s for s in sources if s.enabled]
    return {
        "state": Counter(s.home_state for s in enabled if s.home_state),
        "language": Counter(s.language for s in enabled),
        "scope": Counter(s.scope for s in enabled),
    }


def render_report(
    results: list[OutletResult],
    sources: list[Source],
    now: datetime,
    battleground: list[str] | None = None,
) -> str:
    counts = coverage_counts(sources)
    feeds = sum(len(s.feed_urls) for s in sources if s.enabled)
    outlets = sum(1 for s in sources if s.enabled)
    under = [s for s in ALL_STATES if counts["state"][s] < 2]
    bg_under = [
        s
        for s in battleground or DEFAULT_BATTLEGROUND
        if counts["state"][s] < 4
    ]
    lines = [
        "# Feed validation",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC.",
        "",
        "## Targets",
        "",
        "| Target | Goal | Result |",
        "|---|---|---|",
        f"| Healthy feeds | 250 | {feeds} |",
        f"| Healthy outlets | 250 | {outlets} of {len(sources)} |",
        f"| States below 2 outlets | 0 | {len(under)}: "
        f"{' '.join(under) or 'none'} |",
        f"| Battleground states below 4 outlets | 0 | {len(bg_under)}: "
        + " ".join(f"{s} ({counts['state'][s]})" for s in bg_under)
        + " |",
    ]
    for language in NON_ENGLISH:
        lines.append(
            f"| Outlets in `{language}` | 3 | {counts['language'][language]} |"
        )
    failed = [r for r in results if not r.good]
    lines += [
        "",
        f"## Outlets with no healthy feed ({len(failed)})",
        "",
        "| Outlet | Scope | State | Tier | Most common failure |",
        "|---|---|---|---|---|",
    ]
    for result in sorted(failed, key=lambda r: r.source.poll_tier):
        source = result.source
        reasons = Counter(
            reason
            for reason in result.tried.values()
            if not reason.startswith("ok")
        )
        top = reasons.most_common(1)[0][0] if reasons else "nothing to try"
        lines.append(
            f"| {source.name} | {source.scope} | {source.home_state or '-'} "
            f"| {source.poll_tier} | {top} |"
        )
    lines += [
        "",
        "## Healthy outlets by scope",
        "",
        "| Scope | Outlets |",
        "|---|---|",
    ]
    for scope, count in sorted(counts["scope"].items()):
        lines.append(f"| {scope} | {count} |")
    return "\n".join(lines) + "\n"


async def _discover(
    settings: Settings, seed: list[Source]
) -> list[OutletResult]:
    patterns = load_patterns(settings)
    timeout = httpx.Timeout(settings.poll.timeout_seconds)
    headers = {"User-Agent": settings.poll.user_agent}
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers=headers
    ) as client:
        discoverer = Discoverer(settings, patterns, client)
        now = datetime.now(UTC)
        return await asyncio.gather(
            *(discoverer.outlet(source, now) for source in seed)
        )


def run_discover(
    settings: Settings,
    limit: int | None = None,
    seed_path: Path = Path("sources_seed.csv"),
) -> None:
    """Rediscover seed outlets. Outlets added by hand stay in the list."""
    seed = read_seed(seed_path)
    if limit:
        seed = seed[:limit]
    results = asyncio.run(_discover(settings, seed))
    previous = {s.source_id: s for s in load_sources(settings)}
    sources: list[Source] = []
    for result in results:
        known = previous.get(result.source.source_id)
        source = merge_existing(result.source, known)
        source.feed_urls = [url for url, _ in result.good]
        # An outlet with feeds that is disabled was disabled by hand.
        by_hand = known is not None and not known.enabled and known.feed_urls
        source.enabled = bool(source.feed_urls) and not by_hand
        sources.append(source)
    seen = {source.source_id for source in sources}
    sources.extend(
        source
        for source_id, source in previous.items()
        if source_id not in seen
    )
    write_sources(settings, sources)
    now = datetime.now(UTC)
    report = settings.paths.reports / "feed_validation.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        render_report(
            results,
            sources,
            now,
            battleground_states(settings.paths.config),
        ),
        encoding="utf-8",
    )
    feeds = sum(len(s.feed_urls) for s in sources)
    print(
        f"{sum(s.enabled for s in sources)} of {len(sources)} outlets healthy,"
        f" {feeds} feeds. Report: {report}"
    )


async def _check(
    settings: Settings, sources: list[Source]
) -> list[tuple[Source, str, str]]:
    timeout = httpx.Timeout(settings.poll.timeout_seconds)
    headers = {"User-Agent": settings.poll.user_agent}
    limits: dict[str, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(settings.poll.per_domain_concurrency)
    )
    overall = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    now = datetime.now(UTC)

    async def one(
        client: httpx.AsyncClient, source: Source, url: str
    ) -> tuple[Source, str, str]:
        domain = urlsplit(url).hostname or ""
        async with overall, limits[domain]:
            try:
                response = await client.get(url)
            except Exception as error:
                return source, url, type(error).__name__
        if response.status_code != 200:
            return source, url, f"HTTP {response.status_code}"
        try:
            feed = parse_feed(response.content)
        except Exception as error:
            return source, url, type(error).__name__
        _, reason = validate_feed(feed, source.scope, now)
        return source, url, reason

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers=headers
    ) as client:
        tasks = [
            one(client, source, url)
            for source in sources
            if source.enabled
            for url in source.feed_urls
        ]
        return await asyncio.gather(*tasks)


def run_check(settings: Settings) -> int:
    """Fetch every enabled feed once and print failures. Exit 1 on any."""
    outcomes = asyncio.run(_check(settings, load_sources(settings)))
    failures = [o for o in outcomes if not o[2].startswith("ok")]
    for source, url, reason in failures:
        print(f"FAIL {source.source_id} {url}: {reason}")
    print(f"{len(outcomes) - len(failures)} of {len(outcomes)} feeds healthy")
    return 1 if failures else 0
