import inspect
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from monitor.control import process_requests
from monitor.disk import DiskGuard, DiskLevel, disk_figures
from monitor.lake import BatchWriter, connect
from monitor.maintenance import (
    CLEANUP_AGE,
    backup_catalog,
    backups_to_keep,
    parse_span,
    prune,
    restore_backup,
    run_maintenance,
    thin_versions,
)
from monitor.settings import Paths, Settings

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


def write_rows(conn, settings, batches=6):
    writer = BatchWriter(conn, settings)
    for index in range(batches):
        writer.add(
            "raw.fetches",
            [{"fetch_id": f"{index}-{n}"} for n in range(3)],
        )
        writer.flush()


def count(conn):
    """Rows found by reading them. `count(*)` needs no data file to answer."""
    return len(
        conn.execute("SELECT fetch_id FROM lake.raw.fetches").fetchall()
    )


def test_maintenance_moves_inlined_rows_to_parquet(settings):
    conn = connect(settings)
    write_rows(conn, settings)
    run_maintenance(conn)
    assert list(settings.paths.data.rglob("*.parquet"))
    assert count(conn) == 18
    conn.close()


def test_cleanup_clock_starts_when_a_file_is_replaced(settings):
    """A file's own age does not matter, only how long ago it was replaced."""
    conn = connect(settings)
    for index in range(3):
        conn.execute(
            "INSERT INTO lake.raw.fetches BY NAME "
            f"SELECT 'f{index}-' || i AS fetch_id, now() AS created_at "
            "FROM range(2500) t(i)"
        )
    snapshots = [
        row[0]
        for row in conn.execute(
            "SELECT snapshot_id FROM lake.snapshots() "
            "ORDER BY snapshot_id DESC LIMIT 3"
        ).fetchall()
    ]
    conn.close()
    days_ago = datetime.now(UTC) - timedelta(days=5)
    with sqlite3.connect(settings.paths.catalog) as catalog:
        catalog.executemany(
            "UPDATE ducklake_snapshot SET snapshot_time = ? "
            "WHERE snapshot_id = ?",
            [(f"{days_ago:%Y-%m-%d %H:%M:%S}+00", s) for s in snapshots],
        )
    folder = settings.paths.data / "raw" / "fetches"
    for path in folder.glob("*.parquet"):
        os.utime(path, (days_ago.timestamp(), days_ago.timestamp()))
    conn = connect(settings)
    now = datetime.now(UTC)
    # Five days old, and just replaced by this merge.
    assert run_maintenance(conn, now=now)["files_removed"] == 0
    assert len(list(folder.glob("*.parquet"))) == 4
    almost = now + CLEANUP_AGE - timedelta(minutes=1)
    assert run_maintenance(conn, now=almost)["files_removed"] == 0
    past = now + CLEANUP_AGE + timedelta(minutes=1)
    assert run_maintenance(conn, now=past)["files_removed"] == 3
    assert len(list(folder.glob("*.parquet"))) == 1
    conn.close()


def test_maintenance_commits_with_no_batch_writer(settings):
    """It runs on the connection it is given, and takes nothing else."""
    assert list(inspect.signature(run_maintenance).parameters) == [
        "conn",
        "now",
    ]
    conn = connect(settings)
    write_rows(conn, settings)
    before = conn.execute(
        "SELECT max(snapshot_id) FROM lake.snapshots()"
    ).fetchone()[0]
    run_maintenance(conn)
    after = conn.execute(
        "SELECT max(snapshot_id) FROM lake.snapshots()"
    ).fetchone()[0]
    conn.close()
    assert after > before


@pytest.mark.parametrize(
    "options",
    [
        {"all_but_latest": True},
        {"thin": "15m", "older_than": "0m"},
        {"before": "2099-01-01T00:00:00+00:00"},
    ],
)
def test_prune_keeps_every_row_and_dry_run_changes_nothing(settings, options):
    conn = connect(settings)
    write_rows(conn, settings)
    run_maintenance(conn)
    snapshots = "SELECT count(*) FROM lake.snapshots()"
    before = conn.execute(snapshots).fetchall()[0][0]

    dry = prune(settings, conn, dry_run=True, **options)
    assert conn.execute(snapshots).fetchall()[0][0] == before
    assert count(conn) == 18

    real = prune(settings, conn, **options)
    assert real.expired == dry.expired
    assert real.expired > 0
    assert conn.execute(snapshots).fetchall()[0][0] == before - real.expired
    assert count(conn) == 18
    conn.close()


def test_prune_backups_keeps_newest(settings):
    conn = connect(settings)
    for minute in range(4):
        backup_catalog(settings, NOW + timedelta(minutes=minute))
    assert "2 backups removed" in str(
        prune(settings, conn, backups=2, dry_run=True)
    )
    prune(settings, conn, backups=2)
    remaining = sorted(p.name for p in settings.paths.backups.iterdir())
    assert remaining == [
        "catalog-20261103-1802.sqlite.zst",
        "catalog-20261103-1803.sqlite.zst",
    ]
    conn.close()


def test_backup_restores_to_a_queryable_catalog(settings, tmp_path):
    conn = connect(settings)
    write_rows(conn, settings)
    run_maintenance(conn)
    conn.close()
    assert list(settings.paths.data.rglob("*.parquet"))
    backup = backup_catalog(settings, NOW)

    restored_paths = Paths.under(tmp_path / "restored")
    restored_paths.catalog.parent.mkdir(parents=True)
    restored_paths.data = settings.paths.data
    restore_backup(backup, restored_paths.catalog)
    restored = connect(
        Settings(paths=restored_paths),
        read_only=True,
    )
    assert count(restored) == 18
    restored.close()


def test_backup_retention_keeps_recent_and_one_per_hour(tmp_path):
    paths = [
        tmp_path / f"catalog-20261103-{hour:02d}{minute:02d}.sqlite.zst"
        for hour in range(12, 18)
        for minute in (0, 15, 30, 45)
    ]
    keep = backups_to_keep(paths, 12)
    names = {path.name for path in keep}
    assert "catalog-20261103-1745.sqlite.zst" in names
    assert "catalog-20261103-1200.sqlite.zst" in names
    assert "catalog-20261103-1215.sqlite.zst" not in names
    assert len(keep) == 12 + 3


def test_thin_versions_keeps_one_per_span_and_never_the_newest():
    snapshots = [
        (index, NOW - timedelta(hours=4) + timedelta(minutes=index))
        for index in range(1, 61)
    ]
    snapshots.append((100, NOW))
    expired = thin_versions(
        snapshots, timedelta(minutes=15), timedelta(hours=2), NOW
    )
    assert 100 not in expired
    kept = [s[0] for s in snapshots if s[0] not in expired]
    assert 4 <= len(kept) <= 7
    recent = thin_versions(
        [(1, NOW - timedelta(minutes=1)), (2, NOW)],
        timedelta(minutes=15),
        timedelta(hours=2),
        NOW,
    )
    assert recent == []


def test_parse_span():
    assert parse_span("15m") == timedelta(minutes=15)
    assert parse_span("2h") == timedelta(hours=2)
    with pytest.raises(ValueError):
        parse_span("soon")


@pytest.mark.parametrize(
    ("free", "level", "full_text"),
    [
        (50.0, DiskLevel.OK, True),
        (9.0, DiskLevel.WARN, True),
        (4.0, DiskLevel.DEGRADE, False),
        (2.0, DiskLevel.AUTOPRUNE, False),
    ],
)
def test_disk_guard_levels(settings, free, level, full_text):
    guard = DiskGuard(settings, free_space=lambda path: free)
    assert guard.level() == level
    assert guard.full_text_allowed() is full_text


def test_disk_figures_report_every_category(settings):
    conn = connect(settings, read_only=True)
    figures = disk_figures(settings, conn)
    assert set(figures) >= {
        "lake_live",
        "awaiting_cleanup",
        "catalog",
        "backups",
        "models",
        "logs",
        "free",
    }
    conn.close()


def test_cleanup_removes_replaced_files_and_keeps_time_travel(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    versions = []
    for batch in range(4):
        writer.add(
            "nlp.embeddings",
            [{"item_id": f"{batch}-{n}", "vec": [0.1]} for n in range(5)],
        )
        writer.flush()
        snapshot = conn.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchall()[0][0]
        versions.append((snapshot, (batch + 1) * 5))
    before = conn.execute("SELECT count(*) FROM lake.snapshots()").fetchall()
    run_maintenance(conn, now=datetime.now(UTC))
    on_disk = len(list(settings.paths.data.rglob("*.parquet")))
    assert on_disk >= 5
    later = datetime.now(UTC) + timedelta(hours=3)
    result = run_maintenance(conn, now=later)
    assert result["files_removed"] >= 4
    assert len(list(settings.paths.data.rglob("*.parquet"))) < on_disk
    assert (
        conn.execute("SELECT count(*) FROM lake.snapshots()").fetchall()[0][0]
        >= before[0][0]
    )
    for version, expected in versions:
        rows = conn.execute(
            "SELECT item_id FROM lake.nlp.embeddings "
            f"AT (VERSION => {version})"
        ).fetchall()
        assert len(rows) == expected
    conn.close()


def test_control_requests_reach_a_handler(tmp_path):
    (tmp_path / "abc.request.json").write_text(
        '{"command": "echo", "options": {"value": "hi"}}'
    )
    handled = process_requests(tmp_path, {"echo": lambda value: value})
    assert handled == 1
    assert '"hi"' in (tmp_path / "abc.result.json").read_text()
