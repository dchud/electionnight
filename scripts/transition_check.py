#!/usr/bin/env python3
"""Check that filter clicks on the home page switch in place.

Against a running dashboard, which it only reads:

    uv run python scripts/transition_check.py --base http://127.0.0.1:8030

Or on a scratch lake with seeded data, which it creates under `tmp/` and serves
itself:

    uv run python scripts/transition_check.py --scratch --port 8030

Every filter click must keep the page, change the URL, never show a black map
or an empty panel, and end with the figures a full load of that URL shows. The
exit status is 1 if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "tmp" / "transition-check"
FILTERS = (
    "window:3h",
    "focus:battleground",
    "focus:watchlist",
    "focus:close",
    "focus:all",
    "level:state",
    "level:national",
    "level:all",
    "lang:en",
    "lang:all",
    "copies:all",
    "copies:unique",
    "window:15m",
    "window:since4pm",
    "window:24h",
)
PANELS = (
    ".kpis",
    "#wire-panel",
    "#panel-rising .panel-body",
    "#panel-brief .panel-body",
    "#panel-calls .panel-body",
)
SAMPLER = """
() => {
  window.__samePage = true;
  window.__bad = [];
  const note = (what) => {
    if (!window.__bad.includes(what) && window.__bad.length < 40) {
      window.__bad.push(what);
    }
  };
  window.__sampler = setInterval(() => {
    document
      .querySelectorAll("#us-map path.map-state, #us-map path.map-outline")
      .forEach((path) => {
        const fill = getComputedStyle(path).fill;
        if (fill === "rgb(0, 0, 0)") note("black map path");
      });
    if (!document.querySelector("#us-map path")) note("no map");
    PANELS.forEach((selector) => {
      const panel = document.querySelector(selector);
      if (!panel || !panel.childElementCount) note(`empty ${selector}`);
    });
    const wire = document.getElementById("wire");
    const live = document.getElementById("wire-live");
    if (!wire.childElementCount && !live.childElementCount) {
      note("empty wire");
    }
    document.querySelectorAll("[data-chart]").forEach((chart) => {
      if (
        !chart.querySelector("svg") &&
        !chart.parentElement.querySelector(":scope > .empty")
      ) {
        note(`empty chart ${chart.dataset.chart}`);
      }
    });
  }, 50);
}
""".replace("PANELS", json.dumps(PANELS))
SETTLED = """
() => {
  const rows = document.querySelectorAll(".panel-dim");
  const kpi = document.querySelector(".kpis .kpi-value");
  const charts = window.Monitor.charts || [];
  return (
    !rows.length &&
    kpi &&
    window.Monitor.mapData &&
    charts.every((chart) => chart.view && !chart.running)
  );
}
"""
REFRESH_PANELS = """
() => {
  document.querySelectorAll('[hx-trigger*="sse:snapshot"]').forEach(
    (panel) => window.htmx.trigger(panel, "sse:snapshot")
  );
  document.body.dispatchEvent(new Event("wire-resume"));
  window.Monitor.bus.dispatchEvent(new Event("refresh"));
}
"""
FIGURES = """
() => {
  const first = document.querySelector(
    "#wire-live .wire-row, #wire .wire-row"
  );
  const tables = {};
  const isTime = /^\\d{4}-\\d\\d-\\d\\dT/;
  window.Monitor.charts.forEach((chart) => {
    const rows = chart.view.data("table");
    tables[chart.name] = JSON.stringify(
      rows.map((row) =>
        Object.fromEntries(
          Object.entries(row).filter(
            ([key]) => !["evidence"].includes(key)
          ).map(([key, value]) => [
            key,
            value instanceof Date ? value.getTime()
              : typeof value === "string" && isTime.test(value)
                ? new Date(value).getTime()
                : value,
          ])
        )
      )
    );
  });
  return {
    kpi: document.querySelector(".kpis .kpi-value").innerText,
    wire: first ? first.dataset.article : null,
    map: JSON.stringify(window.Monitor.mapData.states),
    charts: tables,
  };
}
"""


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, name: str, ok: object, detail: object = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failures.append(f"{name}: {detail}")
            print(f"FAIL {name}: {detail}")
        return bool(ok)


def seed_scratch_lake(root: Path) -> None:
    from monitor.lake import BatchWriter, connect, init_lake
    from monitor.settings import Paths, Settings

    settings = Settings(paths=Paths.under(root, config=Path("config")))
    init_lake(settings)
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    now = datetime.now(UTC)
    leans = ("L", "LL", "C", "LR", "R", "NA")
    writer.add(
        "ref.sources",
        [
            {
                "source_id": f"outlet{n}",
                "name": f"Outlet {n}",
                "lean_label": lean,
                "country": "GB" if lean == "NA" else "US",
                "enabled": True,
            }
            for n, lean in enumerate(leans)
        ],
    )
    writer.add(
        "ref.states",
        [
            {
                "usps": "GA",
                "fips": "13",
                "name": "Georgia",
                "battleground": True,
            },
            {
                "usps": "PA",
                "fips": "42",
                "name": "Pennsylvania",
                "battleground": True,
            },
            {
                "usps": "OH",
                "fips": "39",
                "name": "Ohio",
                "battleground": False,
            },
            {
                "usps": "TX",
                "fips": "48",
                "name": "Texas",
                "battleground": False,
            },
        ],
    )
    writer.add(
        "ref.races",
        [
            {
                "race_key": "2026-GA-SEN",
                "state": "GA",
                "office": "senate",
                "close_race": True,
                "watch": True,
                "candidates": [],
            }
        ],
    )
    writer.add(
        "ref.entities",
        [
            {
                "entity_id": "dem",
                "canonical_name": "A Democrat",
                "party": "Democratic",
            },
            {
                "entity_id": "rep",
                "canonical_name": "A Republican",
                "party": "Republican",
            },
            {
                "entity_id": "ind",
                "canonical_name": "An Independent",
                "party": "Independent",
            },
        ],
    )
    writer.add(
        "nlp.storylines",
        [
            {
                "storyline_id": f"story{n}",
                "version": 1,
                "label": f"Storyline number {n}",
                "summary": "A summary.[^1]",
                "citations": "{}",
                "first_seen_at": now - timedelta(hours=8),
                "last_item_at": now,
                "status": "active",
                "states": ["GA"],
            }
            for n in range(6)
        ],
    )
    states = (["GA"], ["PA"], ["OH"], ["TX"], [], ["GA", "PA"])
    levels = (["state"], ["national"], ["local"], ["state", "national"])
    entities = ("dem", "rep", "ind")
    for n in range(600):
        item_id = f"item{n}"
        at = now - timedelta(minutes=n * 1.3 + 1)
        language = "es" if n % 7 == 0 else "en"
        writer.add(
            "raw.items",
            [
                {
                    "item_id": item_id,
                    "source_id": f"outlet{n % len(leans)}",
                    "title": f"Story {n} about the election",
                    "summary": "A summary.",
                    "url": f"https://example.com/{item_id}",
                    "first_seen_at": at,
                    "published_at": at,
                    "language": language,
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
                    "levels": levels[n % len(levels)],
                    "primary_level": levels[n % len(levels)][0],
                    "states": states[n % len(states)],
                    "race_types": ["senate"] if n % 2 else ["house"],
                    "topics": ["turnout", "vote_counting", "exit_polls"][
                        n % 3 :
                    ],
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
                    "item_id": item_id,
                    "storyline_id": f"story{n % 6}",
                    "similarity": 0.95,
                    "assigned_at": at,
                }
            ],
        )
        writer.add(
            "nlp.mentions",
            [
                {
                    "mention_id": f"m{n}",
                    "item_id": item_id,
                    "surface": entities[n % 3],
                    "ent_type": "PERSON",
                    "entity_id": entities[n % 3],
                    "tone": (n % 5) / 2 - 1,
                }
            ],
        )
        if n % 40 == 0:
            writer.add(
                "nlp.race_calls",
                [
                    {
                        "call_id": f"c{n}",
                        "item_id": item_id,
                        "race_key": "2026-GA-SEN",
                        "winner_name": "A Democrat",
                        "winner_entity_id": "dem",
                        "party": "Democratic",
                        "called_by": "AP",
                        "status": "projected",
                        "state": "GA",
                    }
                ],
            )
    writer.flush()
    conn.close()


def serve_scratch(root: Path, port: int) -> None:
    import uvicorn

    from monitor.settings import Paths, Settings
    from monitor.web.app import create_app

    settings = Settings(paths=Paths.under(root, config=Path("config")))
    uvicorn.run(
        create_app(settings), host="127.0.0.1", port=port, log_level="warning"
    )


def start_scratch(port: int) -> subprocess.Popen:
    root = WORK / "lake"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    env = {**os.environ, "MONITOR_ENV_FILE": os.devnull}
    subprocess.run(
        [sys.executable, __file__, "--seed", str(root)],
        check=True,
        env=env,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
    )
    server = subprocess.Popen(
        [sys.executable, __file__, "--serve", str(root), "--port", str(port)],
        env=env,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            with urllib.request.urlopen(
                f"{base}/frag/status-strip", timeout=2
            ):
                return server
        except OSError:
            time.sleep(0.5)
    server.terminate()
    raise SystemExit("the scratch dashboard did not start")


def settle(page) -> None:
    page.wait_for_function(SETTLED, timeout=30000)
    page.wait_for_timeout(300)


def figures_match(context, page, url: str, report: Report, name: str) -> None:
    """Compare the page with a full load, allowing for new data arriving."""
    mismatch: dict[str, object] = {}
    for attempt in range(5):
        fresh = context.new_page()
        fresh.goto(url)
        settle(fresh)
        expected = fresh.evaluate(FIGURES)
        fresh.close()
        shown = page.evaluate(FIGURES)
        mismatch = {
            key: (str(shown[key])[:120], str(expected[key])[:120])
            for key in ("kpi", "wire", "map")
            if shown[key] != expected[key]
        }
        mismatch.update(
            {
                f"chart {chart}": (rows[:120], expected["charts"][chart][:120])
                for chart, rows in shown["charts"].items()
                if rows != expected["charts"][chart]
            }
        )
        if not mismatch:
            break
        # The window moves with the clock, so the two can only be compared
        # when the page has read as recently as the full load.
        page.wait_for_timeout(3000 * (attempt + 1))
        page.evaluate(REFRESH_PANELS)
        settle(page)
    report.check(f"{name}: figures match a full load", not mismatch, mismatch)


def stream_query(requests: list[str]) -> dict[str, list[str]] | None:
    streams = [url for url in requests if urlsplit(url).path == "/events"]
    return parse_qs(urlsplit(streams[-1]).query) if streams else None


def after_switch(
    context, page, requests: list[str], report: Report, name: str
) -> None:
    settle(page)
    url = page.url
    report.check(
        f"{name}: no page load",
        page.evaluate("() => window.__samePage === true"),
    )
    bad = page.evaluate("() => window.__bad.splice(0)")
    report.check(f"{name}: never black or empty", not bad, bad)
    report.check(
        f"{name}: nothing covers the wire",
        page.evaluate(
            """
            () => {
              const panel = document.getElementById("wire-panel");
              const box = panel.getBoundingClientRect();
              const hit = document.elementFromPoint(
                box.left + box.width / 2, box.top + box.height / 2
              );
              return hit && hit.closest("#wire-panel") !== null;
            }
            """
        ),
    )
    expected = parse_qs(urlsplit(url).query)
    report.check(
        f"{name}: event stream carries the query",
        stream_query(requests) == expected,
        (stream_query(requests), expected),
    )
    figures_match(context, page, url, report, name)


def click_filter(page, key: str) -> None:
    page.locator(f'[data-filter="{key}"]').click()


def run_filters(context, page, requests, report: Report) -> None:
    for key in FILTERS:
        before = page.url
        current = page.locator(f'[data-filter="{key}"]').get_attribute(
            "aria-current"
        )
        if current == "true":
            continue
        click_filter(page, key)
        page.wait_for_url(lambda url, seen=before: url != seen, timeout=10000)
        report.check(
            f"{key}: focus stays on the control",
            page.evaluate("() => document.activeElement.dataset.filter")
            == key,
        )
        after_switch(context, page, requests, report, key)
        print(f"ok   {key}")


def run_state_select(context, page, requests, report: Report) -> None:
    select = page.locator('[data-filter="focus:state"]')
    select.select_option(label="GA")
    page.wait_for_url(lambda url: "focus=state" in url, timeout=10000)
    after_switch(context, page, requests, report, "state select")
    report.check(
        "state select keeps its choice", select.input_value().endswith("GA")
    )
    print("ok   state select")
    click_filter(page, "focus:all")
    page.wait_for_url(lambda url: "focus=" not in url, timeout=10000)
    settle(page)


def run_history(context, page, requests, report: Report) -> None:
    start = page.url
    click_filter(page, "level:state")
    page.wait_for_url(lambda url: "level=state" in url, timeout=10000)
    settle(page)
    middle = page.url
    click_filter(page, "copies:all")
    page.wait_for_url(lambda url: "copies=all" in url, timeout=10000)
    settle(page)
    end = page.url
    page.go_back()
    page.wait_for_url(middle, timeout=10000)
    after_switch(context, page, requests, report, "Back")
    report.check(
        "Back: the control shows the state",
        page.locator('[data-filter="copies:unique"]').get_attribute(
            "aria-current"
        )
        == "true",
    )
    page.go_forward()
    page.wait_for_url(end, timeout=10000)
    after_switch(context, page, requests, report, "Forward")
    page.goto(start)
    settle(page)
    page.evaluate(SAMPLER)
    print("ok   Back and Forward")


def run_rapid(context, page, requests, report: Report) -> None:
    page.evaluate(
        """
        async () => {
          for (const key of ["window:15m", "window:3h", "window:24h"]) {
            document.querySelector(`[data-filter="${key}"]`).click();
            await new Promise((done) => setTimeout(done, 20));
          }
        }
        """
    )
    page.wait_for_url(lambda url: "window=24h" in url, timeout=10000)
    after_switch(context, page, requests, report, "three rapid clicks")
    report.check(
        "three rapid clicks: the last one holds",
        page.locator('[data-filter="window:24h"]').get_attribute(
            "aria-current"
        )
        == "true",
        page.url,
    )
    print("ok   three rapid clicks")


def run_keyboard(context, page, requests, report: Report) -> None:
    for key, part in (("w", "window="), ("f", "focus=")):
        before = page.url
        page.locator("#clock").click()
        page.keyboard.press(key)
        page.wait_for_url(lambda url, seen=before: url != seen, timeout=10000)
        report.check(
            f"key {key}: changes the filter", part in page.url, page.url
        )
        after_switch(context, page, requests, report, f"key {key}")
        print(f"ok   key {key}")


def run_dimming(page, report: Report) -> None:
    held: list = []
    page.route("**/frag/rising*", lambda route: held.append(route))
    panel = "#panel-rising .panel-body"
    before = page.locator(panel).inner_html()
    click_filter(page, "copies:all")
    page.wait_for_url(lambda url: "copies=all" in url, timeout=10000)
    dimmed = (
        f"document.querySelector('{panel}').classList.contains('panel-dim')"
    )
    page.wait_for_function(f"() => {dimmed}")
    report.check(
        "dimming: old content stays while the response is on its way",
        page.locator(panel).inner_html() == before,
    )
    opacity = page.evaluate(
        f"() => getComputedStyle(document.querySelector('{panel}')).opacity"
    )
    page.wait_for_timeout(350)
    opacity = page.evaluate(
        f"() => getComputedStyle(document.querySelector('{panel}')).opacity"
    )
    report.check("dimming: old content dims", float(opacity) < 1, opacity)
    page.wait_for_function(f"() => !{dimmed}", timeout=12000)
    report.check("dimming: a timeout clears it", True)
    for route in held:
        route.abort()
    page.unroute("**/frag/rising*")
    settle(page)
    click_filter(page, "copies:unique")
    page.wait_for_url(lambda url: "copies=" not in url, timeout=10000)
    settle(page)
    silent = page.evaluate(
        """
        async (panel) => {
          const element = document.querySelector(panel);
          let dimmed = false;
          const watch = new MutationObserver(() => {
            dimmed = dimmed || element.classList.contains("panel-dim");
          });
          watch.observe(element, { attributes: true });
          window.htmx.trigger(element, "sse:snapshot");
          await new Promise((done) => setTimeout(done, 600));
          watch.disconnect();
          return !dimmed;
        }
        """,
        panel,
    )
    report.check("dimming: a snapshot refresh stays silent", silent)
    print("ok   dimming")


def run_modes(context, page, requests, report: Report) -> None:
    # An open drawer stays open.
    page.locator("#wire .wire-row, #wire-live .wire-row").first.click()
    page.wait_for_function(
        "() => document.getElementById('drawer').dataset.open === '1'"
    )
    click_filter(page, "lang:en")
    page.wait_for_url(lambda url: "lang=en" in url, timeout=10000)
    settle(page)
    report.check(
        "an open drawer stays open",
        page.evaluate(
            "() => document.getElementById('drawer').dataset.open === '1'"
        ),
    )
    page.keyboard.press("Escape")
    click_filter(page, "lang:all")
    page.wait_for_url(lambda url: "lang=" not in url, timeout=10000)
    settle(page)
    # Projector mode and the tour stay on.
    # The tour covers the controls, so it switches by key.
    page.keyboard.press("p")
    page.keyboard.press("t")
    page.wait_for_timeout(300)
    before = page.url
    page.keyboard.press("w")
    page.wait_for_url(lambda url, seen=before: url != seen, timeout=10000)
    settle(page)
    report.check(
        "projector mode and the tour stay on",
        page.evaluate(
            """
            () => document.documentElement.dataset.projector === "1"
              && document.querySelector(".tour-panel") !== null
            """
        ),
    )
    page.keyboard.press("t")
    page.keyboard.press("p")
    page.wait_for_timeout(300)
    settle(page)
    # Replay continues under the new filter.
    requests.clear()
    page.keyboard.press("r")
    page.wait_for_function("() => window.Monitor.replay.active === true")
    page.wait_for_timeout(500)
    click_filter(page, "level:national")
    page.wait_for_url(lambda url: "level=national" in url, timeout=10000)
    page.wait_for_timeout(2500)
    frags = [url for url in requests if "/frag/rising" in url][-1:]
    report.check(
        "replay continues under the new filter",
        page.evaluate("() => window.Monitor.replay.active")
        and page.evaluate(
            "() => document.getElementById('mode-pill').innerText"
        )
        == "REPLAY"
        and frags
        and "as_of" in frags[0]
        and "level=national" in frags[0],
        frags,
    )
    page.keyboard.press("r")
    page.wait_for_load_state()
    settle(page)
    page.evaluate(SAMPLER)
    print("ok   drawer, projector, tour, replay")


def run_fallback(page, report: Report) -> None:
    def fail_fetch(route) -> None:
        if route.request.resource_type == "fetch":
            route.abort()
        else:
            route.continue_()

    page.route("**/?*", fail_fetch)
    click_filter(page, "level:local")
    page.wait_for_function("() => window.__samePage !== true", timeout=15000)
    report.check(
        "a failed switch falls back to a page load",
        "level=local" in page.url,
        page.url,
    )
    page.unroute("**/?*")
    settle(page)
    page.evaluate(SAMPLER)
    print("ok   fallback")


def run_reduced_motion(browser, base: str, report: Report) -> None:
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, reduced_motion="reduce"
    )
    block_outside(context, base)
    page = context.new_page()
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    page.goto(f"{base}/?window=24h")
    settle(page)
    page.evaluate(SAMPLER)
    report.check(
        "reduced motion: no transition",
        page.evaluate(
            """
            () => getComputedStyle(
                document.querySelector("#panel-rising .panel-body")
              ).transitionDuration === "0s"
              && getComputedStyle(document.querySelector("path.map-state"))
              .transitionDuration === "0s"
            """
        ),
    )
    click_filter(page, "level:state")
    page.wait_for_url(lambda url: "level=state" in url, timeout=10000)
    after_switch(context, page, requests, report, "reduced motion")
    context.close()
    print("ok   reduced motion")


def run_other_page(browser, base: str, report: Report) -> None:
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    block_outside(context, base)
    page = context.new_page()
    page.goto(f"{base}/watchlist")
    page.evaluate("() => { window.__samePage = true; }")
    click_filter(page, "window:3h")
    page.wait_for_url(lambda url: "window=3h" in url, timeout=10000)
    page.wait_for_load_state()
    report.check(
        "other pages keep full loads",
        page.evaluate("() => window.__samePage !== true"),
    )
    context.close()


def block_outside(context, base: str) -> None:
    origin = urlsplit(base)
    allowed = f"{origin.scheme}://{origin.netloc}"
    context.route(
        "**/*",
        lambda route: (
            route.continue_()
            if route.request.url.startswith((allowed, "data:"))
            else route.abort()
        ),
    )


def run_checks(base: str) -> int:
    (WORK / "browser-tmp").mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(WORK / "browser-tmp")
    from playwright.sync_api import sync_playwright

    report = Report()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        block_outside(context, base)
        page = context.new_page()
        errors: list[str] = []
        page.on(
            "console",
            lambda message: (
                errors.append(message.text[:200])
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: errors.append(str(error)[:200]))
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.goto(f"{base}/")
        settle(page)
        page.evaluate(SAMPLER)
        run_filters(context, page, requests, report)
        run_state_select(context, page, requests, report)
        run_history(context, page, requests, report)
        run_rapid(context, page, requests, report)
        run_keyboard(context, page, requests, report)
        report.check("no console errors", not errors, errors[:5])
        errors.clear()
        run_dimming(page, report)
        errors.clear()
        run_modes(context, page, requests, report)
        report.check(
            "no console errors in other modes", not errors, errors[:5]
        )
        run_fallback(page, report)
        context.close()
        run_reduced_motion(browser, base, report)
        run_other_page(browser, base, report)
        browser.close()
    print(f"{report.passed} checks passed, {len(report.failures)} failed")
    for failure in report.failures:
        print(f"FAIL {failure}")
    return 1 if report.failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", default="")
    parser.add_argument("--scratch", action="store_true")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--seed", default="", help=argparse.SUPPRESS)
    parser.add_argument("--serve", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.seed:
        seed_scratch_lake(Path(args.seed))
        return
    if args.serve:
        serve_scratch(Path(args.serve), args.port)
        return
    if args.scratch:
        server = start_scratch(args.port)
        try:
            status = run_checks(f"http://127.0.0.1:{args.port}")
        finally:
            server.terminate()
            server.wait(timeout=15)
        raise SystemExit(status)
    if not args.base:
        parser.error("give --base or --scratch")
    raise SystemExit(run_checks(args.base.rstrip("/")))


if __name__ == "__main__":
    main()
