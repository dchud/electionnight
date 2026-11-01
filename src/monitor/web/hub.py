"""Watches the lake for new snapshots and tells connected browsers."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from monitor.logs import get
from monitor.settings import Settings
from monitor.web.readmodel import ReadModel

log = get(__name__)

STATUS_SECONDS = 10.0


class Hub:
    """Refreshes the read model and fans events out to SSE clients."""

    def __init__(self, model: ReadModel, settings: Settings) -> None:
        self.model = model
        self.settings = settings
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self.known_briefs = -1

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.clients.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self.clients):
            if queue.full():
                # A stalled browser loses old events rather than memory.
                queue.get_nowait()
            queue.put_nowait(event)

    def _new_brief(self) -> str | None:
        rows = self.model.rows(
            "SELECT brief_id FROM briefs ORDER BY created_at DESC LIMIT 1"
        )
        count = self.model.rows("SELECT count(*) AS n FROM briefs")[0]["n"]
        changed = self.known_briefs not in {-1, count}
        self.known_briefs = count
        return rows[0]["brief_id"] if changed and rows else None

    async def run(self) -> None:
        last_status = 0.0
        while True:
            await asyncio.sleep(self.settings.web.snapshot_poll_seconds)
            try:
                changed = await asyncio.to_thread(self.model.refresh)
            except Exception as error:
                log.warning("refresh_failed", error=str(error)[:300])
                continue
            if changed:
                snapshot = self.model.snapshot
                self.publish(
                    {
                        "type": "snapshot",
                        "snapshot_id": snapshot.snapshot_id,
                        "committed_at": str(snapshot.committed_at),
                        "changed": list(snapshot.changed),
                    }
                )
                brief_id = self._new_brief()
                if brief_id:
                    self.publish({"type": "brief", "brief_id": brief_id})
            if time.monotonic() - last_status >= STATUS_SECONDS:
                last_status = time.monotonic()
                self.publish({"type": "status"})
