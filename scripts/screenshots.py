#!/usr/bin/env python3
"""Capture dashboard screenshots at each size and mode for review.

The dashboard must be running. Files land in `reports/screens/`.

    uv run python scripts/screenshots.py
    uv run python scripts/screenshots.py --pages / /state/GA --modes light
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

SIZES = {
    "1280x720": (1280, 720),
    "1920x1080": (1920, 1080),
    "2560x1600": (2560, 1600),
}
MODES = {
    "light": {"theme": "light", "projector": "0"},
    "dark": {"theme": "dark", "projector": "0"},
    "projector": {"theme": "light", "projector": "1"},
}
OUTPUT = Path("reports/screens")
SETTLE_MS = 3500


def slug(page_path: str) -> str:
    return page_path.strip("/").replace("/", "-") or "home"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--pages", nargs="+", default=["/"])
    parser.add_argument("--query", default="window=24h")
    parser.add_argument("--sizes", nargs="+", default=list(SIZES))
    parser.add_argument("--modes", nargs="+", default=list(MODES))
    parser.add_argument("--full-page", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    problems = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for size in args.sizes:
            width, height = SIZES[size]
            for mode in args.modes:
                context = browser.new_context(
                    viewport={"width": width, "height": height}
                )
                settings = MODES[mode]
                context.add_init_script(
                    f"localStorage.setItem('theme','{settings['theme']}');"
                    "localStorage.setItem('projector',"
                    f"'{settings['projector']}');"
                )
                page = context.new_page()
                where = f"{mode} {size}"
                page.on(
                    "pageerror",
                    lambda error, at=where: problems.append(f"{at}: {error}"),
                )
                for path in args.pages:
                    page.goto(f"{args.base}{path}?{args.query}")
                    page.wait_for_timeout(SETTLE_MS)
                    overflow = page.evaluate(
                        "document.documentElement.scrollWidth - innerWidth"
                    )
                    if overflow > 0:
                        problems.append(
                            f"{path} {mode} {size}: {overflow}px horizontal "
                            "page scroll"
                        )
                    target = OUTPUT / f"{slug(path)}-{size}-{mode}.png"
                    page.screenshot(path=str(target), full_page=args.full_page)
                    print(target)
                context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM", problem)


if __name__ == "__main__":
    main()
