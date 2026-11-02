from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from monitor.ingest.poller import (
    FeedState,
    Poller,
    load_seen,
    load_validators,
    next_delay,
)
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
FEED_URL = "https://example.com/feed"


@pytest.fixture
def source():
    return Source("ex", "Example", "https://example.com", [FEED_URL])


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_poller(settings, conn, source, handler, seen=None, validators=None):
    writer = BatchWriter(conn, settings)
    poller = Poller(
        settings,
        writer,
        [source],
        seen if seen is not None else set(),
        validators,
        client=client_for(handler),
    )
    return poller, writer


async def test_new_entries_are_written_once(settings, source):
    conn = connect(settings)
    body = (FIXTURES / "rss2.xml").read_bytes()

    def handler(request):
        return httpx.Response(200, content=body, headers={"etag": '"v1"'})

    poller, writer = make_poller(settings, conn, source, handler)
    state = poller.feeds[0]
    await poller.poll_once(state)
    writer.flush()
    await poller.poll_once(state)
    writer.flush()
    count = conn.execute("SELECT count(*) FROM lake.raw.items").fetchall()
    assert count[0][0] == 5
    fetches = conn.execute(
        "SELECT n_new, etag FROM lake.raw.fetches ORDER BY started_at"
    ).fetchall()
    assert fetches == [(5, '"v1"'), (0, '"v1"')]
    conn.close()


async def test_restart_keeps_conditional_get_and_avoids_duplicates(
    settings, source
):
    conn = connect(settings)
    body = (FIXTURES / "rss2.xml").read_bytes()
    seen_headers = []

    def handler(request):
        seen_headers.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=body, headers={"etag": '"v1"'})

    poller, writer = make_poller(settings, conn, source, handler)
    await poller.poll_once(poller.feeds[0])
    writer.flush()

    restarted, writer = make_poller(
        settings,
        conn,
        source,
        handler,
        seen=load_seen(conn),
        validators=load_validators(conn),
    )
    await restarted.poll_once(restarted.feeds[0])
    writer.flush()
    assert seen_headers == [None, '"v1"']
    assert conn.execute("SELECT count(*) FROM lake.raw.items").fetchall() == [
        (5,)
    ]
    assert conn.execute(
        "SELECT count(*) FROM lake.raw.fetches WHERE not_modified"
    ).fetchall() == [(1,)]
    conn.close()


async def test_circuit_opens_after_consecutive_failures(settings, source):
    conn = connect(settings)
    poller, _ = make_poller(
        settings, conn, source, lambda request: httpx.Response(500)
    )
    state = poller.feeds[0]
    for _ in range(settings.poll.circuit_failures):
        await poller.poll_once(state)
    assert state.is_open
    assert poller.health() == {"healthy": 0, "total": 1}
    conn.close()


async def test_connection_error_is_recorded_not_raised(settings, source):
    conn = connect(settings)

    def handler(request):
        raise httpx.ConnectError("boom")

    poller, writer = make_poller(settings, conn, source, handler)
    await poller.poll_once(poller.feeds[0])
    writer.flush()
    assert conn.execute("SELECT error FROM lake.raw.fetches").fetchall() == [
        ("ConnectError",)
    ]
    conn.close()


def test_next_delay_follows_tier_and_backs_off(settings, source):
    settings.poll.jitter_pct = 0
    state = FeedState(source, FEED_URL)
    source.poll_tier = 1
    assert next_delay(settings, state) == 60
    source.poll_tier = 3
    assert next_delay(settings, state) == 600
    source.poll_tier = 1
    state.failures = 2
    assert next_delay(settings, state) == 240
    state.failures = settings.poll.circuit_failures
    assert next_delay(settings, state) == settings.poll.circuit_retry_seconds


async def test_disabled_sources_are_not_polled(settings):
    conn = connect(settings)
    disabled = Source("x", "X", "https://x.org", [FEED_URL], enabled=False)
    writer = BatchWriter(conn, settings)
    poller = Poller(settings, writer, [disabled], set())
    assert poller.feeds == []
    conn.close()


def test_item_time_axis_is_first_seen(settings, source):
    from monitor.ingest.feeds import parse_feed
    from monitor.ingest.poller import item_row

    entry = parse_feed((FIXTURES / "rss2.xml").read_bytes()).entries[0]
    now = datetime(2026, 11, 3, tzinfo=UTC)
    row = item_row(source, FEED_URL, entry, now)
    assert row["first_seen_at"] == now
    assert row["published_at"] != now
