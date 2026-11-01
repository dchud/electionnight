"""The `/events` stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

router = APIRouter()


@router.get("/events")
async def events(request: Request) -> EventSourceResponse:
    web = request.app.state.web
    filters = web.filters(request)

    async def stream() -> AsyncIterator[dict[str, Any]]:
        queue = web.hub.subscribe()
        try:
            # Everything visible now is the browser's to read: it reloads the
            # wire on this event, and only later arrivals are pushed.
            sent_through = web.model.refresh_seq
            yield {"event": "wire-ready", "data": "{}"}
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
                    through = web.model.refresh_seq
                    rows = await asyncio.to_thread(
                        web.panels.wire_recent, filters, sent_through, through
                    )
                    sent_through = through
                    if rows:
                        template = web.templates.get_template(
                            "fragments/wire_rows.html"
                        )
                        html = template.render(items=rows, fresh=True)
                        yield {"event": "wire", "data": html}
        finally:
            web.hub.unsubscribe(queue)

    return EventSourceResponse(stream(), ping=15)
