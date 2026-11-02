"""Dashboard behavior that only a browser shows. Run with `pytest -m slow`.

Each test runs a real server on a scratch lake and its own headless browser.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import clone_lake

from monitor.lake import BatchWriter, connect

pytestmark = [pytest.mark.slow, pytest.mark.enable_socket]

PORT = int(os.environ.get("MONITOR_TEST_PORT", "8030"))
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
def browser(tmp_path):
    os.environ["TMPDIR"] = str(tmp_path)
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
    page.wait_for_selector("path.map-county")
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
