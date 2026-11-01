"""The writer process: poller, parsers, rollups, and maintenance."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from monitor.archive import ArchiveWriter
from monitor.control import (
    drain_spool,
    process_async_requests,
    process_requests,
)
from monitor.disk import DiskGuard, DiskLevel
from monitor.ingest.network import NetworkGate, https_probe
from monitor.ingest.poller import (
    Poller,
    load_seen,
    load_sightings,
    load_validators,
)
from monitor.ingest.sources import load_sources
from monitor.lake import BatchWriter, connect, init_lake, writer_lock
from monitor.llm.budget import load_budget
from monitor.llm.client import LlmClient
from monitor.logs import configure, get
from monitor.maintenance import backup_catalog, prune, run_maintenance
from monitor.offload import Offloader, is_configured
from monitor.orchestrator import Database, Stage
from monitor.parsers.base import Item, item_from_poller
from monitor.parsers.briefs import BriefWriter
from monitor.parsers.classify_a import ClassifyParser
from monitor.parsers.dedupe import DedupeParser
from monitor.parsers.embed import EmbedParser
from monitor.parsers.entities import EntityPass, EntityResolver
from monitor.parsers.extract_b import ExtractParser
from monitor.parsers.fetch_text import FetchTextParser
from monitor.parsers.geo import Gazetteer
from monitor.parsers.lang import LangParser
from monitor.parsers.storylines import StorylineParser
from monitor.ref.loader import sync_ref_tables
from monitor.rollups import RollupEngine
from monitor.settings import Settings

log = get(__name__)

ET = ZoneInfo("America/New_York")
ROLLUP_SECONDS = 45
STATUS_SECONDS = 10
STORYLINE_PASS_SECONDS = 600
REF_SYNC_SECONDS = 60
NIGHTLY_REBUILD_HOUR_ET = 4
ENTITY_PASS_SECONDS = 300
BRIEF_CHECK_SECONDS = 60
LADDER_SECONDS = 15
HEALTH_SECONDS = 60
# While traffic is light, wait a few seconds so a batch can fill.
CLASSIFY_LINGER_SECONDS = 5.0
# Lag at which the pipeline sheds background work, and at which it resumes.
LAG_HIGH_SECONDS = 120.0
LAG_LOW_SECONDS = 60.0
# Full-text fetching is paced per domain on purpose, so waiting there is not
# the pipeline falling behind.
CORE_STAGES = {"lang", "classify_a", "embed", "dedupe", "storylines"}


def running_commit() -> str:
    """The checked-out commit, marked if the working tree has changes."""
    try:
        commit = subprocess.run(
            ["git", "describe", "--always", "--dirty=*", "--exclude=*"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return commit.stdout.strip()


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
        self.gate = NetworkGate(
            settings,
            probe=https_probe(settings),
            on_restored=self._network_restored,
        )
        self.writer = BatchWriter(self.conn, settings)
        self.guard = DiskGuard(settings)
        self.handlers: dict[str, Callable[..., str]] = {
            "prune": self._handle_prune,
            "rollups": self._handle_rollups,
        }
        self.async_handlers: dict[str, Callable[..., Awaitable[str]]] = {
            "brief": self._handle_brief,
            "relabel": self._handle_relabel,
        }
        self.poller: Poller | None = None
        self.llm: LlmClient | None = None
        self.stages: list[Stage] = []
        self.first_stage: Stage | None = None
        self.storylines: StorylineParser | None = None
        self.embed: EmbedParser | None = None
        self.extract_stage: Stage | None = None
        self.entity_pass: EntityPass | None = None
        self.briefs: BriefWriter | None = None
        self.shedding = False
        self.archive = ArchiveWriter(settings)
        self.offloader = Offloader(settings)
        self.rollups = RollupEngine(settings, self.writer)
        self.ref_state: dict[str, Any] = {}
        self.started_at = datetime.now(UTC)
        self.commit = running_commit()
        self.last_maintenance: datetime | None = None
        self.last_backup: datetime | None = None
        self.last_rollup: datetime | None = None
        self.last_rebuild_day: str | None = None

    def _handle_prune(self, **options: Any) -> str:
        return str(prune(self.settings, self.conn, **options))

    def _handle_rollups(self, rebuild: bool = False) -> str:
        counts = self.rollups.run(self.conn, rebuild=rebuild)
        return f"appended {sum(counts.values())} rows: {counts}"

    async def _handle_brief(self, focus: str = "all") -> str:
        if self.briefs is None:
            return "briefs are off: this pipeline runs without the LLM"
        result = await self.briefs.write(focus)
        if result is None:
            return "too few items in the window; no brief written"
        return (
            f"brief written: {result.sentences_kept} sentences, "
            f"{result.sentences_dropped} removed"
        )

    async def _handle_relabel(self) -> str:
        if self.storylines is None:
            return "storylines are not running"
        count = await self.storylines.relabel_active()
        return f"relabeled {count} storylines"

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
        started = time.monotonic()
        result = await self.db.run(run_maintenance, self.conn)
        self.last_maintenance = datetime.now(UTC)
        log.info("maintenance_done", **result)
        self.writer.add(
            "nlp.maintenance_runs",
            [
                {
                    "recorded_at": self.last_maintenance,
                    "kind": "maintenance",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    **result,
                }
            ],
        )

    async def backup_job(self) -> None:
        started = time.monotonic()
        path = await self.db.run(backup_catalog, self.settings)
        self.last_backup = datetime.now(UTC)
        log.info("backup_done", path=str(path))
        self.writer.add(
            "nlp.maintenance_runs",
            [
                {
                    "recorded_at": self.last_backup,
                    "kind": "backup",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "backup_bytes": path.stat().st_size,
                }
            ],
        )

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
        control = self.settings.paths.control
        await self.db.run(process_requests, control, self.handlers)
        await process_async_requests(control, self.async_handlers)
        drain_spool(control, self.writer.add)

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
        counts = await self.db.run(
            sync_ref_tables,
            self.conn,
            self.writer,
            self.settings,
            self.ref_state,
        )
        if counts:
            log.info("ref_synced", **counts)

    async def entity_job(self) -> None:
        if self.entity_pass is not None:
            counts = await self.entity_pass.run()
            if any(counts.values()):
                log.info("entity_pass", **counts)

    async def brief_job(self) -> None:
        if self.briefs is not None and await self.briefs.maybe_write():
            log.info("brief_scheduled")

    async def ladder_job(self) -> None:
        """Automatic degradation: shed background work when behind."""
        lag = max(
            (
                stage.oldest_live_age()
                for stage in self.stages
                if stage.parser.name in CORE_STAGES
            ),
            default=0.0,
        )
        if not self.shedding and lag > LAG_HIGH_SECONDS:
            self.shedding = True
            log.warning("shedding_load", lag_seconds=round(lag))
        elif self.shedding and lag < LAG_LOW_SECONDS:
            self.shedding = False
            log.info("load_restored", lag_seconds=round(lag))
        for stage in self.stages:
            stage.pause_backfill = self.shedding
        if self.embed is not None:
            self.embed.throttle = 0.5 if self.shedding else 1.0

    async def health_job(self) -> None:
        """Keep a history of what the status file overwrites."""
        status = self.status()
        lags = [
            stage["oldest_live_age"]
            for stage in status["stages"]
            if stage["parser"] in CORE_STAGES
        ]
        llm = status["llm"]
        self.writer.add(
            "nlp.health",
            [
                {
                    "recorded_at": status["updated_at"],
                    "feeds_healthy": status["feeds"].get("healthy"),
                    "feeds_total": status["feeds"].get("total"),
                    "shedding": status["shedding"],
                    "free_gb": status["disk"]["free_gb"],
                    "llm_state": (
                        "degraded"
                        if llm.get("degraded")
                        else llm.get("budget", {}).get("state", "off")
                    ),
                    "spent_total": llm.get("budget", {}).get("spent_total"),
                    "lag_seconds": max(lags, default=0.0),
                    "stages": {"stages": status["stages"]},
                }
            ],
        )

    async def offload_job(self) -> None:
        if not is_configured(self.settings):
            return
        result = await asyncio.to_thread(self.offloader.run)
        if result.uploaded or result.deleted_local or result.failed:
            log.info("offload", result=str(result))

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
            "commit": self.commit,
            "last_commit_at": self.writer.last_commit_at or self.started_at,
            "last_maintenance_at": self.last_maintenance,
            "last_backup_at": self.last_backup,
            "last_rollup_at": self.last_rollup,
            "feeds": self.poller.health() if self.poller else {},
            "feed_details": self.poller.details() if self.poller else [],
            "stages": [stage.status() for stage in self.stages],
            "shedding": self.shedding,
            "network": self.gate.status(),
            "archive": self.archive.status(),
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
            self.every(
                ENTITY_PASS_SECONDS,
                "entities",
                self.entity_job,
                first_after=90,
            ),
            self.every(
                BRIEF_CHECK_SECONDS,
                "briefs",
                self.brief_job,
                first_after=120,
            ),
            self.every(LADDER_SECONDS, "ladder", self.ladder_job),
            self.every(HEALTH_SECONDS, "health", self.health_job),
            self.every(
                self.settings.archive.upload_minutes * 60,
                "offload",
                self.offload_job,
                first_after=300,
            ),
        ]

    def build_stages(self) -> list[Stage]:
        """Connect the parsers in dependency order."""
        settings = self.settings
        sources = {s.source_id: s for s in load_sources(settings)}
        threshold = settings.nlp.relevance_threshold

        def relevant(item: Item) -> bool:
            return item.relevant and item.relevance_conf >= threshold

        def relevant_canonical(item: Item) -> bool:
            return relevant(item) and item.is_canonical

        self.storylines = StorylineParser(
            settings, self.db, self.writer, self.llm
        )
        self.embed = EmbedParser(settings)
        stage = lambda parser, **options: Stage(  # noqa: E731
            parser, self.db, self.writer, **options
        )
        lang = stage(LangParser(), linger_seconds=0.2, backfill_batch=2000)
        classify = stage(
            ClassifyParser(settings, self.llm, sources),
            linger_seconds=CLASSIFY_LINGER_SECONDS,
            backfill_batch=240,
        )
        embed = stage(self.embed, linger_seconds=0.5, backfill_batch=640)
        dedupe = stage(
            DedupeParser(settings, self.db),
            linger_seconds=0.2,
            backfill_batch=640,
        )
        story = stage(
            self.storylines,
            accept=relevant_canonical,
            linger_seconds=0.2,
            backfill_batch=320,
        )
        all_stages = [lang, classify, embed, dedupe, story]
        links = [
            (lang, classify),
            (classify, embed),
            (embed, dedupe),
            (dedupe, story),
        ]
        if self.llm is not None:
            resolver = EntityResolver(settings)
            fetch = stage(
                FetchTextParser(
                    settings,
                    sources,
                    self.guard,
                    archive=self.archive,
                    gate=self.gate,
                ),
                accept=relevant,
                linger_seconds=0.0,
                backfill_batch=48,
            )
            extract = stage(
                ExtractParser(
                    settings,
                    self.llm,
                    self.db,
                    sources,
                    resolver,
                    Gazetteer.load(settings.paths.ref),
                ),
                accept=relevant,
                linger_seconds=0.0,
                backfill_batch=48,
            )
            self.extract_stage = extract
            self.entity_pass = EntityPass(
                settings, self.db, self.writer, self.llm, resolver
            )
            self.briefs = BriefWriter(settings, self.db, self.writer, self.llm)
            all_stages += [fetch, extract]
            links += [(classify, fetch), (fetch, extract)]
        if self.stage_filter is not None:
            all_stages = [
                s for s in all_stages if s.parser.name in self.stage_filter
            ]
        for first, second in links:
            if first in all_stages and second in all_stages:
                first.then(second)
        return all_stages

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
            archive=self.archive,
            sightings=load_sightings(self.conn),
            gate=self.gate,
        )

    async def _network_restored(self, gap: dict[str, Any]) -> None:
        """Commit the gap before any item from after it can be read."""
        if self.llm is not None:
            self.llm.clear_failures()
        seconds = (gap["gap_end"] - gap["gap_start"]).total_seconds()
        if seconds >= self.settings.poll.min_gap_seconds:
            self.writer.add("raw.collection_gaps", [gap])
            await self.db.run(self.writer.flush)

    async def run(self) -> None:
        if not self.no_llm and self.settings.anthropic_api_key is not None:
            self.llm = LlmClient(
                self.settings,
                self.writer,
                load_budget(self.conn, self.settings),
                network_up=lambda: self.gate.is_online,
            )
        self.archive.start()
        self.stages = self.build_stages()
        self.first_stage = self.stages[0] if self.stages else None
        if self.entity_pass is not None:
            await self.entity_pass.reload()
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
        await asyncio.to_thread(self.archive.stop)
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
