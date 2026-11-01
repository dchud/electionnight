"""Full pages."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from monitor.web.drilldown import SearchRequest

router = APIRouter()


def _not_found(request: Request, what: str, key: str) -> HTMLResponse:
    web = request.app.state.web
    return web.render(
        request,
        "pages/not_found.html",
        status_code=404,
        what=what,
        key=key,
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    web = request.app.state.web
    return web.render(request, "pages/home.html")


@router.get("/article/{item_id}", response_class=HTMLResponse)
def article(request: Request, item_id: str) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    item = web.drill.article(item_id, filters, filters.query_string())
    if item is None:
        return _not_found(request, "article", item_id)
    return web.render(request, "pages/article.html", filters, item=item)


@router.get("/search", response_class=HTMLResponse)
def search(request: Request) -> HTMLResponse:
    """A plain `def` route, so embedding and ranking run in a worker thread."""
    web = request.app.state.web
    filters = web.filters(request)
    query = SearchRequest.parse(request.query_params)
    pairs = query.query_pairs()
    extra_qs = f"&{urlencode(pairs)}" if pairs else ""
    window_pairs = {
        key: value for key, value in pairs.items() if key != "time"
    }
    return web.render(
        request,
        "pages/search.html",
        filters,
        result=web.drill.search(query, filters),
        search=query,
        choices=web.drill.search_choices(),
        extra_qs=extra_qs,
        window_qs=f"&{urlencode(window_pairs)}" if window_pairs else "",
        result_qs=urlencode(pairs),
    )


@router.get("/story/{storyline_id}", response_model=None)
def story(request: Request, storyline_id: str) -> Response:
    web = request.app.state.web
    filters = web.filters(request)
    resolved = web.drill.resolve_storyline(storyline_id)
    if resolved is None:
        return _not_found(request, "storyline", storyline_id)
    if resolved != storyline_id:
        query = filters.query_string()
        target = f"/story/{resolved}" + (f"?{query}" if query else "")
        return RedirectResponse(target, status_code=307)
    return web.render(
        request,
        "pages/story.html",
        filters,
        **web.drill.page_context(
            "story", storyline_id, filters, request.query_params
        ),
    )


@router.get("/entity/{entity_id}", response_class=HTMLResponse)
def entity(request: Request, entity_id: str) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    if web.drill.entity(entity_id) is None:
        return _not_found(request, "entity", entity_id)
    return web.render(
        request,
        "pages/entity.html",
        filters,
        **web.drill.page_context(
            "entity", entity_id, filters, request.query_params
        ),
    )


@router.get("/state/{usps}", response_class=HTMLResponse)
def state(request: Request, usps: str) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    code = usps.upper()
    if web.drill.state(code) is None:
        return _not_found(request, "state", usps)
    return web.render(
        request,
        "pages/state.html",
        filters,
        **web.drill.page_context("state", code, filters, request.query_params),
    )


@router.get("/sources", response_class=HTMLResponse)
def sources(request: Request) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    return web.render(
        request,
        "pages/sources.html",
        filters,
        **web.reference.sources(filters),
    )


@router.get("/methods", response_class=HTMLResponse)
def methods(request: Request) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    status = web.model.read_status()
    return web.render(
        request,
        "pages/methods.html",
        filters,
        document=web.reference.methods_html(),
        stages=status.get("stages") or [],
        settings=web.settings,
        **web.reference.ledger(filters),
    )


@router.get("/status", response_class=HTMLResponse)
def status(request: Request) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    return web.render(
        request,
        "pages/status.html",
        filters,
        **web.reference.status(filters),
    )


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    return web.render(
        request,
        "pages/watchlist.html",
        filters,
        **web.reference.watchlist(filters),
    )


def _changed(response: HTMLResponse) -> HTMLResponse:
    """Tell the page to refresh anything that depends on the watchlist."""
    response.headers["HX-Trigger"] = "watch-changed"
    return response


@router.post("/watchlist/race", response_class=HTMLResponse)
def watch_race(request: Request, key: str, watched: bool) -> Response:
    """Write the choice to the UI database. The lake is never touched."""
    web = request.app.state.web
    race = web.reference.race_row(key)
    if race is None:
        return Response("no such race", status_code=404)
    web.reference.set_race(race, watched)
    return _changed(
        web.render(
            request,
            "fragments/watch_toggle.html",
            kind="race",
            key=key,
            watched=web.reference.race_watched(race),
        )
    )


@router.post("/watchlist/state", response_class=HTMLResponse)
def watch_state(request: Request, key: str, watched: bool) -> Response:
    web = request.app.state.web
    code = key.upper()
    if web.reference.state_row(code) is None:
        return Response("no such state", status_code=404)
    web.reference.set_state(code, watched)
    return _changed(
        web.render(
            request,
            "fragments/watch_toggle.html",
            kind="state",
            key=code,
            watched=watched,
        )
    )


@router.post("/watchlist/battleground", response_class=HTMLResponse)
def battleground(request: Request, key: str, value: str) -> Response:
    """Set a state's override: `in`, `out`, or `default`."""
    web = request.app.state.web
    code = key.upper()
    row = web.reference.state_row(code)
    choices = {"in": True, "out": False, "default": None}
    if row is None or value not in choices:
        return Response("no such state or choice", status_code=404)
    web.reference.set_battleground(code, choices[value])
    return _changed(
        web.render(
            request,
            "fragments/override_toggle.html",
            state=web.reference.override_context(row),
        )
    )


@router.get("/watchlist/counts", response_class=HTMLResponse)
def watch_counts(request: Request) -> HTMLResponse:
    web = request.app.state.web
    filters = web.filters(request)
    return web.render(
        request,
        "fragments/focus_counts.html",
        filters,
        **web.reference.focus_counts(filters),
    )
