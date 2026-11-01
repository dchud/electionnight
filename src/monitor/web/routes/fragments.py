"""HTML fragments refreshed by htmx."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from monitor.web.filters import ET, Filters

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
    """The article drawer, which shares its template with the article page."""
    web = _web(request)
    filters = web.filters(request)
    item = web.drill.article(item_id, filters, filters.query_string())
    if item is None:
        return web.render(
            request,
            "fragments/missing.html",
            filters,
            status_code=404,
            what="article",
        )
    return web.render(request, "fragments/article.html", filters, item=item)


@router.get("/article/{item_id}/raw", response_class=HTMLResponse)
def article_raw(request: Request, item_id: str) -> HTMLResponse:
    """Every parser's rows for an item, loaded when the section is opened."""
    web = _web(request)
    if web.panels.article(item_id) is None:
        raise HTTPException(404, "no such item")
    return web.render(
        request,
        "fragments/article_raw.html",
        raw_json=web.drill.parser_json(item_id),
    )


SECTION_PAGES = {"story", "entity", "state"}


@router.get("/section/{page}/{key}/{name}", response_class=HTMLResponse)
def section(request: Request, page: str, key: str, name: str) -> HTMLResponse:
    """One refreshable section of a drill-down page."""
    web = _web(request)
    filters = web.filters(request)
    if page not in SECTION_PAGES or not web.drill.exists(page, key):
        raise HTTPException(404, "no such page")
    context = web.drill.section(page, key, name, filters, request.query_params)
    if context is None:
        raise HTTPException(404, "no such section")
    return web.render(
        request, f"fragments/{page}_{name}.html", filters, **context
    )


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


@router.get("/status", response_class=HTMLResponse)
def status(request: Request) -> HTMLResponse:
    web = _web(request)
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/status_body.html",
        filters,
        **web.reference.status(filters),
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
    if kind == "source":
        return "f.source_id = $key", {"key": key}, f"outlet {key}"
    if kind == "county":
        clause = (
            "f.item_id IN (SELECT item_id FROM places "
            "WHERE county_fips = $key)"
        )
        return clause, {"key": key}, f"county {key}"
    if kind == "primary":
        return "f.primary_level = $key", {"key": key}, f"level {key}"
    if kind == "dup":
        return "f.dup_group_id = $key", {"key": key}, "syndication group"
    return "TRUE", {}, "all relevant stories"


def _clock(value: datetime) -> str:
    """A time of day in Eastern time, such as `8:05 pm`."""
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(ET).strftime("%-I:%M %p").lower()


def _also_clause(
    also: str, filters: Filters
) -> tuple[str, dict[str, Any], str]:
    """A second condition, written `kind:key`, such as `source:cnn`."""
    kind, _, key = also.partition(":")
    clause, extra, words = _evidence_clause(kind, key, filters, None)
    return (
        clause.replace("$key", "$also_key"),
        {"also_key": extra["key"]} if "key" in extra else {},
        words,
    )


@router.get("/evidence", response_class=HTMLResponse)
def evidence(
    request: Request,
    kind: str = "all",
    key: str = "",
    bucket: str = "",
    bin_start: datetime | None = None,
    bin_minutes: int = 5,
    also: str = "",
    span: str = "",
    rel: str = "",
    page: int = Query(0, ge=0),
) -> HTMLResponse:
    """The stories behind any number on the dashboard.

    A count may cover more than the global window and the relevant items.
    `span=all` drops the window and `rel=any` drops the relevance filter, so
    the drawer lists what that count counted.
    """
    web = _web(request)
    filters = web.filters(request)
    clause, extra, words = _evidence_clause(kind, key, filters, bin_start)
    label = [web.drill.evidence_label(kind, key) or words]
    if also:
        also_clause, also_extra, also_words = _also_clause(also, filters)
        clause = f"{clause} AND {also_clause}"
        extra = {**extra, **also_extra}
        also_kind, _, also_key = also.partition(":")
        label.append(
            web.drill.evidence_label(also_kind, also_key) or also_words
        )
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
        end = bin_start + timedelta(minutes=bin_minutes)
        label.append(f"{_clock(bin_start)}–{_clock(end)}")
    page_size = 50
    windowed = span != "all"
    if rel == "any":
        label.append("including items not election relevant")
    items, total = web.panels.evidence(
        filters,
        clause,
        extra,
        limit=page_size,
        offset=page * page_size,
        windowed=windowed,
        relevant=rel != "any",
    )
    return web.render(
        request,
        "fragments/evidence.html",
        filters,
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        description=" · ".join([*label, filters.describe(windowed)]),
        next_params=str(request.query_params),
    )
