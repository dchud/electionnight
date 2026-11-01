"""Disk use reporting and the free-space guard."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import duckdb

from monitor.settings import Settings

GB = 1024**3


class DiskLevel(IntEnum):
    OK = 0
    WARN = 1
    DEGRADE = 2
    AUTOPRUNE = 3


FreeSpace = Callable[[Path], float]


def free_gb(path: Path) -> float:
    target = path if path.exists() else Path(".")
    return shutil.disk_usage(target).free / GB


@dataclass
class DiskGuard:
    """Maps free space to a level. The free-space function is injectable."""

    settings: Settings
    free_space: FreeSpace = free_gb

    def free(self) -> float:
        return self.free_space(self.settings.paths.data)

    def level(self) -> DiskLevel:
        free = self.free()
        limits = self.settings.disk
        if free < limits.autoprune_gb:
            return DiskLevel.AUTOPRUNE
        if free < limits.degrade_gb:
            return DiskLevel.DEGRADE
        if free < limits.warn_gb:
            return DiskLevel.WARN
        return DiskLevel.OK

    def full_text_allowed(self) -> bool:
        return self.level() < DiskLevel.DEGRADE


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def lake_file_sizes(conn: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Return (live bytes, live file count) from the catalog."""
    rows = conn.execute(
        """
        SELECT coalesce(sum(file_size_bytes), 0), coalesce(sum(file_count), 0)
        FROM ducklake_table_info('lake')
        """
    ).fetchall()
    return int(rows[0][0]), int(rows[0][1])


def disk_figures(
    settings: Settings, conn: duckdb.DuckDBPyConnection | None = None
) -> dict[str, float]:
    """Bytes per category, plus free space, in GB."""
    paths = settings.paths
    total_lake = dir_size(paths.data)
    live = total_lake
    if conn is not None:
        live = min(lake_file_sizes(conn)[0], total_lake)
    catalog = sum(
        dir_size(paths.catalog.with_name(paths.catalog.name + suffix))
        for suffix in ("", "-wal", "-shm")
    )
    return {
        "lake_live": live / GB,
        "retained_by_snapshots": max(total_lake - live, 0) / GB,
        "catalog": catalog / GB,
        "backups": dir_size(paths.backups) / GB,
        "models": dir_size(paths.models) / GB,
        "reference": dir_size(paths.ref) / GB,
        "logs": dir_size(paths.logs) / GB,
        "free": free_gb(paths.data),
    }


def report_disk(settings: Settings) -> str:
    """Text report for `monitor disk`."""
    conn = None
    if settings.paths.catalog.exists():
        from monitor.lake import connect

        conn = connect(settings, read_only=True)
    try:
        figures = disk_figures(settings, conn)
        lines = [
            f"{'lake live data':<24}{figures['lake_live']:>9.3f} GB",
            f"{'retained by snapshots':<24}"
            f"{figures['retained_by_snapshots']:>9.3f} GB",
            f"{'catalog':<24}{figures['catalog']:>9.3f} GB",
            f"{'backups':<24}{figures['backups']:>9.3f} GB",
            f"{'models':<24}{figures['models']:>9.3f} GB",
            f"{'reference data':<24}{figures['reference']:>9.3f} GB",
            f"{'logs':<24}{figures['logs']:>9.3f} GB",
            f"{'free space':<24}{figures['free']:>9.1f} GB",
        ]
        if conn is not None:
            snapshots = conn.execute(
                "SELECT count(*) FROM lake.snapshots()"
            ).fetchall()[0][0]
            on_disk = sum(1 for _ in settings.paths.data.rglob("*.parquet"))
            live = lake_file_sizes(conn)[1]
            lines.append(f"{'snapshots':<24}{snapshots:>9}")
            lines.append(f"{'parquet files, live':<24}{live:>9}")
            lines.append(f"{'parquet files, on disk':<24}{on_disk:>9}")
        return "\n".join(lines)
    finally:
        if conn is not None:
            conn.close()
