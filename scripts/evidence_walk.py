#!/usr/bin/env python3
"""Click one element of every aggregate on the dashboard home page.

Each click must open an evidence drawer naming its filter, with stories in it,
and the count must match the rows listed. The dashboard must be running.

    uv run python scripts/evidence_walk.py
"""

from __future__ import annotations

import argparse
import re

from playwright.sync_api import Page, sync_playwright

MARKS = "g.mark-rect path, g.mark-symbol path, g.mark-area path"
CHARTS = (
    ("Topic Heat", "topic_heat"),
    ("Entity Rank", "bump"),
    ("Tone by Lean", "tone_by_lean"),
    ("Coverage by Lean", "coverage_matrix"),
    ("Global Press", "global_press"),
)


def drawer_result(page: Page) -> tuple[bool, str]:
    page.wait_for_timeout(700)
    if page.locator("#drawer").get_attribute("data-open") != "1":
        return False, "drawer did not open"
    body = page.locator("#drawer-body")
    heading = body.locator("h2").first.inner_text()
    filters = body.locator(".filter-line")
    if filters.count() == 0:
        return True, f"article: {heading[:60]}"
    match = re.match(r"(\d+) stor", heading)
    if not match:
        return False, f"unexpected heading: {heading[:60]}"
    total = int(match.group(1))
    rows = body.locator(".wire-row").count()
    if total == 0 or rows == 0 or rows > total:
        return False, f"{total} stories, {rows} rows"
    return True, f"{total} stories; {filters.first.inner_text()[:70]}"


def close(page: Page) -> None:
    page.keyboard.press("Escape")
    page.wait_for_timeout(250)


def click_mark(page: Page, chart: str) -> tuple[bool, str]:
    """Click marks until one opens a drawer; decorative marks do not."""
    marks = page.locator(f'[data-chart="{chart}"] svg').locator(MARKS)
    count = marks.count()
    if count == 0:
        return False, "no marks drawn"
    for index in range(min(count, 40)):
        mark = marks.nth(index)
        box = mark.bounding_box()
        if not box or box["width"] < 2 or box["height"] < 2:
            continue
        mark.scroll_into_view_if_needed()
        box = mark.bounding_box()
        page.mouse.click(
            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        )
        page.wait_for_timeout(400)
        if page.locator("#drawer").get_attribute("data-open") == "1":
            return drawer_result(page)
    return False, f"no mark opened a drawer among {count}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--query", default="window=24h")
    args = parser.parse_args()
    failures = 0
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(
            viewport={"width": 1920, "height": 1080}
        ).new_page()
        page.on("pageerror", lambda error: errors.append(str(error)[:200]))
        page.goto(f"{args.base}/?{args.query}")
        page.wait_for_timeout(4000)

        def report(name: str, outcome: tuple[bool, str]) -> None:
            nonlocal failures
            ok, detail = outcome
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
            close(page)

        page.locator(".kpi[data-evidence]").first.click()
        report("KPI tile", drawer_result(page))
        page.locator(".rising-row").first.click()
        report("States rising", drawer_result(page))
        page.locator("#wire .wire-row").first.click()
        report("Live wire row", drawer_result(page))
        footnote = page.locator(".card .fn").first
        footnote.click()
        report("Storyline footnote", drawer_result(page))
        brief_note = page.locator("#brief .fn").first
        if brief_note.count():
            brief_note.click()
            report("Brief footnote", drawer_result(page))
        else:
            print("skip Brief footnote: no brief yet")
        for name, chart in CHARTS:
            report(name, click_mark(page, chart))
        # Named storylines are thin bands; "Other" opens nothing, so scan.
        stream = page.locator('[data-chart="stream"] svg')
        stream.scroll_into_view_if_needed()
        box = stream.bounding_box()
        for column in range(5, 95, 2):
            if "/story/" in page.url:
                break
            for row in range(30, 70, 2):
                page.mouse.click(
                    box["x"] + box["width"] * column / 100,
                    box["y"] + box["height"] * row / 100,
                )
                if "/story/" in page.url:
                    break
                page.wait_for_timeout(25)
        page.wait_for_timeout(500)
        moved = "/story/" in page.url
        failures += not moved
        print(f"{'ok  ' if moved else 'FAIL'} Storyline Stream: {page.url}")
        page.goto(f"{args.base}/?{args.query}")
        page.wait_for_timeout(3000)
        before = page.url
        page.locator(".map-state").nth(20).click()
        page.wait_for_timeout(800)
        moved = "/state/" in page.url
        failures += not moved
        print(
            f"{'ok  ' if moved else 'FAIL'} Map state: {before} -> {page.url}"
        )
        browser.close()
    for error in errors:
        print("PAGE ERROR", error)
    print(f"{failures} failures, {len(errors)} page errors")
    raise SystemExit(1 if failures or errors else 0)


if __name__ == "__main__":
    main()
