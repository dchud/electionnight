"""What the in-place filter switch reads from the page, without a browser.

`scripts/transition_check.py` drives the switch itself in a browser.
"""

import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient

from monitor.lake import BatchWriter, connect
from monitor.web.app import create_app

STATIC = Path("src/monitor/web/static")


@pytest.fixture(scope="module")
def client(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("switch"))
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "ref.sources",
        [{"source_id": "wire", "name": "Wire", "lean_label": "C"}],
    )
    writer.flush()
    conn.close()
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def controls(page: str) -> dict[str, str]:
    """Each filter control's key and link."""
    return {
        key: html.unescape(href)
        for key, href in re.findall(
            r'<a data-filter="([^"]+)" href="([^"]*)"', page
        )
    }


def test_every_filter_link_names_its_filter_and_value(client):
    links = controls(client.get("/").text)
    assert set(links) == {
        "window:15m",
        "window:1h",
        "window:3h",
        "window:since4pm",
        "window:24h",
        "focus:all",
        "focus:battleground",
        "focus:close",
        "focus:watchlist",
        "level:all",
        "level:national",
        "level:state",
        "level:local",
        "lang:en",
        "lang:all",
        "copies:unique",
        "copies:all",
    }
    defaults = {
        "window": "1h",
        "focus": "all",
        "level": "all",
        "lang": "all",
        "copies": "unique",
    }
    for key, href in links.items():
        name, value = key.split(":")
        query = parse_qs(urlsplit(href).query)
        assert query == ({} if value == defaults[name] else {name: [value]})


def test_filter_links_keep_the_other_filters(client):
    links = controls(client.get("/?window=3h&level=state").text)
    query = parse_qs(urlsplit(links["copies:all"]).query)
    assert query == {"window": ["3h"], "level": ["state"], "copies": ["all"]}


def test_state_select_can_be_found_after_a_swap(client):
    page = client.get("/?focus=state:GA").text
    select = re.search(r"<select[^>]*data-filter=\"focus:state\"[^>]*>", page)
    assert select
    assert "data-nav-select" in select[0]
    assert re.search(r'focus%3Dstate%3AGA|focus=state%3AGA"\s+selected', page)


def test_page_gives_the_switch_every_new_url(client):
    query = "window=3h&level=state"
    page = client.get(f"/?{query}").text
    body = re.search(r"<body[^>]*>", page, re.S)[0]
    assert f'sse-connect="/events?{query.replace("&", "&amp;")}"' in body
    assert f'data-qs="{query.replace("&", "&amp;")}"' in body
    assert 'id="main"' in page
    assert '<nav class="controls"' in page
    urls = [
        html.unescape(url) for url in re.findall(r'hx-get="([^"]+)"', page)
    ]
    panels = [url for url in urls if url.startswith("/frag/")]
    assert len(panels) >= 7
    for url in panels:
        if not url.startswith("/frag/status-strip"):
            assert urlsplit(url).query == query


def test_panel_paths_are_unique_so_the_switch_can_pair_them(client):
    page = client.get("/").text
    paths = [
        urlsplit(html.unescape(url)).path
        for url in re.findall(r'hx-get="([^"]+)"', page)
    ]
    assert len(paths) == len(set(paths))


def test_panels_start_empty_so_a_swap_keeps_the_old_content(client):
    page = client.get("/").text
    for tag in re.findall(r"<(?:div|article)[^>]*hx-get=[^>]*>(.?)", page):
        assert tag == "<"


def test_map_paints_fill_as_a_style():
    source = (STATIC / "js/map.js").read_text()
    assert '.attr("fill"' not in source
    assert source.count('.style("fill"') == 5


def test_unpainted_map_uses_the_surface_color():
    css = (STATIC / "css/app.css").read_text()
    for selector in (".map-state", ".map-county"):
        rules = re.findall(re.escape(selector) + r" \{([^}]*)\}", css)
        assert any("fill: var(--surface-2)" in rule for rule in rules)


def test_reduced_motion_removes_the_animation():
    css = (STATIC / "css/app.css").read_text()
    reduced = css[css.index("prefers-reduced-motion: reduce") :]
    assert ".panel-arrive" in reduced
    assert ".map-state" in reduced
    assert "[hx-get]" in reduced
    assert "transition: none" in reduced
    assert "::view-transition-group" in reduced


def test_other_pages_crossfade_unless_motion_is_reduced():
    css = (STATIC / "css/app.css").read_text()
    start = css.index("prefers-reduced-motion: no-preference")
    assert "@view-transition" in css[start : start + 200]
    assert "navigation: auto" in css[start : start + 200]
