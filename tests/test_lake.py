from datetime import UTC, datetime

import duckdb
import pytest

from monitor.lake import (
    TABLES,
    BatchWriter,
    WriterBusy,
    connect,
    current_snapshot,
    hash_id,
    init_lake,
    latest_sql,
    rows_to_arrow,
    writer_is_running,
    writer_lock,
)
from monitor.settings import Paths, Settings


@pytest.fixture
def settings(tmp_path):
    configured = Settings(paths=Paths.under(tmp_path))
    init_lake(configured)
    return configured


def test_init_twice_creates_every_table(settings):
    init_lake(settings)
    conn = connect(settings, read_only=True)
    for table in TABLES:
        assert conn.execute(f"SELECT count(*) FROM lake.{table}").fetchone()
    conn.close()


def test_commit_is_visible_to_read_only_connection(settings):
    writer_conn = connect(settings)
    reader = connect(settings, read_only=True)
    writer = BatchWriter(writer_conn, settings)
    writer.add(
        "raw.items",
        [
            {
                "item_id": "a",
                "title": "first",
                "first_seen_at": datetime.now(UTC),
                "feed_categories": [],
                "raw_entry": {"k": "v"},
            },
            {"item_id": "b", "title": "second", "feed_categories": ["x"]},
        ],
    )
    assert writer.pending() == 2
    assert writer.flush() == {"raw.items": 2}
    rows = reader.execute("SELECT count(*) FROM lake.raw.items").fetchone()
    assert rows[0] == 2
    assert settings.paths.last_commit.exists()
    writer_conn.close()
    reader.close()


def test_read_only_connection_rejects_writes(settings):
    reader = connect(settings, read_only=True)
    with pytest.raises(duckdb.Error):
        reader.execute("INSERT INTO lake.raw.items (item_id) VALUES ('z')")
    reader.close()


def test_small_commits_stay_inlined(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    for index in range(5):
        writer.add("raw.fetches", [{"fetch_id": str(index)}])
        writer.flush()
    files = list(settings.paths.data.rglob("*.parquet"))
    assert files == []
    conn.close()


def test_vectors_round_trip_and_support_cosine(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "nlp.embeddings", [{"item_id": "a", "vec": [1.0, 0.0], "dim": 2}]
    )
    writer.flush()
    similarity = conn.execute(
        "SELECT array_cosine_similarity(vec::FLOAT[2], [1.0, 0.0]::FLOAT[2]) "
        "FROM lake.nlp.embeddings"
    ).fetchone()[0]
    assert similarity == pytest.approx(1.0)
    conn.close()


def test_failed_flush_returns_rows_to_buffer(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add("raw.fetches", [{"no_such_column": 1}])
    with pytest.raises(duckdb.Error):
        writer.flush()
    assert writer.pending() == 1
    conn.close()


def test_latest_sql_returns_newest_row_per_key(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    early = datetime(2026, 11, 3, 12, tzinfo=UTC)
    late = datetime(2026, 11, 3, 13, tzinfo=UTC)
    writer.add(
        "nlp.storyline_assign",
        [
            {"item_id": "a", "storyline_id": "old", "created_at": early},
            {"item_id": "a", "storyline_id": "new", "created_at": late},
        ],
    )
    writer.flush()
    rows = conn.execute(
        latest_sql("nlp.storyline_assign", "item_id")
    ).fetchall()
    assert len(rows) == 1
    columns = [d[0] for d in conn.description]
    assert rows[0][columns.index("storyline_id")] == "new"
    conn.close()


def test_rows_to_arrow_covers_keys_missing_from_first_row():
    table = rows_to_arrow([{"a": 1}, {"a": 2, "b": "x"}])
    assert sorted(table.column_names) == ["a", "b"]
    assert table.column("b").to_pylist() == [None, "x"]


def test_snapshot_id_advances_on_commit(settings):
    conn = connect(settings)
    before = current_snapshot(conn)
    writer = BatchWriter(conn, settings)
    writer.add("raw.fetches", [{"fetch_id": "1"}])
    writer.flush()
    assert current_snapshot(conn) > before
    conn.close()


def test_writer_lock_is_exclusive(settings):
    assert writer_is_running(settings) is False
    with writer_lock(settings), pytest.raises(WriterBusy):
        with writer_lock(settings):
            pass
    assert writer_is_running(settings) is False


def test_hash_id_is_deterministic():
    assert hash_id("a", 1) == hash_id("a", 1)
    assert hash_id("a", 1) != hash_id("a", 2)
