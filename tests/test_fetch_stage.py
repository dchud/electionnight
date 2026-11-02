"""The full-text stage: a page cannot stop an item, nor a domain the others."""

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest

from monitor.disk import DiskGuard
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect
from monitor.orchestrator import Database, Stage
from monitor.parsers.base import Item, ParserOutput
from monitor.parsers.fetch_text import FetchTextParser, canonical_from_html

PAGE = (
    "<html><head>{head}</head><body><article><p>"
    + "Turnout was high across Ohio on Tuesday. " * 30
    + "</p></article></body></html>"
)
INTERVAL = 0.05
# A domain's first story waits one interval between robots.txt and the page.
# This leaves room for a loaded machine and stays far under a queued wait.
PROMPT_SECONDS = 20 * INTERVAL


class Sink:
    """A next stage that records what reached it."""

    name = "sink"
    version = "1"
    batch_size = 1
    max_concurrency = 1
    input_query = "SELECT 1 WHERE FALSE"

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.arrived = asyncio.Event()

    async def run(self, batch: list[Item]) -> ParserOutput:
        self.seen.extend(item.item_id for item in batch)
        self.arrived.set()
        return ParserOutput()


class Pages:
    """A site that answers every request and records when each one came."""

    def __init__(self, head: str = "") -> None:
        self.page = PAGE.format(head=head)
        self.times: dict[str, list[float]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.times.setdefault(host, []).append(time.monotonic())
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(200, html=self.page)


def fetch_parser(settings, handler, interval=INTERVAL):
    settings.nlp.fetch_domain_interval_seconds = interval
    return FetchTextParser(
        settings,
        {"s": Source("s", "S", "https://example.com")},
        DiskGuard(settings, free_space=lambda path: 100.0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def story(item_id, domain):
    return Item(
        item_id,
        source_id="s",
        url=f"https://{domain}/{item_id}",
        title="T",
        relevant=True,
        relevance_conf=0.9,
    )


@pytest.fixture
async def lake(settings):
    conn = connect(settings)
    yield conn, BatchWriter(conn, settings)
    conn.close()


async def stop(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.005)


# A malformed canonical link


@pytest.mark.parametrize(
    "href",
    [
        "https://www.example.com:PORT/story",
        "http://[bad/story",
        "https://[::1]:99999/story",
    ],
)
def test_a_malformed_canonical_link_is_no_canonical_link(href):
    page = f'<html><head><link rel="canonical" href="{href}"></head></html>'
    assert canonical_from_html(page) is None


async def test_a_malformed_canonical_link_does_not_fail_the_item(
    settings, lake
):
    conn, writer = lake
    href = "https://www.example.com:PORT/story"
    head = f'<link rel="canonical" href="{href}">'
    parser = fetch_parser(settings, Pages(head), interval=0.0)
    database = Database(conn)
    sink = Sink()
    stage = Stage(
        parser, database, writer, linger_seconds=0.0, retry_delay=0.01
    )
    after = Stage(sink, database, writer, linger_seconds=0.0)
    stage.then(after)
    tasks = stage.tasks() + after.tasks()
    stage.enqueue(story("a", "example.com"))
    try:
        await asyncio.wait_for(sink.arrived.wait(), 5)
    finally:
        await stop(tasks)
    assert sink.seen == ["a"]
    assert stage.errors == 0
    writer.flush()
    runs = conn.execute(
        "SELECT status, error FROM lake.nlp.parser_runs "
        "WHERE parser = 'fetch_text'"
    ).fetchall()
    assert runs == [("ok", None)]
    text, canonical = conn.execute(
        "SELECT text, canonical_url FROM lake.raw.article_text"
    ).fetchone()
    assert "Turnout was high" in text
    assert canonical is None


async def test_a_malformed_item_url_is_skipped(settings):
    parser = fetch_parser(settings, Pages(), interval=0.0)
    item = Item("a", source_id="s", url="http://[bad/story", title="T")
    assert parser.partition(item) == ""
    output = await parser.run([item])
    assert output.statuses["a"] == ("skipped", "ValueError")


# One busy domain


async def wait_behind(settings, lake, busy: dict[str, int]) -> float:
    """How long a story from a quiet domain waits behind busy domains."""
    conn, writer = lake
    parser = fetch_parser(settings, Pages())
    database = Database(conn)
    sink = Sink()
    stage = Stage(parser, database, writer, linger_seconds=0.0, backfill=False)
    after = Stage(sink, database, writer, linger_seconds=0.0, backfill=False)
    stage.then(after)
    tasks = stage.tasks() + after.tasks()
    for domain, count in busy.items():
        for n in range(count):
            stage.enqueue(story(f"{domain}-{n}", domain))
    # Every worker has taken what it can before the quiet story arrives.
    await asyncio.sleep(0)
    started = time.monotonic()
    stage.enqueue(story("quiet", "quiet.example"))
    try:
        while "quiet" not in sink.seen:
            await sink.arrived.wait()
            sink.arrived.clear()
    finally:
        await stop(tasks)
    return time.monotonic() - started


@pytest.mark.parametrize("stories", [60, 240])
async def test_a_busy_domain_does_not_delay_another_domain(
    settings, lake, stories
):
    waited = await asyncio.wait_for(
        wait_behind(settings, lake, {"busy.example": stories}), 30
    )
    assert waited < PROMPT_SECONDS


async def test_many_busy_domains_do_not_delay_another_domain(settings, lake):
    busy = {f"busy{n}.example": 30 for n in range(20)}
    waited = await asyncio.wait_for(wait_behind(settings, lake, busy), 30)
    assert waited < PROMPT_SECONDS


async def test_the_domain_interval_still_holds(settings, lake):
    conn, writer = lake
    pages = Pages()
    parser = fetch_parser(settings, pages, interval=2 * INTERVAL)
    stage = Stage(
        parser, Database(conn), writer, linger_seconds=0.0, backfill=False
    )
    tasks = stage.tasks()
    for domain in ("a.example", "b.example"):
        for n in range(5):
            stage.enqueue(story(f"{domain}-{n}", domain))
    try:
        await until(lambda: stage.finished == 10)
    finally:
        await stop(tasks)
    for domain in ("a.example", "b.example"):
        times = pages.times[domain]
        # One robots.txt and five pages.
        assert len(times) == 6
        gaps = [
            later - sooner
            for sooner, later in zip(times, times[1:], strict=False)
        ]
        # The handler runs just after the pace, so a gap can read short by
        # the time one request took to get there.
        assert min(gaps) >= INTERVAL


async def test_total_concurrency_stays_at_the_worker_count(settings, lake):
    conn, writer = lake
    running = 0
    peak = 0
    per_domain: dict[str, int] = {}
    worst_domain = 0
    release = asyncio.Event()

    async def handler(request):
        nonlocal running, peak, worst_domain
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        host = request.url.host
        running += 1
        per_domain[host] = per_domain.get(host, 0) + 1
        peak = max(peak, running)
        worst_domain = max(worst_domain, per_domain[host])
        await release.wait()
        running -= 1
        per_domain[host] -= 1
        return httpx.Response(200, html=PAGE.format(head=""))

    parser = fetch_parser(settings, handler, interval=0.0)
    stage = Stage(
        parser, Database(conn), writer, linger_seconds=0.0, backfill=False
    )
    tasks = stage.tasks()
    assert parser.max_concurrency == 16
    for n in range(40):
        stage.enqueue(story(f"s{n}", f"d{n % 20}.example"))
    try:
        await until(lambda: peak >= 16)
        await asyncio.sleep(0.05)
        assert peak == 16
        assert worst_domain == 1
        release.set()
        await until(lambda: stage.finished == 40)
    finally:
        release.set()
        await stop(tasks)
    assert worst_domain == 1


# Partitions in a stage


class Partitioned:
    """Runs until told to finish, one partition per `source_id`."""

    name = "partitioned"
    version = "1"
    batch_size = 1
    max_concurrency = 2
    input_query = "SELECT 1 WHERE FALSE"

    def __init__(self, fail=(), unwritable=()) -> None:
        self.fail = set(fail)
        self.unwritable = set(unwritable)
        self.started: list[str] = []
        self.finish = asyncio.Event()

    @staticmethod
    def partition(item: Item) -> str:
        return item.source_id

    async def run(self, batch: list[Item]) -> ParserOutput:
        ids = {item.item_id for item in batch}
        self.started.extend(item.item_id for item in batch)
        # The other worker sets its item aside while this one runs.
        await asyncio.sleep(0.01)
        if self.fail & ids:
            raise RuntimeError("failed")
        await self.finish.wait()
        output = ParserOutput()
        if self.unwritable & ids:
            output.add("no.such_table", {"item_id": "x"})
        return output


def seen_now(item_id, source_id, backfill=False):
    return Item(
        item_id,
        source_id=source_id,
        first_seen_at=datetime.now(UTC),
        backfill=backfill,
    )


async def test_a_set_aside_item_counts_as_waiting(settings, lake):
    conn, writer = lake
    parser = Partitioned()
    stage = Stage(
        parser, Database(conn), writer, linger_seconds=0.0, backfill=False
    )
    tasks = stage.tasks()
    for item_id in ("a", "b", "c"):
        stage.enqueue(seen_now(item_id, "one"))
    try:
        await until(lambda: stage.parked)
        await until(lambda: len(stage.parked["one"]) == 2)
        assert parser.started == ["a"]
        assert stage.depth == 2
        assert stage.oldest_live_age() > 0
        parser.finish.set()
        await until(lambda: stage.finished == 3)
        assert parser.started == ["a", "b", "c"]
        assert stage.depth == 0
    finally:
        await stop(tasks)


async def test_a_failure_frees_the_partition(settings, lake):
    conn, writer = lake
    parser = Partitioned(fail={"a"})
    parser.finish.set()
    stage = Stage(
        parser,
        Database(conn),
        writer,
        linger_seconds=0.0,
        backfill=False,
        retry_delay=0.01,
    )
    tasks = stage.tasks()
    stage.enqueue(seen_now("a", "one"))
    stage.enqueue(seen_now("b", "one"))
    try:
        await until(lambda: "b" in parser.started)
        await until(lambda: stage.errors == 1)
        await until(lambda: not stage.busy and not stage.parked)
    finally:
        await stop(tasks)


async def test_a_failure_after_the_parser_ran_frees_the_partition(
    settings, lake
):
    conn, writer = lake
    parser = Partitioned(unwritable={"a"})
    parser.finish.set()
    stage = Stage(
        parser, Database(conn), writer, linger_seconds=0.0, backfill=False
    )
    tasks = stage.tasks()
    stage.enqueue(seen_now("a", "one"))
    stage.enqueue(seen_now("b", "one"))
    try:
        await until(lambda: "b" in parser.started)
        await until(lambda: not stage.busy and not stage.parked)
    finally:
        await stop(tasks)


async def test_paused_backfill_does_not_strand_a_live_item(settings, lake):
    conn, writer = lake
    parser = Partitioned()
    stage = Stage(
        parser, Database(conn), writer, linger_seconds=0.0, backfill=False
    )
    tasks = stage.tasks()
    stage.pause_backfill = True
    stage.enqueue(seen_now("first", "one"))
    try:
        await until(lambda: parser.started == ["first"])
        # Set aside in this order, the backfill item comes out first.
        stage.parked["one"] = [
            seen_now("old", "one", backfill=True),
            seen_now("new", "one"),
        ]
        parser.finish.set()
        await until(lambda: "new" in parser.started)
        assert "old" not in parser.started
        assert [item.item_id for item in stage.backlog] == ["old"]
    finally:
        await stop(tasks)


async def test_parked_backfill_holds_back_the_feeder(settings, lake):
    conn, writer = lake
    stage = Stage(
        Partitioned(),
        Database(conn),
        writer,
        linger_seconds=0.0,
        backfill=False,
    )
    stage.parked["one"] = [
        seen_now(f"old{n}", "one", backfill=True) for n in range(5)
    ]
    stage.parked["two"] = [seen_now("live", "two")]
    assert stage._waiting_backfill() == 5
