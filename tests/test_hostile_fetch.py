"""Requests a hostile feed can cause: where they go, and how much they read."""

import asyncio
import gzip
import socket
import time
import tracemalloc
import zlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from structlog.testing import capture_logs

from monitor.disk import DiskGuard
from monitor.ingest.download import build_client
from monitor.ingest.feeds import parse_news_sitemap
from monitor.ingest.network import NetworkGate
from monitor.ingest.poller import FeedState, Poller
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect, hash_id
from monitor.parsers.base import Item
from monitor.parsers.fetch_text import FetchTextParser, canonical_from_html

LOOPBACK_PORT = 8034
PUBLIC = "93.184.216.34"
OTHER_PUBLIC = "151.101.1.1"
RSS_HEAD = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
RSS_TAIL = "</channel></rss>"
FEED = (
    f"{RSS_HEAD}<item><title>Polls close in Ohio</title>"
    f"<link>https://example.com/a</link></item>{RSS_TAIL}"
).encode()
PAGE = (
    "<html><head><title>Story</title></head><body><article><p>"
    + "Turnout was high across Ohio on Tuesday. " * 30
    + "</p></article></body></html>"
)


class Chunks(httpx.AsyncByteStream):
    """A response body that counts how much of it was pulled."""

    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.pulled = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.pulled += 1
            yield chunk


def gzip_chunks(size: int, piece: int = 1024) -> list[bytes]:
    packed = gzip.compress(b" " * size, 1)
    return [packed[i : i + piece] for i in range(0, len(packed), piece)]


def source(url: str = f"http://{PUBLIC}/feed") -> Source:
    return Source("s", "S", "https://example.com/", [url])


def writer_on(settings) -> BatchWriter:
    return BatchWriter(connect(settings), settings)


def plain_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def guarded_client(settings, handler, addresses=None) -> httpx.AsyncClient:
    """The production client, with its transport and resolver replaced."""
    from monitor.ingest.download import build_client

    async def resolve(host: str) -> list[str]:
        if addresses is None or host not in addresses:
            raise socket.gaierror(8, "not known")
        return addresses[host]

    return build_client(
        settings,
        5.0,
        8,
        transport=httpx.MockTransport(handler),
        resolve=resolve,
    )


def poller_with(settings, client, url=f"http://{PUBLIC}/feed", gate=None):
    return Poller(
        settings,
        writer_on(settings),
        [source(url)],
        set(),
        client=client,
        gate=gate,
    )


def parser_with(settings, client) -> FetchTextParser:
    settings.nlp.fetch_domain_interval_seconds = 0.0
    return FetchTextParser(
        settings,
        {"s": source()},
        DiskGuard(settings, free_space=lambda path: 100.0),
        client=client,
    )


def story(url: str = f"http://{PUBLIC}/story") -> Item:
    return Item("a", source_id="s", url=url, title="T")


async def down() -> bool:
    return False


# Addresses


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "[::1]",
        "[fe80::1]",
        "[fc00::1]",
        "[::ffff:127.0.0.1]",
        "[::ffff:10.0.0.1]",
    ],
)
async def test_non_global_addresses_are_refused(settings, address):
    def handler(request):
        raise AssertionError(f"request reached {request.url}")

    poller = poller_with(
        settings,
        guarded_client(settings, handler),
        f"http://{address}/feed",
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error == "BlockedAddress"
    assert poller.feeds[0].failures == 1


@pytest.mark.parametrize(
    "address", [PUBLIC, "[2606:2800:220:1:248:1893:25c8:1946]"]
)
async def test_global_addresses_are_allowed(settings, address):
    poller = poller_with(
        settings,
        guarded_client(
            settings, lambda request: httpx.Response(200, content=FEED)
        ),
        f"http://{address}/feed",
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error is None


async def test_the_default_poller_client_refuses_loopback(settings):
    poller = Poller(
        settings,
        writer_on(settings),
        [source("http://127.0.0.1:9/feed")],
        set(),
    )
    await poller.poll_once(poller.feeds[0])
    await poller.client.aclose()
    assert poller.feeds[0].last_error == "BlockedAddress"


async def test_the_default_fetch_client_refuses_loopback(settings):
    settings.nlp.fetch_domain_interval_seconds = 0.0
    parser = FetchTextParser(
        settings,
        {"s": source()},
        DiskGuard(settings, free_space=lambda path: 100.0),
    )
    output = await parser.run([story("http://127.0.0.1:9/story")])
    await parser.client.aclose()
    assert output.statuses["a"] == ("skipped", "BlockedAddress")


async def test_loopback_is_refused_unless_the_setting_allows_it(settings):
    from monitor.settings import Poll

    assert Poll().allow_loopback is False
    settings.poll.allow_loopback = True
    poller = poller_with(
        settings,
        guarded_client(
            settings, lambda request: httpx.Response(200, content=FEED)
        ),
        "http://127.0.0.1:9/feed",
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error is None


async def test_the_loopback_setting_does_not_open_private_networks(settings):
    settings.poll.allow_loopback = True

    def handler(request):
        raise AssertionError(f"request reached {request.url}")

    poller = poller_with(
        settings, guarded_client(settings, handler), "http://10.0.0.1/feed"
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error == "BlockedAddress"


async def test_a_name_resolving_to_a_private_address_is_refused(settings):
    def handler(request):
        raise AssertionError(f"request reached {request.url}")

    poller = poller_with(
        settings,
        guarded_client(
            settings, handler, {"internal.example": [PUBLIC, "10.0.0.5"]}
        ),
        "http://internal.example/feed",
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error == "BlockedAddress"


async def test_a_redirect_to_a_private_address_is_refused(settings):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/"}
        )

    poller = poller_with(settings, guarded_client(settings, handler))
    await poller.poll_once(poller.feeds[0])
    assert seen == [PUBLIC]
    assert poller.feeds[0].last_error == "BlockedAddress"


async def test_a_sitemap_index_child_on_a_private_address_is_refused(settings):
    seen = []
    index = (
        b'<?xml version="1.0"?><sitemapindex><sitemap>'
        b"<loc>http://internal.news.example/news.xml</loc>"
        b"</sitemap></sitemapindex>"
    )

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, content=index)

    poller = poller_with(
        settings,
        guarded_client(
            settings,
            handler,
            {
                "news.example": [PUBLIC],
                "internal.news.example": ["10.0.0.5"],
            },
        ),
    )
    read = await poller._read_sitemap(
        [source()],
        "http://news.example/index.xml",
        datetime(2026, 11, 3, tzinfo=UTC),
    )
    assert read.new == 0
    assert seen == ["news.example"]


async def test_robots_and_pages_on_a_private_address_are_not_requested(
    settings,
):
    def handler(request):
        raise AssertionError(f"request reached {request.url}")

    parser = parser_with(
        settings,
        guarded_client(settings, handler, {"internal.example": ["10.0.0.5"]}),
    )
    output = await parser.run([story("http://internal.example/story")])
    assert output.statuses["a"] == ("skipped", "BlockedAddress")
    assert parser.robots == {}


async def test_a_blocked_address_with_no_network_is_not_a_feed_failure(
    settings,
):
    def handler(request):
        raise AssertionError(f"request reached {request.url}")

    gate = NetworkGate(settings, probe=down)
    poller = poller_with(
        settings,
        guarded_client(settings, handler),
        "http://192.168.1.1/portal",
        gate,
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].failures == 0
    assert gate.is_online is False


async def test_a_failed_lookup_with_no_network_is_not_a_feed_failure(settings):
    def handler(request):
        raise AssertionError(f"request reached {request.url}")

    gate = NetworkGate(settings, probe=down)
    poller = poller_with(
        settings,
        guarded_client(settings, handler),
        "http://unknown.example/feed",
        gate,
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].failures == 0
    assert gate.is_online is False


async def test_a_failed_lookup_with_the_network_up_is_a_feed_failure(settings):
    def handler(request):
        raise AssertionError(f"request reached {request.url}")

    poller = poller_with(
        settings,
        guarded_client(settings, handler),
        "http://unknown.example/feed",
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].failures == 1


async def test_resolving_a_name_does_not_block_the_event_loop(
    settings, monkeypatch
):
    from monitor.ingest.download import AddressGuard

    def slow_lookup(host, port, *args, **kwargs):
        time.sleep(0.4)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    guard = AddressGuard()
    gaps = []

    async def heartbeat():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            gaps.append(time.monotonic() - last)
            last = time.monotonic()

    beat = asyncio.ensure_future(heartbeat())
    await guard.check(httpx.Request("GET", "http://feed.example/"))
    beat.cancel()
    assert gaps
    assert max(gaps) < 0.3


async def test_a_host_is_resolved_once_and_then_again_after_the_cache_expires(
    monkeypatch,
):
    from monitor.ingest import download
    from monitor.ingest.download import AddressGuard

    lookups = []

    async def resolve(host):
        lookups.append(host)
        return [PUBLIC]

    guard = AddressGuard(resolve=resolve)
    request = httpx.Request("GET", "http://feed.example/")
    await guard.check(request)
    await guard.check(request)
    assert lookups == ["feed.example"]
    clock = time.monotonic() + download.HOST_CACHE_SECONDS + 1
    monkeypatch.setattr(download.time, "monotonic", lambda: clock)
    await guard.check(request)
    assert lookups == ["feed.example", "feed.example"]


async def test_a_refusal_is_not_cached_past_a_wi_fi_login():
    from monitor.ingest.download import AddressGuard, BlockedAddress

    answers = [["10.0.0.5"], [PUBLIC]]

    async def resolve(host):
        return answers.pop(0)

    guard = AddressGuard(resolve=resolve)
    request = httpx.Request("GET", "http://feed.example/")
    with pytest.raises(BlockedAddress):
        await guard.check(request)
    await guard.check(request)


async def test_redirects_follow_with_the_conditional_headers_kept(settings):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("if-none-match")))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/feed"})
        return httpx.Response(304)

    poller = poller_with(
        settings, plain_client(handler), f"http://{PUBLIC}/old"
    )
    state = poller.feeds[0]
    state.etag = '"v1"'
    await poller.poll_once(state)
    assert seen == [("/old", '"v1"'), ("/feed", '"v1"')]
    assert state.last_error is None


async def test_a_redirect_loop_counts_against_the_feed(settings):
    poller = poller_with(
        settings,
        plain_client(
            lambda request: httpx.Response(
                302, headers={"location": str(request.url)}
            )
        ),
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error == "TooManyRedirects"


# Robots rules for the host a redirect reaches


async def test_robots_rules_of_the_redirect_target_are_applied(settings):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path))
        if request.url.path == "/robots.txt":
            if request.url.host == OTHER_PUBLIC:
                return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            return httpx.Response(404)
        return httpx.Response(
            302, headers={"location": f"http://{OTHER_PUBLIC}/final"}
        )

    output = await parser_with(settings, plain_client(handler)).run([story()])
    assert output.statuses["a"] == ("skipped", "robots.txt disallows")
    assert (OTHER_PUBLIC, "/final") not in seen
    assert (OTHER_PUBLIC, "/robots.txt") in seen


async def test_a_redirect_to_an_allowed_host_is_fetched(settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == PUBLIC:
            return httpx.Response(
                302, headers={"location": f"http://{OTHER_PUBLIC}/final"}
            )
        return httpx.Response(200, text=PAGE)

    output = await parser_with(settings, plain_client(handler)).run([story()])
    assert "Turnout" in output.rows["raw.article_text"][0]["text"]


# Byte caps


async def test_a_compressed_feed_stops_at_the_byte_cap(settings):
    chunks = gzip_chunks(40_000_000)
    body = Chunks(chunks)

    def handler(request):
        return httpx.Response(
            200, stream=body, headers={"content-encoding": "gzip"}
        )

    poller = poller_with(settings, plain_client(handler))
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error == "BodyTooLarge"
    assert body.pulled < len(chunks)


async def test_a_compressed_page_stops_at_the_byte_cap(settings):
    chunks = gzip_chunks(40_000_000)
    body = Chunks(chunks)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, stream=body, headers={"content-encoding": "gzip"}
        )

    output = await parser_with(settings, plain_client(handler)).run([story()])
    row = output.rows["raw.article_text"][0]
    assert row["http_status"] == 200
    assert row["n_chars"] == 0
    assert body.pulled < len(chunks)


async def test_an_oversized_robots_file_stops_at_the_byte_cap(settings):
    chunks = gzip_chunks(40_000_000)
    body = Chunks(chunks)
    pages = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, stream=body, headers={"content-encoding": "gzip"}
            )
        pages.append(request.url.path)
        return httpx.Response(200, text=PAGE)

    await parser_with(settings, plain_client(handler)).run([story()])
    assert body.pulled < len(chunks)
    assert pages == []


async def test_one_compressed_chunk_cannot_expand_past_the_cap():
    from monitor.ingest.download import BodyTooLarge, download

    packed = gzip.compress(b"\0" * 200_000_000, 1)

    def handler(request):
        return httpx.Response(
            200, stream=Chunks([packed]), headers={"content-encoding": "gzip"}
        )

    tracemalloc.start()
    with pytest.raises(BodyTooLarge):
        await download(plain_client(handler), f"http://{PUBLIC}/", 1000, 30.0)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 20_000_000


async def test_a_redirect_body_is_not_read(settings):
    body = Chunks([b"x" * 1024] * 100)

    def handler(request):
        if request.url.path == "/feed":
            return httpx.Response(
                302, stream=body, headers={"location": "/moved"}
            )
        return httpx.Response(200, content=FEED)

    poller = poller_with(settings, plain_client(handler))
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error is None
    assert body.pulled == 0


async def test_a_news_sitemap_may_be_larger_than_a_feed(settings):
    from monitor.ingest.feeds import FEED_MAX_BYTES, SITEMAP_MAX_BYTES

    assert FEED_MAX_BYTES < SITEMAP_MAX_BYTES
    padding = "<!--" + " " * (FEED_MAX_BYTES + 1000) + "-->"
    sitemap = (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        f"{padding}<url><loc>https://example.com/late</loc><news:news>"
        "<news:title>Late story</news:title><news:publication_date>"
        "2026-11-03T20:00:00Z</news:publication_date></news:news></url>"
        "</urlset>"
    ).encode()

    poller = poller_with(
        settings,
        plain_client(lambda request: httpx.Response(200, content=sitemap)),
    )
    read = await poller._read_sitemap(
        [source()],
        f"http://{PUBLIC}/news.xml",
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert (read.files, read.new) == (1, 1)
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].last_error == "BodyTooLarge"


async def test_a_sitemap_over_its_cap_is_not_read(settings):
    from monitor.ingest.feeds import SITEMAP_MAX_BYTES

    chunks = gzip_chunks(SITEMAP_MAX_BYTES * 2)
    body = Chunks(chunks)

    def handler(request):
        return httpx.Response(
            200, stream=body, headers={"content-encoding": "gzip"}
        )

    poller = poller_with(settings, plain_client(handler))
    read = await poller._read_sitemap(
        [source()],
        f"http://{PUBLIC}/news.xml",
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert (read.files, read.new, read.error) == (0, 0, "BodyTooLarge")
    assert body.pulled < len(chunks)


@pytest.mark.parametrize("coding", ["gzip, gzip", "zstd", "br"])
async def test_codings_that_cannot_be_bounded_are_refused(coding):
    from monitor.ingest.download import download

    def handler(request):
        return httpx.Response(
            200, stream=Chunks([b"x"]), headers={"content-encoding": coding}
        )

    with pytest.raises(httpx.DecodingError):
        await download(plain_client(handler), f"http://{PUBLIC}/", 1000, 30.0)


async def test_a_mislabeled_coding_passes_the_body_through():
    from monitor.ingest.download import download

    def handler(request):
        return httpx.Response(
            200,
            stream=Chunks([b"plain"]),
            headers={"content-encoding": "UTF-8"},
        )

    response = await download(
        plain_client(handler), f"http://{PUBLIC}/", 1000, 30.0
    )
    assert response.content == b"plain"


async def test_download_keeps_headers_text_and_decodes_gzip():
    from monitor.ingest.download import download

    seen = {}
    packed = gzip.compress("caf\xe9".encode("latin-1"))

    def handler(request):
        seen["accept"] = request.headers.get("accept-encoding")
        return httpx.Response(
            200,
            stream=Chunks([packed[:5], packed[5:]]),
            headers={
                "content-encoding": "gzip",
                "content-type": "text/html; charset=latin-1",
                "etag": '"v1"',
            },
        )

    response = await download(
        plain_client(handler), f"http://{PUBLIC}/", 1000, 30.0
    )
    assert seen["accept"] == "gzip, deflate"
    assert response.text == "caf\xe9"
    assert response.headers["etag"] == '"v1"'
    assert "content-encoding" not in response.headers
    assert str(response.url) == f"http://{PUBLIC}/"


# Total deadlines


async def test_a_slow_feed_stops_at_the_total_deadline(settings):
    settings.poll.download_deadline_seconds = 0.2
    body = Chunks([b"x"] * 200, delay=0.05)
    poller = poller_with(
        settings,
        plain_client(lambda request: httpx.Response(200, stream=body)),
    )
    started = time.monotonic()
    await poller.poll_once(poller.feeds[0])
    assert time.monotonic() - started < 2.0
    assert poller.feeds[0].last_error == "DeadlineExceeded"


async def test_a_deadline_with_no_network_is_not_a_feed_failure(settings):
    settings.poll.download_deadline_seconds = 0.2
    gate = NetworkGate(settings, probe=down)
    body = Chunks([b"x"] * 200, delay=0.05)
    poller = poller_with(
        settings,
        plain_client(lambda request: httpx.Response(200, stream=body)),
        gate=gate,
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].failures == 0
    assert gate.is_online is False


async def test_a_slow_page_stops_at_the_total_deadline(settings):
    settings.nlp.fetch_deadline_seconds = 0.2
    body = Chunks([b"x"] * 200, delay=0.05)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, stream=body)

    started = time.monotonic()
    output = await parser_with(settings, plain_client(handler)).run([story()])
    assert time.monotonic() - started < 2.0
    assert output.statuses["a"] == ("skipped", "DeadlineExceeded")


# Page regex


def test_the_canonical_regex_stays_fast_on_a_hostile_page():
    page = "<link rel='canonical' " * (20_000 // 22)
    started = time.perf_counter()
    assert canonical_from_html(page) is None
    assert time.perf_counter() - started < 2.0


def test_the_canonical_regex_still_finds_the_link():
    page = (
        '<html><head><LINK data-x="1" REL="canonical" class="a" '
        'href="https://example.com/story?utm_source=x"></head></html>'
    )
    assert canonical_from_html(page) == "https://example.com/story"


# Entries per poll


def many_entries(count: int, start: int = 0) -> bytes:
    items = "".join(
        f"<item><title>Story {n}</title>"
        f"<link>https://example.com/{n}</link></item>"
        for n in range(start, start + count)
    )
    return f"{RSS_HEAD}{items}{RSS_TAIL}".encode()


async def test_one_poll_takes_at_most_200_new_entries(settings):
    delivered = []
    poller = poller_with(
        settings,
        plain_client(
            lambda request: httpx.Response(200, content=many_entries(500))
        ),
    )
    poller.on_new_items = delivered.append
    state = poller.feeds[0]
    assert await poller.poll_once(state) == 200
    assert sum(len(rows) for rows in delivered) == 200
    assert len(poller.sightings) == 200


async def test_entries_past_the_cap_arrive_on_later_polls(settings):
    poller = poller_with(
        settings,
        plain_client(
            lambda request: httpx.Response(200, content=many_entries(500))
        ),
    )
    state = poller.feeds[0]
    counts = [await poller.poll_once(state) for _ in range(4)]
    assert counts == [200, 200, 100, 0]


async def test_the_cap_applies_after_the_sitemap_since_filter(settings):
    since = datetime(2026, 11, 3, 12, tzinfo=UTC)

    def entry(n: int, at: datetime) -> str:
        return (
            f"<url><loc>https://example.com/{n}</loc><news:news>"
            f"<news:title>Story {n}</news:title><news:publication_date>"
            f"{at.isoformat()}</news:publication_date></news:news></url>"
        )

    old = [entry(n, since - timedelta(hours=1)) for n in range(300)]
    late = [entry(1000 + n, since + timedelta(hours=1)) for n in range(150)]
    more = [entry(2000 + n, since + timedelta(hours=2)) for n in range(150)]
    sitemap = (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        + "".join(old + late + more)
        + "</urlset>"
    ).encode()
    delivered = []
    poller = poller_with(
        settings,
        plain_client(lambda request: httpx.Response(200, content=sitemap)),
    )
    poller.on_new_items = delivered.extend
    state = FeedState(source(), f"http://{PUBLIC}/news.xml")
    assert await poller.poll_once(state, parse_news_sitemap, since) == 200
    assert all(row["published_at"] >= since for row in delivered)


async def test_validators_wait_until_the_cap_cuts_nothing(settings):
    """A capped poll keeps the old validators, so the next one reads on."""
    body = many_entries(500)

    def handler(request):
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return httpx.Response(200, content=body, headers={"etag": '"v1"'})

    poller = poller_with(settings, plain_client(handler))
    state = poller.feeds[0]
    counts = [await poller.poll_once(state) for _ in range(4)]
    assert counts == [200, 200, 100, 0]
    assert state.etag == '"v1"'


async def test_a_capped_poll_keeps_the_validators_it_had(settings):
    def handler(request):
        return httpx.Response(
            200, content=many_entries(300), headers={"etag": '"new"'}
        )

    poller = poller_with(settings, plain_client(handler))
    state = poller.feeds[0]
    state.etag = '"old"'
    assert await poller.poll_once(state) == 200
    assert state.etag == '"old"'


def news_entries(names, at):
    return "".join(
        f"<url><loc>https://example.com/{name}</loc><news:news>"
        f"<news:title>Story {name}</news:title><news:publication_date>"
        f"{at.isoformat()}</news:publication_date></news:news></url>"
        for name in names
    )


def news_body(entries):
    return (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        f"{entries}</urlset>"
    ).encode()


async def test_the_cap_counts_only_stories_that_would_be_new(settings):
    settings.poll.resume_stagger_seconds = 0
    since = datetime.now(UTC) - timedelta(hours=3)
    in_gap = since + timedelta(minutes=30)
    known = [f"known{n}" for n in range(210)]
    fresh = [f"fresh{n}" for n in range(100)]
    body = news_body(news_entries(known + fresh, in_gap))
    delivered = []
    poller = Poller(
        settings,
        writer_on(settings),
        [source()],
        {hash_id(f"https://example.com/{name}") for name in known},
        client=plain_client(lambda request: httpx.Response(200, content=body)),
        gate=NetworkGate(settings),
        on_new_items=delivered.extend,
        news_sitemaps={"s": f"http://{PUBLIC}/news.xml"},
    )
    await poller.catch_up(since)
    assert sorted(row["url"] for row in delivered) == sorted(
        f"https://example.com/{name}" for name in fresh
    )
    assert poller.last_catch_up["cut"] == 0


async def test_one_sitemap_read_takes_up_to_500_and_logs_the_rest(settings):
    settings.poll.resume_stagger_seconds = 0
    since = datetime.now(UTC) - timedelta(hours=3)
    in_gap = since + timedelta(minutes=30)
    body = news_body(news_entries(range(700), in_gap))
    delivered = []
    poller = Poller(
        settings,
        writer_on(settings),
        [source()],
        set(),
        client=plain_client(lambda request: httpx.Response(200, content=body)),
        gate=NetworkGate(settings),
        on_new_items=delivered.extend,
        news_sitemaps={"s": f"http://{PUBLIC}/news.xml"},
    )
    with capture_logs() as logs:
        await poller.catch_up(since)
    assert len(delivered) == 500
    capped = [e for e in logs if e["event"] == "feed_entries_capped"]
    assert [(e["n_cut"], e["limit"]) for e in capped] == [(200, 500)]
    assert poller.last_catch_up["cut"] == 200


# Compressed bodies, as real servers frame them


def members(*parts: bytes) -> bytes:
    return b"".join(gzip.compress(part) for part in parts)


async def fetched(chunks: list[bytes], coding: str = "gzip", cap: int = 1000):
    from monitor.ingest.download import download

    body = Chunks(chunks)

    def handler(request):
        return httpx.Response(
            200, stream=body, headers={"content-encoding": coding}
        )

    response = await download(
        plain_client(handler), f"http://{PUBLIC}/", cap, 30.0
    )
    return response.content


@pytest.mark.parametrize(
    "cut",
    [
        pytest.param(None, id="one chunk"),
        pytest.param("member", id="split between members"),
        pytest.param("magic", id="split inside the next header"),
    ],
)
async def test_every_gzip_member_is_decoded(cut):
    first, second = gzip.compress(b"first "), gzip.compress(b"second")
    whole = first + second
    at = {None: len(whole), "member": len(first), "magic": len(first) + 1}[cut]
    chunks = [whole[:at], whole[at:]] if at < len(whole) else [whole]
    assert await fetched(chunks) == b"first second"


async def test_gzip_members_past_the_cap_are_too_large():
    from monitor.ingest.download import BodyTooLarge

    with pytest.raises(BodyTooLarge):
        await fetched([members(b"a" * 800, b"b" * 800)])


async def test_bytes_after_a_gzip_stream_count_against_the_cap():
    from monitor.ingest.download import BodyTooLarge

    chunks = [gzip.compress(b"small")] + [b"\0" * 1024] * 50
    body = Chunks(chunks)

    def handler(request):
        return httpx.Response(
            200, stream=body, headers={"content-encoding": "gzip"}
        )

    from monitor.ingest.download import download

    with pytest.raises(BodyTooLarge):
        await download(plain_client(handler), f"http://{PUBLIC}/", 2000, 30.0)
    assert body.pulled < len(chunks)


async def test_a_little_padding_after_a_gzip_stream_is_ignored():
    assert await fetched([gzip.compress(b"small") + b"\0\0\n"]) == b"small"


@pytest.mark.parametrize(
    ("coding", "packed"),
    [
        ("gzip", gzip.compress(b"x" * 500)),
        ("deflate", zlib.compress(b"x" * 500)),
    ],
)
async def test_a_truncated_compressed_body_is_a_failed_download(
    coding, packed
):
    with pytest.raises(httpx.RemoteProtocolError):
        await fetched([packed[: len(packed) // 2]], coding)


async def test_a_gzip_body_cut_in_its_second_member_is_truncated():
    packed = members(b"first", b"x" * 100)
    with pytest.raises(httpx.RemoteProtocolError):
        await fetched([packed[:-4]])


@pytest.mark.parametrize("size", [1, 3, 1024])
async def test_raw_deflate_decodes_however_it_is_chunked(size):
    engine = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    packed = engine.compress(b"raw deflate body") + engine.flush()
    chunks = [packed[i : i + size] for i in range(0, len(packed), size)]
    assert await fetched(chunks, "deflate") == b"raw deflate body"


async def test_a_truncated_body_stores_no_validators(settings):
    feed = many_entries(5)
    packed = gzip.compress(feed)

    def handler(request):
        return httpx.Response(
            200,
            stream=Chunks([packed[: len(packed) // 2]]),
            headers={"content-encoding": "gzip", "etag": '"partial"'},
        )

    poller = poller_with(settings, plain_client(handler))
    state = poller.feeds[0]
    assert await poller.poll_once(state) == 0
    assert state.etag is None
    assert state.failures == 1
    assert state.last_error == "RemoteProtocolError"


async def test_a_truncated_body_with_no_network_is_not_a_feed_failure(
    settings,
):
    packed = gzip.compress(many_entries(5))

    async def down():
        return False

    def handler(request):
        return httpx.Response(
            200,
            stream=Chunks([packed[: len(packed) // 2]]),
            headers={"content-encoding": "gzip", "etag": '"partial"'},
        )

    gate = NetworkGate(settings, probe=down)
    poller = poller_with(settings, plain_client(handler), gate=gate)
    state = poller.feeds[0]
    assert await poller.poll_once(state) == 0
    assert state.failures == 0
    assert state.etag is None
    assert gate.is_online is False


# Servers that fail when asked for compressed bodies


class PickyServer:
    """A loopback server whose answer depends on `Accept-Encoding`.

    `mode` says what it does when the header names gzip: `hang` or `close`.
    With `refuse_plain`, it treats every other request the same way.
    """

    def __init__(self, mode: str, refuse_plain: bool = False) -> None:
        self.mode = mode
        self.refuse_plain = refuse_plain
        self.asked: list[str] = []
        self.hanging: list[asyncio.Event] = []
        self.server = None

    async def answer(self, reader, writer) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().lower()
            line = next(
                (
                    part
                    for part in head.split("\r\n")
                    if part.startswith("accept-encoding:")
                ),
                "",
            )
            accepted = line.partition(":")[2].strip()
            self.asked.append(accepted)
            if "gzip" in accepted or self.refuse_plain:
                if self.mode == "hang":
                    release = asyncio.Event()
                    self.hanging.append(release)
                    await release.wait()
                return
            feed = many_entries(3)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/rss+xml\r\n"
                b"Connection: close\r\n"
                b"Content-Length: " + str(len(feed)).encode() + b"\r\n\r\n"
            )
            writer.write(feed)
            await writer.drain()
        finally:
            writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(
            self.answer, "127.0.0.1", LOOPBACK_PORT
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        for release in self.hanging:
            release.set()
        self.server.close()
        await self.server.wait_closed()


def loopback_poller(settings, gate=None):
    settings.poll.allow_loopback = True
    settings.poll.download_deadline_seconds = 5.0
    client = build_client(settings, 0.3, 4)
    return poller_with(
        settings,
        client,
        f"http://127.0.0.1:{LOOPBACK_PORT}/feed",
        gate=gate,
    )


@pytest.fixture
def no_plain_hosts(monkeypatch):
    monkeypatch.setattr(
        "monitor.ingest.download.PLAIN_HOSTS", set(), raising=False
    )


@pytest.mark.enable_socket
@pytest.mark.parametrize("mode", ["hang", "close"])
async def test_a_host_that_fails_on_gzip_is_read_through_a_retry(
    settings, no_plain_hosts, mode
):
    async with PickyServer(mode) as server:
        poller = loopback_poller(settings)
        state = poller.feeds[0]
        assert await poller.poll_once(state) == 3
        assert state.failures == 0
        assert state.last_error is None
        assert server.asked == ["gzip, deflate", "identity"]
        # Later polls ask for plain bytes first.
        server.asked.clear()
        assert await poller.poll_once(state) == 0
        assert server.asked == ["identity"]
        assert state.failures == 0


@pytest.mark.enable_socket
async def test_a_server_failing_both_ways_is_one_feed_failure(
    settings, no_plain_hosts
):
    async with PickyServer("close", refuse_plain=True) as server:
        poller = loopback_poller(settings)
        state = poller.feeds[0]
        assert await poller.poll_once(state) == 0
        assert state.failures == 1
        assert server.asked == ["gzip, deflate", "identity"]
        # Nothing was learned, so the next poll asks for gzip again.
        server.asked.clear()
        await poller.poll_once(state)
        assert server.asked == ["gzip, deflate", "identity"]
        assert state.failures == 2


@pytest.mark.enable_socket
async def test_an_outage_does_not_retry(settings, no_plain_hosts):
    async def down():
        return False

    async with PickyServer("hang") as server:
        gate = NetworkGate(settings, probe=down)
        poller = loopback_poller(settings, gate=gate)
        state = poller.feeds[0]
        started = time.monotonic()
        assert await poller.poll_once(state) == 0
        assert time.monotonic() - started < 1.5
        assert server.asked == ["gzip, deflate"]
        assert state.failures == 0
        assert gate.is_online is False


@pytest.mark.enable_socket
async def test_the_retry_stays_within_the_total_deadline(
    settings, no_plain_hosts
):
    from monitor.ingest.download import DeadlineExceeded, download

    settings.poll.allow_loopback = True
    client = build_client(settings, 0.3, 4)

    async def up():
        return True

    async with PickyServer("hang", refuse_plain=True) as server:
        started = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            await download(
                client,
                f"http://127.0.0.1:{LOOPBACK_PORT}/feed",
                1000,
                0.45,
                network_up=up,
            )
        assert time.monotonic() - started < 1.0
        assert server.asked == ["gzip, deflate", "identity"]
    await client.aclose()


async def test_a_failed_connection_is_not_retried(no_plain_hosts):
    from monitor.ingest.download import download

    seen = []

    def refused(request):
        seen.append(request.headers["accept-encoding"])
        raise httpx.ConnectError("refused", request=request)

    async def up():
        return True

    with pytest.raises(httpx.ConnectError):
        await download(
            plain_client(refused),
            f"http://{PUBLIC}/",
            1000,
            5.0,
            network_up=up,
        )
    assert seen == ["gzip, deflate"]


async def test_the_retry_applies_to_sitemaps(settings, no_plain_hosts):
    settings.poll.resume_stagger_seconds = 0
    since = datetime.now(UTC) - timedelta(hours=3)
    body = news_body(news_entries(["a"], since + timedelta(minutes=30)))
    asked = []

    def handler(request):
        asked.append(request.headers["accept-encoding"])
        if "gzip" in asked[-1]:
            raise httpx.ReadError("stream reset", request=request)
        return httpx.Response(200, content=body)

    poller = poller_with(settings, plain_client(handler))
    poller.news_sitemaps = {"s": f"http://{PUBLIC}/news.xml"}
    await poller.catch_up(since)
    assert asked == ["gzip, deflate", "identity"]
    assert poller.last_catch_up["new_items"] == 1


async def test_the_retry_applies_to_pages_and_robots(settings, no_plain_hosts):
    asked = []

    def handler(request):
        asked.append((request.url.path, request.headers["accept-encoding"]))
        if "gzip" in asked[-1][1]:
            raise httpx.ReadError("stream reset", request=request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text=PAGE)

    output = await parser_with(settings, plain_client(handler)).run([story()])
    assert output.rows["raw.article_text"][0]["http_status"] == 200
    assert asked == [
        ("/robots.txt", "gzip, deflate"),
        ("/robots.txt", "identity"),
        ("/story", "identity"),
    ]


async def test_a_redirect_asks_each_host_by_what_is_known_of_it(
    no_plain_hosts, monkeypatch
):
    from monitor.ingest.download import download

    monkeypatch.setattr(
        "monitor.ingest.download.PLAIN_HOSTS",
        {"plain.example"},
        raising=False,
    )
    asked = []

    def handler(request):
        asked.append((request.url.host, request.headers["accept-encoding"]))
        if request.url.host == "plain.example":
            return httpx.Response(
                302, headers={"location": "http://other.example/"}
            )
        return httpx.Response(200, content=b"body")

    await download(plain_client(handler), "http://plain.example/", 1000, 5.0)
    assert asked == [
        ("plain.example", "identity"),
        ("other.example", "gzip, deflate"),
    ]
