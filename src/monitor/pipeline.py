"""The writer process: poller, parsers, rollups, and maintenance."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from monitor.control import drain_spool, process_requests
from monitor.disk import DiskGuard, DiskLevel
from monitor.ingest.poller import Poller, load_seen, load_validators
from monitor.ingest.sources import load_sources
from monitor.lake import BatchWriter, connect, init_lake, writer_lock
from monitor.llm.budget import load_budget
from monitor.llm.client import LlmClient
from monitor.logs import configure, get
from monitor.maintenance import backup_catalog, prune, run_maintenance
from monitor.orchestrator import Database, Stage
from monitor.parsers.base import Item, item_from_poller
from monitor.parsers.classify_a import ClassifyParser
from monitor.parsers.dedupe import DedupeParser
from monitor.parsers.embed import EmbedParser
from monitor.parsers.lang import LangParser
from monitor.parsers.storylines import StorylineParser
from monitor.rollups import RollupEngine
from monitor.settings import Settings

log = get(__name__)

ET = ZoneInfo("America/New_York")
ROLLUP_SECONDS = 45
STATUS_SECONDS = 10
STORYLINE_PASS_SECONDS = 600
REF_SYNC_SECONDS = 60
NIGHTLY_REBUILD_HOUR_ET = 4


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
        self.stage_filter = set(stages) if stages else None
        self.conn: duckdb.DuckDBPyConnection = connect(settings)
        self.db = Database(self.conn)
        self.writer = BatchWriter(self.conn, settings)
        self.guard = DiskGuard(settings)
        self.handlers: dict[str, Callable[..., str]] = {
            "prune": self._handle_prune,
            "rollups": self._handle_rollups,
        }
        self.poller: Poller | None = None
        self.llm: LlmClient | None = None
        self.stages: list[Stage] = []
        self.first_stage: Stage | None = None
        self.storylines: StorylineParser | None = None
        self.rollups = RollupEngine(settings, self.writer)
        self.ref_state: dict[str, Any] = {}
        self.started_at = datetime.now(UTC)
        self.last_maintenance: datetime | None = None
        self.last_backup: datetime | None = None
        self.last_rollup: datetime | None = None
        self.last_rebuild_day: str | None = None

    def _handle_prune(self, **options: Any) -> str:
        return str(prune(self.settings, self.conn, **options))

    def _handle_rollups(self, rebuild: bool = False) -> str:
        counts = self.rollups.run(self.conn, rebuild=rebuild)
        return f"appended {sum(counts.values())} rows: {counts}"

    async def every(
        self,
        seconds: float,
        name: str,
        job: Callable[[], Awaitable[None]],
        first_after: float | None = None,
    ) -> None:
        """Run a job on an interval, optionally sooner the first time.

        Long-interval jobs set `first_after` so that restarts during the day
        cannot keep postponing them.
        """
        delay = seconds if first_after is None else first_after
        while True:
            await asyncio.sleep(delay)
            delay = seconds
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
        counts = await self.db.run(self.writer.flush)
        if counts:
            log.info("committed", **counts)

    async def maintenance_job(self) -> None:
        await self.db.run(run_maintenance, self.conn)
        self.last_maintenance = datetime.now(UTC)
        log.info("maintenance_done")

    async def backup_job(self) -> None:
        path = await self.db.run(backup_catalog, self.settings)
        self.last_backup = datetime.now(UTC)
        log.info("backup_done", path=str(path))

    async def disk_job(self) -> None:
        level = self.guard.level()
        if level >= DiskLevel.AUTOPRUNE:
            log.error("disk_autoprune", free_gb=round(self.guard.free(), 2))
            await self.db.run(
                lambda: prune(
                    self.settings, self.conn, thin="15m", older_than="1h"
                )
            )
        elif level > DiskLevel.OK:
            log.warning(
                "disk_low",
                level=level.name,
                free_gb=round(self.guard.free(), 2),
            )

    async def control_job(self) -> None:
        await self.db.run(
            process_requests, self.settings.paths.control, self.handlers
        )
        drain_spool(self.settings.paths.control, self.writer.add)

    async def rollup_job(self) -> None:
        counts = await self.db.run(self.rollups.run, self.conn)
        self.last_rollup = datetime.now(UTC)
        if counts:
            log.info("rollups", rows=sum(counts.values()))
        await self._maybe_nightly_rebuild()

    async def _maybe_nightly_rebuild(self) -> None:
        now = datetime.now(ET)
        day = now.strftime("%Y-%m-%d")
        if (
            now.hour == NIGHTLY_REBUILD_HOUR_ET
            and day != self.last_rebuild_day
        ):
            self.last_rebuild_day = day
            counts = await self.db.run(
                lambda: self.rollups.run(self.conn, rebuild=True)
            )
            log.info("rollups_rebuilt", rows=sum(counts.values()))

    async def storyline_pass_job(self) -> None:
        if self.storylines is not None:
            await asyncio.to_thread(self.storylines.periodic)

    async def ref_sync_job(self) -> None:
        from monitor.ref.loader import sync_ref_tables

        counts = await self.db.run(
            sync_ref_tables,
            self.conn,
            self.writer,
            self.settings,
            self.ref_state,
        )
        if counts:
            log.info("ref_synced", **counts)

    async def status_job(self) -> None:
        path = self.settings.paths.status
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.status(), default=str))
        os.replace(temporary, path)

    def status(self) -> dict[str, Any]:
        """Live state for the dashboard, which cannot read this process."""
        return {
            "updated_at": datetime.now(UTC),
            "started_at": self.started_at,
            "last_commit_at": self.writer.last_commit_at,
            "last_maintenance_at": self.last_maintenance,
            "last_backup_at": self.last_backup,
            "last_rollup_at": self.last_rollup,
            "feeds": self.poller.health() if self.poller else {},
            "feed_details": self.poller.details() if self.poller else [],
            "stages": [stage.status() for stage in self.stages],
            "llm": self.llm.status() if self.llm else {"disabled": True},
            "disk": {
                "level": self.guard.level().name,
                "free_gb": round(self.guard.free(), 2),
            },
        }

    def periodic_jobs(self) -> list[Awaitable[None]]:
        commit = self.settings.commit
        return [
            self.every(commit.batch_seconds, "commit", self.commit_job),
            self.every(
                commit.maintenance_minutes * 60,
                "maintenance",
                self.maintenance_job,
                first_after=120,
            ),
            self.every(
                commit.backup_minutes * 60,
                "backup",
                self.backup_job,
                first_after=180,
            ),
            self.every(60, "disk", self.disk_job),
            self.every(2, "control", self.control_job),
            self.every(ROLLUP_SECONDS, "rollups", self.rollup_job),
            self.every(
                STORYLINE_PASS_SECONDS, "storylines", self.storyline_pass_job
            ),
            self.every(REF_SYNC_SECONDS, "ref_sync", self.ref_sync_job),
            self.every(STATUS_SECONDS, "status", self.status_job),
        ]

    def build_stages(self) -> list[Stage]:
        """Connect the parsers in dependency order."""
        settings = self.settings
        sources = {s.source_id: s for s in load_sources(settings)}
        threshold = settings.nlp.relevance_threshold

        def relevant_canonical(item: Item) -> bool:
            return (
                item.relevant
                and item.relevance_conf >= threshold
                and item.is_canonical
            )

        self.storylines = StorylineParser(
            settings, self.db, self.writer, self.llm
        )
        chain = [
            Stage(
                LangParser(),
                self.db,
                self.writer,
                linger_seconds=0.2,
                backfill_batch=2000,
            ),
            Stage(
                ClassifyParser(settings, self.llm, sources),
                self.db,
                self.writer,
                linger_seconds=1.0,
                backfill_batch=240,
            ),
            Stage(
                EmbedParser(settings),
                self.db,
                self.writer,
                linger_seconds=0.5,
                backfill_batch=640,
            ),
            Stage(
                DedupeParser(settings, self.db),
                self.db,
                self.writer,
                linger_seconds=0.2,
                backfill_batch=640,
            ),
            Stage(
                self.storylines,
                self.db,
                self.writer,
                accept=relevant_canonical,
                linger_seconds=0.2,
                backfill_batch=320,
            ),
        ]
        if self.stage_filter is not None:
            chain = [
                stage
                for stage in chain
                if stage.parser.name in self.stage_filter
            ]
        for stage, following in zip(chain, chain[1:], strict=False):
            stage.then(following)
        return chain

    def on_new_items(self, rows: list[dict[str, Any]]) -> None:
        if self.first_stage is None:
            return
        for row in rows:
            self.first_stage.enqueue(item_from_poller(row))

    def build_poller(self) -> Poller:
        return Poller(
            self.settings,
            self.writer,
            load_sources(self.settings),
            load_seen(self.conn),
            load_validators(self.conn),
            on_new_items=self.on_new_items,
        )

    async def run(self) -> None:
        if not self.no_llm and self.settings.anthropic_api_key is not None:
            self.llm = LlmClient(
                self.settings,
                self.writer,
                load_budget(self.conn, self.settings),
            )
        self.stages = self.build_stages()
        self.first_stage = self.stages[0] if self.stages else None
        self.poller = self.build_poller()
        log.info(
            "pipeline_started",
            feeds=len(self.poller.feeds),
            seen=len(self.poller.seen),
            stages=[stage.parser.name for stage in self.stages],
            llm=self.llm is not None,
        )
        tasks = [asyncio.ensure_future(j) for j in self.periodic_jobs()]
        tasks.append(asyncio.ensure_future(self.poller.run()))
        for stage in self.stages:
            tasks.extend(stage.tasks())
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()
        log.info("pipeline_stopping")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.storylines is not None:
            self.storylines.flush_snapshots()
        await self.db.run(self.writer.flush)
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
