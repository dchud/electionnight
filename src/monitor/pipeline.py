"""The writer process: poller, parsers, rollups, and maintenance."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import duckdb

from monitor.control import process_requests
from monitor.disk import DiskGuard, DiskLevel
from monitor.ingest.poller import Poller, load_seen, load_validators
from monitor.ingest.sources import load_sources
from monitor.lake import BatchWriter, connect, init_lake, writer_lock
from monitor.logs import configure, get
from monitor.maintenance import (
    backup_catalog,
    prune,
    run_maintenance,
)
from monitor.settings import Settings

log = get(__name__)


class Pipeline:
    """Holds the single lake connection and runs every periodic job."""

    def __init__(
        self,
        settings: Settings,
        no_llm: bool = False,
        stages: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.no_llm = no_llm
        self.stages = stages
        self.conn: duckdb.DuckDBPyConnection = connect(settings)
        self.db_lock = asyncio.Lock()
        self.writer = BatchWriter(self.conn, settings)
        self.guard = DiskGuard(settings)
        self.handlers: dict[str, Callable[..., str]] = {
            "prune": self._handle_prune,
        }
        self.poller: Poller | None = None
        self.started_at = datetime.now(UTC)

    def _handle_prune(self, **options: Any) -> str:
        return str(prune(self.settings, self.conn, **options))

    async def locked(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run blocking lake work off the event loop, one job at a time."""
        async with self.db_lock:
            return await asyncio.to_thread(func, *args)

    async def every(
        self,
        seconds: float,
        name: str,
        job: Callable[[], Awaitable[None]],
    ) -> None:
        while True:
            await asyncio.sleep(seconds)
            started = time.monotonic()
            try:
                await job()
            except Exception:
                log.exception("job_failed", job=name)
            else:
                elapsed = time.monotonic() - started
                if elapsed > 5:
                    log.info("job_slow", job=name, seconds=round(elapsed, 1))

    async def commit_job(self) -> None:
        counts = await self.locked(self.writer.flush)
        if counts:
            log.info("committed", **counts)

    async def maintenance_job(self) -> None:
        await self.locked(run_maintenance, self.conn)
        log.info("maintenance_done")

    async def backup_job(self) -> None:
        path = await self.locked(backup_catalog, self.settings)
        log.info("backup_done", path=str(path))

    async def disk_job(self) -> None:
        level = self.guard.level()
        if level >= DiskLevel.AUTOPRUNE:
            log.error(
                "disk_autoprune",
                free_gb=round(self.guard.free(), 2),
            )
            await self.locked(
                self._handle_autoprune,
            )
        elif level > DiskLevel.OK:
            log.warning(
                "disk_low", level=level.name, free_gb=self.guard.free()
            )

    def _handle_autoprune(self) -> None:
        prune(self.settings, self.conn, thin="15m", older_than="1h")

    async def control_job(self) -> None:
        await self.locked(
            process_requests, self.settings.paths.control, self.handlers
        )

    def periodic_jobs(self) -> list[Awaitable[None]]:
        commit = self.settings.commit
        return [
            self.every(commit.batch_seconds, "commit", self.commit_job),
            self.every(
                commit.maintenance_minutes * 60,
                "maintenance",
                self.maintenance_job,
            ),
            self.every(commit.backup_minutes * 60, "backup", self.backup_job),
            self.every(60, "disk", self.disk_job),
            self.every(2, "control", self.control_job),
        ]

    def build_poller(self) -> Poller:
        return Poller(
            self.settings,
            self.writer,
            load_sources(self.settings),
            load_seen(self.conn),
            load_validators(self.conn),
        )

    async def run(self) -> None:
        self.poller = self.build_poller()
        log.info(
            "pipeline_started",
            feeds=len(self.poller.feeds),
            seen=len(self.poller.seen),
        )
        tasks = [asyncio.ensure_future(j) for j in self.periodic_jobs()]
        tasks.append(asyncio.ensure_future(self.poller.run()))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()
        log.info("pipeline_stopping")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.locked(self.writer.flush)
        self.conn.close()
        log.info("pipeline_stopped")


def run_pipeline(
    settings: Settings,
    no_llm: bool = False,
    stages: list[str] | None = None,
) -> None:
    configure("pipeline", settings.paths.logs)
    init_lake(settings)
    with writer_lock(settings):
        asyncio.run(Pipeline(settings, no_llm, stages).run())
