"""The dashboard application. Read-only against the lake."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from monitor.logs import configure, get
from monitor.settings import Settings
from monitor.web.filters import (
    FOCUSES,
    LEVELS,
    WINDOWS,
    Filters,
    init_ui_db,
)
from monitor.web.hub import Hub
from monitor.web.panels import Panels, lean_glyph, lean_text
from monitor.web.readmodel import ReadModel

log = get(__name__)

ET = ZoneInfo("America/New_York")
WEB_DIR = Path(__file__).parent
FOOTNOTE_RE = re.compile(r"\[\^(\w+)\]")
ALL_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY"
).split()


def et_time(value: datetime | None) -> str:
    """A time of day in Eastern time, such as `8:05 pm`."""
    if value is None:
        return ""
    return value.astimezone(ET).strftime("%-I:%M %p").lower()


def et_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(ET).strftime("%b %-d, %-I:%M %p ET")


def render_cited(
    markdown: str | None, citations: dict[str, str] | None
) -> Markup:
    """Escape model text and turn `[^n]` markers into footnote links."""
    if not markdown:
        return Markup("")
    citations = citations or {}

    def footnote(match: re.Match[str]) -> str:
        number = match.group(1)
        item_id = citations.get(number)
        if not item_id:
            return ""
        return (
            f'<sup class="fn" data-item="{escape(item_id)}" '
            f'tabindex="0" role="button">[{escape(number)}]</sup>'
        )

    paragraphs = [
        f"<p>{FOOTNOTE_RE.sub(footnote, str(escape(part)))}</p>"
        for part in markdown.split("\n\n")
    ]
    return Markup("".join(paragraphs))


def first_sentence(markdown: str | None) -> str:
    """The first sentence, with its citation markers."""
    if not markdown:
        return ""
    match = re.match(r"(.+?[.!?。！？](?:\[\^\w+\])*)(?:\s|$)", markdown, re.S)
    return match.group(1) if match else markdown


def create_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    env = templates.env
    env.filters["et_time"] = et_time
    env.filters["et_datetime"] = et_datetime
    env.filters["glyph"] = lean_glyph
    env.filters["lean_text"] = lean_text
    env.filters["cited"] = render_cited
    env.filters["first_sentence"] = first_sentence
    env.globals.update(
        WINDOWS=WINDOWS,
        LEVELS=LEVELS,
        FOCUSES=FOCUSES,
        ALL_STATES=ALL_STATES,
    )
    return templates


class Web:
    """Everything a route needs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        init_ui_db(settings.paths.ui_db)
        self.model = ReadModel(settings)
        self.panels = Panels(settings, self.model)
        self.templates = create_templates()
        self.hub = Hub(self.model, settings)

    def filters(self, request: Request) -> Filters:
        return Filters.parse(
            request.query_params, self.settings.web.default_window
        )

    def render(
        self,
        request: Request,
        template: str,
        filters: Filters | None = None,
        **context: Any,
    ) -> HTMLResponse:
        filters = filters or self.filters(request)
        return self.templates.TemplateResponse(
            request,
            template,
            {
                "filters": filters,
                "qs": filters.query_string(),
                "now": datetime.now(UTC),
                "throttle": self.settings.web.fragment_throttle_seconds,
                "tour_seconds": self.settings.nlp.tour_seconds,
                **context,
            },
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    web: Web = app.state.web
    await asyncio.to_thread(web.model.refresh, True)
    task = asyncio.ensure_future(web.hub.run())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def create_app(settings: Settings) -> FastAPI:
    from monitor.web.routes import api, fragments, pages, sse

    app = FastAPI(title="Election Night Monitor", lifespan=lifespan)
    app.state.web = Web(settings)
    app.mount(
        "/static",
        StaticFiles(directory=str(WEB_DIR / "static")),
        name="static",
    )
    app.include_router(pages.router)
    app.include_router(fragments.router)
    app.include_router(api.router)
    app.include_router(sse.router)
    return app


def serve(settings: Settings, host: str, port: int) -> None:
    configure("web", settings.paths.logs)
    log.info("web_starting", host=host, port=port)
    uvicorn.run(
        create_app(settings), host=host, port=port, log_level="warning"
    )
