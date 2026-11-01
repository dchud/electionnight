#!/usr/bin/env python3
"""Load tests on a scratch lake. The real lake is never touched.

Burst: pushes synthetic items through every keyword-path stage and reports how
long each took to finish. Dashboard: seeds a large lake and measures fragment
latency against the p95 target.

    uv run python scripts/load_test.py burst --items 2000 --minutes 5
    uv run python scripts/load_test.py dashboard --items 100000
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from monitor.lake import BatchWriter, connect, hash_id, init_lake
from monitor.logs import configure
from monitor.parsers.base import item_from_poller
from monitor.pipeline import CORE_STAGES, Pipeline
from monitor.settings import Paths, Settings, get_settings
from monitor.web.app import create_app

ROOT = Path("tmp/load")
STATES = "GA MI NC ME NH OH TX IA AK NE MN AZ WI PA NV KS NY CA FL".split()
TOPICS = ["turnout", "senate_race", "legal_challenge", "vote_counting"]
WORDS = (
    "senate race polls voters election county results turnout governor "
    "ballot campaign candidate court lawsuit counting midterm lines early"
).split()
PATHS = [
    "/frag/kpis",
    "/frag/wire",
    "/frag/rising",
    "/frag/states-rising",
    "/frag/race-calls",
    "/frag/translated",
    "/api/data/stream",
    "/api/data/topic_heat",
    "/api/data/map",
    "/api/data/coverage_matrix",
    "/api/data/global_press",
    "/api/data/bump",
    "/api/data/tone_by_lean",
]
VARIANTS = [
    "window=1h",
    "window=3h",
    "window=24h",
    "window=24h&focus=battleground",
    "window=3h&focus=close",
    "window=3h&level=state",
    "window=24h&lang=en",
    "window=24h&copies=all",
    "window=3h&focus=state:GA",
]


def scratch_settings() -> Settings:
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)
    paths = Paths.under(ROOT, config=Path("config"))
    paths.models = get_settings().paths.models
    settings = Settings(paths=paths)
    settings.anthropic_api_key = None
    init_lake(settings)
    return settings


def headline() -> str:
    return " ".join(random.choices(WORDS, k=9)).capitalize()


def item_row(number: int, seen_at: datetime) -> dict:
    url = f"https://load.example/{number}"
    return {
        "item_id": hash_id(url),
        "source_id": f"source-{number % 40}",
        "feed_url": "https://load.example/feed",
        "url": url,
        "canonical_url": url,
        "guid": url,
        "title": headline(),
        "summary": headline(),
        "author": "",
        "published_at": seen_at,
        "first_seen_at": seen_at,
        "language": "en",
        "feed_categories": [],
        "raw_entry": {},
    }


async def run_burst(items: int, minutes: float) -> None:
    settings = scratch_settings()
    configure("load", settings.paths.logs)
    pipeline = Pipeline(settings, no_llm=True)
    pipeline.stages = pipeline.build_stages()
    pipeline.first_stage = pipeline.stages[0]
    tasks = [t for stage in pipeline.stages for t in stage.tasks()]
    interval = minutes * 60 / items
    commits = asyncio.ensure_future(
        pipeline.every(
            settings.commit.batch_seconds, "commit", pipeline.commit_job
        )
    )
    ladder = asyncio.ensure_future(
        pipeline.every(15, "ladder", pipeline.ladder_job)
    )
    started = time.monotonic()
    worst = 0.0
    shed = False
    for number in range(items):
        row = item_row(number, datetime.now(UTC))
        pipeline.writer.add("raw.items", [row])
        pipeline.first_stage.enqueue(item_from_poller(row))
        await asyncio.sleep(interval)
        lag = max(
            s.oldest_live_age()
            for s in pipeline.stages
            if s.parser.name in CORE_STAGES
        )
        worst = max(worst, lag)
        shed = shed or pipeline.shedding
    while any(s.depth for s in pipeline.stages):  # noqa: ASYNC110
        await asyncio.sleep(0.5)
    elapsed = time.monotonic() - started
    await asyncio.sleep(1)
    for task in [*tasks, commits, ladder]:
        task.cancel()
    await asyncio.gather(*tasks, commits, ladder, return_exceptions=True)
    pipeline.writer.flush()
    counts = {
        table: pipeline.conn.execute(
            f"SELECT count(*) FROM lake.{table}"
        ).fetchall()[0][0]
        for table in (
            "raw.items",
            "nlp.classify_a",
            "nlp.embeddings",
            "nlp.dup_groups",
        )
    }
    pipeline.conn.close()
    print(f"{items} items in {elapsed:.0f} s; worst queue age {worst:.1f} s")
    print(f"load shedding triggered: {shed}")
    print(counts)
    print("target: visible within 90 s of first seen")
    print("PASS" if worst + settings.commit.batch_seconds < 90 else "FAIL")


def seed_large(settings: Settings, items: int) -> None:
    conn = connect(settings)
    now = datetime.now(UTC)
    span = timedelta(hours=48).total_seconds()
    batch = 20_000
    entities = [f"entity-{n}" for n in range(200)]
    for start in range(0, items, batch):
        writer = BatchWriter(conn, settings)
        rows, classes, dups, assigns, extracts, mentions = (
            [],
            [],
            [],
            [],
            [],
            [],
        )
        for number in range(start, min(start + batch, items)):
            age = random.random() ** 2 * span
            seen = now - timedelta(seconds=age)
            row = item_row(number, seen)
            item_id = row["item_id"]
            rows.append(row)
            relevant = random.random() < 0.4
            classes.append(
                {
                    "item_id": item_id,
                    "election_relevant": relevant,
                    "relevance_conf": 0.9,
                    "us_politics": relevant,
                    "levels": ["state"] if relevant else [],
                    "primary_level": "state" if relevant else "none",
                    "states": random.sample(STATES, 2) if relevant else [],
                    "race_types": ["senate"] if relevant else [],
                    "topics": random.sample(TOPICS, 2) if relevant else [],
                    "free_tags": [],
                    "is_results_report": False,
                    "is_opinion": False,
                    "method": "keywords",
                }
            )
            dups.append(
                {
                    "item_id": item_id,
                    "dup_group_id": item_id,
                    "is_canonical": random.random() < 0.85,
                    "method": "none",
                }
            )
            if relevant:
                assigns.append(
                    {
                        "item_id": item_id,
                        "storyline_id": f"story-{number % 400}",
                        "similarity": 0.95,
                        "assigned_at": seen,
                    }
                )
                extracts.append(
                    {
                        "item_id": item_id,
                        "summary_en": "Summary.",
                        "article_tone": random.uniform(-1, 1),
                        "topics_refined": [],
                        "raw": {"race_keys": []},
                    }
                )
                for entity in random.sample(entities, 3):
                    mentions.append(
                        {
                            "mention_id": hash_id(item_id, entity),
                            "item_id": item_id,
                            "surface": entity,
                            "ent_type": "PERSON",
                            "entity_id": entity,
                            "tone": random.uniform(-2, 2),
                        }
                    )
        writer.add("raw.items", rows)
        writer.add("nlp.classify_a", classes)
        writer.add("nlp.dup_groups", dups)
        writer.add("nlp.storyline_assign", assigns)
        writer.add("nlp.extract_b", extracts)
        writer.add("nlp.mentions", mentions)
        writer.flush()
    writer = BatchWriter(conn, settings)
    writer.add(
        "nlp.storylines",
        [
            {
                "storyline_id": f"story-{n}",
                "version": 1,
                "label": f"Storyline {n}",
                "summary": "",
                "citations": "{}",
                "first_seen_at": now - timedelta(hours=40),
                "last_item_at": now,
                "status": "active",
                "states": [],
            }
            for n in range(400)
        ],
    )
    writer.add(
        "ref.sources",
        [
            {
                "source_id": f"source-{n}",
                "name": f"Outlet {n}",
                "lean_label": ["L", "LL", "C", "LR", "R", "Unrated"][n % 6],
                "country": "US",
            }
            for n in range(40)
        ],
    )
    writer.add(
        "ref.entities",
        [
            {
                "entity_id": entity,
                "canonical_name": entity,
                "ent_type": "PERSON",
                "party": ["Democratic", "Republican"][n % 2],
                "aliases": [],
            }
            for n, entity in enumerate(entities)
        ],
    )
    writer.flush()
    conn.close()


def run_dashboard(items: int) -> None:
    settings = scratch_settings()
    started = time.monotonic()
    seed_large(settings, items)
    print(f"seeded {items} items in {time.monotonic() - started:.0f} s")
    app = create_app(settings)
    timings: list[float] = []
    slowest: list[tuple[float, str]] = []
    with TestClient(app) as client:
        web = app.state.web
        print(f"read model refresh took {web.model.refresh_ms} ms")
        for variant in VARIANTS:
            for path in PATHS:
                # Each pass misses the cache, as the first viewer after a new
                # snapshot does.
                web.model.cache = type(web.model.cache)(512)
                began = time.perf_counter()
                response = client.get(f"{path}?{variant}")
                took = (time.perf_counter() - began) * 1000
                assert response.status_code == 200, (path, variant)
                timings.append(took)
                slowest.append((took, f"{path}?{variant}"))
    timings.sort()
    p95 = statistics.quantiles(timings, n=20)[18]
    print(
        f"{len(timings)} uncached requests at {items} items: "
        f"p50 {statistics.median(timings):.0f} ms, p95 {p95:.0f} ms, "
        f"max {timings[-1]:.0f} ms"
    )
    for took, label in sorted(slowest, reverse=True)[:5]:
        print(f"  {took:7.0f} ms {label}")
    print("target: p95 under 150 ms")
    print("PASS" if p95 < 150 else "FAIL")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("test", choices=["burst", "dashboard"])
    parser.add_argument("--items", type=int, default=2000)
    parser.add_argument("--minutes", type=float, default=5)
    args = parser.parse_args()
    if args.test == "burst":
        asyncio.run(run_burst(args.items, args.minutes))
    else:
        run_dashboard(args.items)


if __name__ == "__main__":
    main()
