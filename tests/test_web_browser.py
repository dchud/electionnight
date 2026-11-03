"""Dashboard behavior that only a browser shows. Run with `pytest -m slow`.

Each test runs a real server on a scratch lake and its own headless browser.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import duckdb
import pytest
from conftest import clone_lake

from monitor.lake import TABLES, BatchWriter, connect
from monitor.web.panels import load_poll_closes
from monitor.web.under import et_label

pytestmark = [pytest.mark.slow, pytest.mark.enable_socket]


def free_port() -> int:
    """A port nothing listens on. Each test process takes its own."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
SERVE = """
import sys
from pathlib import Path
import uvicorn
from monitor.settings import Paths, Settings
from monitor.web.app import create_app
from monitor.web import panels
# Seeded claims are dated now, before polls first close.
panels.load_poll_closes = lambda _config: []
root = Path(sys.argv[1])
settings = Settings(paths=Paths.under(root, config=Path("config")))
uvicorn.run(
    create_app(settings), host="127.0.0.1", port=int(sys.argv[2]),
    log_level="warning",
)
"""


def add_item(settings, item_id, **fields):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    now = datetime.now(UTC)
    source = fields.pop("source", "wire")
    writer.add(
        "raw.items",
        [
            {
                "item_id": item_id,
                "source_id": source,
                "title": f"Headline {item_id}",
                "summary": "Summary.",
                "url": f"https://example.com/{item_id}",
                "first_seen_at": now,
                "published_at": now,
                "language": "en",
                "feed_categories": [],
            }
        ],
    )
    writer.add(
        "nlp.classify_a",
        [
            {
                "item_id": item_id,
                "election_relevant": True,
                "relevance_conf": 0.9,
                "levels": ["state"],
                "primary_level": "state",
                "states": fields.pop("states", ["GA"]),
                "race_types": [],
                "topics": fields.pop("topics", ["turnout"]),
                "free_tags": [],
                "is_opinion": False,
                "is_results_report": False,
            }
        ],
    )
    entity = fields.pop("entity", None)
    if entity:
        writer.add(
            "nlp.mentions",
            [
                {
                    "mention_id": f"m-{item_id}",
                    "item_id": item_id,
                    "surface": entity,
                    "ent_type": "PERSON",
                    "entity_id": entity,
                    "tone": fields.pop("mention_tone", None),
                }
            ],
        )
    tone = fields.pop("tone", None)
    if tone is not None:
        writer.add(
            "nlp.extract_b",
            [
                {
                    "item_id": item_id,
                    "summary_en": "Summary.",
                    "article_tone": tone,
                    "topics_refined": [],
                    "raw": json.dumps({"race_keys": []}),
                }
            ],
        )
    if fields.pop("call", False):
        writer.add(
            "nlp.race_calls",
            [
                {
                    "call_id": f"call-{item_id}",
                    "item_id": item_id,
                    "race_key": "2026-GA-SEN",
                    "winner_name": "Jon Doe",
                    "party": "Democratic",
                    "called_by": "AP",
                    "status": "projected",
                    "state": "GA",
                }
            ],
        )
    writer.flush()
    conn.close()


class Server:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.process = None

    def start(self) -> None:
        env = {**os.environ, "MONITOR_ENV_FILE": os.devnull}
        self.process = subprocess.Popen(
            [sys.executable, "-c", SERVE, str(self.root), str(PORT)],
            env=env,
            stdin=subprocess.DEVNULL,
        )
        for _ in range(120):
            try:
                with urllib.request.urlopen(
                    f"{BASE}/frag/status-strip", timeout=2
                ):
                    return
            except OSError:
                time.sleep(0.25)
        raise AssertionError("the dashboard did not start")

    def kill(self) -> None:
        if self.process is not None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait()
            self.process = None


@pytest.fixture
def scratch(lake_template, tmp_path):
    settings = clone_lake(lake_template, tmp_path)
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "ref.sources",
        [
            {"source_id": "wire", "name": "Wire", "lean_label": "C"},
            {"source_id": "left", "name": "Left", "lean_label": "L"},
        ],
    )
    writer.add(
        "ref.entities",
        [
            {
                "entity_id": "ind",
                "canonical_name": "An Independent",
                "party": "Independent",
            }
        ],
    )
    writer.flush()
    conn.close()
    server = Server(tmp_path)
    yield settings, server
    server.kill()


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        yield launched
        launched.close()


def open_page(browser, path="/?window=3h"):
    context = browser.new_context(viewport={"width": 1920, "height": 1400})
    context.route(
        "**/*",
        lambda route: (
            route.continue_()
            if route.request.url.startswith((BASE, "data:"))
            else route.abort()
        ),
    )
    page = context.new_page()
    page.goto(BASE + path)
    page.wait_for_function("() => window.Monitor && window.Monitor.mapData")
    return context, page


def wait_for_row(page, words, timeout=20000):
    page.wait_for_function(
        "(words) => document.getElementById('wire-panel').innerText"
        ".includes(words)",
        arg=words,
        timeout=timeout,
    )


def test_wire_recovers_stories_from_a_server_restart(scratch, browser):
    settings, server = scratch
    add_item(settings, "before")
    server.start()
    context, page = open_page(browser)
    wait_for_row(page, "Headline before")
    add_item(settings, "live")
    wait_for_row(page, "Headline live")
    server.kill()
    add_item(settings, "during")
    page.wait_for_timeout(1000)
    assert "Headline during" not in page.inner_text("#wire-panel")
    server.start()
    wait_for_row(page, "Headline during")
    add_item(settings, "after")
    wait_for_row(page, "Headline after")
    text = page.inner_text("#wire-panel")
    assert text.count("Headline during") == 1
    context.close()


def test_hidden_tabs_free_their_connections(scratch, browser):
    settings, server = scratch
    add_item(settings, "seed")
    server.start()
    context, first = open_page(browser)
    hide = """
    () => {
      Object.defineProperty(document, "hidden", {
        value: true, configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    }
    """
    first.evaluate(hide)
    pages = [first]
    for _ in range(6):
        page = context.new_page()
        page.goto(f"{BASE}/?window=3h")
        page.wait_for_function("() => window.Monitor.mapData")
        page.evaluate(hide)
        pages.append(page)
    status = first.evaluate(
        """
        async () => {
          const stop = new AbortController();
          setTimeout(() => stop.abort(), 10000);
          const response = await fetch("/frag/kpis", { signal: stop.signal });
          return response.status;
        }
        """
    )
    assert status == 200
    first.evaluate(
        """
        () => {
          Object.defineProperty(document, "hidden", {
            value: false, configurable: true,
          });
          document.dispatchEvent(new Event("visibilitychange"));
        }
        """
    )
    add_item(settings, "on-return")
    wait_for_row(first, "Headline on-return")
    context.close()


def wait_settled(page):
    page.wait_for_function(
        """
        () => !document.querySelector(".panel-dim")
          && window.Monitor.mapData
          && window.Monitor.charts.every((chart) => !chart.running)
        """
    )
    page.wait_for_timeout(200)


def shown_filters(page):
    return page.evaluate(
        """
        () => ({
          search: location.search,
          qs: document.body.dataset.qs,
          wire: document.getElementById("wire").getAttribute("hx-get"),
          stream: document.body.getAttribute("sse-connect"),
          level: document
            .querySelector('[data-filter="level:state"]')
            .getAttribute("aria-current"),
        })
        """
    )


def test_back_then_forward_during_a_switch_shows_the_url(scratch, browser):
    settings, server = scratch
    add_item(settings, "seed")
    server.start()
    context, page = open_page(browser)
    wait_settled(page)
    page.locator('[data-filter="level:state"]').click()
    page.wait_for_url(lambda url: "level=state" in url)
    wait_settled(page)

    def slow(route):
        if route.request.resource_type == "fetch":
            time.sleep(0.8)
        route.continue_()

    page.route("**/?*", slow)
    page.evaluate(
        "() => { history.back(); setTimeout(() => history.forward(), 150); }"
    )
    page.wait_for_timeout(2500)
    page.unroute("**/?*")
    wait_settled(page)
    shown = shown_filters(page)
    assert shown["search"] == "?window=3h&level=state"
    assert shown["qs"] == "window=3h&level=state"
    assert "level=state" in shown["wire"]
    assert "level=state" in shown["stream"]
    assert shown["level"] == "true"
    context.close()


HIDDEN = """
(hidden) => {
  Object.defineProperty(document, "hidden", {
    value: hidden, configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}
"""


def test_a_switch_landing_in_a_hidden_tab_still_refreshes_on_return(
    scratch, browser
):
    settings, server = scratch
    add_item(settings, "seed")
    server.start()
    context, page = open_page(browser)
    wait_settled(page)

    def slow(route):
        if route.request.resource_type == "fetch":
            time.sleep(0.5)
        route.continue_()

    page.route("**/?*", slow)
    page.locator('[data-filter="level:state"]').click()
    page.evaluate(HIDDEN, True)
    page.wait_for_url(lambda url: "level=state" in url)
    page.unroute("**/?*")
    wait_settled(page)
    asked = []
    page.on("request", lambda request: asked.append(request.url))
    page.evaluate(HIDDEN, False)
    page.wait_for_timeout(1500)
    assert any("/frag/kpis" in url for url in asked), asked
    context.close()


STALE = """
(selector) => {
  const panel = document.querySelector(selector);
  const note = panel && panel.querySelector(":scope > .stale-note");
  return note ? note.innerText : null;
}
"""


def test_a_panel_that_fails_after_a_switch_says_so(scratch, browser):
    settings, server = scratch
    add_item(settings, "seed")
    server.start()
    context, page = open_page(browser)
    wait_settled(page)
    panel = "#panel-rising .panel-body"
    page.route("**/frag/rising*", lambda route: route.fulfill(status=500))
    page.locator('[data-filter="level:national"]').click()
    page.wait_for_url(lambda url: "level=national" in url)
    page.wait_for_function(f"{STALE}", arg=panel)
    assert "previous filters" in page.evaluate(STALE, panel)
    assert not page.evaluate(
        f"() => document.querySelector('{panel}')"
        ".classList.contains('panel-dim')"
    )
    page.unroute("**/frag/rising*")
    page.evaluate(
        f"() => window.htmx.trigger(document.querySelector('{panel}'), "
        '"sse:snapshot")'
    )
    page.wait_for_function(f"(s) => !({STALE})(s)", arg=panel)
    context.close()


def test_a_panel_with_no_answer_after_a_switch_says_so(scratch, browser):
    settings, server = scratch
    add_item(settings, "seed")
    server.start()
    context, page = open_page(browser)
    wait_settled(page)
    panel = "#panel-rising .panel-body"
    held = []
    page.route("**/frag/rising*", lambda route: held.append(route))
    page.locator('[data-filter="level:national"]').click()
    page.wait_for_url(lambda url: "level=national" in url)
    page.wait_for_function(f"{STALE}", arg=panel, timeout=12000)
    for route in held:
        route.continue_()
    page.wait_for_function(f"(s) => !({STALE})(s)", arg=panel)
    page.unroute("**/frag/rising*")
    context.close()


def test_a_chart_and_the_map_that_fail_after_a_switch_say_so(scratch, browser):
    settings, server = scratch
    add_item(settings, "seed")
    server.start()
    context, page = open_page(browser)
    wait_settled(page)
    page.route(
        "**/api/data/**",
        lambda route: (
            route.fulfill(status=500)
            if "level=national" in route.request.url
            else route.continue_()
        ),
    )
    page.locator('[data-filter="level:national"]').click()
    page.wait_for_url(lambda url: "level=national" in url)
    chart = "#panel-heat .panel-chart"
    page.wait_for_function(f"{STALE}", arg=chart)
    page.wait_for_function(f"{STALE}", arg="#us-map")
    page.unroute("**/api/data/**")
    page.locator("[data-toggle-theme]").click()
    page.wait_for_function(f"(s) => !({STALE})(s)", arg=chart)
    page.wait_for_function(f"(s) => !({STALE})(s)", arg="#us-map")
    context.close()


def test_race_call_and_heat_clicks_open_their_counts(scratch, browser):
    settings, server = scratch
    add_item(settings, "claim", call=True)
    add_item(settings, "claim2", call=True, source="left")
    server.start()
    context, page = open_page(browser)
    page.locator("#panel-calls tr[data-evidence]").click()
    heading = page.locator("#drawer-body h2")
    heading.wait_for()
    assert heading.inner_text().startswith("2 stories · 2 outlets")
    page.keyboard.press("Escape")
    rect = page.locator('[data-chart="topic_heat"] g.mark-rect path').first
    rect.scroll_into_view_if_needed()
    rect.click()
    page.wait_for_function(
        "() => document.getElementById('drawer-body').innerText"
        ".includes('turnout')"
    )
    assert page.locator("#drawer-body h2").inner_text().startswith("2 stories")
    context.close()


def test_independent_has_a_line_on_the_bump_chart(scratch, browser):
    settings, server = scratch
    for number in range(3):
        add_item(settings, f"i{number}", entity="ind")
    server.start()
    context, page = open_page(browser)
    page.wait_for_selector('[data-chart="bump"] g.mark-symbol path')
    fills = page.evaluate(
        """
        () => [...document.querySelectorAll(
          '[data-chart="bump"] g.mark-symbol path[aria-label*="Rank"]'
        )].map((path) => path.getAttribute("fill"))
        """
    )
    assert fills
    assert all(fill and fill != "none" for fill in fills)
    context.close()


def test_a_bump_point_answers_to_the_pointer_beside_its_dot(scratch, browser):
    settings, server = scratch
    for number in range(3):
        add_item(settings, f"i{number}", entity="ind")
    server.start()
    context, page = open_page(browser)
    selector = '[data-chart="bump"] g.mark-symbol path[aria-label*="Rank"]'
    page.wait_for_selector(selector)
    page.locator(selector).first.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    dot = page.evaluate(
        """
        (selector) => {
          const path = [...document.querySelectorAll(selector)]
            .find((node) => node.getAttribute("fill") !== "transparent");
          const box = path.getBoundingClientRect();
          return {
            x: box.x + box.width / 2,
            y: box.y + box.height / 2,
            width: box.width,
          };
        }
        """,
        selector,
    )
    assert dot["width"] < 8
    page.mouse.move(dot["x"] + 6, dot["y"])
    page.wait_for_timeout(300)
    assert "Rank" in page.locator("#vg-tooltip-element").inner_text()
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_heat_cells_run_from_the_first_to_the_last_stop(
    scratch, browser, theme
):
    settings, server = scratch
    add_item(settings, "i0")
    server.start()
    start = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    table = [
        {
            "topic": "turnout",
            "bin_start": (start + timedelta(minutes=10 * n)).isoformat(),
            "bin_end": (start + timedelta(minutes=10 * n + 10)).isoformat(),
            "n": n + 1,
            "total": 15,
        }
        for n in range(5)
    ]
    context, page = open_page(browser)
    page.route(
        "**/api/data/topic_heat*",
        lambda route: route.fulfill(json={"table": table, "rules": []}),
    )
    page.evaluate(f"() => localStorage.setItem('theme', '{theme}')")
    page.reload()
    selector = (
        '[data-chart="topic_heat"] g.mark-rect path[aria-label*="Stories"]'
    )
    page.wait_for_selector(selector)
    found = page.evaluate(
        """
        (selector) => {
          const stops = window.Monitor.heatStops()
            .map((stop) => window.d3.rgb(stop).toString());
          const fills = {};
          document.querySelectorAll(selector).forEach((path) => {
            const count = /Stories: (\\d+)/
              .exec(path.getAttribute("aria-label"))[1];
            fills[count] = window.d3.rgb(path.getAttribute("fill")).toString();
          });
          return {stops, fills};
        }
        """,
        selector,
    )
    assert found["fills"]["1"] == found["stops"][0]
    assert found["fills"]["5"] == found["stops"][4]
    context.close()


def test_the_volume_map_tops_out_at_the_last_heat_stop(scratch, browser):
    settings, server = scratch
    for number in range(3):
        add_item(settings, f"i{number}")
    server.start()
    context, page = open_page(browser)
    page.locator('#map-metric button[data-metric="volume"]').click()
    page.wait_for_timeout(600)
    top, fills = page.evaluate(
        """
        () => [
          window.d3.rgb(window.Monitor.heatStops()[4]).toString(),
          [...document.querySelectorAll("#us-map .map-state")]
            .map((path) => window.d3.rgb(path.style.fill).toString()),
        ]
        """
    )
    assert top in fills
    context.close()


def test_a_selected_control_has_a_tint_a_ring_and_its_text_color(
    scratch, browser
):
    settings, server = scratch
    add_item(settings, "i0")
    server.start()
    context, page = open_page(browser)
    found = page.evaluate(
        """
        () => {
          const root = getComputedStyle(document.documentElement);
          const color = (name) => window.d3
            .rgb(root.getPropertyValue(name).trim()).toString();
          const links = [...document.querySelectorAll(
            '.control-group a[aria-current="true"]'
          )];
          return {
            count: links.length,
            bad: links.filter((link) => {
              const style = getComputedStyle(link);
              return window.d3.rgb(style.backgroundColor).toString()
                  !== color("--selected-bg")
                || window.d3.rgb(style.color).toString()
                  !== color("--selected-text")
                || !style.boxShadow.includes(color("--accent"));
            }).length,
          };
        }
        """
    )
    assert found["count"] >= 2
    assert found["bad"] == 0
    context.close()


def test_selecting_a_control_keeps_its_font_weight(scratch, browser):
    settings, server = scratch
    add_item(settings, "i0")
    server.start()
    context, page = open_page(browser)
    weights = page.evaluate(
        """
        () => [...document.querySelectorAll(".control-group a")]
          .map((link) => getComputedStyle(link).fontWeight)
        """
    )
    assert len(set(weights)) == 1
    context.close()


def test_hover_keeps_the_tint_on_the_current_methods_section(scratch, browser):
    _, server = scratch
    server.start()
    context = browser.new_context(viewport={"width": 1920, "height": 1400})
    page = context.new_page()
    page.goto(f"{BASE}/methods")
    current = page.locator('.doc-nav a[aria-current="true"]').first
    current.wait_for()
    before = current.evaluate("el => getComputedStyle(el).backgroundColor")
    current.hover()
    page.wait_for_timeout(100)
    after = current.evaluate("el => getComputedStyle(el).backgroundColor")
    assert before == after
    context.close()


def test_the_projector_key_repaints_tone_dots_on_rows_already_shown(
    scratch, browser
):
    settings, server = scratch
    add_item(settings, "toned", tone=-1.5)
    server.start()
    context, page = open_page(browser)
    wait_for_row(page, "Headline toned")
    dot = page.locator("#wire-panel .tone-dot").first
    before = dot.evaluate("el => getComputedStyle(el).backgroundColor")
    page.keyboard.press("p")
    page.wait_for_timeout(100)
    after = dot.evaluate("el => getComputedStyle(el).backgroundColor")
    assert before != after
    context.close()


def test_a_casing_lies_under_every_battleground_outline(scratch, browser):
    settings, server = scratch
    add_item(settings, "i0")
    server.start()
    context, page = open_page(browser)
    page.route(
        "**/api/data/map*",
        lambda route: route.fulfill(
            json={**route.fetch().json(), "battleground": ["GA", "PA"]}
        ),
    )
    page.reload()
    page.wait_for_selector("#us-map path.map-outline")
    found = page.evaluate(
        """
        () => {
          const first = document.querySelector("#us-map .map-outline-casing");
          const last = document.querySelector("#us-map .map-outline");
          return {
            casings: document
              .querySelectorAll("#us-map .map-outline-casing").length,
            outlines: document.querySelectorAll("#us-map .map-outline").length,
            order: first.compareDocumentPosition(last)
              & Node.DOCUMENT_POSITION_FOLLOWING,
          };
        }
        """
    )
    assert found["casings"] == found["outlines"] == 2
    assert found["order"]
    context.close()


def test_map_tooltip_gives_the_toned_count_and_flags_low_n(scratch, browser):
    settings, server = scratch
    for number in range(6):
        add_item(settings, f"t{number}", tone=1.0)
    for number in range(4):
        add_item(settings, f"u{number}")
    server.start()
    context, page = open_page(browser)
    page.locator('#map-metric button[data-metric="tone"]').click()
    page.wait_for_timeout(600)
    page.locator('path.map-state[data-usps="GA"]').hover(force=True)
    page.wait_for_timeout(250)
    tip = page.locator("#map-readout").inner_text()
    assert re.search(
        r"Georgia · Mean tone 1\.00 · 6 toned stories · low n", tip
    )
    context.close()


def test_map_readout_stays_in_place_and_says_what_attention_means(
    scratch, browser
):
    settings, server = scratch
    add_item(settings, "ga", states=["GA"])
    add_item(settings, "oh", states=["OH"])
    server.start()
    context, page = open_page(browser)
    readout = page.locator("#map-readout")
    page.wait_for_selector("path.map-state")
    assert "1.00 is normal" in readout.inner_text()
    start = readout.bounding_box()
    page.locator('path.map-state[data-usps="GA"]').hover(force=True)
    page.wait_for_timeout(250)
    assert "Georgia · Attention" in readout.inner_text()
    assert re.search(r"(above|below) normal|normal", readout.inner_text())
    first = readout.bounding_box()
    page.locator('path.map-state[data-usps="OH"]').hover(force=True)
    page.wait_for_timeout(250)
    assert "Ohio" in readout.inner_text()
    assert readout.bounding_box() == first == start
    assert not page.locator("#tip").is_visible()
    page.mouse.move(5, 5)
    page.wait_for_timeout(500)
    assert "1.00 is normal" in readout.inner_text()
    bar = page.locator(".map-legend-bar").bounding_box()
    words = page.locator(".map-legend-row").nth(1).locator("span")
    assert words.nth(0).inner_text() == "below normal"
    assert words.nth(1).inner_text() == "above normal"
    right = words.nth(1).bounding_box()
    assert abs(right["x"] + right["width"] - (bar["x"] + bar["width"])) < 2
    context.close()


def test_county_map_uses_a_fixed_readout(scratch, browser):
    settings, server = scratch
    add_item(settings, "ga", states=["GA"])
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "ref.states",
        [{"usps": "GA", "fips": "13", "name": "Georgia"}],
    )
    writer.flush()
    conn.close()
    server.start()
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    page.goto(f"{BASE}/state/GA?window=24h")
    # The hover handlers are bound once the counties are painted.
    page.wait_for_selector("svg.map-painted path.map-county", state="attached")
    readout = page.locator(".map-readout").first
    page.locator("path.map-county").first.hover(force=True)
    page.wait_for_timeout(250)
    assert " · " in readout.inner_text()
    assert "stor" in readout.inner_text()
    assert not page.locator("#tip").is_visible()
    context.close()


def test_bucket_without_coverage_draws_no_cell(scratch, browser):
    settings, server = scratch
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "nlp.storylines",
        [
            {
                "storyline_id": "s1",
                "version": 1,
                "label": "A storyline",
                "first_seen_at": datetime.now(UTC),
                "last_item_at": datetime.now(UTC),
                "status": "active",
            }
        ],
    )
    writer.add(
        "raw.items",
        [
            {
                "item_id": "cell",
                "source_id": "wire",
                "title": "Cell",
                "summary": "",
                "url": "https://example.com/cell",
                "first_seen_at": datetime.now(UTC),
                "published_at": datetime.now(UTC),
                "language": "en",
                "feed_categories": [],
            }
        ],
    )
    writer.add(
        "nlp.classify_a",
        [
            {
                "item_id": "cell",
                "election_relevant": True,
                "relevance_conf": 0.9,
                "levels": ["state"],
                "primary_level": "state",
                "states": [],
                "race_types": [],
                "topics": [],
                "free_tags": [],
                "is_opinion": False,
                "is_results_report": False,
            }
        ],
    )
    writer.add(
        "nlp.storyline_assign",
        [
            {
                "item_id": "cell",
                "storyline_id": "s1",
                "similarity": 0.95,
                "assigned_at": datetime.now(UTC),
            }
        ],
    )
    writer.flush()
    conn.close()
    server.start()
    context, page = open_page(browser)
    page.wait_for_selector('[data-chart="coverage_matrix"] g.mark-rect path')
    labels = page.evaluate(
        """
        () => [...document.querySelectorAll(
          '[data-chart="coverage_matrix"] g.mark-rect path'
        )].map((path) => path.getAttribute("aria-label"))
        """
    )
    cells = [label for label in labels if label]
    assert len(cells) == 1
    assert "Bucket: C" in cells[0]
    axis = page.inner_text('[data-chart="coverage_matrix"]')
    for bucket in ("L", "LL", "C", "LR", "R", "Non-US"):
        assert bucket in axis
    context.close()


def test_panels_follow_new_data_under_the_filter_chosen_in_place(
    scratch, browser
):
    settings, server = scratch
    add_item(settings, "start", states=["GA"])
    server.start()
    context, page = open_page(browser)
    page.evaluate("() => { window.__samePage = true; }")
    page.locator('[data-filter="level:local"]').click()
    page.wait_for_url(lambda url: "level=local" in url)
    page.locator('[data-filter="level:state"]').click()
    page.wait_for_url(lambda url: "level=state" in url)
    page.wait_for_function(
        "() => document.getElementById('wire-panel').innerText"
        ".includes('Headline start')"
    )
    page.select_option('[data-filter="focus:state"]', label="GA")
    page.wait_for_url(lambda url: "focus=state" in url)
    page.wait_for_timeout(1500)
    add_item(settings, "ohio", states=["OH"])
    for number in range(3):
        add_item(settings, f"georgia{number}", states=["GA"])
    wait_for_row(page, "Headline georgia2")
    page.wait_for_function(
        "() => document.querySelector('.kpis .kpi-value').innerText !== '0.1'"
    )
    assert "Headline ohio" not in page.inner_text("#wire-panel")
    assert page.evaluate("() => window.__samePage === true")
    context.close()


def test_county_map_is_never_black(scratch, browser):
    settings, server = scratch
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "ref.states",
        [
            {
                "usps": "GA",
                "fips": "13",
                "name": "Georgia",
                "battleground": True,
            }
        ],
    )
    writer.flush()
    conn.close()
    server.start()
    context = browser.new_context()
    context.route(
        "**/*",
        lambda route: (
            route.continue_()
            if route.request.url.startswith((BASE, "data:"))
            else route.abort()
        ),
    )
    page = context.new_page()
    page.add_init_script(
        """
        window.__black = false;
        setInterval(() => {
          document.querySelectorAll("path.map-county").forEach((path) => {
            if (getComputedStyle(path).fill === "rgb(0, 0, 0)") {
              window.__black = true;
            }
          });
        }, 20);
        """
    )
    page.goto(f"{BASE}/state/GA")
    page.wait_for_selector("svg.map-painted path.map-county", state="attached")
    assert page.evaluate("() => window.__black") is False
    context.close()


def test_chips_explain_themselves_at_once(scratch, browser):
    settings, server = scratch
    add_item(settings, "chip", states=["GA"])
    server.start()
    context, page = open_page(browser)
    wait_for_row(page, "Headline chip")
    chip = page.locator('#wire-panel .chip:has-text("GA")').first
    tip = page.locator("#tip")
    assert chip.evaluate("el => getComputedStyle(el).cursor") != "help"
    chip.hover()
    page.wait_for_timeout(50)
    assert tip.is_visible()
    assert tip.inner_text() == "Georgia"
    # Above the chip, not under the pointer.
    box, shown = chip.bounding_box(), tip.bounding_box()
    assert shown["y"] + shown["height"] <= box["y"]
    page.locator("#wire-panel .wire-head").first.hover()
    page.wait_for_timeout(50)
    assert not tip.is_visible()
    chip.hover()
    page.wait_for_timeout(50)
    assert tip.is_visible()
    assert tip.inner_text() == "Georgia"
    context.close()


def open_methods(browser, projector=False, width=1280, height=720):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(f"{BASE}/methods")
    if projector:
        page.keyboard.press("p")
    page.wait_for_selector("[data-doc-nav]")
    return page


def marked(page):
    return page.eval_on_selector_all(
        '[data-doc-nav] a[aria-current="true"]',
        "links => links.map(link => link.getAttribute('href'))",
    )


def choose(page, href):
    page.evaluate(
        "href => document.querySelector("
        '`[data-doc-nav] a[href="${href}"]`).click()',
        href,
    )
    page.wait_for_timeout(150)


def in_view(page):
    """The heading at the line the script marks by, read from the page."""
    return page.evaluate(
        """() => {
          const line = document.querySelector('.app-header')
            .getBoundingClientRect().bottom + 40;
          let current = null;
          for (const link of document.querySelectorAll('[data-doc-nav] a')) {
            const element = document.getElementById(link.hash.slice(1));
            if (element && element.getBoundingClientRect().top <= line) {
              current = link.getAttribute('href');
            }
          }
          return current;
        }"""
    )


@pytest.mark.parametrize("projector", [False, True])
def test_methods_navigation_marks_the_section_in_view(
    scratch, browser, projector
):
    _, server = scratch
    server.start()
    page = open_methods(browser, projector)
    page.mouse.move(800, 300)
    for _ in range(12):
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(100)
        assert marked(page) == [in_view(page)]
    assert page.evaluate(
        "document.documentElement.scrollWidth "
        "<= document.documentElement.clientWidth"
    )


def test_methods_navigation_reaches_every_heading_below_the_header(
    scratch, browser
):
    _, server = scratch
    server.start()
    page = open_methods(browser)
    hrefs = page.eval_on_selector_all(
        "[data-doc-nav] a",
        "links => links.map(link => link.getAttribute('href'))",
    )
    assert len(hrefs) > 30
    for href in hrefs:
        choose(page, href)
        assert marked(page) == [href]
        top, header = page.evaluate(
            """href => [
              document.getElementById(href.slice(1))
                .getBoundingClientRect().top,
              document.querySelector('.app-header')
                .getBoundingClientRect().bottom,
            ]""",
            href,
        )
        assert top >= header - 1, href


def test_a_chosen_section_lets_go_when_the_page_moves(scratch, browser):
    _, server = scratch
    server.start()
    page = open_methods(browser)
    choose(page, "#storylines")
    assert marked(page) == ["#storylines"]
    # A scrollbar drag moves the page with no wheel or key event.
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(200)
    assert marked(page) == ["#from-feed-to-screen"]


def test_a_section_at_the_end_stays_marked_after_a_click(scratch, browser):
    _, server = scratch
    server.start()
    page = open_methods(browser)
    choose(page, "#in-service")
    page.wait_for_timeout(200)
    assert marked(page) == ["#in-service"]


def test_the_back_button_moves_the_mark_with_the_hash(scratch, browser):
    _, server = scratch
    server.start()
    page = open_methods(browser)
    choose(page, "#dedupe")
    choose(page, "#cost")
    assert marked(page) == ["#cost"]
    page.go_back()
    page.wait_for_timeout(300)
    assert page.evaluate("window.location.hash") == "#dedupe"
    assert marked(page) == ["#dedupe"]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("projector", [False, True])
@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080)])
def test_the_table_map_reads_at_a_glance(
    scratch, browser, theme, projector, width, height
):
    _, server = scratch
    server.start()
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(f"{BASE}/under")
    page.evaluate(
        """([theme, projector]) => {
          localStorage.setItem('theme', theme);
          localStorage.setItem('projector', projector ? '1' : '0');
        }""",
        [theme, projector],
    )
    page.goto(f"{BASE}/under/lake")
    found = page.evaluate(
        """() => {
          const svg = document.querySelector('.tm-wrap svg');
          const box = svg.getBoundingClientRect();
          const view = svg.viewBox.baseVal;
          const scale = Math.min(
            box.width / view.width, box.height / view.height);
          const texts = [...svg.querySelectorAll('text')]
            .filter((t) => t.textContent)
            .map((t) => ({
              text: t.textContent, box: t.getBBox(),
              size: parseFloat(getComputedStyle(t).fontSize) * scale,
            }));
          const overlaps = [];
          for (let i = 0; i < texts.length; i++) {
            for (let j = i + 1; j < texts.length; j++) {
              const a = texts[i].box, b = texts[j].box;
              if (a.x < b.x + b.width && b.x < a.x + a.width
                  && a.y < b.y + b.height - 2 && b.y < a.y + a.height - 2) {
                overlaps.push([texts[i].text, texts[j].text]);
              }
            }
          }
          const outside = texts.filter((t) => (
            t.box.x < 0 || t.box.y < 0
            || t.box.x + t.box.width > view.width
            || t.box.y + t.box.height > view.height
          )).map((t) => t.text);
          const rows = [...svg.querySelectorAll('a')].map((a) => {
            const row = a.querySelector('rect').getBBox();
            const inRow = [...a.querySelectorAll('text')]
              .filter((t) => t.textContent)
              .map((t) => t.getBBox());
            return {
              name: a.getAttribute('aria-label'),
              fits: inRow.every((t) => (
                t.x >= row.x && t.x + t.width <= row.x + row.width
                && t.y >= row.y - 1 && t.y + t.height <= row.y + row.height + 1
              )),
            };
          });
          return {
            overlaps,
            outside,
            tooWide: rows.filter((row) => !row.fits).map((row) => row.name),
            height: box.height,
            header: document.querySelector('.u-header')
              .getBoundingClientRect().height,
            viewport: innerHeight,
            smallest: Math.min(...texts.map((t) => t.size)),
            tableName: Math.min(...[...svg.querySelectorAll('.tm-name')]
              .map((t) => parseFloat(getComputedStyle(t).fontSize) * scale)),
            pageWide: document.documentElement.scrollWidth
              <= document.documentElement.clientWidth,
            links: svg.querySelectorAll('a').length,
          };
        }"""
    )
    assert found["overlaps"] == []
    assert found["outside"] == []
    assert found["tooWide"] == []
    assert found["height"] + found["header"] <= found["viewport"]
    assert found["tableName"] >= 14
    assert found["smallest"] >= 12.8
    assert found["pageWide"]
    assert found["links"] == len(TABLES)


def test_a_map_row_opens_the_table_browser_entry(scratch, browser):
    _, server = scratch
    server.start()
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto(f"{BASE}/under/lake")
    page.locator('.tm-wrap a[aria-label="nlp.mentions"]').click()
    page.wait_for_load_state()
    assert "table=nlp.mentions" in page.url
    assert page.locator("#detail h2").first.inner_text() == "nlp.mentions"


def test_a_wide_table_of_newest_rows_scrolls_inside_itself(scratch, browser):
    settings, server = scratch
    add_item(settings, "seed")
    server.start()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"{BASE}/under/lake?table=raw.items")
    frame = page.locator("#detail .u-scroll:has(.u-rows-table)").last
    frame.wait_for()
    assert frame.evaluate("(node) => node.scrollWidth > node.clientWidth")
    assert frame.evaluate("(node) => getComputedStyle(node).overflowX") == (
        "auto"
    )
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth"
    )


def test_a_copied_sql_block_is_the_statement_that_ran(scratch, browser):
    _, server = scratch
    server.start()
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto(f"{BASE}/under/lake?table=raw.items")
    copied = page.evaluate(
        """() => {
          const block = document.querySelector('pre.u-sql');
          const details = block.closest('details');
          if (details) details.open = true;
          const range = document.createRange();
          range.selectNodeContents(block);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          return selection.toString();
        }"""
    )
    assert copied.strip().startswith("SELECT")
    assert len(duckdb.connect().extract_statements(copied)) == 1


def test_a_copied_echo_parses_the_same_as_what_was_typed(scratch, browser):
    _, server = scratch
    server.start()
    typed = (
        "SELECT $q$ one FROM two WHERE three $q$ AS kept, E'it\\'s FROM x' AS "
        "also_kept, 1 AS padding -- ' FROM comment WHERE x = 0"
    )
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto(f"{BASE}/under/sql")
    page.fill("textarea[name=sql]", typed)
    page.click("button[type=submit]")
    page.wait_for_load_state()
    copied = page.evaluate(
        """() => {
          const range = document.createRange();
          range.selectNodeContents(document.querySelector('pre.u-sql'));
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          return selection.toString();
        }"""
    )
    assert copied == typed


def rows_scratch(settings, count=60):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "raw.items",
        [
            {
                "item_id": f"row-{n:03}",
                "source_id": "wire",
                "title": f"Headline {n}",
                "summary": "s" * 200 + "PASTTHECUT",
                "url": f"https://example.com/{n}",
                "first_seen_at": datetime.now(UTC),
            }
            for n in range(count)
        ],
    )
    writer.flush()
    conn.close()


def projector_page(browser, width=1280, height=720, permissions=(), script=""):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        permissions=list(permissions),
    )
    context.route(
        "**/*",
        lambda route: (
            route.continue_()
            if route.request.url.startswith((BASE, "data:"))
            else route.abort()
        ),
    )
    context.add_init_script(
        "try { localStorage.setItem('projector', '1'); } catch (error) {}"
        + script
    )
    return context, context.new_page()


def test_rows_expand_page_collapse_and_survive_a_refresh(scratch, browser):
    settings, server = scratch
    rows_scratch(settings)
    server.start()
    _, page = projector_page(browser)
    page.goto(f"{BASE}/under/data-model?table=raw.items")
    recent = page.locator(
        '[data-rows-open="recent"][data-rows-table="raw.items"]'
    )
    body = page.locator('[data-rows-body="raw.items"]')
    assert not body.is_visible()
    url = page.url
    recent.click()
    body.locator("table").wait_for()
    assert page.url == url
    assert recent.get_attribute("aria-expanded") == "true"
    assert "Rows 1 to 25 of 60" in body.inner_text()
    assert body.locator("button", has_text="Newer").is_disabled()
    # A wide table scrolls in the panel, so its last columns can be reached.
    assert (
        body.locator(".u-scroll").evaluate(
            "(box) => getComputedStyle(box).overflowX"
        )
        == "auto"
    )
    assert "_file" in body.locator("thead").inner_text()
    assert "PASTTHECUT" not in body.inner_text()
    assert "cut" in body.locator("table").inner_text()

    body.locator("button", has_text="Older").click()
    page.wait_for_function(
        "() => document.querySelector('[data-rows-body]')"
        ".innerText.includes('Rows 26 to 50')"
    )
    page.locator("[data-under-refresh]").first.click()
    page.wait_for_timeout(1500)
    body = page.locator('[data-rows-body="raw.items"]')
    assert "Rows 26 to 50 of 60" in body.inner_text()
    recent = page.locator(
        '[data-rows-open="recent"][data-rows-table="raw.items"]'
    )
    assert recent.get_attribute("aria-expanded") == "true"

    body.locator("button", has_text="Older").click()
    page.wait_for_function(
        "() => document.querySelector('[data-rows-body]')"
        ".innerText.includes('Rows 51 to 60')"
    )
    assert body.locator("button", has_text="Older").is_disabled()
    assert page.evaluate(
        "() => document.documentElement.scrollWidth"
        " <= document.documentElement.clientWidth"
    )

    recent.click()
    page.wait_for_timeout(100)
    assert not body.is_visible()
    assert recent.get_attribute("aria-expanded") == "false"
    assert body.inner_text() == ""

    sample = page.locator(
        '[data-rows-open="sample"][data-rows-table="raw.items"]'
    )
    sample.click()
    body.locator("table").wait_for()
    first = body.locator("table").inner_text()
    assert "Sample 1:" in body.inner_text()
    body.locator("button", has_text="Another sample").click()
    page.wait_for_function(
        "() => document.querySelector('[data-rows-body]')"
        ".innerText.includes('Sample 2:')"
    )
    assert body.locator("table").inner_text() != first
    body.locator("button", has_text="Previous sample").click()
    page.wait_for_function(
        "() => document.querySelector('[data-rows-body]')"
        ".innerText.includes('Sample 1:')"
    )
    assert body.locator("table").inner_text() == first
    body.locator("button", has_text="Close").click()
    page.wait_for_timeout(100)
    assert not body.is_visible()


def test_rows_open_inside_the_inventory_table(scratch, browser):
    settings, server = scratch
    rows_scratch(settings)
    server.start()
    _, page = projector_page(browser)
    page.goto(f"{BASE}/under/inventory")
    page.locator(
        '[data-rows-open="recent"][data-rows-table="raw.items"]'
    ).click()
    body = page.locator('[data-rows-body="raw.items"]')
    body.locator("table").wait_for()
    assert "Rows 1 to 25 of 60" in body.inner_text()
    assert page.evaluate(
        "() => document.documentElement.scrollWidth"
        " <= document.documentElement.clientWidth"
    )


def copied_block(page):
    return page.evaluate(
        """() => document.querySelector('pre.u-sql').textContent"""
    ).strip()


def serialized(sql):
    text = (
        duckdb.connect()
        .execute("SELECT json_serialize_sql(?)", [sql])
        .fetchone()[0]
    )
    tree = json.loads(text)

    def clean(value):
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if not key.startswith("query_location")
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(tree)


def test_the_copy_button_copies_the_query_text_only(scratch, browser):
    settings, server = scratch
    rows_scratch(settings)
    server.start()
    _, page = projector_page(
        browser, permissions=("clipboard-read", "clipboard-write")
    )
    page.goto(f"{BASE}/under/data-model?table=raw.items")
    page.locator(
        '[data-rows-open="recent"][data-rows-table="raw.items"]'
    ).click()
    body = page.locator('[data-rows-body="raw.items"]')
    body.locator("table").wait_for()
    button = body.locator("[data-copy-sql]")
    shown = body.locator("pre.u-sql").evaluate("(pre) => pre.textContent")
    button.click()
    page.wait_for_function(
        "() => document.querySelector('[data-rows-body] [data-copy-sql]')"
        ".textContent === 'Copied'"
    )
    pasted = page.evaluate("() => navigator.clipboard.readText()")
    assert pasted == shown.strip()
    assert "Copy" not in pasted
    assert pasted.startswith("SELECT")
    assert serialized(pasted) == serialized(shown)
    assert serialized(pasted)["error"] is False
    page.wait_for_function(
        "() => document.querySelector('[data-rows-body] [data-copy-sql]')"
        ".textContent === 'Copy'"
    )


def test_the_copy_button_falls_back_without_the_clipboard_api(
    scratch, browser
):
    _, server = scratch
    server.start()
    _, page = projector_page(
        browser,
        script="""
        Object.defineProperty(navigator, 'clipboard', {value: undefined});
        document.addEventListener('copy', () => {
          const box = document.activeElement;
          window.copiedText = box.value;
        });
        """,
    )
    page.goto(f"{BASE}/under/queries")
    page.locator("details.u-query[open] [data-copy-sql]").first.click()
    page.wait_for_function("() => window.copiedText !== undefined")
    copied = page.evaluate("() => window.copiedText")
    assert copied == copied_block(page)
    assert (
        "Copied"
        in page.locator(
            "details.u-query[open] [data-copy-sql]"
        ).first.inner_text()
    )


@pytest.mark.parametrize(
    ("width", "height", "projector"),
    [(1280, 720, True), (1920, 1080, False)],
)
def test_a_card_link_stays_clear_of_its_header(
    scratch, browser, width, height, projector
):
    settings, server = scratch
    add_item(settings, "linked", entity="ind")
    server.start()
    context = browser.new_context(viewport={"width": width, "height": height})
    context.route(
        "**/*",
        lambda route: (
            route.continue_()
            if route.request.url.startswith((BASE, "data:"))
            else route.abort()
        ),
    )
    if projector:
        context.add_init_script(
            "try { localStorage.setItem('projector', '1'); } catch (e) {}"
        )
    page = context.new_page()
    page.goto(f"{BASE}/?window=3h")
    page.wait_for_function("() => window.Monitor && window.Monitor.mapData")
    found = page.evaluate(
        """() => [...document.querySelectorAll('[data-card-link]')].map(
          (link) => {
            const box = link.getBoundingClientRect();
            const head = link.closest('.panel-head');
            const room = head.getBoundingClientRect();
            const hits = [...head.children].filter((other) => {
              if (other === link) return false;
              const b = other.getBoundingClientRect();
              return b.left < box.right && box.left < b.right
                && b.top < box.bottom && box.top < b.bottom;
            }).map((other) => other.className);
            return {
              key: link.dataset.cardLink,
              width: box.width,
              height: box.height,
              inside: box.left >= room.left && box.right <= room.right
                && box.top >= room.top && box.bottom <= room.bottom,
              hits,
              text: link.textContent,
              size: parseFloat(getComputedStyle(link).fontSize),
            };
          })"""
    )
    assert len(found) == 11
    for link in found:
        assert link["hits"] == [], link
        assert link["inside"], link
        assert link["height"] >= 22, link
        assert link["width"] >= 28, link
        assert link["text"] in {"SQL", "Code"}
        assert link["size"] >= 11, link
    assert page.evaluate(
        "() => document.documentElement.scrollWidth"
        " <= document.documentElement.clientWidth"
    )


def test_a_card_link_keeps_up_with_an_in_place_switch(scratch, browser):
    settings, server = scratch
    add_item(settings, "linked")
    server.start()
    context, page = open_page(browser, "/?window=3h")
    marked = page.evaluate("() => { window.samePage = true; return true; }")
    assert marked
    link = page.locator('[data-card-link="stream"]')
    assert "window=3h" in link.get_attribute("href")
    page.locator('a[data-filter="window:24h"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-card-link=stream]')"
        ".getAttribute('href').includes('window=24h')"
    )
    assert page.evaluate("() => window.samePage") is True
    page.locator('a[data-filter="level:state"]').first.click()
    page.wait_for_function(
        "() => document.querySelector('[data-card-link=brief]')"
        ".getAttribute('href').includes('level=state')"
    )
    assert page.evaluate("() => window.samePage") is True
    brief = page.locator('[data-card-link="brief"]').get_attribute("href")
    assert brief.endswith("#code")
    link.click()
    page.wait_for_url("**/under/panels/stream?**")
    assert "window=24h" in page.url
    assert "level=state" in page.url
    back = page.locator("a", has_text="Back to the dashboard").first
    href = back.get_attribute("href")
    assert "window=24h" in href
    assert "level=state" in href
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("path", ["/methods", "/under/panels/tone"])
def test_math_draws_and_fits_at_1280_in_projector_mode(
    scratch, browser, path, theme
):
    _, server = scratch
    server.start()
    _, page = projector_page(
        browser,
        script=f"localStorage.setItem('theme', '{theme}');",
    )
    page.goto(f"{BASE}{path}")
    page.wait_for_selector(".katex")
    found = page.evaluate(
        """() => {
          const text = getComputedStyle(document.body).color;
          const shown = [...document.querySelectorAll('.katex')];
          const boxes = [...document.querySelectorAll('.math-display')];
          return {
            drawn: shown.length,
            raw: document.querySelector('.math-display, .math-inline')
              ? [...document.querySelectorAll('.math-display, .math-inline')]
                .filter((box) => !box.querySelector('.katex')).length
              : 0,
            wide: boxes.filter(
              (box) => box.scrollWidth > box.clientWidth + 1
            ).length,
            pageWide: document.documentElement.scrollWidth
              > document.documentElement.clientWidth,
            sameColor: getComputedStyle(
              document.querySelector('.katex-html')).color,
            bodyColor: text,
            size: parseFloat(
              getComputedStyle(document.querySelector('.katex')).fontSize),
            images: document.querySelectorAll('.doc img').length,
          };
        }"""
    )
    assert found["drawn"] > 0
    assert found["raw"] == 0
    assert found["wide"] == 0
    assert not found["pageWide"]
    assert found["size"] >= 14
    assert found["images"] == 0


# A card's page in a replay.


def start_replay(page):
    page.evaluate(
        "() => document.querySelector('[data-toggle-replay]').click()"
    )
    page.wait_for_function("() => window.Monitor.replay.active")


def as_of_in(url):
    text = parse_qs(urlsplit(url).query)["as_of"][0]
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.mark.parametrize("value", ["1", "2026", "not-a-time"])
def test_a_value_that_is_not_a_moment_starts_no_replay(
    scratch, browser, value
):
    _, server = scratch
    server.start()
    _, page = open_page(browser, f"/?window=3h&as_of={value}")
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.Monitor.replay.active") is False


def test_a_moment_with_no_zone_replays_it_as_utc(scratch, browser):
    _, server = scratch
    server.start()
    at = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
    text = at.strftime("%Y-%m-%dT%H:%M:%S")
    _, page = open_page(browser, f"/?window=3h&as_of={text}")
    page.wait_for_function(
        "() => window.Monitor && window.Monitor.replay.active"
    )
    resumed = page.evaluate("() => window.Monitor.replay.asOf")
    assert 0 <= resumed - at.timestamp() * 1000 < 60_000


def test_exit_replay_reloads_from_a_page_with_a_hash(scratch, browser):
    _, server = scratch
    server.start()
    _, page = open_page(browser, "/?window=3h#band-3")
    start_replay(page)
    page.evaluate(
        "() => document.querySelector('[data-toggle-replay]').click()"
    )
    page.wait_for_function(
        "() => window.Monitor && !window.Monitor.replay.active"
    )
    assert "#band-3" not in page.url
    assert page.locator("#mode-pill").inner_text() != "REPLAY"


def test_a_card_link_in_a_replay_carries_the_moment_and_back_returns_to_it(
    scratch, browser
):
    _, server = scratch
    server.start()
    _, page = open_page(browser, "/?window=3h")
    start_replay(page)
    replayed = page.evaluate("() => window.Monitor.replay.asOf")

    page.locator('[data-card-link="rising"]').click()
    page.wait_for_url("**/under/panels/rising*")
    moment = as_of_in(page.url)
    assert abs(moment.timestamp() * 1000 - replayed) < 60_000
    assert "window=3h" in page.url
    note = page.locator("[data-replay-moment]").inner_text()
    assert et_label(moment) in note

    page.locator("#under-main").get_by_text("Back to the dashboard").click()
    page.wait_for_function(
        "() => window.Monitor && window.Monitor.replay.active"
    )
    assert page.locator("#mode-pill").inner_text() == "REPLAY"
    resumed = page.evaluate("() => window.Monitor.replay.asOf")
    assert 0 <= resumed - moment.timestamp() * 1000 < 60_000
    assert "window=3h" in page.url

    page.evaluate(
        "() => document.querySelector('[data-toggle-replay]').click()"
    )
    page.wait_for_load_state()
    page.wait_for_function("() => window.Monitor")
    assert "as_of" not in page.url
    assert page.locator("#mode-pill").inner_text() == "LIVE"
    assert not page.evaluate("() => window.Monitor.replay.active")


# What a refresh of a view keeps.


def refreshed(page, refresh):
    """Run a refresh, and wait until it has replaced the view's content."""
    page.evaluate(
        "() => { window.before = document.querySelector('.u-section'); }"
    )
    refresh()
    page.wait_for_function(
        "() => document.querySelector('.u-section') !== window.before"
    )


@pytest.mark.parametrize("how", ["button", "key"])
def test_formulas_stay_drawn_after_a_refresh_of_a_card_page(
    scratch, browser, how
):
    _, server = scratch
    server.start()
    _, page = projector_page(browser)
    page.goto(f"{BASE}/under/panels/rising")
    page.wait_for_selector(".katex")
    drawn = page.locator(".katex").count()
    assert drawn

    def refresh():
        if how == "button":
            page.locator("[data-under-refresh]").first.click()
        else:
            page.keyboard.press("r")

    refreshed(page, refresh)
    page.wait_for_selector(".katex")
    assert page.locator(".katex").count() == drawn
    assert page.locator(".math-display:not(:has(.katex))").count() == 0
    assert "\\frac" not in page.locator("#methods").inner_text()


def test_a_refresh_keeps_the_horizontal_scroll_of_a_rows_table(
    scratch, browser
):
    settings, server = scratch
    rows_scratch(settings)
    server.start()
    _, page = projector_page(browser)
    page.goto(f"{BASE}/under/data-model?table=raw.items")
    page.locator(
        '[data-rows-open="recent"][data-rows-table="raw.items"]'
    ).click()
    box = page.locator('[data-rows-body="raw.items"] .u-scroll')
    box.locator("table").wait_for()
    box.evaluate("(element) => { element.scrollLeft = 240; }")
    assert box.evaluate("(element) => element.scrollLeft") == 240
    refreshed(page, page.locator("[data-under-refresh]").first.click)
    assert box.evaluate("(element) => element.scrollLeft") == 240


def test_a_sample_opened_from_the_no_time_message_is_the_open_mode(
    scratch, browser
):
    settings, server = scratch
    conn = connect(settings)
    conn.execute("ALTER TABLE lake.raw.archive_index DROP COLUMN created_at")
    conn.close()
    server.start()
    _, page = projector_page(browser)
    page.goto(f"{BASE}/under/data-model?table=raw.archive_index")
    controls = '[data-rows-table="raw.archive_index"]'
    recent = page.locator(f'[data-rows-open="recent"]{controls}')
    sample = page.locator(f'[data-rows-open="sample"]{controls}')
    body = page.locator('[data-rows-body="raw.archive_index"]')
    recent.click()
    body.get_by_role("button", name="Show a sample").click()
    body.get_by_role("button", name="Another sample").wait_for()
    assert body.get_attribute("data-rows-mode") == "sample"
    assert sample.get_attribute("aria-expanded") == "true"
    assert recent.get_attribute("aria-expanded") == "false"
    sample.click()
    assert not body.is_visible()


def load_config_closes():
    return load_poll_closes(Path("config"))


def rule_rows(closes):
    return [
        {
            "time": close.at.isoformat(),
            "label": close.clock,
            "detail": f"Polls close: {' '.join(close.states)}",
        }
        for close in closes
    ]


def overlapping_text(page, chart, words):
    """Pairs of the named labels on a chart whose text boxes overlap."""
    return page.evaluate(
        """
        ([chart, words]) => {
          const boxes = [...document.querySelectorAll(
            `[data-chart="${chart}"] svg text`
          )].filter((node) => words.includes(node.textContent.trim()))
            .map((node) => ({
              text: node.textContent.trim(),
              box: node.getBoundingClientRect(),
            }));
          const pairs = [];
          boxes.forEach((a, index) => {
            boxes.slice(index + 1).forEach((b) => {
              const apart = a.box.right <= b.box.left
                || b.box.right <= a.box.left
                || a.box.bottom <= b.box.top
                || b.box.bottom <= a.box.top;
              if (!apart) pairs.push([a.text, b.text]);
            });
          });
          return pairs;
        }
        """,
        [chart, words],
    )


def chart_text(page, chart):
    return page.evaluate(
        """
        (chart) => [...document.querySelectorAll(
          `[data-chart="${chart}"] svg text`
        )].map((node) => node.textContent.trim())
        """,
        chart,
    )


@pytest.mark.parametrize(
    "hours,screen_width", [(7, 1280), (24, 1280), (24, 700)]
)
def test_poll_closing_labels_stay_apart_and_leave_the_chart_its_width(
    scratch, browser, hours, screen_width
):
    settings, server = scratch
    add_item(settings, "i0")
    server.start()
    closes = load_config_closes()
    latest = datetime(2026, 11, 4, 3, 35, tzinfo=UTC)
    gap = [
        {
            "time": "2026-11-04T00:40:00+00:00",
            "label": "Offline",
            "detail": "Collection stopped",
        },
        {
            "time": "2026-11-04T00:50:00+00:00",
            "label": "Back online",
            "detail": "Collection resumed",
        },
    ]
    rules = sorted(
        rule_rows(c for c in closes if c.at <= latest) + gap,
        key=lambda rule: datetime.fromisoformat(rule["time"]),
    )
    start = latest - timedelta(hours=hours)
    table = [
        {
            "bin_start": (start + timedelta(hours=step)).isoformat(),
            "n": 5,
            "label": "A storyline",
            "storyline_id": "s1",
            "total": 50,
        }
        for step in range(hours + 1)
    ]
    context, page = projector_page(browser, screen_width)
    page.route(
        "**/api/data/stream*",
        lambda route: route.fulfill(json={"table": table, "rules": rules}),
    )
    page.goto(f"{BASE}/?window=24h")
    page.wait_for_selector('[data-chart="stream"] g.mark-area path')
    page.wait_for_timeout(300)
    words = [rule["label"] for rule in rules]
    drawn = [text for text in chart_text(page, "stream") if text in words]
    assert sorted(drawn) == sorted(words)
    assert overlapping_text(page, "stream", words) == []
    width = page.evaluate(
        """
        () => {
          const svg = document.querySelector('[data-chart="stream"] svg');
          const area = svg.querySelector("g.mark-area path");
          return area.getBoundingClientRect().width
            / svg.getBoundingClientRect().width;
        }
        """
    )
    assert width > 0.8
    context.close()


def test_entity_rank_names_the_newer_line_when_two_end_at_one_rank(
    scratch, browser
):
    settings, server = scratch
    add_item(settings, "i0")
    server.start()
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    older, newer = now - timedelta(hours=1), now

    def row(name, at, rank):
        return {
            "bin_start": at.isoformat(),
            "entity_id": name.lower(),
            "entity": name,
            "party": "Other",
            "rank": rank,
            "n": 5,
            "evidence": "",
        }

    table = [
        row("Alpha", older, 1),
        row("Bravo", older, 2),
        row("Charlie", older, 3),
        row("Delta", newer, 1),
        row("Echo", newer, 2),
    ]
    context, page = open_page(browser)
    page.route(
        "**/api/data/bump*",
        lambda route: route.fulfill(json={"table": table, "rules": []}),
    )
    page.reload()
    page.wait_for_selector('[data-chart="bump"] g.mark-symbol path')
    page.wait_for_timeout(300)
    shown = set(chart_text(page, "bump"))
    assert {"Delta", "Echo", "Charlie"} <= shown
    assert not {"Alpha", "Bravo"} & shown
    context.close()


@pytest.mark.parametrize("width,height", [(1280, 720), (1920, 1080)])
def test_the_tour_fills_the_screen(scratch, browser, width, height):
    settings, server = scratch
    add_item(settings, "i0")
    server.start()
    context, page = projector_page(browser, width, height)
    page.goto(f"{BASE}/?window=3h")
    page.wait_for_function("() => window.Monitor && window.Monitor.mapData")
    page.keyboard.press("t")
    page.wait_for_selector(".tour-panel")
    box = page.evaluate(
        """
        () => {
          const box = document.querySelector(".tour-panel")
            .getBoundingClientRect();
          return [box.top, box.left, box.width, box.height];
        }
        """
    )
    assert box == [0, 0, width, height]
    context.close()
