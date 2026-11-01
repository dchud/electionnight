"""Collection and storage: failure, restart, reprocessing, and recovery."""

import asyncio
import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs
from typer.testing import CliRunner

import monitor.archive
import monitor.maintenance
from monitor.archive import ArchiveWriter
from monitor.cli import app
from monitor.control import (
    SpoolWriter,
    drain_spool,
    run_or_request,
)
from monitor.ingest import discover
from monitor.ingest.feeds import parse_feed, parse_news_sitemap
from monitor.ingest.network import NetworkGate
from monitor.ingest.poller import Poller
from monitor.ingest.sources import Source, load_sources, write_sources
from monitor.lake import (
    BatchWriter,
    WriterBusy,
    connect,
    init_lake,
    writer_is_running,
    writer_lock,
)
from monitor.maintenance import (
    RestoreError,
    backup_catalog,
    restore_catalog,
    run_maintenance,
)
from monitor.orchestrator import Database, Stage
from monitor.parsers.base import Item, ParserOutput, needs_parser_sql
from monitor.parsers.dedupe import DedupeParser
from monitor.parsers.embed import EMBED_DIM
from monitor.pipeline import Pipeline
from monitor.rollups import RollupEngine, replay_report
from monitor.settings import Paths, Settings, get_settings, load_settings
from monitor.web.app import create_app

ROOT = Path(__file__).resolve().parent.parent
RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
    b"<item><title>Good story</title><link>https://example.com/a</link></item>"
    b"%s</channel></rss>"
)


@pytest.fixture
def lake(settings):
    conn = connect(settings)
    yield conn, BatchWriter(conn, settings)
    conn.close()


class Recording:
    batch_size = 8
    max_concurrency = 1

    def __init__(self, name="rec", version="1"):
        self.name = name
        self.version = version
        self.input_query = needs_parser_sql(name, version)
        self.seen: list[str] = []
        self.fail = False

    async def run(self, batch):
        if self.fail:
            raise RuntimeError("boom")
        self.seen.extend(item.item_id for item in batch)
        return ParserOutput()


def fast_stage(parser, conn, writer, **options):
    return Stage(
        parser,
        Database(conn),
        writer,
        linger_seconds=0.01,
        retry_delay=0.01,
        **options,
    )


async def run_workers(stages, condition, timeout=5.0):
    tasks = [asyncio.ensure_future(stage.worker()) for stage in stages]
    try:
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def seed_items(writer, ids, seen_at=None):
    seen_at = seen_at or datetime.now(UTC)
    writer.add(
        "raw.items",
        [
            {
                "item_id": item_id,
                "title": item_id,
                "first_seen_at": seen_at,
                "feed_categories": [],
            }
            for item_id in ids
        ],
    )


def finished_run(item_id, parser, version="1", status="ok"):
    return {
        "item_id": item_id,
        "parser": parser,
        "parser_version": version,
        "status": status,
    }


# Reprocessing after a version bump


async def test_rerun_item_reaches_only_stages_that_still_owe_it(
    lake, settings
):
    conn, writer = lake
    ids = ["a", "b", "c", "owed"]
    seed_items(writer, ids, datetime.now(UTC) - timedelta(hours=6))
    writer.add("nlp.parser_runs", [finished_run(i, "up") for i in ids])
    writer.add(
        "nlp.parser_runs",
        [finished_run(i, "down") for i in ids if i != "owed"],
    )
    writer.flush()
    up, down = Recording("up", "2"), Recording("down", "1")
    upstream = fast_stage(up, conn, writer)
    downstream = fast_stage(down, conn, writer)
    upstream.then(downstream)
    await upstream._load_backlog()
    await run_workers(
        [upstream, downstream], lambda: len(up.seen) == len(ids), timeout=5
    )
    writer.flush()
    assert sorted(up.seen) == sorted(ids)
    assert downstream.live.empty()
    assert down.seen == []
    await downstream._load_backlog()
    assert [item.item_id for item in downstream.backlog] == ["owed"]


def unit_vector():
    vector = np.zeros(EMBED_DIM, dtype=np.float32)
    vector[0] = 1.0
    return vector


def seed_dedupe(writer, rows):
    """Rows of (item_id, title, seen_at, group, canonical)."""
    writer.add(
        "raw.items",
        [
            {
                "item_id": item_id,
                "title": title,
                "first_seen_at": seen_at,
                "published_at": seen_at,
                "feed_categories": [],
            }
            for item_id, title, seen_at, _, _ in rows
        ],
    )
    writer.add(
        "nlp.embeddings",
        [
            {
                "item_id": item_id,
                "vec": unit_vector().tolist(),
                "dim": EMBED_DIM,
            }
            for item_id, _, _, _, _ in rows
        ],
    )
    writer.add(
        "nlp.dup_groups",
        [
            {
                "item_id": item_id,
                "dup_group_id": group,
                "is_canonical": canonical,
                "method": "none",
            }
            for item_id, _, _, group, canonical in rows
        ],
    )
    writer.flush()


def rerun_item(item_id, title, seen_at):
    return Item(
        item_id,
        title=title,
        first_seen_at=seen_at,
        published_at=seen_at,
        vec=unit_vector(),
    )


async def test_dedupe_never_matches_an_item_to_itself(lake, settings):
    conn, writer = lake
    seen_at = datetime.now(UTC) - timedelta(hours=3)
    title = "A headline about turnout in Georgia"
    seed_dedupe(writer, [("solo", title, seen_at, "g-solo", True)])
    parser = DedupeParser(settings, Database(conn))
    output = await parser.run([rerun_item("solo", title, seen_at)])
    row = output.rows["nlp.dup_groups"][0]
    assert row["is_canonical"] is True
    assert row["method"] == "none"
    decision = output.rows["nlp.cluster_decisions"][0]
    assert decision["decision"] == "new"
    assert decision["nearest_id"] is None
    assert parser.index.size == 1


@pytest.mark.parametrize("order", [("first", "copy"), ("copy", "first")])
async def test_dedupe_rerun_keeps_the_first_seen_item_canonical(
    lake, settings, order
):
    conn, writer = lake
    first_seen = datetime.now(UTC) - timedelta(hours=3)
    copy_seen = first_seen + timedelta(minutes=10)
    title = "A headline about turnout in Georgia"
    seed_dedupe(
        writer,
        [
            ("first", title, first_seen, "group", True),
            ("copy", title, copy_seen, "group", False),
        ],
    )
    parser = DedupeParser(settings, Database(conn))
    seen = {"first": first_seen, "copy": copy_seen}
    canonical = {}
    for item_id in order:
        output = await parser.run([rerun_item(item_id, title, seen[item_id])])
        canonical[item_id] = output.rows["nlp.dup_groups"][0]["is_canonical"]
    assert canonical == {"first": True, "copy": False}
    assert parser.index.size == 2


async def test_dedupe_rerun_is_stable_for_items_seen_in_the_same_poll(
    lake, settings
):
    conn, writer = lake
    seen_at = datetime.now(UTC) - timedelta(hours=3)
    title = "A headline about turnout in Georgia"
    seed_dedupe(
        writer,
        [
            ("one", title, seen_at, "group", True),
            ("two", title, seen_at, "group", False),
        ],
    )
    parser = DedupeParser(settings, Database(conn))
    output = await parser.run(
        [rerun_item("one", title, seen_at), rerun_item("two", title, seen_at)]
    )
    flags = [row["is_canonical"] for row in output.rows["nlp.dup_groups"]]
    assert sorted(flags) == [False, True]


async def test_dedupe_still_matches_a_new_copy(lake, settings):
    conn, writer = lake
    seen_at = datetime.now(UTC) - timedelta(hours=3)
    title = "A headline about turnout in Georgia"
    seed_dedupe(writer, [("first", title, seen_at, "group", True)])
    parser = DedupeParser(settings, Database(conn))
    output = await parser.run(
        [rerun_item("new", title, seen_at + timedelta(minutes=5))]
    )
    row = output.rows["nlp.dup_groups"][0]
    assert row["is_canonical"] is False
    assert row["dup_group_id"] == "group"


# Scratch lake


def scratch_env(monkeypatch, tmp_path):
    """A working directory with live paths, and SCRATCH_DIR set."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCRATCH_DIR", "scratch")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "hf"))
    get_settings.cache_clear()


def test_scratch_dir_places_every_path_under_it(tmp_path):
    root = tmp_path / "scratch"
    settings = Settings(scratch_dir=root)
    for name in Paths.model_fields:
        path = getattr(settings.paths, name)
        assert path.is_relative_to(root), name


def test_scratch_dir_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path))
    settings = Settings()
    assert settings.paths.writer_lock == tmp_path / "writer.lock"
    assert settings.paths.control == tmp_path / "control"


def test_blank_scratch_dir_means_none(monkeypatch):
    monkeypatch.setenv("SCRATCH_DIR", "")
    settings = Settings()
    assert settings.scratch_dir is None
    assert settings.paths == Paths()


@pytest.mark.parametrize(
    "names",
    [
        ["PATHS__CATALOG"],
        ["PATHS__DATA"],
        ["PATHS__CATALOG", "PATHS__DATA"],
        ["PATHS__WRITER_LOCK"],
    ],
)
def test_a_partial_path_override_is_refused(monkeypatch, tmp_path, names):
    for name in names:
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    with pytest.raises(ValidationError, match="SCRATCH_DIR"):
        Settings()


def test_paths_outside_the_lake_may_move_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("PATHS__MODELS", str(tmp_path / "models"))
    monkeypatch.setenv("PATHS__LOGS", str(tmp_path / "logs"))
    settings = Settings()
    assert settings.paths.catalog == Paths().catalog


def test_load_settings_gives_scratch_its_own_config(monkeypatch, tmp_path):
    scratch_env(monkeypatch, tmp_path)
    Path("config").mkdir()
    Path("config/sources.csv").write_text("live")
    settings = load_settings(os.devnull)
    copy = settings.paths.config / "sources.csv"
    assert copy == Path("scratch/config/sources.csv")
    assert copy.read_text() == "live"
    copy.write_text("edited")
    load_settings(os.devnull)
    assert copy.read_text() == "edited"
    assert Path("config/sources.csv").read_text() == "live"


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(path): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_cli_under_scratch_dir_cannot_touch_live_paths(monkeypatch, tmp_path):
    scratch_env(monkeypatch, tmp_path)
    live = Paths()
    live.backups.mkdir(parents=True)
    live.control.mkdir(parents=True)
    (live.backups / "catalog-20261103-1200.sqlite.zst").write_bytes(b"real")
    runner = CliRunner()
    try:
        with writer_lock(Settings.model_construct(paths=live)):
            before = tree(Path("data"))
            for command in (
                ["init"],
                ["backup", "--now"],
                ["rollups"],
                ["prune", "--all-but-latest", "--backups", "0"],
                ["disk"],
            ):
                result = runner.invoke(app, command)
                assert result.exit_code == 0, (command, result.output)
            assert tree(Path("data")) == before
    finally:
        get_settings.cache_clear()
    assert Path("scratch/catalog.sqlite").exists()
    assert Path("scratch/writer.lock").exists()


def test_cli_refuses_a_partial_path_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATHS__CATALOG", "scratch/catalog.sqlite")
    monkeypatch.setenv("PATHS__DATA", "scratch/lake/")
    get_settings.cache_clear()
    live = Paths()
    live.control.mkdir(parents=True)
    try:
        with writer_lock(Settings.model_construct(paths=live)):
            result = CliRunner().invoke(
                app, ["prune", "--all-but-latest", "--backups", "0"]
            )
    finally:
        get_settings.cache_clear()
    assert result.exit_code != 0
    assert list(live.control.iterdir()) == []


def test_run_of_show_uses_the_one_scratch_setting():
    text = (ROOT / "docs" / "run_of_show.md").read_text()
    assert "SCRATCH_DIR=" in text
    assert "PATHS__CATALOG" not in text


# Rollup window


def test_a_bin_keeps_its_count_as_it_leaves_the_window(lake, settings):
    conn, writer = lake
    bin_start = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    writer.add(
        "raw.items",
        [
            {
                "item_id": f"i{n}",
                "source_id": "s",
                "title": "t",
                "first_seen_at": bin_start + timedelta(seconds=n),
                "feed_categories": [],
            }
            for n in range(300)
        ],
    )
    writer.flush()
    engine = RollupEngine(settings, writer)
    now = bin_start + timedelta(hours=1, minutes=59)
    engine.run(conn, now=now)
    writer.flush()
    while now < bin_start + timedelta(hours=2, minutes=7):
        now += timedelta(seconds=45)
        engine.run(conn, now=now)
        writer.flush()
    rows = conn.execute(
        "SELECT n_items FROM lake.gold.bin_counts "
        "WHERE level = 'none' ORDER BY created_at"
    ).fetchall()
    assert rows == [(300,)]


def test_a_late_classification_reaches_a_bin_past_the_window(lake, settings):
    conn, writer = lake
    now = datetime.now(UTC)
    seed_items(writer, ["late"], now - timedelta(hours=3))
    writer.flush()
    engine = RollupEngine(settings, writer)
    engine.run(conn, now=now, rebuild=True)
    engine.run(conn, now=now)
    writer.flush()
    writer.add(
        "nlp.classify_a",
        [
            {
                "item_id": "late",
                "election_relevant": True,
                "relevance_conf": 0.9,
                "primary_level": "state",
                "states": ["GA"],
                "topics": [],
                "levels": [],
                "race_types": [],
                "free_tags": [],
            }
        ],
    )
    writer.flush()
    engine.run(conn, now=now + timedelta(seconds=45))
    writer.flush()
    rows = conn.execute(
        "SELECT n_relevant FROM lake.gold.bin_counts WHERE level = 'state'"
    ).fetchall()
    assert rows == [(1,)]


# Failures that must not stop collection


async def test_gate_comes_back_online_when_its_callback_fails(settings):
    async def probe():
        return True

    async def restored(gap):
        raise OSError(28, "No space left on device")

    gate = NetworkGate(settings, probe=probe, on_restored=restored)
    gate._go_offline("network", datetime.now(UTC))
    with capture_logs() as logs:
        await gate.check_restored()
    assert gate.is_online
    assert "gate_restore_failed" in [entry["event"] for entry in logs]


async def test_gate_watch_survives_a_failing_probe(settings, monkeypatch):
    monkeypatch.setattr("monitor.ingest.network.TICK_SECONDS", 0.01)
    settings.poll.probe_seconds = 0.0
    calls = []

    async def probe():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("probe bug")
        return True

    gate = NetworkGate(settings, probe=probe)
    gate._go_offline("network", datetime.now(UTC))
    task = asyncio.ensure_future(gate.watch())
    with capture_logs() as logs:
        await asyncio.wait_for(gate.online.wait(), 5)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert "gate_watch_failed" in [entry["event"] for entry in logs]


def feed_client():
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=RSS % b"")
        )
    )


def two_feeds():
    return [
        Source(
            "good",
            "G",
            "https://g.example",
            ["https://g.example/f"],
            poll_tier=1,
        ),
        Source(
            "bad",
            "B",
            "https://b.example",
            ["https://b.example/f"],
            poll_tier=1,
        ),
    ]


async def test_one_feed_raising_leaves_the_others_polling(lake, settings):
    _, writer = lake
    settings.poll.tier_seconds = [1, 1, 1]
    client = feed_client()
    poller = Poller(settings, writer, two_feeds(), set(), client=client)
    real = poller.poll_once

    async def poll_once(state, *args, **kwargs):
        if state.source.source_id == "bad":
            raise RuntimeError("escaped")
        return await real(state, *args, **kwargs)

    poller.poll_once = poll_once
    task = asyncio.ensure_future(poller.run())
    with capture_logs() as logs:
        await asyncio.sleep(2.5)
    assert not task.done()
    assert not client.is_closed
    feeds = {feed.source.source_id: feed for feed in poller.feeds}
    assert feeds["good"].failures == 0
    assert feeds["good"].last_success_at is not None
    assert feeds["bad"].last_error == "RuntimeError"
    assert "feed_poll_failed" in [entry["event"] for entry in logs]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert client.is_closed


async def test_a_short_tier_list_does_not_kill_a_feed(lake, settings):
    _, writer = lake
    settings.poll.tier_seconds = [1]
    source = Source(
        "s", "S", "https://s.example", ["https://s.example/f"], poll_tier=3
    )
    poller = Poller(settings, writer, [source], set(), client=feed_client())
    task = asyncio.ensure_future(poller.run_feed(poller.feeds[0]))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_worker_survives_a_failure_after_the_parser_ran(lake, settings):
    conn, writer = lake

    class BadTable(Recording):
        async def run(self, batch):
            self.seen.extend(item.item_id for item in batch)
            output = ParserOutput()
            if batch[0].item_id == "poison":
                output.add("no.such_table", {"item_id": "poison"})
            return output

    parser = BadTable()
    stage = fast_stage(parser, conn, writer, retry_backfill_seconds=0.01)
    stage.enqueue(Item("poison", first_seen_at=datetime.now(UTC)))
    with capture_logs() as logs:
        task = asyncio.ensure_future(stage.worker())
        await asyncio.sleep(0.1)
        stage.enqueue(Item("next", first_seen_at=datetime.now(UTC)))
        await asyncio.sleep(0.1)
    assert not task.done()
    assert parser.seen == ["poison", "next"]
    assert "poison" not in stage.tracked
    assert "stage_worker_failed" in [entry["event"] for entry in logs]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_a_failed_commit_signal_does_not_raise_from_flush(lake, settings):
    conn, writer = lake
    settings.paths.last_commit.mkdir()
    writer.add("raw.fetches", [{"fetch_id": "a"}])
    with capture_logs() as logs:
        assert writer.flush() == {"raw.fetches": 1}
    assert "commit_signal_failed" in [entry["event"] for entry in logs]


@pytest.fixture
def pipeline(settings):
    built = Pipeline(settings, no_llm=True, stages=["lang", "classify_a"])
    built.stages = built.build_stages()
    built.first_stage = built.stages[0]
    yield built
    built.conn.close()


class CatchUpRecorder:
    def __init__(self):
        self.calls = []

    async def catch_up(self, since):
        self.calls.append(since)


async def test_network_return_survives_a_failed_flush(pipeline, monkeypatch):
    pipeline.poller = CatchUpRecorder()

    def failing_flush():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pipeline.writer, "flush", failing_flush)
    now = datetime.now(UTC)
    gap = {
        "gap_start": now - timedelta(hours=1),
        "gap_end": now,
        "reason": "network",
    }
    with capture_logs() as logs:
        await pipeline._network_restored(gap)
    await asyncio.sleep(0)
    assert pipeline.poller.calls == [gap["gap_start"]]
    assert pipeline.writer.pending() == 1
    assert "gap_flush_failed" in [entry["event"] for entry in logs]


async def test_a_failed_catch_up_is_logged(pipeline):
    class Failing:
        async def catch_up(self, since):
            raise RuntimeError("catch-up bug")

    pipeline.poller = Failing()
    now = datetime.now(UTC)
    gap = {
        "gap_start": now - timedelta(hours=1),
        "gap_end": now,
        "reason": "network",
    }
    with capture_logs() as logs:
        await pipeline._record_gap(gap)
        await asyncio.gather(pipeline.catch_up_task, return_exceptions=True)
        await asyncio.sleep(0)
    assert "catch_up_failed" in [entry["event"] for entry in logs]


async def test_a_task_that_ends_makes_the_pipeline_exit_nonzero(
    settings, monkeypatch
):
    monkeypatch.setattr("monitor.pipeline.load_sources", lambda _: [])
    pipeline = Pipeline(settings, no_llm=True, stages=["lang"])

    async def ends():
        raise asyncio.CancelledError

    pipeline.periodic_jobs = lambda: [ends()]
    with capture_logs() as logs:
        code = await asyncio.wait_for(pipeline.run(), 20)
    assert code == 1
    assert "task_died" in [entry["event"] for entry in logs]


async def test_a_clean_stop_exits_zero(settings, monkeypatch):
    monkeypatch.setattr("monitor.pipeline.load_sources", lambda _: [])
    pipeline = Pipeline(settings, no_llm=True, stages=["lang"])
    pipeline.periodic_jobs = lambda: []
    started = asyncio.Event()

    async def note_downtime():
        started.set()

    pipeline.note_downtime = note_downtime
    task = asyncio.ensure_future(pipeline.run())
    await asyncio.wait_for(started.wait(), 20)
    pipeline.stop.set()
    assert await asyncio.wait_for(task, 20) == 0


# One bad entry in a feed


@pytest.mark.parametrize(
    "link",
    [
        b"http://example.com:abc/x",
        b"http://[example.com/x",
        b"http://example.com:99999/x",
    ],
)
def test_one_malformed_link_keeps_the_rest_of_the_feed(link):
    bad = b"<item><title>Bad</title><link>%s</link></item>" % link
    feed = parse_feed(RSS % bad)
    assert [entry.title for entry in feed.entries] == ["Good story"]


@pytest.mark.parametrize("ref", [b"&#xD83D;", b"&#55357;", b"&#xdfff;"])
def test_a_surrogate_character_reference_keeps_the_feed(ref):
    bad = b"<item><title>Odd %s title</title>" % ref
    bad += b"<link>https://example.com/b</link></item>"
    feed = parse_feed(RSS % bad)
    assert [entry.url for entry in feed.entries] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_one_malformed_link_keeps_the_rest_of_a_news_sitemap():
    body = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
    xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
    <url><loc>http://example.com:abc/x</loc><news:news>
      <news:title>Bad</news:title></news:news></url>
    <url><loc>https://example.com/a</loc><news:news>
      <news:title>Good</news:title></news:news></url></urlset>"""
    feed = parse_news_sitemap(body)
    assert [entry.title for entry in feed.entries] == ["Good"]


def test_sources_check_reports_a_feed_it_cannot_parse(monkeypatch, tmp_path):
    def broken(content):
        raise ValueError("cannot parse")

    real = httpx.AsyncClient

    class Mock(real):
        def __init__(self, **options):
            options.pop("timeout", None)
            super().__init__(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=b"x")
                ),
                **options,
            )

    monkeypatch.setattr(discover, "parse_feed", broken)
    monkeypatch.setattr(discover.httpx, "AsyncClient", Mock)
    settings = Settings(paths=Paths.under(tmp_path))
    source = Source("s", "S", "https://s.example", ["https://s.example/f"])
    outcomes = asyncio.run(discover._check(settings, [source]))
    assert outcomes == [(source, "https://s.example/f", "ValueError")]


async def test_discover_records_a_feed_it_cannot_parse(monkeypatch, tmp_path):
    def broken(content):
        raise ValueError("cannot parse")

    monkeypatch.setattr(discover, "parse_feed", broken)
    settings = Settings(paths=Paths.under(tmp_path))
    finder = discover.Discoverer(settings, {}, feed_client())
    result = discover.OutletResult(source=outlet("s"))
    await finder.try_feed("https://s.example/f", result, datetime.now(UTC))
    assert result.tried == {"https://s.example/f": "ValueError"}
    assert result.good == []


# Lag


def test_only_live_items_count_as_lag(lake, settings):
    conn, writer = lake
    stage = fast_stage(Recording(), conn, writer)
    old = datetime.now(UTC) - timedelta(hours=6)
    stage.live.put_nowait(Item("backfill", first_seen_at=old, backfill=True))
    assert stage.oldest_live_age() == 0.0
    stage.live.put_nowait(Item("live", first_seen_at=old))
    assert stage.oldest_live_age() > 3600


async def test_backfill_does_not_shed_load_downstream(pipeline):
    old = datetime.now(UTC) - timedelta(hours=6)
    lang, classify = pipeline.stages[0], pipeline.stages[1]
    item = Item("old", title="t", first_seen_at=old, backfill=True)
    lang._forward([item], ParserOutput())
    await pipeline.ladder_job()
    assert classify.live.empty()
    assert pipeline.shedding is False
    assert pipeline.embed.throttle == 1.0


async def test_a_retried_backfill_item_waits_behind_live_items(lake, settings):
    conn, writer = lake
    stage = fast_stage(Recording(), conn, writer)
    old = datetime.now(UTC) - timedelta(hours=6)
    item = Item("old", first_seen_at=old, backfill=True)
    stage._fail([item], "boom")
    await asyncio.sleep(0.05)
    assert stage.live.empty()
    assert [waiting.item_id for waiting in stage.backlog] == ["old"]


# Retry after exhausting attempts


async def test_an_item_that_exhausts_retries_returns_through_backfill(
    lake, settings
):
    conn, writer = lake
    seed_items(writer, ["x"])
    writer.flush()
    parser = Recording()
    parser.fail = True
    stage = fast_stage(parser, conn, writer, retry_backfill_seconds=0.05)
    task = asyncio.ensure_future(stage.worker())
    stage.enqueue(Item("x", first_seen_at=datetime.now(UTC)))
    deadline = time.monotonic() + 5
    while stage.errors == 0:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    writer.flush()
    assert "x" in stage.tracked
    parser.fail = False
    await asyncio.sleep(0.1)
    assert "x" not in stage.tracked
    await stage._load_backlog()
    while parser.seen != ["x"]:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_failed_hydrate_leaves_items_owed(lake, settings):
    conn, writer = lake
    seed_items(writer, ["y"])
    writer.flush()
    stage = fast_stage(Recording(), conn, writer)
    real = stage.db.fetch
    calls = []

    async def fetch(sql, params=None):
        calls.append(sql)
        if len(calls) == 2:
            raise duckdb.IOException("hiccup")
        return await real(sql, params)

    stage.db = type("Db", (), {"fetch": staticmethod(fetch)})()
    with pytest.raises(duckdb.IOException):
        await stage._load_backlog()
    assert "y" not in stage.tracked
    await stage._load_backlog()
    assert [item.item_id for item in stage.backlog] == ["y"]
    assert stage.tracked == {"y"}


async def test_an_item_arriving_live_during_hydrate_is_not_doubled(
    lake, settings
):
    conn, writer = lake
    seed_items(writer, ["z"])
    writer.flush()
    stage = fast_stage(Recording(), conn, writer)
    real = stage.db.fetch
    calls = []

    async def fetch(sql, params=None):
        calls.append(sql)
        if len(calls) == 2:
            stage.enqueue(Item("z", first_seen_at=datetime.now(UTC)))
        return await real(sql, params)

    stage.db = type("Db", (), {"fetch": staticmethod(fetch)})()
    await stage._load_backlog()
    assert stage.live.qsize() == 1
    assert len(stage.backlog) == 0


# Restoring the catalog


def write_parquet_rows(conn, writer, batches=4, tag="b"):
    for batch in range(batches):
        writer.add(
            "raw.fetches",
            [{"fetch_id": f"{tag}{batch}-{n}"} for n in range(2500)],
        )
        writer.flush()


def read_all(conn):
    return len(
        conn.execute("SELECT fetch_id FROM lake.raw.fetches").fetchall()
    )


def test_restore_uses_the_newest_backup_whose_files_all_exist(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    write_parquet_rows(conn, writer)
    now = datetime.now(UTC)
    stale = backup_catalog(settings, now - timedelta(hours=4))
    run_maintenance(conn, now=now)
    run_maintenance(conn, now=now + timedelta(hours=3))
    good = backup_catalog(settings, now - timedelta(hours=1))
    corrupt = settings.paths.backups / (
        f"catalog-{now + timedelta(hours=1):%Y%m%d-%H%M}.sqlite.zst"
    )
    corrupt.write_bytes(b"not a backup")
    conn.close()

    report = restore_catalog(settings)

    assert good.name in report
    assert corrupt.name in report
    assert stale.name not in report
    damaged = list(
        settings.paths.catalog.parent.glob("catalog.sqlite.damaged*")
    )
    assert damaged
    reader = connect(settings, read_only=True)
    assert read_all(reader) == 10000
    reader.close()


def test_restore_changes_nothing_when_no_backup_is_usable(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    write_parquet_rows(conn, writer)
    now = datetime.now(UTC)
    stale = backup_catalog(settings, now)
    run_maintenance(conn, now=now)
    run_maintenance(conn, now=now + timedelta(hours=3))
    conn.close()
    before = settings.paths.catalog.read_bytes()
    with pytest.raises(RestoreError, match=stale.name):
        restore_catalog(settings)
    assert settings.paths.catalog.read_bytes() == before
    assert list(settings.paths.catalog.parent.glob("*.damaged*")) == []


def test_restore_dry_run_names_the_backup_and_changes_nothing(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    write_parquet_rows(conn, writer, batches=1)
    backup = backup_catalog(settings)
    conn.close()
    before = settings.paths.catalog.read_bytes()
    report = restore_catalog(settings, dry_run=True)
    assert backup.name in report
    assert settings.paths.catalog.read_bytes() == before
    assert list(settings.paths.backups.glob("*.check*")) == []


def test_restore_refuses_while_the_pipeline_runs(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    write_parquet_rows(conn, writer, batches=1)
    backup_catalog(settings)
    conn.close()
    before = settings.paths.catalog.read_bytes()
    with writer_lock(settings), pytest.raises(WriterBusy):
        restore_catalog(settings)
    assert settings.paths.catalog.read_bytes() == before


WAL_WRITER = """
import os, signal, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("CREATE TABLE left_in_wal(x)")
rows = [(n,) for n in range(2000)]
db.executemany("INSERT INTO left_in_wal VALUES (?)", rows)
os.kill(os.getpid(), signal.SIGKILL)
"""

CLI = "from monitor.cli import app; app()"


def restore_steps() -> list[str]:
    text = (ROOT / "docs" / "run_of_show.md").read_text()
    section = text.split("### Restoring the catalog")[1]
    block = section.split("```bash")[1].split("```")[0]
    return [line for line in block.splitlines() if line.strip()]


def test_documented_restore_steps_work_with_a_nonempty_wal(tmp_path):
    steps = restore_steps()
    assert not [step for step in steps if "data/" in step]
    assert steps[-1] == "scripts/restart_pipeline.sh"
    root = tmp_path / "scratch"
    settings = Settings(scratch_dir=root)
    init_lake(settings)
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    write_parquet_rows(conn, writer, batches=2)
    backup_catalog(settings, datetime.now(UTC) - timedelta(minutes=10))
    write_parquet_rows(conn, writer, batches=2, tag="after")
    conn.close()
    subprocess.run(
        [sys.executable, "-c", WAL_WRITER, str(settings.paths.catalog)],
        check=False,
    )
    wal = settings.paths.catalog.with_name("catalog.sqlite-wal")
    assert wal.stat().st_size > 0

    env = {**os.environ, "SCRATCH_DIR": str(root)}
    env["MONITOR_ENV_FILE"] = os.devnull
    # The last step starts a collector against real feeds, so it does not run.
    for step in steps[:-1]:
        words = shlex.split(step)
        if words[:3] == ["uv", "run", "monitor"]:
            words = [sys.executable, "-c", CLI, *words[3:]]
        subprocess.run(words, cwd=ROOT, env=env, check=True)

    assert not wal.exists() or wal.stat().st_size == 0
    with sqlite3.connect(settings.paths.catalog) as catalog:
        assert catalog.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        names = {
            row[0]
            for row in catalog.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "left_in_wal" not in names
    conn = connect(settings)
    assert read_all(conn) == 5000
    writer = BatchWriter(conn, settings)
    writer.add("raw.fetches", [{"fetch_id": "after-restore"}])
    assert writer.flush() == {"raw.fetches": 1}
    assert read_all(conn) == 5001
    conn.close()


# Time zones


@pytest.fixture
def eastern_system(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_cursors_read_time_in_utc(settings, eastern_system):
    conn = connect(settings)
    cursor = conn.cursor()
    zone = cursor.execute("SELECT current_setting('TimeZone')").fetchone()
    assert zone == ("UTC",)
    cursor.close()
    conn.close()


async def test_restart_keeps_the_day_a_live_item_would_get(
    settings, eastern_system
):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    # 9 pm Eastern on one day is 2 am UTC on the next.
    published = datetime.now(UTC).replace(
        hour=2, minute=0, second=0, microsecond=0
    )
    seed_dedupe(
        writer,
        [("first", "Georgia turnout breaks a record", published, "g", True)],
    )
    parser = DedupeParser(settings, Database(conn))
    copy = rerun_item("copy", "A reworded headline about voters", published)
    output = await parser.run([copy])
    row = output.rows["nlp.dup_groups"][0]
    assert parser.index.days[0] == published.strftime("%Y-%m-%d")
    assert row["is_canonical"] is False
    assert row["method"] == "embedding"
    conn.close()


def test_a_naive_prune_time_is_eastern(monkeypatch):
    snapshots = [
        (1, datetime(2026, 11, 3, 16, 30, tzinfo=UTC)),
        (2, datetime(2026, 11, 3, 17, 30, tzinfo=UTC)),
        (3, datetime(2026, 11, 3, 20, 0, tzinfo=UTC)),
    ]
    monkeypatch.setattr(monitor.maintenance, "_snapshots", lambda _: snapshots)
    versions = monitor.maintenance._versions_to_expire(
        None,
        None,
        None,
        "2026-11-03T12:00:00",
        False,
        datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
    )
    assert versions == [1]


def test_replay_check_reads_naive_times_as_eastern(
    lake, settings, monkeypatch
):
    conn, writer = lake
    seed_items(writer, ["a"], datetime(2026, 11, 3, 17, 2, tzinfo=UTC))
    writer.flush()
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        report = replay_report(
            settings,
            datetime(2026, 11, 3, 12, 0),
            datetime(2026, 11, 3, 12, 5),
        )
    finally:
        monkeypatch.undo()
        time.tzset()
    assert "1 bins with data" in report


@pytest.mark.parametrize(
    ("command", "option"),
    [("prune", "--before"), ("replay-check", "--from")],
)
def test_help_says_naive_times_are_eastern(command, option):
    result = CliRunner().invoke(
        app, [command, "--help"], env={"NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0
    # Help can carry color codes and wrap lines, depending on the terminal.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert option in plain
    assert "Eastern" in plain


def test_prune_before_accepts_the_minute_form(monkeypatch, tmp_path):
    scratch_env(monkeypatch, tmp_path)
    try:
        assert CliRunner().invoke(app, ["init"]).exit_code == 0
        result = CliRunner().invoke(
            app, ["prune", "--dry-run", "--before", "2026-11-03 12:00"]
        )
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output


# The writer's failure counts


def test_a_commit_resets_the_failure_count(lake, settings):
    conn, writer = lake
    writer.add("raw.fetches", [{"no_such_column": 1}])
    writer.flush()
    writer.flush()
    assert writer._failures == {"raw.fetches": 2}
    writer._buffers.clear()
    writer.add("raw.fetches", [{"fetch_id": "ok"}])
    writer.flush()
    assert writer._failures == {}
    writer.add("raw.fetches", [{"no_such_column": 1}])
    writer.flush()
    assert writer.pending() == 1
    path = settings.paths.control / "rejected-raw.fetches.jsonl"
    assert not path.exists()


def test_transient_failures_are_not_counted(lake, settings):
    _, writer = lake
    rows = [{"fetch_id": "a"}]
    writer._hold_back("raw.fetches", rows, duckdb.IOException("full"))
    assert writer._failures == {}
    assert writer.held_rows() == 1
    assert writer.pending() == 1


def test_a_failed_rejection_write_keeps_the_rows(lake, settings):
    _, writer = lake
    (settings.paths.control / "rejected-raw.fetches.jsonl").mkdir()
    rows = [{"fetch_id": "a"}]
    for _ in range(3):
        writer._hold_back("raw.fetches", list(rows), ValueError("bad"))
        writer._buffers.clear()
    assert writer.rejected_rows == 0
    writer._hold_back("raw.fetches", list(rows), ValueError("bad"))
    assert writer.pending() == 1


# Archive


def test_archive_survives_a_failing_flush(settings):
    settings.archive.flush_seconds = 0
    archive = ArchiveWriter(settings)
    archive.start()
    assert archive.add("feeds", "u", "s", b"first")
    deadline = time.monotonic() + 5
    while archive.records == 0:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    stream = archive.streams["feeds"]
    real = stream.writer

    class Failing:
        def __getattr__(self, name):
            return getattr(real, name)

        def flush(self, *args):
            raise OSError(28, "No space left on device")

    stream.writer = Failing()
    with capture_logs() as logs:
        time.sleep(1.5)
    assert archive.thread.is_alive()
    assert archive.status()["alive"] is True
    assert "archive_write_failed" in [entry["event"] for entry in logs]
    stream.writer = real
    assert archive.add("feeds", "v", "s", b"second")
    while archive.records < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    archive.stop()


def test_a_dead_archive_thread_is_visible_and_cannot_block_stop(settings):
    archive = ArchiveWriter(settings)
    archive.thread = threading.Thread(target=lambda: None)
    archive.thread.start()
    archive.thread.join()
    assert archive.add("feeds", "u", "s", b"body") is None
    assert archive.dropped == 1
    assert archive.status()["alive"] is False
    monkey_queue = monitor.archive.queue.Queue(maxsize=1)
    monkey_queue.put(object())
    archive.queue = monkey_queue
    archive.stop_timeout = 0.05
    done = threading.Event()

    def stop():
        archive.stop()
        done.set()

    threading.Thread(target=stop, daemon=True).start()
    assert done.wait(5)


async def test_a_dropped_record_is_not_reported_as_unchanged(
    lake, settings, monkeypatch
):
    conn, writer = lake
    bodies = [RSS % b"", RSS % b" ", RSS % b"  "]
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=bodies.pop(0))
        )
    )
    monkeypatch.setattr(monitor.archive, "free_gb", lambda path: 1.0)
    source = Source(
        "s", "S", "https://s.example", ["https://s.example/f"], poll_tier=1
    )
    poller = Poller(
        settings,
        writer,
        [source],
        set(),
        client=client,
        archive=ArchiveWriter(settings),
    )
    for _ in range(3):
        await poller.poll_once(poller.feeds[0])
    writer.flush()
    rows = conn.execute(
        "SELECT DISTINCT unchanged FROM lake.raw.fetch_details"
    ).fetchall()
    assert rows == [(False,)]
    assert poller.archive.dropped == 3


async def test_an_identical_body_is_reported_as_unchanged(
    lake, settings, monkeypatch
):
    conn, writer = lake
    client = feed_client()
    monkeypatch.setattr(monitor.archive, "free_gb", lambda path: 100.0)
    source = Source(
        "s", "S", "https://s.example", ["https://s.example/f"], poll_tier=1
    )
    poller = Poller(
        settings,
        writer,
        [source],
        set(),
        client=client,
        archive=ArchiveWriter(settings),
    )
    await poller.poll_once(poller.feeds[0])
    await poller.poll_once(poller.feeds[0])
    writer.flush()
    rows = conn.execute(
        "SELECT unchanged FROM lake.raw.fetch_details ORDER BY created_at"
    ).fetchall()
    assert rows == [(False,), (True,)]


# Rediscovery


def outlet(source_id, **fields):
    defaults = {
        "name": source_id,
        "homepage": f"https://{source_id}.example",
        "feed_urls": [f"https://{source_id}.example/old"],
    }
    return Source(source_id, **{**defaults, **fields})


def discover_with(monkeypatch, tmp_path, existing, seed, limit=None):
    settings = Settings(
        paths=Paths.under(tmp_path, config=tmp_path / "config")
    )
    write_sources(settings, existing)

    async def found(_, outlets):
        return [
            discover.OutletResult(
                source=item, good=[(f"{item.homepage}/new", 10)]
            )
            for item in outlets
        ]

    monkeypatch.setattr(discover, "_discover", found)
    monkeypatch.setattr(discover, "read_seed", lambda path: list(seed))
    discover.run_discover(settings, limit=limit)
    return {source.source_id: source for source in load_sources(settings)}


def test_discover_limit_keeps_the_other_outlets(monkeypatch, tmp_path):
    existing = [outlet(name) for name in ("a", "b", "c")]
    seed = [outlet(name, feed_urls=[]) for name in ("a", "b", "c")]
    sources = discover_with(monkeypatch, tmp_path, existing, seed, limit=1)
    assert sorted(sources) == ["a", "b", "c"]
    assert sources["a"].feed_urls == ["https://a.example/new"]
    assert sources["b"].feed_urls == ["https://b.example/old"]


def test_discover_keeps_hand_edits(monkeypatch, tmp_path):
    existing = [
        outlet("off", enabled=False, poll_tier=1, notes="hand note"),
        outlet("tier", poll_tier=3),
        outlet("empty", enabled=False, feed_urls=[]),
    ]
    seed = [
        outlet("off", feed_urls=[], poll_tier=2),
        outlet("tier", feed_urls=[], poll_tier=1),
        outlet("empty", feed_urls=[]),
    ]
    sources = discover_with(monkeypatch, tmp_path, existing, seed)
    assert sources["off"].enabled is False
    assert sources["off"].poll_tier == 1
    assert sources["off"].notes == "hand note"
    assert sources["tier"].poll_tier == 3
    assert sources["empty"].enabled is True


# Spool


def test_a_spool_file_is_complete_when_it_appears(settings):
    spool = SpoolWriter(settings)
    rows = [{"call_id": "a"}, {"call_id": "b"}]
    spool.add("nlp.llm_calls", rows)
    spool.add("nlp.llm_calls", [{"call_id": "c"}])
    moved = []
    control = settings.paths.control
    assert len(list(control.glob("*.spool.jsonl"))) == 2
    assert list(control.glob("*.tmp")) == []
    assert drain_spool(control, lambda table, r: moved.extend(r)) == 3
    assert sorted(row["call_id"] for row in moved) == ["a", "b", "c"]
    spool.add("nlp.llm_calls", [{"call_id": "d"}])
    assert drain_spool(control, lambda table, r: moved.extend(r)) == 1


def test_a_torn_spool_file_is_set_aside_and_the_rest_drain(settings):
    control = settings.paths.control
    (control / "a.spool.jsonl").write_text(
        '{"table": "nlp.llm_calls", "row": {"call_id": "torn"}}\n{"table": "nl'
    )
    (control / "b.spool.jsonl").write_text(
        '{"table": "nlp.llm_calls", "row": {"call_id": "whole"}}\n'
    )
    moved = []
    with capture_logs() as logs:
        drain_spool(control, lambda table, rows: moved.extend(rows))
    assert [row["call_id"] for row in moved] == ["whole"]
    assert sorted(path.name for path in control.iterdir()) == ["a.spool.bad"]
    assert "spool_set_aside" in [entry["event"] for entry in logs]
    assert drain_spool(control, lambda table, rows: moved.extend(rows)) == 0


# Replay report


def test_replay_report_counts_each_item_once(lake, settings):
    conn, writer = lake
    moment = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    seed_items(writer, [f"i{n}" for n in range(10)], moment)
    for version, relevant in (("1", False), ("2", True)):
        writer.add(
            "nlp.classify_a",
            [
                {
                    "item_id": f"i{n}",
                    "election_relevant": relevant,
                    "parser_version": version,
                    "created_at": moment + timedelta(minutes=int(version)),
                }
                for n in range(10)
            ],
        )
    writer.flush()
    report = replay_report(settings, moment, moment + timedelta(minutes=5))
    line = report.splitlines()[1].split()
    assert line[1:3] == ["10", "10"]


# Pipeline downtime


def seed_fetch(writer, started):
    writer.add(
        "raw.fetches",
        [{"fetch_id": "last", "started_at": started, "http_status": 200}],
    )
    writer.flush()


async def test_downtime_becomes_a_gap_and_starts_catch_up(pipeline):
    pipeline.poller = CatchUpRecorder()
    last = datetime.now(UTC) - timedelta(hours=1)
    seed_fetch(pipeline.writer, last)
    await pipeline.note_downtime()
    await asyncio.sleep(0)
    rows = pipeline.conn.execute(
        "SELECT gap_start, reason FROM lake.raw.collection_gaps"
    ).fetchall()
    assert rows == [(last, "pipeline")]
    assert pipeline.poller.calls == [last]


async def test_a_short_restart_is_not_a_gap(pipeline):
    pipeline.poller = CatchUpRecorder()
    seed_fetch(pipeline.writer, datetime.now(UTC) - timedelta(seconds=10))
    await pipeline.note_downtime()
    rows = pipeline.conn.execute(
        "SELECT count(*) FROM lake.raw.collection_gaps"
    ).fetchall()
    assert rows == [(0,)]
    assert pipeline.poller.calls == []


async def test_a_gap_too_short_to_catch_up_is_still_recorded(pipeline):
    pipeline.poller = CatchUpRecorder()
    seed_fetch(pipeline.writer, datetime.now(UTC) - timedelta(minutes=2))
    await pipeline.note_downtime()
    await asyncio.sleep(0)
    rows = pipeline.conn.execute(
        "SELECT reason FROM lake.raw.collection_gaps"
    ).fetchall()
    assert rows == [("pipeline",)]
    assert pipeline.poller.calls == []


async def test_a_first_start_is_not_a_gap(pipeline):
    pipeline.poller = CatchUpRecorder()
    await pipeline.note_downtime()
    rows = pipeline.conn.execute(
        "SELECT count(*) FROM lake.raw.collection_gaps"
    ).fetchall()
    assert rows == [(0,)]


def test_status_page_names_a_pipeline_gap(lake, settings):
    conn, writer = lake
    now = datetime.now(UTC)
    writer.add(
        "raw.collection_gaps",
        [
            {
                "gap_start": now - timedelta(minutes=50),
                "gap_end": now - timedelta(minutes=10),
                "reason": "pipeline",
            }
        ],
    )
    writer.flush()
    with TestClient(create_app(settings)) as client:
        assert "Pipeline stopped" in client.get("/status").text


# Held and rejected rows


def test_status_counts_held_and_rejected_rows(pipeline):
    writer = pipeline.writer
    writer.add("raw.fetches", [{"no_such_column": 1}])
    writer.flush()
    assert pipeline.status()["writer"] == {"held_rows": 1, "rejected_rows": 0}
    writer.flush()
    writer.flush()
    assert pipeline.status()["writer"] == {"held_rows": 0, "rejected_rows": 1}
    writer.add("raw.fetches", [{"fetch_id": "ok"}])
    writer.flush()
    assert pipeline.status()["writer"]["held_rows"] == 0


def test_a_new_writer_counts_rows_rejected_earlier(lake, settings):
    conn, writer = lake
    writer.add("raw.fetches", [{"no_such_column": 1}, {"no_such_column": 2}])
    for _ in range(3):
        writer.flush()
    assert BatchWriter(conn, settings).rejected_rows == 2


def test_strip_warns_about_held_and_rejected_rows(settings):
    now = datetime.now(UTC).isoformat()
    settings.paths.status.write_text(
        json.dumps(
            {
                "updated_at": now,
                "last_commit_at": now,
                "writer": {"held_rows": 12, "rejected_rows": 3},
                "archive": {"alive": False},
            }
        )
    )
    with TestClient(create_app(settings)) as client:
        strip = client.get("/frag/status-strip").text
    assert "12 rows held back" in strip
    assert "3 rows rejected" in strip
    assert "Archive writer stopped" in strip


# The writer lock around direct commands


def test_a_direct_command_holds_the_writer_lock(settings, monkeypatch):
    held = []

    def direct(settings, command, **options):
        held.append(writer_is_running(settings))
        return "done"

    monkeypatch.setattr("monitor.control._direct", direct)
    assert run_or_request(settings, "rollups") == "done"
    assert held == [True]
    assert writer_is_running(settings) is False


def test_a_running_pipeline_receives_the_request(settings, monkeypatch):
    monkeypatch.setattr("monitor.control.REQUEST_TIMEOUT_SECONDS", 0.01)
    with writer_lock(settings), pytest.raises(TimeoutError):
        run_or_request(settings, "rollups", rebuild=True)
    requests = list(settings.paths.control.glob("*.request.json"))
    assert len(requests) == 1
    assert json.loads(requests[0].read_text()) == {
        "command": "rollups",
        "options": {"rebuild": True},
    }


# Stopping the pipeline

HOLDER = """
import fcntl, os, signal, sys
handle = open(sys.argv[1], "a+")
fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
handle.seek(0)
handle.truncate()
handle.write(str(os.getpid()))
handle.flush()
print("ready", flush=True)
signal.pause()
"""


def start_holder(lock, *title):
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock), *title],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "ready"
    return process


def stop_pipeline(root):
    return subprocess.run(
        [str(ROOT / "scripts" / "stop_pipeline.sh")],
        env={**os.environ, "SCRATCH_DIR": str(root)},
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_stop_script_leaves_an_unrelated_process_alone(tmp_path):
    other = subprocess.Popen(["sleep", "60"])
    try:
        (tmp_path / "writer.lock").write_text(str(other.pid))
        result = stop_pipeline(tmp_path)
        assert result.returncode == 0
        assert "not signaling" in result.stdout
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_stop_script_stops_the_pipeline_holding_the_lock(tmp_path):
    process = start_holder(tmp_path / "writer.lock", "monitor", "pipeline")
    # The pipeline's parent collects its exit status. Here, that is this test.
    threading.Thread(target=process.wait, daemon=True).start()
    try:
        result = stop_pipeline(tmp_path)
        assert result.returncode == 0, result.stderr
        assert process.wait(timeout=5) == -signal.SIGTERM
    finally:
        process.kill()
        process.wait()


def test_stop_script_refuses_another_command_holding_the_lock(tmp_path):
    process = start_holder(tmp_path / "writer.lock", "monitor", "rollups")
    try:
        result = stop_pipeline(tmp_path)
        assert result.returncode == 1
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_stop_script_ignores_a_dead_pid_and_a_missing_lock(tmp_path):
    assert stop_pipeline(tmp_path).returncode == 0
    gone = subprocess.Popen(["true"])
    gone.wait()
    (tmp_path / "writer.lock").write_text(str(gone.pid))
    assert stop_pipeline(tmp_path).returncode == 0


def test_restart_script_stops_through_the_checked_script():
    script = (ROOT / "scripts" / "restart_pipeline.sh").read_text()
    assert "scripts/stop_pipeline.sh" in script
    assert "kill " not in script
