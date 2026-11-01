"""The `/events` stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

router = APIRouter()


@router.get("/events")
async def events(request: Request) -> EventSourceResponse:
    web = request.app.state.web
    filters = web.filters(request)
    queue = web.hub.subscribe()

    async def stream() -> AsyncIterator[dict[str, Any]]:
        since = datetime.now(UTC)
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), 15.0)
                except TimeoutError:
                    continue
                kind = event["type"]
                yield {"event": kind, "data": json.dumps(event)}
                if kind == "snapshot":
                    rows = await asyncio.to_thread(
                        web.panels.wire_since, filters, since
                    )
                    if rows:
                        since = max(row["first_seen_at"] for row in rows)
                        template = web.templates.get_template(
                            "fragments/wire_rows.html"
                        )
                        html = template.render(items=rows, fresh=True)
                        yield {"event": "wire", "data": html}
        finally:
            web.hub.unsubscribe(queue)

    return EventSourceResponse(stream(), ping=15)
