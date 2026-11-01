import asyncio
from datetime import UTC, datetime

import pytest

from monitor.lake import BatchWriter, connect, init_lake
from monitor.orchestrator import Database, Stage
from monitor.parsers.base import Item, ParserOutput, needs_parser_sql
from monitor.parsers.lang import LangParser, resolve_language
from monitor.settings import Paths, Settings


@pytest.fixture
def settings(tmp_path):
    configured = Settings(paths=Paths.under(tmp_path))
    init_lake(configured)
    return configured


class Recording:
    name = "rec"
    version = "1"
    batch_size = 4
    max_concurrency = 1
    input_query = needs_parser_sql("rec", "1")

    def __init__(self, fail_times=0):
        self.seen: list[str] = []
        self.fail_times = fail_times

    async def run(self, batch):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        self.seen.extend(item.item_id for item in batch)
        return ParserOutput()


def stage_for(parser, conn, settings, **kwargs):
    writer = BatchWriter(conn, settings)
    return (
        Stage(
            parser,
            Database(conn),
            writer,
            linger_seconds=0.01,
            retry_delay=0.01,
            **kwargs,
        ),
        writer,
    )


async def run_until(stage, condition, timeout=5.0):
    task = asyncio.ensure_future(stage.worker())
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_live_items_flow_to_next_stage_and_record_runs(settings):
    conn = connect(settings)
    first_parser, second_parser = Recording(), Recording()
    first, writer = stage_for(first_parser, conn, settings)
    second, _ = stage_for(second_parser, conn, settings)
    first.then(second)
    first.enqueue(Item("a", first_seen_at=datetime.now(UTC)))
    first.enqueue(Item("a"))  # a duplicate is ignored
    await run_until(first, lambda: first.finished == 1)
    assert first_parser.seen == ["a"]
    assert second.live.qsize() == 1
    writer.flush()
    assert conn.execute(
        "SELECT item_id, status FROM lake.nlp.parser_runs"
    ).fetchall() == [("a", "ok")]
    conn.close()


async def test_stage_filter_skips_items_it_does_not_accept(settings):
    conn = connect(settings)
    stage, _ = stage_for(
        Recording(), conn, settings, accept=lambda item: item.relevant
    )
    stage.enqueue(Item("no"))
    stage.enqueue(Item("yes", relevant=True))
    assert stage.live.qsize() == 1
    conn.close()


async def test_failing_parser_retries_then_records_error(settings):
    conn = connect(settings)
    parser = Recording(fail_times=99)
    stage, writer = stage_for(parser, conn, settings)
    stage.enqueue(Item("a"))
    await run_until(stage, lambda: stage.errors == 1)
    writer.flush()
    assert conn.execute(
        "SELECT status FROM lake.nlp.parser_runs"
    ).fetchall() == [("error",)]
    conn.close()


async def test_failing_parser_recovers_within_attempts(settings):
    conn = connect(settings)
    parser = Recording(fail_times=2)
    stage, _ = stage_for(parser, conn, settings)
    stage.enqueue(Item("a"))
    await run_until(stage, lambda: stage.finished == 1)
    assert parser.seen == ["a"]
    assert stage.errors == 0
    conn.close()


async def test_backfill_finds_owed_items_and_skips_finished_ones(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    now = datetime.now(UTC)
    writer.add(
        "raw.items",
        [
            {
                "item_id": item_id,
                "source_id": "s",
                "title": item_id,
                "summary": "",
                "url": "u",
                "canonical_url": "u",
                "first_seen_at": now,
                "language": "en",
                "feed_categories": [],
            }
            for item_id in ("done", "owed")
        ],
    )
    writer.add(
        "nlp.parser_runs",
        [
            {
                "item_id": "done",
                "parser": "rec",
                "parser_version": "1",
                "status": "ok",
            },
            {
                "item_id": "owed",
                "parser": "rec",
                "parser_version": "0",
                "status": "ok",
            },
        ],
    )
    writer.flush()
    parser = Recording()
    stage, _ = stage_for(parser, conn, settings)
    await stage._load_backlog()
    assert [item.item_id for item in stage.backlog] == ["owed"]
    await run_until(stage, lambda: stage.finished == 1)
    assert parser.seen == ["owed"]
    await stage._load_backlog()
    assert len(stage.backlog) == 0
    conn.close()


async def test_live_items_go_before_backlog(settings):
    conn = connect(settings)
    parser = Recording()
    parser.batch_size = 1
    stage, _ = stage_for(parser, conn, settings)
    stage.backlog.append(Item("old"))
    stage.enqueue(Item("new"))
    await run_until(stage, lambda: stage.finished == 2)
    assert parser.seen == ["new", "old"]
    conn.close()


async def test_paused_backfill_leaves_backlog_waiting(settings):
    conn = connect(settings)
    parser = Recording()
    stage, _ = stage_for(parser, conn, settings)
    stage.pause_backfill = True
    stage.backlog.append(Item("old"))
    stage.enqueue(Item("new"))
    await run_until(stage, lambda: stage.finished == 1)
    assert parser.seen == ["new"]
    assert len(stage.backlog) == 1
    conn.close()


@pytest.mark.parametrize(
    ("declared", "detected", "confidence", "expected"),
    [
        ("en", "en", 0.9, "en"),
        ("es", "en", 0.95, "en"),
        ("es", "en", 0.4, "es"),
        ("", "fr", 0.3, "fr"),
        ("ar", None, 0.0, "ar"),
    ],
)
def test_resolve_language(declared, detected, confidence, expected):
    assert resolve_language(declared, detected, confidence)[0] == expected


async def test_lang_parser_detects_and_updates_item():
    parser = LangParser()
    items = [
        Item("a", title="Voters head to the polls in Georgia", language=""),
        Item(
            "b",
            title="Los votantes acuden a las urnas en Georgia hoy",
            language="es",
        ),
    ]
    output = await parser.run(items)
    assert [row["language"] for row in output.rows["nlp.lang"]] == ["en", "es"]
    assert items[1].language == "es"
