"""The under-the-hood teaching views. Plain `def` routes, so all of their work
runs in worker threads, off the event loop.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response

from monitor.web.filters import Filters
from monitor.web.under import Under, console_available, valid_item_id
from monitor.web.under_coverage import coverage
from monitor.web.under_trace import (
    archive_record,
    records,
    trace,
    trace_candidates,
)

router = APIRouter(prefix="/under")

MAX_SQL_BYTES = 100_000
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
LOOPBACK_NAMES = {"127.0.0.1", "[::1]", "localhost", "testserver"}

NAV = (
    ("index", "/under", "Overview"),
    ("coverage", "/under/coverage", "Coverage"),
    ("inventory", "/under/inventory", "Inventory"),
    ("storage", "/under/storage", "Storage"),
    ("data-model", "/under/data-model", "Data model"),
    ("models", "/under/models", "Models"),
    ("trace", "/under/trace", "Trace"),
    ("records", "/under/records", "Records"),
    ("feeds", "/under/feeds", "Feeds"),
    ("costs", "/under/costs", "Costs"),
    ("sql", "/under/sql", "SQL"),
)


def _under(request: Request) -> Under:
    return request.app.state.web.under


def _page(
    request: Request,
    template: str,
    view: str,
    filters: Filters | None = None,
    auto_refresh: bool = True,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    web = request.app.state.web
    return web.render(
        request,
        f"under/{template}.html",
        filters,
        status_code=status_code,
        view=view,
        nav=NAV,
        snapshot=web.under.header(),
        auto_refresh=auto_refresh,
        **context,
    )


def _window(request: Request) -> Filters:
    return Filters.parse(request.query_params, "24h")


@router.get("", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    under = _under(request)
    return _page(
        request,
        "index",
        "index",
        overview=under.overview(),
        candidates=trace_candidates(under),
    )


@router.get("/coverage", response_class=HTMLResponse)
def coverage_view(request: Request) -> HTMLResponse:
    filters = _window(request)
    return _page(
        request,
        "coverage",
        "coverage",
        filters,
        **coverage(_under(request), filters),
    )


@router.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request) -> HTMLResponse:
    filters = _window(request)
    return _page(
        request,
        "inventory",
        "inventory",
        filters,
        **_under(request).inventory(filters),
    )


@router.get("/storage", response_class=HTMLResponse)
def storage(request: Request) -> HTMLResponse:
    return _page(
        request,
        "storage",
        "storage",
        **_under(request).storage(request.query_params),
    )


@router.get("/data-model", response_class=HTMLResponse)
def data_model(request: Request) -> HTMLResponse:
    return _page(
        request,
        "data_model",
        "data-model",
        **_under(request).data_model(request.query_params),
    )


@router.get("/models", response_class=HTMLResponse)
def models(request: Request) -> HTMLResponse:
    return _page(request, "models", "models", **_under(request).model_map())


@router.get("/trace", response_class=HTMLResponse)
def trace_picker(request: Request) -> HTMLResponse:
    """Recent items to trace. Any other item is reached by record search."""
    return _page(
        request,
        "trace_picker",
        "trace",
        auto_refresh=False,
        candidates=trace_candidates(_under(request)),
    )


@router.get("/trace/{item_id}", response_class=HTMLResponse)
def trace_view(request: Request, item_id: str) -> HTMLResponse:
    filters = _window(request)
    data = (
        trace(_under(request), item_id, filters)
        if valid_item_id(item_id)
        else None
    )
    if data is None:
        return _page(
            request,
            "trace_missing",
            "trace",
            status_code=404,
            auto_refresh=False,
            item_id=item_id,
        )
    return _page(
        request, "trace", "trace", filters, auto_refresh=False, **data
    )


@router.get("/trace/{item_id}/archive/{kind}", response_class=HTMLResponse)
def trace_archive(request: Request, item_id: str, kind: str) -> HTMLResponse:
    if not valid_item_id(item_id):
        return HTMLResponse("No such item.", status_code=404)
    return _page(
        request,
        "fragments/archive",
        "trace",
        **archive_record(_under(request), item_id, kind),
    )


@router.get("/records", response_class=HTMLResponse)
def records_view(request: Request) -> HTMLResponse:
    data = records(_under(request), request.query_params)
    pairs = data["pairs"]
    return _page(
        request,
        "records",
        "records",
        auto_refresh=False,
        next_qs=urlencode({**pairs, "page": data["page"] + 1}),
        prev_qs=urlencode({**pairs, "page": max(0, data["page"] - 1)}),
        **data,
    )


@router.get("/feeds", response_class=HTMLResponse)
def feeds(request: Request) -> HTMLResponse:
    return _page(request, "feeds", "feeds", **_under(request).feeds())


@router.get("/costs", response_class=HTMLResponse)
def costs(request: Request) -> HTMLResponse:
    return _page(request, "costs", "costs", **_under(request).costs())


def _local_request(request: Request) -> bool:
    """Loopback peer, loopback Host, and no other site's Origin.

    The Host check stops a rebound DNS name reaching this from a web page, and
    the Origin check stops another page posting a query.
    """
    client = request.client.host if request.client else ""
    if client not in LOOPBACK_HOSTS:
        return False
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    if host not in LOOPBACK_NAMES:
        return False
    origin = request.headers.get("origin")
    if origin:
        name = origin.split("://", 1)[-1].rsplit(":", 1)[0]
        return name in LOOPBACK_NAMES
    return True


@router.get("/sql", response_model=None)
def sql_page(request: Request) -> Response:
    if not _local_request(request):
        return Response("The SQL console is loopback only.", status_code=403)
    return _page(
        request,
        "sql",
        "sql",
        auto_refresh=False,
        available=console_available(),
        result=None,
        sql="SELECT parser, status, count(*) AS n\n"
        "FROM lake.nlp.parser_runs\nGROUP BY ALL\nORDER BY n DESC",
    )


@router.post("/sql", response_model=None)
async def sql_run(request: Request) -> Response:
    """Reads the form body here, and runs the query in a worker thread."""
    if not _local_request(request):
        return Response("The SQL console is loopback only.", status_code=403)
    body = (await request.body())[:MAX_SQL_BYTES].decode("utf-8", "replace")
    sql = (parse_qs(body).get("sql") or [""])[0]
    result = await run_in_threadpool(_under(request).console, sql)
    return _page(
        request,
        "sql",
        "sql",
        auto_refresh=False,
        available=console_available(),
        result=result,
        sql=sql,
    )
