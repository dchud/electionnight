"""Runs parsers as stages connected by in-process queues.

Items move between stages without waiting for a lake commit, so a chain of
parsers can finish well inside one commit interval. The lake records what
finished: on restart, and for a new parser version, each stage's `input_query`
finds the work still owed and feeds it in behind live items.
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
    """One parser, its queues, and its workers."""

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
    ) -> None:
        self.parser = parser
        self.db = db
        self.writer = writer
        self.accept = accept or (lambda item: True)
        self.linger_seconds = linger_seconds
        self.backfill_enabled = backfill
        self.backfill_batch = backfill_batch
        self.retry_delay = retry_delay
        self.next_stages: list[Stage] = []
        self.live: asyncio.Queue[Item] = asyncio.Queue()
        self.backlog: deque[Item] = deque()
        self.tracked: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.paused = False
        self.pause_backfill = False
        self.finished = 0
        self.errors = 0
        self.lag_seconds = 0.0

    @property
    def depth(self) -> int:
        return self.live.qsize() + len(self.backlog)

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

    async def _next_batch(self) -> list[Item]:
        batch: list[Item] = []
        while not batch:
            if self.paused:
                await asyncio.sleep(1.0)
                continue
            item = self._pop_nowait()
            if item is not None:
                batch.append(item)
                break
            try:
                batch.append(await asyncio.wait_for(self.live.get(), 0.5))
            except TimeoutError:
                continue
        deadline = time.monotonic() + self.linger_seconds
        while len(batch) < self.parser.batch_size:
            item = self._pop_nowait()
            if item is not None:
                batch.append(item)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(
                    await asyncio.wait_for(self.live.get(), remaining)
                )
            except TimeoutError:
                break
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
        self.writer.add("nlp.parser_runs", runs)
        for table, rows in output.rows.items():
            self.writer.add(table, rows)

    def _forward(self, batch: list[Item], output: ParserOutput) -> None:
        for item in batch:
            status, _ = output.statuses.get(item.item_id, ("ok", None))
            if status in {"ok", "skipped"}:
                for stage in self.next_stages:
                    stage.enqueue(item)

    def _fail(self, batch: list[Item], error: str) -> None:
        for item in batch:
            attempts = self.attempts.get(item.item_id, 0) + 1
            self.attempts[item.item_id] = attempts
            if attempts < MAX_ATTEMPTS:
                delay = self.retry_delay * 2 ** (attempts - 1)
                asyncio.get_running_loop().call_later(
                    delay * random.uniform(0.8, 1.2),
                    self.live.put_nowait,
                    item,
                )
            else:
                self.errors += 1
                self.attempts.pop(item.item_id, None)
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
        now = time.time()
        stamps = [
            item.first_seen_at.timestamp()
            for item in batch
            if item.first_seen_at
        ]
        if stamps:
            self.lag_seconds = now - max(stamps)

    async def worker(self) -> None:
        while True:
            batch = await self._next_batch()
            await self._process(batch)

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
        self.tracked.update(owed)
        rows = await self.db.fetch(HYDRATE_SQL, {"ids": owed})
        self.backlog.extend(item_from_row(row) for row in rows)

    def tasks(self) -> list[asyncio.Task[None]]:
        tasks = [
            asyncio.ensure_future(self.worker())
            for _ in range(self.parser.max_concurrency)
        ]
        if self.backfill_enabled:
            tasks.append(asyncio.ensure_future(self.feeder()))
        return tasks

    def status(self) -> dict[str, Any]:
        return {
            "parser": self.parser.name,
            "version": self.parser.version,
            "depth": self.depth,
            "finished": self.finished,
            "errors": self.errors,
            "lag_seconds": round(self.lag_seconds, 1),
            "paused": self.paused,
        }
