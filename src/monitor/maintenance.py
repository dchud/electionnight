"""Lake maintenance, catalog backups, and snapshot pruning.

Nothing here deletes current data rows. Pruning removes old snapshots and the
files only they reference, so replay, which filters by time, keeps working.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import zstandard

from monitor.lake import as_utc, connect, writer_lock
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

CLEANUP_AGE = timedelta(hours=2)
BACKUP_NAME = re.compile(r"catalog-(\d{8}-\d{4})\.sqlite\.zst$")
CATALOG_COMPANIONS = ("-wal", "-shm")
LIVE_FILES_SQL = """
SELECT s.path, t.path, f.path
FROM ducklake_data_file f
JOIN ducklake_table t ON t.table_id = f.table_id AND t.end_snapshot IS NULL
JOIN ducklake_schema s ON s.schema_id = t.schema_id AND s.end_snapshot IS NULL
WHERE f.end_snapshot IS NULL
"""


class RestoreError(RuntimeError):
    """No backup can replace the catalog."""


def run_maintenance(
    conn: duckdb.DuckDBPyConnection, now: datetime | None = None
) -> dict[str, int]:
    """Flush inlined rows, merge small files, delete replaced files.

    A merged file carries the snapshot range of each row, so time travel works
    without the files it replaced. Those are deleted once they are 2 hours
    old, which leaves any query still reading them time to finish.
    """
    cutoff = (now or datetime.now(UTC)) - CLEANUP_AGE
    conn.execute("CALL ducklake_flush_inlined_data('lake')").fetchall()
    merged = conn.execute(
        "CALL ducklake_merge_adjacent_files('lake')"
    ).fetchall()
    removed = conn.execute(
        "CALL ducklake_cleanup_old_files('lake', "
        f"older_than => TIMESTAMPTZ '{cutoff.isoformat()}')"
    ).fetchall()
    return {"tables_merged": len(merged), "files_removed": len(removed)}


def backup_catalog(settings: Settings, now: datetime | None = None) -> Path:
    """Copy the catalog with SQLite's online backup, compress, and rotate."""
    now = now or datetime.now(UTC)
    backups = settings.paths.backups
    backups.mkdir(parents=True, exist_ok=True)
    target = backups / f"catalog-{now:%Y%m%d-%H%M}.sqlite.zst"
    temporary = target.with_suffix(".tmp")
    source = sqlite3.connect(settings.paths.catalog)
    copy = sqlite3.connect(temporary)
    try:
        with copy:
            source.backup(copy)
    finally:
        copy.close()
        source.close()
    compressed = target.with_suffix(".tmpz")
    with temporary.open("rb") as raw, compressed.open("wb") as out:
        zstandard.ZstdCompressor(level=6).copy_stream(raw, out)
    temporary.unlink()
    compressed.replace(target)
    rotate_backups(settings)
    return target


def backup_time(path: Path) -> datetime | None:
    match = BACKUP_NAME.search(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M").replace(tzinfo=UTC)


def backups_to_keep(paths: list[Path], keep_recent: int) -> set[Path]:
    """The newest N, plus the oldest backup in each earlier hour."""
    pairs = [(backup_time(path), path) for path in paths]
    dated = sorted(
        ((when, path) for when, path in pairs if when),
        key=lambda pair: pair[0],
        reverse=True,
    )
    keep = {path for _, path in dated[:keep_recent]}
    hourly: dict[datetime, Path] = {}
    for when, path in dated:
        hourly[when.replace(minute=0)] = path
    keep.update(hourly.values())
    return keep


def rotate_backups(settings: Settings) -> list[Path]:
    paths = list(settings.paths.backups.glob("catalog-*.sqlite.zst"))
    keep = backups_to_keep(paths, settings.commit.backups_keep_recent)
    removed = [path for path in paths if path not in keep]
    for path in removed:
        path.unlink()
    return removed


def _decompress(path: Path, target: Path) -> None:
    with path.open("rb") as compressed, target.open("wb") as out:
        zstandard.ZstdDecompressor().copy_stream(compressed, out)


def restore_backup(path: Path, target: Path) -> None:
    """Decompress a backup to a catalog file, whole or not at all.

    A leftover WAL goes too, because SQLite would replay it onto the older
    file and corrupt it.
    """
    temporary = target.with_name(target.name + ".restoring")
    _decompress(path, temporary)
    for suffix in CATALOG_COMPANIONS:
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    temporary.replace(target)


def backup_problem(settings: Settings, path: Path) -> str | None:
    """Why this backup cannot replace the catalog, or None if it can.

    Maintenance deletes replaced Parquet files after a delay, and a prune
    deletes them at once, so an older backup can name files that are gone.
    """
    checked = path.with_name(path.name + ".check")
    try:
        _decompress(path, checked)
        catalog = sqlite3.connect(checked)
        try:
            result = catalog.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                return f"catalog check failed: {result}"
            rows = catalog.execute(LIVE_FILES_SQL).fetchall()
        finally:
            catalog.close()
    except (zstandard.ZstdError, sqlite3.DatabaseError, OSError) as error:
        return f"cannot be read: {error}"
    finally:
        for suffix in ("", *CATALOG_COMPANIONS):
            checked.with_name(checked.name + suffix).unlink(missing_ok=True)
    root = settings.paths.data.resolve()
    missing = [
        row
        for row in rows
        if not root.joinpath(*(part or "" for part in row)).exists()
    ]
    if missing:
        return f"{len(missing)} of {len(rows)} Parquet files are gone"
    return None


def _set_aside(catalog: Path) -> Path:
    """Keep the replaced catalog with its WAL, still paired by name."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    aside = catalog.with_name(f"{catalog.name}.damaged-{stamp}")
    for suffix in ("", *CATALOG_COMPANIONS):
        source = catalog.with_name(catalog.name + suffix)
        if source.exists():
            source.rename(aside.with_name(aside.name + suffix))
    return aside


def restore_catalog(settings: Settings, dry_run: bool = False) -> str:
    """Replace the catalog with the newest backup that can serve as one.

    Raises `WriterBusy` while a pipeline runs. Raises `RestoreError`, with
    nothing changed, when no backup is usable. The report names each newer
    backup that was skipped and why.
    """
    with writer_lock(settings):
        found = settings.paths.backups.glob("catalog-*.sqlite.zst")
        pairs = [(backup_time(path), path) for path in found]
        newest_first = sorted(
            ((when, path) for when, path in pairs if when),
            key=lambda pair: pair[0],
            reverse=True,
        )
        report: list[str] = []
        for _, path in newest_first:
            problem = backup_problem(settings, path)
            if problem is not None:
                report.append(f"skipped {path.name}: {problem}")
                continue
            if dry_run:
                report.append(f"would restore {path.name}")
            else:
                catalog = settings.paths.catalog
                aside = _set_aside(catalog)
                restore_backup(path, catalog)
                report.append(f"restored {path.name}")
                report.append(f"the replaced catalog is now {aside}")
            return "\n".join(report)
    raise RestoreError("\n".join(["no usable backup", *report]))


def parse_span(text: str) -> timedelta:
    """Parse `15m`, `2h`, or `1d`."""
    match = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not match:
        raise ValueError(f"expected a span such as 15m, 2h, or 1d: {text}")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(
        minutes=amount if unit == "m" else 0,
        hours=amount if unit == "h" else 0,
        days=amount if unit == "d" else 0,
    )


def thin_versions(
    snapshots: list[tuple[int, datetime]],
    span: timedelta,
    older_than: timedelta,
    now: datetime,
) -> list[int]:
    """Snapshot ids to expire: all but one per span, older than the cutoff.

    The newest snapshot is never returned.
    """
    if not snapshots:
        return []
    ordered = sorted(snapshots)
    newest = ordered[-1][0]
    cutoff = now - older_than
    seconds = span.total_seconds()
    kept_buckets: set[int] = set()
    expire: list[int] = []
    for snapshot_id, when in ordered:
        if snapshot_id == newest or when >= cutoff:
            continue
        bucket = int(when.timestamp() // seconds)
        if bucket in kept_buckets:
            expire.append(snapshot_id)
        else:
            kept_buckets.add(bucket)
    return expire


@dataclass
class PruneResult:
    expired: int
    files_freed: int
    bytes_freed: int
    backups_removed: int
    dry_run: bool

    def __str__(self) -> str:
        verb = "would expire" if self.dry_run else "expired"
        return (
            f"{verb} {self.expired} snapshots; "
            f"{self.files_freed} files, "
            f"{self.bytes_freed / 1024**2:.1f} MB; "
            f"{self.backups_removed} backups removed"
        )


def _snapshots(
    conn: duckdb.DuckDBPyConnection,
) -> list[tuple[int, datetime]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT snapshot_id, snapshot_time FROM lake.snapshots()"
        ).fetchall()
    ]


def _versions_to_expire(
    conn: duckdb.DuckDBPyConnection,
    thin: str | None,
    older_than: str | None,
    before: str | None,
    all_but_latest: bool,
    now: datetime,
) -> list[int]:
    snapshots = _snapshots(conn)
    if not snapshots:
        return []
    newest = max(s[0] for s in snapshots)
    if all_but_latest:
        return [s[0] for s in snapshots if s[0] != newest]
    if before:
        limit = as_utc(datetime.fromisoformat(before))
        return [s[0] for s in snapshots if s[1] < limit and s[0] != newest]
    if thin:
        return thin_versions(
            snapshots,
            parse_span(thin),
            parse_span(older_than or "0m"),
            now,
        )
    return []


def _old_file_figures(conn: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    rows = conn.execute(
        "CALL ducklake_cleanup_old_files('lake', dry_run => true, "
        "cleanup_all => true)"
    ).fetchall()
    total = 0
    for row in rows:
        path = Path(str(row[0]))
        if path.exists():
            total += path.stat().st_size
    return len(rows), total


def prune(
    settings: Settings,
    conn: duckdb.DuckDBPyConnection,
    thin: str | None = None,
    older_than: str | None = None,
    before: str | None = None,
    all_but_latest: bool = False,
    backups: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PruneResult:
    """Expire snapshots, delete files only they used, vacuum the catalog."""
    now = now or datetime.now(UTC)
    versions = _versions_to_expire(
        conn, thin, older_than, before, all_but_latest, now
    )
    files, freed = _old_file_figures(conn)
    if versions:
        listing = ", ".join(str(v) for v in versions)
        if dry_run:
            expired = conn.execute(
                "CALL ducklake_expire_snapshots('lake', dry_run => true, "
                f"versions => [{listing}])"
            ).fetchall()
            log.info("prune_dry_run", snapshots=len(expired))
        else:
            conn.execute(
                "CALL ducklake_expire_snapshots('lake', "
                f"versions => [{listing}])"
            ).fetchall()
    backups_removed = 0
    if backups is not None:
        found = sorted(
            settings.paths.backups.glob("catalog-*.sqlite.zst"), reverse=True
        )
        doomed = found[backups:]
        backups_removed = len(doomed)
        if not dry_run:
            for path in doomed:
                path.unlink()
    if not dry_run and versions:
        files, freed = _old_file_figures(conn)
        conn.execute(
            "CALL ducklake_cleanup_old_files('lake', cleanup_all => true)"
        ).fetchall()
        vacuum_catalog(settings)
    return PruneResult(
        expired=len(versions),
        files_freed=files,
        bytes_freed=freed,
        backups_removed=backups_removed,
        dry_run=dry_run,
    )


def vacuum_catalog(settings: Settings) -> None:
    """Reclaim catalog space. Skipped when other connections hold it open."""
    catalog = sqlite3.connect(settings.paths.catalog, timeout=5)
    try:
        catalog.execute("VACUUM")
    except sqlite3.OperationalError as error:
        log.warning("catalog_vacuum_skipped", error=str(error))
    finally:
        catalog.close()


def prune_standalone(settings: Settings, **options: Any) -> str:
    """Run prune with its own connection, when no pipeline holds the lock."""
    conn = connect(settings)
    try:
        return str(prune(settings, conn, **options))
    finally:
        conn.close()
