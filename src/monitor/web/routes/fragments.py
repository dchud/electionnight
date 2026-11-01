"""HTML fragments refreshed by htmx."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from monitor.web.filters import Filters

router = APIRouter(prefix="/frag")


def _web(request: Request):
    return request.app.state.web


@router.get("/cite/{item_id}", response_class=HTMLResponse)
def cite(request: Request, item_id: str) -> HTMLResponse:
    """The popover shown when hovering a footnote."""
    web = _web(request)
    item = web.panels.article(item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    return web.render(request, "fragments/cite.html", item=item)


@router.get("/article/{item_id}", response_class=HTMLResponse)
def article(request: Request, item_id: str) -> HTMLResponse:
    web = _web(request)
    item = web.panels.article(item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    return web.render(request, "fragments/article.html", item=item)


@router.get("/kpis", response_class=HTMLResponse)
def kpis(request: Request) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/kpis.html",
        filters,
        kpis=web.panels.kpis(filters),
    )


@router.get("/status-strip", response_class=HTMLResponse)
def status_strip(request: Request) -> HTMLResponse:
    web = _web(request)
    return web.render(
        request, "fragments/status_strip.html", strip=web.panels.status_strip()
    )


@router.get("/wire", response_class=HTMLResponse)
def wire(request: Request) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/wire_rows.html",
        filters,
        items=web.panels.wire(filters),
    )


@router.get("/rising", response_class=HTMLResponse)
def rising(request: Request) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/rising.html",
        filters,
        cards=web.panels.rising(filters),
    )


@router.get("/brief", response_class=HTMLResponse)
def brief(request: Request, offset: int = 0) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/brief.html",
        filters,
        brief=web.panels.latest_brief(filters, offset),
        offset=offset,
        total=web.panels.brief_count(filters),
    )


@router.get("/states-rising", response_class=HTMLResponse)
def states_rising(request: Request) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/states_rising.html",
        filters,
        rows=web.panels.states_rising(filters),
    )


@router.get("/race-calls", response_class=HTMLResponse)
def race_calls(request: Request) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/race_calls.html",
        filters,
        rows=web.panels.race_calls(filters),
    )


@router.get("/translated", response_class=HTMLResponse)
def translated(request: Request) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/translated.html",
        filters,
        items=web.panels.latest_translated(filters),
    )


def _evidence_clause(
    kind: str, key: str, filters: Filters, bin_start: datetime | None
) -> tuple[str, dict, str]:
    """SQL, params, and words for one aggregate's underlying items."""
    if kind == "state":
        return "list_contains(f.states, $key)", {"key": key}, key
    if kind == "storyline":
        return "f.storyline_id = $key", {"key": key}, "storyline"
    if kind == "topic":
        return "list_contains(f.topics, $key)", {"key": key}, key
    if kind == "entity":
        clause = (
            "f.item_id IN (SELECT item_id FROM mentions "
            "WHERE entity_id = $key)"
        )
        return clause, {"key": key}, "entity"
    if kind == "bucket":
        clause = (
            "coalesce((SELECT lean_label FROM sources s "
            "WHERE s.source_id = f.source_id), 'Unrated') = $key"
        )
        return clause, {"key": key}, f"lean {key}"
    if kind == "language":
        return "f.language = $key", {"key": key}, key
    if kind == "items":
        ids = [part for part in key.split(",") if part]
        return "f.item_id IN (SELECT unnest($ids))", {"ids": ids}, "claims"
    return "TRUE", {}, "all relevant stories"


@router.get("/evidence", response_class=HTMLResponse)
def evidence(
    request: Request,
    kind: str = "all",
    key: str = "",
    bucket: str = "",
    bin_start: datetime | None = None,
    bin_minutes: int = 5,
    page: int = Query(0, ge=0),
) -> HTMLResponse:
    """The stories behind any number on the dashboard."""
    web = _web(request)
    filters = web.filters(request)
    clause, extra, words = _evidence_clause(kind, key, filters, bin_start)
    label = [words]
    if bucket:
        bucket_clause, bucket_extra, bucket_words = _evidence_clause(
            "bucket", bucket, filters, None
        )
        clause = f"{clause} AND {bucket_clause.replace('$key', '$bucket')}"
        extra = {**extra, "bucket": bucket_extra["key"]}
        label.append(bucket_words)
    if bin_start is not None:
        clause += " AND f.first_seen_at >= $bin_a AND f.first_seen_at < $bin_b"
        extra = {
            **extra,
            "bin_a": bin_start,
            "bin_b": bin_start + timedelta(minutes=bin_minutes),
        }
        label.append(f"{bin_start.strftime('%H:%M')} UTC, {bin_minutes} min")
    page_size = 50
    items, total = web.panels.evidence(
        filters, clause, extra, limit=page_size, offset=page * page_size
    )
    return web.render(
        request,
        "fragments/evidence.html",
        filters,
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        description=" · ".join([*label, filters.describe()]),
        next_params=str(request.query_params),
    )
