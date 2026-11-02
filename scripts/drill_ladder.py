#!/usr/bin/env python3
"""Run each rung of the degradation ladder on a scratch lake.

The LLM rung sends one invalid key to the real API, then recovers with the key
from `.env`, so it spends a fraction of a cent. The failure window is shortened
from its configured value so the drill takes seconds. Disk levels use an
injected free-space function, and web-slow uses injected query times.

    uv run python scripts/drill_ladder.py
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic

from monitor.disk import DiskGuard
from monitor.lake import init_lake
from monitor.llm.budget import Budget
from monitor.llm.client import LlmClient
from monitor.parsers.base import Item
from monitor.parsers.classify_a import ClassifyParser
from monitor.parsers.entities import EntityResolver
from monitor.parsers.extract_b import ExtractParser
from monitor.parsers.geo import Gazetteer
from monitor.pipeline import Pipeline
from monitor.settings import Paths, Settings, load_settings
from monitor.web.hub import Hub
from monitor.web.readmodel import ReadModel

ROOT = Path("tmp/drill-ladder")
REPORT = Path("reports/degradation_drill.md")
GAZETTEER = Path("data/ref")
TITLE = "Early voting opens in Ohio for the midterm election"
FAILURE_WINDOW_SECONDS = 5


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, bool]] = []

    def check(self, rung: str, what: str, seen: Any, ok: bool) -> None:
        self.rows.append((rung, what, str(seen), ok))
        print(f"{'ok  ' if ok else 'FAIL'} {rung}: {what}: {seen}")


async def drill_llm(settings: Settings, results: Results) -> None:
    rung = "LLM"
    configured = settings.llm.degrade_after_seconds
    settings.llm.degrade_after_seconds = FAILURE_WINDOW_SECONDS
    bad = anthropic.AsyncAnthropic(api_key="invalid-drill-key", max_retries=0)
    llm = LlmClient(settings, None, Budget(settings), client=bad)
    good = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), max_retries=1
    )
    classify = ClassifyParser(settings, llm, {})
    started = time.monotonic()
    output = await classify.run([Item("a", title=TITLE)])
    row = output.rows["nlp.classify_a"][0]
    results.check(
        rung,
        f"invalid key moves Stage A to keywords "
        f"(window {FAILURE_WINDOW_SECONDS} s, configured {configured} s)",
        f"method={row['method']}, after {time.monotonic() - started:.0f} s",
        row["method"] == "keywords",
    )
    results.check(
        rung,
        "last error names the failure",
        llm.last_error,
        bool(llm.last_error),
    )
    results.check(
        rung,
        "strip input reports degraded",
        llm.status()["degraded"],
        llm.status()["degraded"],
    )
    pipeline = Pipeline(settings, no_llm=True)
    extract = ExtractParser(
        settings,
        llm,
        pipeline.db,
        {},
        EntityResolver(settings),
        Gazetteer.load(GAZETTEER),
    )
    item = Item("b", title=TITLE, summary=TITLE)
    llm.last_probe = time.monotonic()
    try:
        await asyncio.wait_for(extract._call(item, TITLE), 3)
        held = False
    except TimeoutError:
        held = True
    results.check(rung, "Stage B holds items while degraded", held, held)
    llm.client = good
    llm.last_probe = 0.0
    output = await classify.run([Item("c", title=TITLE)])
    row = output.rows["nlp.classify_a"][0]
    results.check(
        rung,
        "valid key: a probe recovers Stage A",
        f"method={row['method']}",
        row["method"] != "keywords",
    )
    results.check(
        rung,
        "degraded state clears",
        llm.status()["degraded"],
        not llm.status()["degraded"],
    )
    result = await extract._call(item, TITLE)
    results.check(
        rung, "Stage B resumes", type(result).__name__, result is not None
    )
    pipeline.conn.close()
    settings.llm.degrade_after_seconds = configured


async def drill_lag(pipeline: Pipeline, results: Results) -> None:
    rung = "Lag"
    stage = pipeline.stages[1]
    old = Item("a", first_seen_at=datetime.now(UTC) - timedelta(minutes=5))
    stage.live.put_nowait(old)
    await pipeline.ladder_job()
    results.check(
        rung, "lag over 120 s sheds load", pipeline.shedding, pipeline.shedding
    )
    results.check(
        rung,
        "embed rate halves",
        pipeline.embed.throttle,
        pipeline.embed.throttle == 0.5,
    )
    paused = all(s.pause_backfill for s in pipeline.stages)
    results.check(rung, "backfill pauses", paused, paused)
    stage.live.get_nowait()
    await pipeline.ladder_job()
    results.check(
        rung,
        "recovers when lag clears",
        pipeline.shedding,
        not pipeline.shedding,
    )
    results.check(
        rung,
        "embed rate returns",
        pipeline.embed.throttle,
        pipeline.embed.throttle == 1.0,
    )


async def drill_disk(pipeline: Pipeline, results: Results) -> None:
    rung = "Disk"
    for free, expected in (
        (9.0, "WARN"),
        (4.0, "DEGRADE"),
        (2.0, "AUTOPRUNE"),
        (30.0, "OK"),
    ):
        pipeline.guard = DiskGuard(
            pipeline.settings, free_space=lambda _p, f=free: f
        )
        await pipeline.disk_job()
        level = pipeline.guard.level().name
        results.check(rung, f"{free:.0f} GB free", level, level == expected)


def drill_web(settings: Settings, results: Results) -> None:
    rung = "Web"
    model = ReadModel(settings)
    hub = Hub(model, settings)
    limit = settings.web.slow_query_ms
    gap = settings.web.slow_throttle_seconds
    now = time.monotonic()
    model.query_times.extend((now, limit * 2.0) for _ in range(20))
    hub._update_slow()
    results.check(rung, f"p95 {limit * 2} ms is slow", hub.slow, hub.slow)
    hub.pending = True
    hub.last_snapshot_sent = now
    held = not hub._snapshot_due(now + gap - 1)
    results.check(rung, f"refresh waits under {gap} s", held, held)
    due = hub._snapshot_due(now + gap)
    results.check(rung, f"refresh goes out at {gap} s", due, due)
    model.query_times.clear()
    hub._update_slow()
    results.check(
        rung, "recovers when queries are fast", hub.slow, not hub.slow
    )


def write_report(results: Results, settings: Settings) -> None:
    failed = sum(not row[3] for row in results.rows)
    lines = [
        "# Degradation drill",
        "",
        f"Run {datetime.now(UTC):%Y-%m-%d %H:%M} UTC on a scratch lake.",
        f"{len(results.rows) - failed} of {len(results.rows)} checks passed.",
        "",
        "The LLM rung used a real invalid key against the API, then the real "
        "key.",
        f"Its failure window was {FAILURE_WINDOW_SECONDS} s instead of "
        f"{settings.llm.degrade_after_seconds} s, and the probe wait was "
        "skipped.",
        "Disk levels used an injected free-space function. Web-slow used "
        "injected query times.",
        "",
        "| Rung | Check | Seen | Result |",
        "|---|---|---|---|",
    ]
    for rung, what, seen, ok in results.rows:
        lines.append(
            f"| {rung} | {what} | {seen} | {'pass' if ok else 'FAIL'} |"
        )
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> int:
    shutil.rmtree(ROOT, ignore_errors=True)
    settings = load_settings()
    settings.paths = Paths.under(ROOT, config=Path("config"))
    settings.archive.s3_bucket = ""
    init_lake(settings)
    results = Results()
    await drill_llm(settings, results)
    pipeline = Pipeline(settings, no_llm=True)
    pipeline.stages = pipeline.build_stages()
    pipeline.first_stage = pipeline.stages[0]
    await drill_lag(pipeline, results)
    await drill_disk(pipeline, results)
    pipeline.conn.close()
    drill_web(settings, results)
    write_report(results, settings)
    failed = sum(not row[3] for row in results.rows)
    print(f"{failed} failed; wrote {REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
