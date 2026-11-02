"""Runs parsers as stages connected by in-process queues.

Items move between stages without waiting for a lake commit, so a chain of
parsers can finish well inside one commit interval. The lake records what
finished: on restart, and for a new parser version, each stage's `input_query`
finds the work still owed and feeds it in behind live items.

Only live items move through the queues. A backfilled item reaches a later
stage through that stage's own `input_query`, so it runs there only if that
stage still owes it at its current version.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb

from monitor.lake import BatchWriter
from monitor.logs import get
from monitor.parsers.base import (
    HYDRATE_SQL,
    Item,
    Parser,
    ParserOutput,
    item_from_row,
)

log = get(__name__)

MAX_ATTEMPTS = 3
BACKFILL_POLL_SECONDS = 10.0
RETRY_BACKFILL_SECONDS = 300.0
IDLE_AFTER_FAILURE_SECONDS = 1.0


@dataclass
class Database:
    """Serializes lake reads and commits on the pipeline's connection."""

    conn: duckdb.DuckDBPyConnection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _fetch(self, sql: str, params: Any) -> list[tuple[Any, ...]]:
        cursor = self.conn.cursor()
        try:
            return cursor.execute(sql, params).fetchall()
        finally:
            cursor.close()

    async def fetch(
        self, sql: str, params: Any = None
    ) -> list[tuple[Any, ...]]:
        async with self.lock:
            return await asyncio.to_thread(self._fetch, sql, params)

    async def run(self, func: Callable[..., Any], *args: Any) -> Any:
        async with self.lock:
            return await asyncio.to_thread(func, *args)


class Stage:
    """One parser, its queues, and its workers.

    A parser with a `partition` method runs one item per partition at a time.
    An item whose partition is busy is set aside until that partition is free,
    so it holds no worker and items of other partitions go ahead of it.
    """

    def __init__(
        self,
        parser: Parser,
        db: Database,
        writer: BatchWriter,
        accept: Callable[[Item], bool] | None = None,
        linger_seconds: float = 0.5,
        backfill: bool = True,
        backfill_batch: int = 200,
        retry_delay: float = 5.0,
        retry_backfill_seconds: float = RETRY_BACKFILL_SECONDS,
    ) -> None:
        self.parser = parser
        self.db = db
        self.writer = writer
        self.accept = accept or (lambda item: True)
        self.linger_seconds = linger_seconds
        self.backfill_enabled = backfill
        self.backfill_batch = backfill_batch
        self.retry_delay = retry_delay
        self.retry_backfill_seconds = retry_backfill_seconds
        self.next_stages: list[Stage] = []
        self.live: asyncio.Queue[Item] = asyncio.Queue()
        self.backlog: deque[Item] = deque()
        self.partition: Callable[[Item], str] | None = getattr(
            parser, "partition", None
        )
        self.busy: set[str] = set()
        self.parked: dict[str, list[Item]] = {}
        self.tracked: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.paused = False
        self.pause_backfill = False
        self.finished = 0
        self.errors = 0
        self.lag_seconds = 0.0

    @property
    def depth(self) -> int:
        parked = sum(len(items) for items in self.parked.values())
        return self.live.qsize() + len(self.backlog) + parked

    def then(self, *stages: Stage) -> Stage:
        self.next_stages.extend(stages)
        return self

    def enqueue(self, item: Item) -> None:
        """Queue a live item unless this stage already has it."""
        if self.accept(item) and item.item_id not in self.tracked:
            self.tracked.add(item.item_id)
            self.live.put_nowait(item)

    def _pop_nowait(self) -> Item | None:
        try:
            return self.live.get_nowait()
        except asyncio.QueueEmpty:
            pass
        if self.backlog and not self.pause_backfill:
            return self.backlog.popleft()
        return None

    def _claim(self, item: Item) -> bool:
        """Take an item, or set it aside while its partition is busy."""
        if self.partition is None:
            return True
        key = self.partition(item)
        if key in self.busy:
            self.parked.setdefault(key, []).append(item)
            return False
        self.busy.add(key)
        return True

    def _release(self, batch: list[Item]) -> None:
        """Free these items' partitions and requeue what was set aside."""
        if self.partition is None:
            return
        for item in batch:
            key = self.partition(item)
            self.busy.discard(key)
            for waiting in self.parked.pop(key, []):
                self._requeue(waiting)

    async def _next_batch(self) -> list[Item]:
        batch: list[Item] = []
        while not batch:
            if self.paused:
                await asyncio.sleep(1.0)
                continue
            item = self._pop_nowait()
            if item is None:
                try:
                    item = await asyncio.wait_for(self.live.get(), 0.5)
                except TimeoutError:
                    continue
            if self._claim(item):
                batch.append(item)
        deadline = time.monotonic() + self.linger_seconds
        while len(batch) < self.parser.batch_size:
            item = self._pop_nowait()
            if item is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self.live.get(), remaining)
                except TimeoutError:
                    break
            if self._claim(item):
                batch.append(item)
        return batch

    def _record_runs(self, batch: list[Item], output: ParserOutput) -> None:
        runs = []
        for item in batch:
            status, error = output.statuses.get(item.item_id, ("ok", None))
            runs.append(
                {
                    "item_id": item.item_id,
                    "parser": self.parser.name,
                    "parser_version": self.parser.version,
                    "status": status,
                    "error": error,
                }
            )
        # Runs go last, so a failure here leaves the items owed, not finished.
        for table, rows in output.rows.items():
            self.writer.add(table, rows)
        self.writer.add("nlp.parser_runs", runs)

    def _forward(self, batch: list[Item], output: ParserOutput) -> None:
        for item in batch:
            if item.backfill:
                continue
            status, _ = output.statuses.get(item.item_id, ("ok", None))
            if status in {"ok", "skipped"}:
                for stage in self.next_stages:
                    stage.enqueue(item)

    def _requeue(self, item: Item) -> None:
        if item.backfill:
            self.backlog.append(item)
        else:
            self.live.put_nowait(item)

    def _release_later(self, item_ids: list[str]) -> None:
        """Let backfill find these items again, after a pause."""
        asyncio.get_running_loop().call_later(
            self.retry_backfill_seconds,
            self.tracked.difference_update,
            item_ids,
        )

    def _fail(self, batch: list[Item], error: str) -> None:
        for item in batch:
            attempts = self.attempts.get(item.item_id, 0) + 1
            self.attempts[item.item_id] = attempts
            if attempts < MAX_ATTEMPTS:
                delay = self.retry_delay * 2 ** (attempts - 1)
                asyncio.get_running_loop().call_later(
                    delay * random.uniform(0.8, 1.2),
                    self._requeue,
                    item,
                )
            else:
                self.errors += 1
                self.attempts.pop(item.item_id, None)
                self._release_later([item.item_id])
                self.writer.add(
                    "nlp.parser_runs",
                    [
                        {
                            "item_id": item.item_id,
                            "parser": self.parser.name,
                            "parser_version": self.parser.version,
                            "status": "error",
                            "error": error[:500],
                        }
                    ],
                )

    async def _process(self, batch: list[Item]) -> None:
        try:
            output = await self.parser.run(batch)
        except Exception as error:
            log.warning(
                "parser_failed",
                parser=self.parser.name,
                error=f"{type(error).__name__}: {error}"[:300],
                n=len(batch),
            )
            self._fail(batch, f"{type(error).__name__}: {error}")
            return
        self._record_runs(batch, output)
        requeued = []
        for item in batch:
            status, _ = output.statuses.get(item.item_id, ("ok", None))
            if status == "retry":
                requeued.append(item)
        if requeued:
            self._fail(requeued, "retry requested")
        self._forward([item for item in batch if item not in requeued], output)
        self.finished += len(batch)
        # Backfill items are old on purpose, so only live items measure lag.
        now = time.time()
        stamps = [
            item.first_seen_at.timestamp()
            for item in batch
            if item.first_seen_at and not item.backfill
        ]
        if stamps:
            self.lag_seconds = now - max(stamps)

    async def worker(self) -> None:
        """Run batches until cancelled. One failure must not end this task."""
        while True:
            batch: list[Item] = []
            try:
                batch = await self._next_batch()
                await self._process(batch)
            except Exception:
                log.exception(
                    "stage_worker_failed",
                    parser=self.parser.name,
                    n=len(batch),
                )
                self.errors += len(batch)
                self._release_later([item.item_id for item in batch])
                if not batch:
                    await asyncio.sleep(IDLE_AFTER_FAILURE_SECONDS)
            finally:
                self._release(batch)

    async def feeder(self) -> None:
        """Pull work the lake says is owed, behind live items."""
        while True:
            await asyncio.sleep(BACKFILL_POLL_SECONDS * random.uniform(0.5, 1))
            if self.pause_backfill or self.paused:
                continue
            if len(self.backlog) >= self.backfill_batch // 2:
                continue
            try:
                await self._load_backlog()
            except Exception as error:
                log.warning(
                    "backfill_failed",
                    parser=self.parser.name,
                    error=str(error)[:300],
                )

    async def _load_backlog(self) -> None:
        limit = self.backfill_batch + len(self.tracked)
        rows = await self.db.fetch(self.parser.input_query, {"limit": limit})
        owed = [r[0] for r in rows if r[0] not in self.tracked]
        owed = owed[: self.backfill_batch]
        if not owed:
            return
        rows = await self.db.fetch(HYDRATE_SQL, {"ids": owed})
        for row in rows:
            item = item_from_row(row)
            # A live item may have arrived during the query.
            if item.item_id in self.tracked:
                continue
            item.backfill = True
            self.tracked.add(item.item_id)
            self.backlog.append(item)

    def tasks(self) -> list[asyncio.Task[None]]:
        tasks = [
            asyncio.ensure_future(self.worker())
            for _ in range(self.parser.max_concurrency)
        ]
        if self.backfill_enabled:
            tasks.append(asyncio.ensure_future(self.feeder()))
        return tasks

    def oldest_live_age(self) -> float:
        """Seconds the oldest waiting live item has been in the system."""
        queued = list(self.live._queue)  # type: ignore[attr-defined]
        parked = [item for items in self.parked.values() for item in items]
        waiting = [
            item.first_seen_at.timestamp()
            for item in queued + parked
            if item.first_seen_at and not item.backfill
        ]
        return time.time() - min(waiting) if waiting else 0.0

    def status(self) -> dict[str, Any]:
        return {
            "parser": self.parser.name,
            "version": self.parser.version,
            "depth": self.depth,
            "finished": self.finished,
            "errors": self.errors,
            "lag_seconds": round(self.lag_seconds, 1),
            "paused": self.paused,
            "backfill_paused": self.pause_backfill,
            "oldest_live_age": round(self.oldest_live_age(), 1),
        }
