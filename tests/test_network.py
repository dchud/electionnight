"""Outages of the whole network do not count against feeds."""

import asyncio
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from monitor.ingest.feeds import (
    parse_news_sitemap,
    same_site,
    sitemap_children,
)
from monitor.ingest.network import NetworkGate
from monitor.ingest.poller import FeedState, Poller, next_delay
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect, hash_id, init_lake
from monitor.llm.budget import Budget
from monitor.llm.client import LlmClient
from monitor.parsers.base import Item
from monitor.parsers.fetch_text import FetchTextParser
from monitor.pipeline import Pipeline
from monitor.web.app import create_app
from monitor.web.readmodel import ReadModel

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Polls close in Ohio</title>
<link>https://example.com/a</link></item></channel></rss>"""
LOGIN_PAGE = b"<html><body>Sign in to campus Wi-Fi</body></html>"


class Network:
    """A probe and a transport that fail together."""

    def __init__(self) -> None:
        self.up = True
        self.body = FEED
        self.requests = 0

    async def probe(self) -> bool:
        return self.up

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if not self.up:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(200, content=self.body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def source(tier=3):
    return Source(
        source_id="s",
        name="S",
        homepage="https://example.com/",
        feed_urls=["https://example.com/feed"],
        poll_tier=tier,
    )


@pytest.fixture
def lake(settings):
    init_lake(settings)
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    yield writer
    conn.close()


def poller_on(settings, writer, network, gate=None):
    gate = gate or NetworkGate(settings, probe=network.probe)
    poller = Poller(
        settings,
        writer,
        [source()],
        set(),
        client=network.client(),
        gate=gate,
    )
    return poller, gate


async def test_outage_does_not_count_against_the_feed(settings, lake):
    network = Network()
    poller, gate = poller_on(settings, lake, network)
    network.up = False
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].failures == 0
    assert poller.feeds[0].last_error is None
    assert gate.is_online is False
    assert gate.offline_reason == "network"
    assert poller.health() == {"healthy": 1, "total": 1}


async def test_one_failing_feed_still_counts_when_the_network_is_up(
    settings, lake
):
    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    gate = NetworkGate(settings)
    poller = Poller(
        settings,
        lake,
        [source()],
        set(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(refused)),
        gate=gate,
    )
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].failures == 1
    assert gate.is_online is True


async def test_login_page_is_not_a_feed_failure_or_a_validator(settings, lake):
    network = Network()
    poller, gate = poller_on(settings, lake, network)
    state = poller.feeds[0]
    state.etag = "real-etag"

    def login(request):
        return httpx.Response(200, content=LOGIN_PAGE, headers={"etag": "x"})

    poller.client = httpx.AsyncClient(transport=httpx.MockTransport(login))
    network.up = False
    await poller.poll_once(state)
    assert state.failures == 0
    assert state.etag == "real-etag"
    assert gate.is_online is False


async def test_empty_feed_counts_when_the_network_is_up(settings, lake):
    network = Network()
    network.body = LOGIN_PAGE
    poller, gate = poller_on(settings, lake, network)
    await poller.poll_once(poller.feeds[0])
    assert poller.feeds[0].failures == 1
    assert gate.is_online is True


async def test_failing_feeds_share_one_probe(settings):
    calls = 0

    async def probe():
        nonlocal calls
        calls += 1
        return True

    gate = NetworkGate(settings, probe=probe)
    assert all(await asyncio.gather(*(gate.reachable() for _ in range(50))))
    assert calls == 1


async def test_return_records_the_gap_and_wakes_waiting_feeds(settings):
    gaps = []

    async def restored(gap):
        gaps.append(gap)

    network = Network()
    network.up = False
    gate = NetworkGate(settings, probe=network.probe, on_restored=restored)
    assert await gate.reachable() is False
    waiting = asyncio.ensure_future(gate.wait_online())
    sleeping = asyncio.ensure_future(gate.sleep_or_resume(600))
    await asyncio.sleep(0)
    await gate.check_restored()
    assert gate.is_online is False
    network.up = True
    gate._last_probe = float("-inf")
    await gate.check_restored()
    assert gate.is_online is True
    assert await asyncio.wait_for(waiting, 1) is True
    assert await asyncio.wait_for(sleeping, 1) is True
    assert len(gaps) == 1
    assert gaps[0]["reason"] == "network"
    assert gaps[0]["gap_end"] >= gaps[0]["gap_start"]


async def test_delay_runs_out_when_the_network_stays_up(settings):
    gate = NetworkGate(settings)
    assert await gate.wait_online() is False
    assert await gate.sleep_or_resume(0.01) is False


async def test_clock_difference_means_the_laptop_slept(settings):
    gate = NetworkGate(settings)
    wall = time.time() - 3000
    gate.note_clocks(wall, 100.0, wall + 2705, 105.0)
    assert gate.is_online is False
    assert gate.offline_reason == "sleep"
    assert gate.offline_since == datetime.fromtimestamp(wall, UTC)


async def test_ordinary_ticks_are_not_sleep(settings):
    gate = NetworkGate(settings)
    wall = time.time()
    gate.note_clocks(wall, 100.0, wall + 5.1, 105.0)
    assert gate.is_online is True


async def test_sleep_widens_an_outage_noticed_on_waking(settings):
    network = Network()
    network.up = False
    gate = NetworkGate(settings, probe=network.probe)
    await gate.reachable()
    before = gate.offline_since - timedelta(minutes=45)
    wall = before.timestamp()
    gate.note_clocks(wall, 100.0, wall + 2705, 105.0)
    assert gate.offline_since == before
    assert gate.offline_reason == "sleep"


async def test_outage_start_holds_when_the_clock_check_finishes_first(
    settings,
):
    release = asyncio.Event()

    async def slow_failure():
        await release.wait()
        return False

    gate = NetworkGate(settings, probe=slow_failure)
    probing = asyncio.ensure_future(gate.reachable())
    await asyncio.sleep(0)
    wall = time.time() - 2705
    gate.note_clocks(wall, 100.0, wall + 2700, 105.0)
    release.set()
    assert await probing is False
    assert gate.offline_since == datetime.fromtimestamp(wall, UTC)
    assert gate.offline_reason == "sleep"


async def test_outage_start_moves_earlier_when_the_probe_finishes_first(
    settings,
):
    async def down():
        return False

    gate = NetworkGate(settings, probe=down)
    assert await gate.reachable() is False
    assert gate.offline_reason == "network"
    wall = time.time() - 2705
    gate.note_clocks(wall, 100.0, wall + 2700, 105.0)
    assert gate.offline_since == datetime.fromtimestamp(wall, UTC)
    assert gate.offline_reason == "sleep"


async def test_a_later_start_never_replaces_an_earlier_one(settings):
    gate = NetworkGate(settings)
    start = datetime.now(UTC) - timedelta(minutes=45)
    gate._go_offline("sleep", start)
    gate._go_offline("network", start + timedelta(minutes=1))
    assert gate.offline_since == start
    assert gate.offline_reason == "sleep"


async def test_a_lid_close_keeps_its_gap_through_a_slow_probe(settings):
    built = Pipeline(settings, no_llm=True)
    calls = []

    async def catch_up(since, after_first_polls=False):
        calls.append(since)

    built.poller = SimpleNamespace(
        catch_up=catch_up, catch_up_since=None, last_catch_up=None
    )
    release = asyncio.Event()

    async def slow_failure():
        await release.wait()
        return False

    gate = built.gate
    gate.probe = slow_failure
    probing = asyncio.ensure_future(gate.reachable())
    await asyncio.sleep(0)
    wall = time.time() - 2705
    gate.note_clocks(wall, 100.0, wall + 2700, 105.0)
    release.set()
    await probing

    async def up():
        return True

    gate.probe = up
    await gate.check_restored()
    await built.catch_up_task
    rows = built.conn.execute(
        "SELECT reason, round(epoch(gap_end - gap_start) / 60) "
        "FROM lake.raw.collection_gaps"
    ).fetchall()
    assert rows == [("sleep", 45.0)]
    assert calls == [datetime.fromtimestamp(wall, UTC)]
    built.conn.close()


async def test_a_probe_from_before_the_return_cannot_hold_collection(settings):
    release = asyncio.Event()

    async def slow_failure():
        await release.wait()
        return False

    async def up():
        return True

    gate = NetworkGate(settings, probe=slow_failure)
    probing = asyncio.ensure_future(gate.reachable())
    await asyncio.sleep(0)
    gate._go_offline("sleep", datetime.now(UTC) - timedelta(minutes=45))
    gate.probe = up
    await gate.check_restored()
    assert gate.is_online
    release.set()
    await probing
    assert gate.is_online
    assert gate.offline_since is None


def test_backoff_never_outlasts_the_circuit_retry(settings):
    state = FeedState(source(tier=3), "https://example.com/feed")
    limit = settings.poll.circuit_retry_seconds
    jitter = 1 + settings.poll.jitter_pct / 100
    for failures in range(1, settings.poll.circuit_failures + 2):
        state.failures = failures
        assert next_delay(settings, state) <= limit * jitter


def test_llm_does_not_degrade_with_no_network(settings):
    online = {"up": False}
    llm = LlmClient(
        settings,
        None,
        Budget(settings),
        client=object(),
        network_up=lambda: online["up"],
    )
    settings.llm.degrade_after_seconds = 0
    llm._note_failure("APIConnectionError")
    assert llm.failing_since is None
    assert llm.degraded is False
    online["up"] = True
    llm._note_failure("HTTP 401")
    assert llm.degraded is True
    online["up"] = False
    assert llm.degraded is False
    llm.clear_failures()
    online["up"] = True
    assert llm.degraded is False


async def test_article_waits_out_an_outage_and_gets_its_text(
    settings, tmp_path
):
    settings.nlp.fetch_domain_interval_seconds = 0
    network = Network()
    network.up = False
    gate = NetworkGate(settings, probe=network.probe)
    page = "<html><head><title>t</title></head><body><p>" + "words " * 80
    calls = {"n": 0}

    def handler(request):
        if not network.up:
            raise httpx.ConnectError("no route", request=request)
        calls["n"] += 1
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text=page + "</p></body></html>")

    guard = type("Guard", (), {"full_text_allowed": lambda self: True})()
    parser = FetchTextParser(
        settings,
        {},
        guard,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gate=gate,
    )
    item = Item("a", url="https://example.com/a", source_id="s")
    running = asyncio.ensure_future(parser.run([item]))
    await asyncio.sleep(0.05)
    assert running.done() is False
    assert gate.is_online is False
    assert "example.com" not in parser.robots
    network.up = True
    gate._last_probe = float("-inf")
    await gate.check_restored()
    output = await asyncio.wait_for(running, 5)
    assert item.item_id not in output.statuses
    assert calls["n"] == 2
    assert output.rows["raw.article_text"][0]["http_status"] == 200


def seeded_app(settings, gap_start, gap_end, items):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "raw.items",
        [
            {
                "item_id": item_id,
                "source_id": "wire",
                "title": f"Story {item_id}",
                "summary": "",
                "url": f"https://example.com/{item_id}",
                "first_seen_at": seen,
                "published_at": published,
                "language": "en",
                "feed_categories": [],
            }
            for item_id, seen, published in items
        ],
    )
    writer.add(
        "raw.collection_gaps",
        [{"gap_start": gap_start, "gap_end": gap_end, "reason": "sleep"}],
    )
    writer.flush()
    conn.close()
    return create_app(settings)


def test_stories_published_in_a_gap_land_at_their_published_time(settings):
    now = datetime.now(UTC)
    gap_start, gap_end = (
        now - timedelta(minutes=60),
        now - timedelta(minutes=5),
    )
    on_return = gap_end + timedelta(minutes=1)
    in_gap = gap_start + timedelta(minutes=20)
    fresh = gap_end + timedelta(seconds=30)
    late = gap_start - timedelta(minutes=30)
    app = seeded_app(
        settings,
        gap_start,
        gap_end,
        [
            ("in_gap", on_return, in_gap),
            ("after_gap", on_return, fresh),
            ("before_gap", on_return, late),
        ],
    )
    with TestClient(app) as client:
        rows = client.app.state.web.model.rows(
            "SELECT item_id, first_seen_at FROM facts"
        )
    placed = {row["item_id"]: row["first_seen_at"] for row in rows}
    assert placed["in_gap"] == in_gap
    assert placed["after_gap"] == on_return
    assert placed["before_gap"] == on_return


def test_charts_and_status_name_the_gap(settings):
    now = datetime.now(UTC)
    start, end = now - timedelta(minutes=50), now - timedelta(minutes=10)
    app = seeded_app(settings, start, end, [])
    with TestClient(app) as client:
        strip = client.get("/frag/status-strip").text
        status = client.get("/status").text
        web = client.app.state.web
        rules = web.panels.poll_rules(
            SimpleNamespace(window_start=now - timedelta(hours=2), now=now)
        )
    assert "No collection" in strip
    assert "Collection gaps" in status
    assert "Laptop asleep" in status
    assert {"Offline", "Back online"} <= {rule["label"] for rule in rules}


def test_strip_says_when_collection_is_offline(settings):
    now = datetime.now(UTC)
    settings.paths.status.write_text(
        json.dumps(
            {
                "updated_at": now.isoformat(),
                "last_commit_at": now.isoformat(),
                "network": {
                    "online": False,
                    "offline_since": (now - timedelta(minutes=3)).isoformat(),
                    "reason": "network",
                },
            }
        )
    )
    with TestClient(create_app(settings)) as client:
        strip = client.get("/frag/status-strip").text
    assert "Offline since" in strip
    assert "not collecting" in strip


SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://example.com/new?utm_source=x</loc><news:news>
  <news:publication><news:name>S</news:name>
  <news:language>es</news:language></news:publication>
  <news:publication_date>{new}</news:publication_date>
  <news:title>Polls close &amp; counting starts</news:title></news:news></url>
<url><loc>https://example.com/old</loc><news:news>
  <news:publication_date>{old}</news:publication_date>
  <news:title>An older story</news:title></news:news></url>
<url><loc>https://example.com/undated</loc><news:news>
  <news:title>No date given</news:title></news:news></url>
<url><loc>https://example.com/plain</loc></url>
</urlset>"""


def sitemap_body(new, old):
    text = SITEMAP.format(new=new.isoformat(), old=old.isoformat())
    return text.encode()


def test_news_sitemap_entries_have_title_date_and_language():
    new = datetime(2026, 11, 3, 23, 0, tzinfo=UTC)
    old = new - timedelta(days=1)
    feed = parse_news_sitemap(sitemap_body(new, old))
    assert [entry.url for entry in feed.entries] == [
        "https://example.com/new?utm_source=x",
        "https://example.com/old",
        "https://example.com/undated",
    ]
    first = feed.entries[0]
    assert first.title == "Polls close & counting starts"
    assert first.canonical_url == "https://example.com/new"
    assert first.published_at == new
    assert first.language == "es"
    assert first.summary == ""
    assert feed.entries[2].published_at is None


@pytest.mark.parametrize("body", [b"<html><p>Sign in</p>", b"not xml", b""])
def test_a_login_page_is_not_a_sitemap(body):
    feed = parse_news_sitemap(body)
    assert feed.entries == []
    assert feed.bozo is True


def test_sitemap_index_lists_news_files_first():
    body = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap><loc>https://example.com/pages.xml</loc></sitemap>
    <sitemap><loc>https://example.com/news-1.xml</loc></sitemap>
    </sitemapindex>"""
    assert sitemap_children(body) == ["https://example.com/news-1.xml"]
    assert sitemap_children(SITEMAP.encode()) == []


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        ("https://www.example.com/news.xml", True),
        ("https://sitemaps.example.com/news.xml", True),
        ("https://example.com/news.xml", True),
        ("https://evil.test/news.xml", False),
        ("http://127.0.0.1/news.xml", False),
        ("https://notexample.com/news.xml", False),
    ],
)
def test_a_sitemap_index_cannot_send_us_elsewhere(child, expected):
    assert same_site("https://www.example.com/robots", child) is expected


@pytest.mark.parametrize(
    ("parent", "child", "expected"),
    [
        ("https://www.independent.co.uk/x", "https://evil.co.uk/x", False),
        ("https://www.smh.com.au/x", "https://other.com.au/x", False),
        ("https://abcnews.go.com/x", "https://espn.go.com/x", False),
        ("https://www.independent.co.uk/x", "https://independent.co.uk/x", 1),
        ("https://independent.co.uk/x", "https://www.independent.co.uk/x", 1),
        ("https://www.independent.co.uk/x", "https://a.independent.co.uk/", 1),
        ("https://www.example.com/x", "http://[bad/x", False),
        ("http://[bad/x", "https://www.example.com/x", False),
        ("https://www.example.com/x", "not a url", False),
    ],
)
def test_same_site_is_the_host_or_a_subdomain_of_it(parent, child, expected):
    assert same_site(parent, child) is bool(expected)


async def test_a_malformed_child_does_not_cost_the_outlet_its_stories(rig):
    now = datetime.now(UTC)
    index = (
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>http://[bad/news-1.xml</loc></sitemap>"
        b"<sitemap><loc>https://example.com/news-2.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    story = news_sitemap([("kept", now - timedelta(minutes=10))])

    def handler(request):
        body = index if request.url.path == "/news.xml" else story
        return httpx.Response(200, content=body)

    rig.listen({"s": "https://example.com/news.xml"})
    rig.poller.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    await rig.outage_ends(now - timedelta(minutes=30))
    await rig.finished()
    assert rig.urls() == ["https://example.com/kept"]
    assert rig.poller.last_catch_up["gave_up"] == []


def bbc_sources():
    return [
        Source(name, name, "https://www.bbc.com/", ["u"], language=language)
        for name, language in (
            ("bbc-news", "en"),
            ("bbc-news-arabic", "ar"),
            ("bbc-news-mundo", "es"),
        )
    ]


BBC_SITEMAP = "https://www.bbc.com/sitemaps/https-index-com-mundo-news.xml"


def shared_rig(rig, language):
    now = datetime.now(UTC)
    body = news_sitemap(
        [(f"s{n}", now - timedelta(minutes=n + 1)) for n in range(4)],
        language,
    )

    def handler(request):
        rig.requests.append(str(request.url))
        return httpx.Response(200, content=body)

    rig.listen({source.source_id: BBC_SITEMAP for source in bbc_sources()})
    rig.poller = Poller(
        rig.settings,
        rig.pipeline.writer,
        bbc_sources(),
        set(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gate=rig.gate,
        on_new_items=rig.delivered.extend,
        news_sitemaps={
            source.source_id: BBC_SITEMAP for source in bbc_sources()
        },
    )
    rig.pipeline.poller = rig.poller
    return now


async def test_a_shared_sitemap_is_read_once_and_filed_by_language(rig):
    now = shared_rig(rig, "es")
    await rig.outage_ends(now - timedelta(minutes=30))
    await rig.finished()
    assert rig.requests == [BBC_SITEMAP]
    assert {row["source_id"] for row in rig.delivered} == {"bbc-news-mundo"}
    assert len(rig.delivered) == 4
    assert rig.poller.last_catch_up["outlets"] == 1


async def test_a_shared_sitemap_with_no_matching_outlet_is_skipped(rig):
    now = shared_rig(rig, "de")
    with capture_logs() as logs:
        await rig.outage_ends(now - timedelta(minutes=30))
        await rig.finished()
    assert rig.requests == [BBC_SITEMAP]
    assert rig.delivered == []
    assert rig.poller.catch_up_since is None
    assert rig.poller.last_catch_up["gave_up"] == []
    assert "sitemap_no_outlet" in [entry["event"] for entry in logs]


async def test_a_sitemap_listed_once_is_filed_whatever_its_language(rig):
    now = datetime.now(UTC)
    body = news_sitemap([("a", now - timedelta(minutes=1))], "de")
    rig.listen({"s": "https://example.com/a.xml"})
    rig.poller.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body)
        )
    )
    await rig.outage_ends(now - timedelta(minutes=30))
    await rig.finished()
    assert [row["source_id"] for row in rig.delivered] == ["s"]


def test_the_discovery_script_lists_a_shared_url_once():
    spec = importlib.util.spec_from_file_location(
        "find_news_sitemaps", "scripts/find_news_sitemaps.py"
    )
    script = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = script
    spec.loader.exec_module(script)
    outlets = {source.source_id: source for source in bbc_sources()}
    solo = Source("solo", "Solo", "https://solo.example/", ["u"])
    found = [
        script.Found(outlets["bbc-news"], BBC_SITEMAP, 20, "es"),
        script.Found(outlets["bbc-news-arabic"], BBC_SITEMAP, 20, "es"),
        script.Found(outlets["bbc-news-mundo"], BBC_SITEMAP, 20, "es"),
        script.Found(solo, "https://solo.example/news.xml", 5, "de"),
        script.Found(
            outlets["bbc-news"], "https://shared.example/a.xml", 5, "de"
        ),
        script.Found(
            outlets["bbc-news-arabic"], "https://shared.example/a.xml", 5, "de"
        ),
    ]
    kept = script.one_outlet_per_url(found)
    assert [(row.source.source_id, row.url) for row in kept] == [
        ("bbc-news-mundo", BBC_SITEMAP),
        ("solo", "https://solo.example/news.xml"),
    ]


async def catching_up(settings, lake, handler, since):
    settings.poll.resume_stagger_seconds = 0
    rows = []
    poller = Poller(
        settings,
        lake,
        [source()],
        set(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gate=NetworkGate(settings),
        on_new_items=rows.extend,
        news_sitemaps={"s": "https://example.com/news.xml"},
    )
    await poller.catch_up(since)
    return poller, rows


async def test_catch_up_adds_only_stories_from_the_gap(settings, lake):
    since = datetime.now(UTC) - timedelta(hours=1)
    new = since + timedelta(minutes=10)
    old = since - timedelta(hours=1)

    def handler(request):
        return httpx.Response(200, content=sitemap_body(new, old))

    poller, rows = await catching_up(settings, lake, handler, since)
    assert [row["url"] for row in rows] == [
        "https://example.com/new?utm_source=x"
    ]
    assert rows[0]["feed_url"] == "https://example.com/news.xml"
    assert poller.catch_up_since is None
    assert poller.last_catch_up["new_items"] == 1
    assert poller.last_catch_up["outlets"] == 1
    assert poller.feeds[0].failures == 0


async def test_catch_up_skips_stories_a_feed_already_gave(settings, lake):
    since = datetime.now(UTC) - timedelta(hours=1)
    new = since + timedelta(minutes=10)
    old = since - timedelta(hours=1)

    def handler(request):
        return httpx.Response(200, content=sitemap_body(new, old))

    settings.poll.resume_stagger_seconds = 0
    seen = {hash_id("https://example.com/new")}
    poller = Poller(
        settings,
        lake,
        [source()],
        seen,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gate=NetworkGate(settings),
        news_sitemaps={"s": "https://example.com/news.xml"},
    )
    await poller.catch_up(since)
    assert poller.last_catch_up["new_items"] == 0


async def test_catch_up_follows_a_sitemap_index_on_the_same_site(
    settings, lake
):
    since = datetime.now(UTC) - timedelta(hours=1)
    new = since + timedelta(minutes=10)
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/news.xml":
            return httpx.Response(
                200,
                content=b"""<sitemapindex
                xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap><loc>https://example.com/news-1.xml</loc></sitemap>
                <sitemap><loc>http://127.0.0.1/news-2.xml</loc></sitemap>
                </sitemapindex>""",
            )
        return httpx.Response(200, content=sitemap_body(new, new))

    _, rows = await catching_up(settings, lake, handler, since)
    assert "http://127.0.0.1/news-2.xml" not in requested
    assert "https://example.com/news-1.xml" in requested
    assert len(rows) >= 1


def news_sitemap(stories, language="en"):
    entries = "".join(
        f"<url><loc>https://example.com/{name}</loc><news:news>"
        f"<news:publication><news:language>{language}</news:language>"
        f"</news:publication><news:publication_date>{at.isoformat()}"
        f"</news:publication_date><news:title>Story {name}</news:title>"
        "</news:news></url>"
        for name, at in stories
    )
    return (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        f"{entries}</urlset>"
    ).encode()


class Returning:
    """The restore path as it runs: a pipeline's gate, gap rule, and poller."""

    def __init__(self, settings):
        settings.poll.resume_stagger_seconds = 0.05
        settings.poll.probe_cache_seconds = 0.0
        self.settings = settings
        self.pipeline = Pipeline(settings, no_llm=True)
        self.gate = self.pipeline.gate
        self.gate.probe = self.probe
        self.up = True
        self.stories = {}
        self.holds = {}
        self.broken = set()
        self.requests = []
        self.reading = {}
        self.most_reading = 0
        self.delivered = []
        self.poller = None

    def listen(self, sitemaps, sources=None):
        self.poller = Poller(
            self.settings,
            self.pipeline.writer,
            sources or [Source("s", "S", "https://example.com/", ["u"])],
            set(),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler)
            ),
            gate=self.gate,
            on_new_items=self.delivered.extend,
            news_sitemaps=sitemaps,
        )
        self.pipeline.poller = self.poller

    async def probe(self):
        return self.up

    async def handler(self, request):
        path = request.url.path
        self.requests.append(path)
        if not self.up or path in self.broken:
            raise httpx.ConnectError("no route to host", request=request)
        self.reading[path] = self.reading.get(path, 0) + 1
        self.most_reading = max(self.most_reading, self.reading[path])
        try:
            if path in self.holds:
                await self.holds[path].wait()
            return httpx.Response(
                200, content=news_sitemap(self.stories.get(path, []))
            )
        finally:
            self.reading[path] -= 1

    def urls(self):
        return sorted(row["url"] for row in self.delivered)

    async def drop(self):
        """The network goes, and a failing request tells the gate."""
        self.up = False
        assert await self.gate.reachable() is False

    async def back(self):
        self.up = True
        self.gate._last_probe = float("-inf")
        await self.gate.check_restored()
        assert self.gate.is_online

    async def outage_ends(self, start):
        self.gate._go_offline("network", start)
        await self.back()

    async def finished(self):
        await asyncio.wait_for(self.pipeline.catch_up_task, 10)


@pytest.fixture
def rig(settings):
    made = Returning(settings)
    yield made
    if made.pipeline.catch_up_task is not None:
        made.pipeline.catch_up_task.cancel()
    made.pipeline.conn.close()


async def test_reads_that_fail_before_the_gate_knows_do_not_finish(rig):
    now = datetime.now(UTC)
    start = now - timedelta(minutes=30)
    rig.stories["/a.xml"] = [("gone", now - timedelta(minutes=10))]
    rig.listen({"s": "https://example.com/a.xml"})
    await rig.outage_ends(start)
    # Nothing has told the gate yet, so it still says online.
    rig.up = False
    await asyncio.sleep(0.2)
    assert rig.poller.catch_up_since == start
    assert rig.poller.last_catch_up is None
    await rig.back()
    await rig.finished()
    assert rig.urls() == ["https://example.com/gone"]
    assert rig.poller.catch_up_since is None
    assert rig.poller.last_catch_up["since"] == start


async def test_a_sitemap_skipped_during_a_drop_is_read_after_it(rig):
    rig.settings.poll.catch_up_concurrency = 2
    now = datetime.now(UTC)
    for name in "abc":
        rig.stories[f"/{name}.xml"] = [
            (f"{name}-story", now - timedelta(minutes=10))
        ]
    rig.holds = {"/a.xml": asyncio.Event(), "/b.xml": asyncio.Event()}
    rig.listen(
        {name: f"https://example.com/{name}.xml" for name in "abc"},
        [Source(name, name, "https://example.com/", ["u"]) for name in "abc"],
    )
    await rig.outage_ends(now - timedelta(minutes=30))
    await asyncio.sleep(0.2)
    await rig.drop()
    rig.holds["/b.xml"].set()
    await asyncio.sleep(0.05)
    await rig.back()
    rig.holds["/a.xml"].set()
    await rig.finished()
    assert rig.urls() == [
        "https://example.com/a-story",
        "https://example.com/b-story",
        "https://example.com/c-story",
    ]
    assert rig.poller.catch_up_since is None


async def test_a_gap_returning_during_a_run_is_read_over(rig):
    now = datetime.now(UTC)
    start = now - timedelta(minutes=30)
    rig.stories["/a.xml"] = [("before", now - timedelta(minutes=10))]
    rig.holds = {"/b.xml": asyncio.Event()}
    rig.listen(
        {
            "a": "https://example.com/a.xml",
            "b": "https://example.com/b.xml",
        },
        [Source(name, name, "https://example.com/", ["u"]) for name in "ab"],
    )
    await rig.outage_ends(start)
    await asyncio.sleep(0.2)
    assert rig.urls() == ["https://example.com/before"]
    # A second, short gap, with a story published during it.
    await rig.drop()
    rig.stories["/a.xml"].append(("during", datetime.now(UTC)))
    await rig.back()
    rig.holds["/b.xml"].set()
    await rig.finished()
    assert rig.urls() == [
        "https://example.com/before",
        "https://example.com/during",
    ]
    assert rig.poller.last_catch_up["since"] == start


async def test_a_pending_catch_up_restarts_after_a_short_gap(rig):
    now = datetime.now(UTC)
    start = now - timedelta(minutes=30)
    rig.stories["/a.xml"] = [("gone", now - timedelta(minutes=10))]
    rig.listen({"s": "https://example.com/a.xml"})
    await rig.outage_ends(start)
    await rig.drop()
    await asyncio.sleep(0.2)
    assert rig.poller.catch_up_since == start
    await rig.back()
    await rig.finished()
    assert rig.urls() == ["https://example.com/gone"]
    assert rig.poller.last_catch_up["since"] == start


async def test_a_return_starts_catch_up_owed_with_no_run_going(rig):
    now = datetime.now(UTC)
    start = now - timedelta(minutes=30)
    rig.stories["/a.xml"] = [("gone", now - timedelta(minutes=10))]
    rig.listen({"s": "https://example.com/a.xml"})
    rig.poller.catch_up_since = start
    await rig.outage_ends(now - timedelta(seconds=5))
    await rig.finished()
    assert rig.urls() == ["https://example.com/gone"]
    assert rig.poller.catch_up_since is None


async def test_catch_up_runs_never_overlap(rig):
    now = datetime.now(UTC)
    start = now - timedelta(minutes=30)
    rig.stories["/a.xml"] = [("gone", now - timedelta(minutes=10))]
    rig.holds = {"/a.xml": asyncio.Event()}
    rig.listen({"s": "https://example.com/a.xml"})
    await rig.outage_ends(start)
    await asyncio.sleep(0.2)
    running = rig.pipeline.catch_up_task
    await rig.poller.catch_up(now - timedelta(minutes=5))
    await rig.outage_ends(now - timedelta(minutes=20))
    assert rig.pipeline.catch_up_task is running
    await asyncio.sleep(0.2)
    rig.holds["/a.xml"].set()
    await rig.finished()
    assert rig.most_reading == 1
    assert rig.poller.catch_up_since is None


async def test_a_sitemap_that_keeps_failing_is_given_up_and_logged(rig):
    now = datetime.now(UTC)
    rig.stories["/good.xml"] = [("kept", now - timedelta(minutes=10))]
    rig.broken = {"/bad.xml"}
    rig.settings.poll.resume_stagger_seconds = 0
    rig.listen(
        {
            "good": "https://example.com/good.xml",
            "bad": "https://example.com/bad.xml",
        },
        [
            Source(name, name, "https://example.com/", ["u"])
            for name in ("good", "bad")
        ],
    )
    with capture_logs() as logs:
        await rig.outage_ends(now - timedelta(minutes=30))
        await rig.finished()
    assert rig.requests.count("/bad.xml") == 3
    assert rig.requests.count("/good.xml") == 1
    assert rig.urls() == ["https://example.com/kept"]
    assert rig.poller.catch_up_since is None
    assert rig.poller.last_catch_up["gave_up"] == [
        "https://example.com/bad.xml"
    ]
    assert "catch_up_gave_up" in [entry["event"] for entry in logs]


async def test_a_network_drop_does_not_count_toward_giving_up(rig):
    now = datetime.now(UTC)
    rig.stories["/a.xml"] = [("gone", now - timedelta(minutes=10))]
    rig.listen({"s": "https://example.com/a.xml"})
    await rig.outage_ends(now - timedelta(minutes=30))
    for _ in range(4):
        rig.up = False
        await asyncio.sleep(0.2)
        assert rig.poller.catch_up_since is not None
        await rig.back()
    await rig.finished()
    assert rig.urls() == ["https://example.com/gone"]
    assert rig.poller.last_catch_up["gave_up"] == []


async def test_an_error_status_counts_as_an_answer(rig):
    now = datetime.now(UTC)

    def missing(request):
        rig.requests.append(request.url.path)
        return httpx.Response(404)

    rig.listen({"s": "https://example.com/a.xml"})
    rig.poller.client = httpx.AsyncClient(
        transport=httpx.MockTransport(missing)
    )
    await rig.outage_ends(now - timedelta(minutes=30))
    await rig.finished()
    assert rig.requests == ["/a.xml"]
    assert rig.poller.last_catch_up["gave_up"] == []


async def test_restart_reads_sitemaps_after_first_feed_polls(settings):
    settings.poll.resume_stagger_seconds = 0
    settings.poll.tier_seconds = [1, 1, 1]
    now = datetime.now(UTC)
    site = news_sitemap([("a", now - timedelta(minutes=5))])
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b"<item><title>Story a</title><link>https://example.com/a</link>"
        b"<description>The feed summary</description></item>"
        b"</channel></rss>"
    )

    def handler(request):
        is_sitemap = request.url.path.endswith(".xml")
        return httpx.Response(200, content=site if is_sitemap else feed)

    built = Pipeline(settings, no_llm=True)
    delivered = []
    built.poller = Poller(
        settings,
        built.writer,
        [source(tier=1)],
        set(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gate=built.gate,
        on_new_items=delivered.extend,
        news_sitemaps={"s": "https://example.com/news.xml"},
    )
    built.writer.add(
        "raw.fetches",
        [{"fetch_id": "last", "started_at": now - timedelta(hours=1)}],
    )
    built.writer.flush()
    await built.note_downtime()
    running = asyncio.ensure_future(built.poller.run())
    await asyncio.wait_for(built.catch_up_task, 10)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    assert [row["summary"] for row in delivered] == ["The feed summary"]
    built.conn.close()


async def test_a_slow_commit_does_not_hold_the_gate(rig, monkeypatch):
    monkeypatch.setattr(
        "monitor.pipeline.GAP_COMMIT_WAIT_SECONDS", 0.05, raising=False
    )
    now = datetime.now(UTC)
    rig.listen({})
    conn = rig.pipeline.conn
    await rig.pipeline.db.lock.acquire()
    rig.gate._go_offline("network", now - timedelta(minutes=30))
    rig.up = True
    rig.gate._last_probe = float("-inf")
    await asyncio.wait_for(rig.gate.check_restored(), 2)
    assert rig.gate.is_online
    assert rig.pipeline.writer.pending() == 1
    rig.pipeline.db.lock.release()
    await asyncio.wait_for(rig.pipeline.gap_commit, 5)
    assert rig.pipeline.writer.pending() == 0
    rows = conn.execute(
        "SELECT reason FROM lake.raw.collection_gaps"
    ).fetchall()
    assert rows == [("network",)]


async def test_a_slow_commit_still_places_stories_at_their_published_time(
    rig, monkeypatch
):
    monkeypatch.setattr(
        "monitor.pipeline.GAP_COMMIT_WAIT_SECONDS", 0.05, raising=False
    )
    now = datetime.now(UTC)
    published = now - timedelta(minutes=20)
    rig.listen({})
    await rig.pipeline.db.lock.acquire()
    rig.gate._go_offline("network", now - timedelta(minutes=30))
    rig.up = True
    rig.gate._last_probe = float("-inf")
    await asyncio.wait_for(rig.gate.check_restored(), 2)
    # A story read on return goes in behind the gap row, not ahead of it.
    rig.pipeline.writer.add(
        "raw.items",
        [
            {
                "item_id": "in_gap",
                "source_id": "wire",
                "title": "A story",
                "summary": "",
                "url": "https://example.com/in_gap",
                "first_seen_at": datetime.now(UTC),
                "published_at": published,
                "language": "en",
                "feed_categories": [],
            }
        ],
    )
    rig.pipeline.db.lock.release()
    await asyncio.wait_for(rig.pipeline.gap_commit, 5)
    model = ReadModel(rig.settings)
    model.refresh()
    rows = model.rows("SELECT item_id, first_seen_at FROM facts")
    assert [row["first_seen_at"] for row in rows] == [published]


async def test_short_gap_does_not_catch_up_and_long_gap_does(settings):
    built = Pipeline(settings, no_llm=True)
    calls = []

    async def catch_up(since, after_first_polls=False):
        calls.append(since)

    built.poller = SimpleNamespace(
        catch_up=catch_up, catch_up_since=None, last_catch_up=None
    )
    now = datetime.now(UTC)
    limit = settings.poll.catch_up_min_gap_seconds
    short = now - timedelta(seconds=limit - 1)
    await built._network_restored(
        {"gap_start": short, "gap_end": now, "reason": "network"}
    )
    assert built.catch_up_task is None
    long = now - timedelta(seconds=limit + 1)
    await built._network_restored(
        {"gap_start": long, "gap_end": now, "reason": "sleep"}
    )
    await built.catch_up_task
    assert calls == [long]
    built.conn.close()
