from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from pydantic import BaseModel, SecretStr

from monitor.archive import ArchiveWriter, read_archive
from monitor.ingest.feeds import parse_feed, raw_entry
from monitor.ingest.poller import Poller, load_sightings
from monitor.ingest.sources import Source
from monitor.lake import TABLES, BatchWriter, connect, hash_id, init_lake
from monitor.llm.budget import Budget
from monitor.llm.client import LlmClient, LlmRefused
from monitor.offload import Offloader, is_configured
from monitor.orchestrator import Database
from monitor.parsers.base import Item
from monitor.parsers.dedupe import DedupeIndex, DedupeParser
from monitor.parsers.extract_b import quote_problem
from monitor.parsers.storylines import StorylineEngine

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def unit(*values):
    vector = np.zeros(384, dtype=np.float32)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


FEED_URL = "https://example.com/feed"
NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)


class Answer(BaseModel):
    word: str


def llm_response(text='{"word": "ok"}', stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model="claude-haiku-5-5",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class FakeApi:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **params):
        return self.replies.pop(0)


# Raw entries


def test_raw_entry_keeps_original_html_and_tags():
    feed = parse_feed((FIXTURES / "rss2.xml").read_bytes())
    assert "<p>" in feed.entries[0].raw["summary"]
    assert feed.entries[0].raw["tags"] == ["Politics"]
    assert feed.entries[0].raw["title"].startswith("Story 1")


def test_raw_entry_is_capped():
    entry = {"summary": "x" * 20000, "content": [{"value": "y" * 20000}]}
    raw = raw_entry(entry)
    assert raw["truncated"] is True
    assert len(raw["summary"]) + len(raw["content"]) <= 8000


# Sightings and fetch details


def make_poller(settings, conn, handler, sightings=None, archive=None):
    writer = BatchWriter(conn, settings)
    source = Source("ex", "Example", "https://example.com", [FEED_URL])
    poller = Poller(
        settings,
        writer,
        [source],
        set(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sightings=sightings,
        archive=archive,
    )
    return poller, writer


async def test_sighting_written_once_then_again_when_a_title_changes(settings):
    conn = connect(settings)
    body = (FIXTURES / "rss2.xml").read_bytes()
    bodies = [body, body, body.replace(b"Story 1:", b"Edited 1:")]

    def handler(request):
        return httpx.Response(200, content=bodies.pop(0))

    poller, writer = make_poller(settings, conn, handler)
    for _ in range(3):
        await poller.poll_once(poller.feeds[0])
    writer.flush()
    assert conn.execute(
        "SELECT count(*) FROM lake.raw.sightings"
    ).fetchall() == [(6,)]
    assert len(load_sightings(conn)) == 5
    details = conn.execute(
        "SELECT n_bytes > 0, bozo FROM lake.raw.fetch_details"
    ).fetchall()
    assert details == [(True, False)] * 3
    conn.close()


# Archive


async def test_archive_round_trips_exact_bytes_and_skips_unchanged(settings):
    archive = ArchiveWriter(settings)
    archive.start()
    binary = bytes(range(256)) * 50
    first = archive.add("feeds", FEED_URL, "ex", binary, skip_unchanged=True)
    assert first["n_bytes"] == len(binary)
    assert (
        archive.add("feeds", FEED_URL, "ex", binary, skip_unchanged=True)
        is None
    )
    assert archive.unchanged == 1
    second = archive.add("feeds", FEED_URL, "ex", b"changed\nbody", {"a": 1})
    assert second is not None
    archive.stop()
    path = settings.paths.archive / "feeds" / first["archive_file"]
    records = list(read_archive(path))
    assert [body for _, body in records] == [binary, b"changed\nbody"]
    assert records[1][0]["a"] == 1
    assert records[0][0]["sha256"] == first["sha256"]


def test_archive_recovers_a_file_left_open_by_a_crash(settings):
    folder = settings.paths.archive / "feeds"
    folder.mkdir(parents=True)
    (folder / "feeds-x.rec.zst.part").write_bytes(b"")
    archive = ArchiveWriter(settings)
    archive.start()
    archive.stop()
    assert (folder / "feeds-x.rec.zst").exists()
    assert list(read_archive(folder / "feeds-x.rec.zst")) == []


def test_archive_stops_when_disk_is_low_and_no_bucket(settings, monkeypatch):
    monkeypatch.setattr("monitor.archive.free_gb", lambda path: 14.0)
    archive = ArchiveWriter(settings)
    assert archive.add("pages", "k", "s", b"x") is None
    assert archive.dropped == 1
    settings.archive.s3_bucket = "bucket"
    assert archive.add("pages", "k", "s", b"x") is None
    settings.archive.s3_access_key_id = SecretStr("id")
    settings.archive.s3_secret_access_key = SecretStr("secret")
    assert archive.add("pages", "k", "s", b"x") is not None


async def test_poller_archives_changed_bodies_only(settings):
    conn = connect(settings)
    body = (FIXTURES / "rss2.xml").read_bytes()
    bodies = [body, body, body + b" "]

    def handler(request):
        return httpx.Response(200, content=bodies.pop(0))

    archive = ArchiveWriter(settings)
    archive.start()
    poller, writer = make_poller(settings, conn, handler, archive=archive)
    for _ in range(3):
        await poller.poll_once(poller.feeds[0])
    archive.stop()
    writer.flush()
    assert conn.execute(
        "SELECT count(*) FROM lake.raw.archive_index"
    ).fetchall() == [(2,)]
    unchanged = conn.execute(
        "SELECT count(*) FILTER (WHERE unchanged) FROM lake.raw.fetch_details"
    ).fetchall()
    assert unchanged == [(1,)]
    conn.close()


# LLM traces


async def test_trace_keeps_request_response_prompt_and_items(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    client = LlmClient(
        settings, writer, Budget(settings), client=FakeApi(llm_response())
    )
    await client.structured(
        "classify_a",
        Answer,
        "system prompt",
        "user text",
        batch_id="b1",
        item_ids=["i1", "i2"],
    )
    writer.flush()
    rows = conn.execute(
        """
        SELECT batch_id, model_served, item_ids, request_text, response_text,
               stop_reason, prompt_hash
        FROM lake.nlp.llm_traces
        """
    ).fetchall()
    assert rows[0][:6] == (
        "b1",
        "claude-haiku-5-5",
        ["i1", "i2"],
        "user text",
        '{"word": "ok"}',
        "end_turn",
    )
    assert conn.execute(
        "SELECT text FROM lake.nlp.llm_prompts WHERE prompt_hash = $h",
        {"h": rows[0][6]},
    ).fetchall() == [("system prompt",)]
    calls = conn.execute(
        "SELECT t.call_id FROM lake.nlp.llm_traces t "
        "JOIN lake.nlp.llm_calls c USING (call_id)"
    ).fetchall()
    assert len(calls) == 1
    conn.close()


async def test_refused_and_invalid_responses_are_traced_too(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    client = LlmClient(
        settings,
        writer,
        Budget(settings),
        client=FakeApi(
            llm_response("not json"),
            llm_response(),
            llm_response("", "refusal"),
        ),
    )
    await client.structured("x", Answer, "s", "u")
    with pytest.raises(LlmRefused):
        await client.structured("x", Answer, "s", "u")
    writer.flush()
    rows = conn.execute(
        "SELECT response_text, stop_reason FROM lake.nlp.llm_traces "
        "ORDER BY created_at"
    ).fetchall()
    assert ("not json", "end_turn") in rows
    assert ("", "refusal") in rows
    prompts = conn.execute(
        "SELECT count(*) FROM lake.nlp.llm_prompts"
    ).fetchall()
    assert prompts == [(1,)]
    conn.close()


# Clustering decisions


def test_storyline_decision_keeps_true_nearest_similarity(settings):
    settings.nlp.storyline_join = 0.99
    engine = StorylineEngine(settings)
    first, _ = engine.assign("a", unit(1, 0), "t", "s", NOW, [], None)
    assert engine.last_decision["decision"] == "new"
    assert engine.last_decision["nearest_id"] is None
    second, shown = engine.assign("b", unit(1, 0.3), "t", "s", NOW, [], None)
    assert second is not first
    assert shown == 1.0
    decision = engine.last_decision
    assert decision["decision"] == "new"
    assert decision["nearest_id"] == first.storyline_id
    assert 0.9 < decision["nearest_similarity"] < 0.99
    assert decision["threshold"] == 0.99


async def test_dedupe_records_nearest_even_without_a_match(settings):
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    items = [
        Item(
            "a",
            title="One story on Georgia",
            first_seen_at=NOW,
            vec=unit(1, 0),
        ),
        Item(
            "b",
            title="Other story on rates",
            first_seen_at=NOW,
            vec=unit(1, 1),
        ),
    ]
    output = await parser.run(items)
    rows = {r["item_id"]: r for r in output.rows["nlp.cluster_decisions"]}
    assert rows["a"]["decision"] == "new"
    assert rows["a"]["nearest_id"] is None
    assert rows["b"]["decision"] == "new"
    assert rows["b"]["nearest_similarity"] == pytest.approx(0.7071, abs=0.01)
    assert rows["b"]["simhash_distance"] > 3
    conn.close()


# Rejected quotes


def test_quote_problem_reasons():
    text = "The governor spoke at length."
    assert quote_problem("The governor spoke", text) is None
    assert quote_problem(None, text) == "no quote"
    assert quote_problem("Never said this", text) == "not verbatim"
    assert quote_problem(" ".join(["x"] * 26), text) == "too long"


# Offload


class FakeS3:
    def __init__(self, stored_size=None, fail=False):
        self.objects = {}
        self.stored_size = stored_size
        self.fail = fail

    def upload_file(self, filename, bucket, key):
        if self.fail:
            raise OSError("no network")
        self.objects[key] = Path(filename).stat().st_size

    def head_object(self, Bucket, Key):
        size = self.objects[Key]
        return {"ContentLength": self.stored_size or size}


@pytest.fixture
def offload_settings(settings):
    settings.archive.s3_bucket = "bucket"
    settings.archive.s3_access_key_id = SecretStr("id")
    settings.archive.s3_secret_access_key = SecretStr("secret")
    folder = settings.paths.archive / "feeds"
    folder.mkdir(parents=True)
    (folder / "feeds-1.rec.zst").write_bytes(b"closed")
    (folder / "feeds-2.rec.zst.part").write_bytes(b"open")
    return settings


def test_offload_is_off_without_a_bucket(settings):
    assert is_configured(settings) is False
    client = FakeS3()
    assert Offloader(settings, client).run().uploaded == 0
    assert client.objects == {}


def test_offload_uploads_closed_files_once_and_keeps_recent_ones(
    offload_settings,
):
    client = FakeS3()
    offloader = Offloader(offload_settings, client)
    result = offloader.run()
    assert result.uploaded == 1
    assert list(client.objects) == [
        "electionnight/archive/feeds/feeds-1.rec.zst"
    ]
    assert result.deleted_local == 0
    assert offloader.run().uploaded == 0
    closed = offload_settings.paths.archive / "feeds" / "feeds-1.rec.zst"
    assert closed.exists()


def test_offload_deletes_only_verified_old_files(offload_settings):
    closed = offload_settings.paths.archive / "feeds" / "feeds-1.rec.zst"
    later = closed.stat().st_mtime + 25 * 3600
    assert (
        Offloader(offload_settings, FakeS3()).run(now=later).deleted_local == 1
    )
    assert not closed.exists()


@pytest.mark.parametrize("client", [FakeS3(stored_size=1), FakeS3(fail=True)])
def test_failed_or_short_upload_keeps_the_local_file(offload_settings, client):
    closed = offload_settings.paths.archive / "feeds" / "feeds-1.rec.zst"
    later = closed.stat().st_mtime + 25 * 3600
    result = Offloader(offload_settings, client).run(now=later)
    assert result.failed == ["feeds-1.rec.zst"]
    assert result.deleted_local == 0
    assert closed.exists()


# Schema


def test_new_tables_exist_after_init(settings):
    init_lake(settings)
    conn = connect(settings, read_only=True)
    for table in TABLES:
        conn.execute(f"SELECT * FROM lake.{table} LIMIT 0")
    conn.close()
    assert hash_id("a") == hash_id("a")
