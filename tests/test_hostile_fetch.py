"""Requests a hostile feed can cause: where they go, and how much they read."""

import asyncio
import gzip
import socket
import time
import tracemalloc
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from monitor.disk import DiskGuard
from monitor.ingest.feeds import parse_news_sitemap
from monitor.ingest.network import NetworkGate
from monitor.ingest.poller import FeedState, Poller
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect
from monitor.parsers.base import Item
from monitor.parsers.fetch_text import FetchTextParser, canonical_from_html

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
    _, new = await poller._read_sitemap(
        source(),
        "http://news.example/index.xml",
        datetime(2026, 11, 3, tzinfo=UTC),
    )
    assert new == 0
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
    files, new = await poller._read_sitemap(
        source(),
        f"http://{PUBLIC}/news.xml",
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert (files, new) == (1, 1)
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
    result = await poller._read_sitemap(
        source(),
        f"http://{PUBLIC}/news.xml",
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert result == (0, 0)
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
