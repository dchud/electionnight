#!/usr/bin/env python3
"""Take the network away from a real poller, then give it back.

Serves generated feeds from a local server and runs the real poller, writer,
and read model on a scratch lake. Taking the server down refuses connections,
as a dropped network does. Sleep is simulated by feeding the gate a clock
difference, because the monotonic clock only stops during real system sleep.
Network returns go through the pipeline's own gap handling.

The last part runs the sequence of closing a laptop and moving it: sleep with a
slow probe failing across the wake, a return, catch-up, and a short drop during
that catch-up.

    uv run python scripts/drill_outage.py
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from monitor.ingest.network import NetworkGate
from monitor.ingest.poller import Poller
from monitor.ingest.sources import Source
from monitor.lake import init_lake
from monitor.pipeline import Pipeline
from monitor.settings import Paths, Settings
from monitor.web.readmodel import ReadModel

ROOT = Path("tmp/drill-outage")
REPORT = Path("reports/outage_drill.md")
PORT = 8765
ITEMS_KEPT = 10
OVERRUN = 25
SLOW_PROBE_SECONDS = 3.0
SITEMAP_DELAY_SECONDS = 0.25
SHORT_DROP_SECONDS = 3.0


class Feeds:
    """Generated stories, each with a published time."""

    def __init__(self, n_feeds: int) -> None:
        self.n_feeds = n_feeds
        self.stories: dict[int, list[tuple[str, datetime]]] = {
            feed: [] for feed in range(n_feeds)
        }
        self.counter = 0
        self.sitemap_reads = 0
        self.sitemap_delay = 0.0
        self.lock = threading.Lock()

    def publish(self, label: str, at: datetime | None = None) -> None:
        with self.lock:
            for feed in range(self.n_feeds):
                self.counter += 1
                self.stories[feed].append(
                    (f"{label}-{self.counter}", at or datetime.now(UTC))
                )

    def sitemap(self, feed: int) -> bytes:
        time.sleep(self.sitemap_delay)
        with self.lock:
            self.sitemap_reads += 1
            stories = list(self.stories[feed])
        entries = "".join(
            f"<url><loc>https://example.com/{name}</loc><news:news>"
            f"<news:publication_date>{at.isoformat()}"
            f"</news:publication_date><news:title>{name} midterm election"
            "</news:title></news:news></url>"
            for name, at in stories
        )
        return (
            '<?xml version="1.0"?><urlset '
            'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
            f"{entries}</urlset>"
        ).encode()

    def body(self, feed: int) -> bytes:
        with self.lock:
            stories = self.stories[feed][-ITEMS_KEPT:]
        entries = "".join(
            f"<item><title>{name} midterm election</title>"
            f"<link>https://example.com/{name}</link>"
            f"<pubDate>{format_datetime(at)}</pubDate></item>"
            for name, at in stories
        )
        return (
            '<?xml version="1.0"?><rss version="2.0"><channel><title>f'
            f"</title>{entries}</channel></rss>"
        ).encode()


def serve(feeds: Feeds, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            feed = int(self.path.rsplit("/", 1)[1])
            sitemap = self.path.startswith("/sitemap/")
            body = feeds.sitemap(feed) if sitemap else feeds.body(feed)
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.allow_reuse_address = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Drill:
    def __init__(self, n_feeds: int, outage: float, port: int) -> None:
        shutil.rmtree(ROOT, ignore_errors=True)
        self.port = port
        self.settings = Settings(
            paths=Paths.under(ROOT, config=Path("config"))
        )
        self.settings.poll.tier_seconds = [3, 3, 3]
        self.settings.poll.jitter_pct = 0
        self.settings.poll.probe_seconds = 1.0
        self.settings.poll.resume_stagger_seconds = 5.0
        self.settings.poll.min_gap_seconds = 1.0
        self.settings.poll.catch_up_min_gap_seconds = 1.0
        self.settings.poll.allow_loopback = True
        init_lake(self.settings)
        self.pipeline = Pipeline(self.settings, no_llm=True)
        self.conn = self.pipeline.conn
        self.writer = self.pipeline.writer
        self.feeds = Feeds(n_feeds)
        self.outage = outage
        self.up = True
        self.probe_delay = 0.0
        self.probes_running = 0
        self.rows: list[tuple[str, str, bool]] = []
        self.gate: NetworkGate = self.pipeline.gate
        self.gate.probe = self.probe
        sources = [
            Source(
                source_id=f"s{n}",
                name=f"S{n}",
                homepage="https://example.com/",
                feed_urls=[f"http://127.0.0.1:{port}/{n}"],
                poll_tier=(n % 3) + 1,
            )
            for n in range(n_feeds)
        ]
        self.poller = Poller(
            self.settings,
            self.writer,
            sources,
            set(),
            gate=self.gate,
            news_sitemaps={
                f"s{n}": f"http://127.0.0.1:{port}/sitemap/{n}"
                for n in range(n_feeds)
            },
        )
        self.pipeline.poller = self.poller

    async def probe(self) -> bool:
        up = self.up
        if self.probe_delay:
            self.probes_running += 1
            await asyncio.sleep(self.probe_delay)
            self.probes_running -= 1
        return up

    def check(self, what: str, seen: Any, ok: bool) -> None:
        self.rows.append((what, str(seen), ok))
        print(f"{'ok  ' if ok else 'FAIL'} {what}: {seen}")

    def seen(self, label: str) -> int:
        return sum(1 for r in self.poller_rows() if r.startswith(label))

    def poller_rows(self) -> list[str]:
        self.writer.flush()
        rows = self.conn.execute(
            "SELECT split_part(url, '/', 4) FROM lake.raw.items"
        ).fetchall()
        return [row[0] for row in rows]

    async def wait_for_all(
        self, label: str, limit: float, want: int | None = None
    ) -> float:
        want = want or self.feeds.n_feeds
        started = time.monotonic()
        while time.monotonic() - started < limit:
            if self.seen(label) >= want:
                break
            await asyncio.sleep(0.5)
        return time.monotonic() - started

    async def run(self) -> int:
        n = self.feeds.n_feeds
        self.feeds.publish("before")
        server = serve(self.feeds, self.port)
        task = asyncio.ensure_future(self.poller.run())
        took = await self.wait_for_all("before", 30)
        self.check(
            f"all {n} feeds read before the outage",
            f"{took:.0f} s",
            self.seen("before") == n,
        )

        # A network outage.
        server.shutdown()
        server.server_close()
        self.up = False
        down_at = datetime.now(UTC)
        before = self.conn.execute(
            "SELECT count(*) FROM lake.raw.fetches WHERE error IS NOT NULL"
        ).fetchone()[0]
        await asyncio.sleep(self.outage / 2)
        self.check(
            "outage is noticed",
            self.gate.offline_reason,
            not self.gate.is_online,
        )
        for _ in range(OVERRUN):
            self.feeds.publish("during", down_at + timedelta(seconds=1))
        await asyncio.sleep(self.outage / 2)
        failed = sum(1 for f in self.poller.feeds if f.failures)
        self.check(
            "no feed is penalized",
            f"{failed} of {n} with failures",
            failed == 0,
        )
        health = self.poller.health()
        self.check("feed health is unchanged", health, health["healthy"] == n)
        self.writer.flush()
        after = self.conn.execute(
            "SELECT count(*) FROM lake.raw.fetches WHERE error IS NOT NULL"
        ).fetchone()[0]
        self.check(
            "no failed fetches are recorded", after - before, after == before
        )

        # The network returns.
        server = serve(self.feeds, self.port)
        self.up = True
        took = await self.wait_for_all("during", 60, n * OVERRUN)
        self.check(
            f"every feed catches up after {self.outage:.0f} s offline, with "
            f"{OVERRUN} stories published against a feed window of "
            f"{ITEMS_KEPT}",
            f"{took:.0f} s, {self.seen('during')} of {n * OVERRUN} stories",
            self.seen("during") == n * OVERRUN,
        )
        recovered = self.conn.execute(
            "SELECT count(*) FROM lake.raw.items "
            "WHERE feed_url LIKE '%/sitemap/%'"
        ).fetchone()[0]
        self.check(
            "stories past the feed window come from sitemaps",
            recovered,
            recovered >= n * (OVERRUN - ITEMS_KEPT),
        )
        gaps = self.conn.execute(
            "SELECT reason FROM lake.raw.collection_gaps"
        ).fetchall()
        self.check("the gap is recorded", gaps, gaps == [("network",)])

        # Stories published in the gap land at their published time.
        model = ReadModel(self.settings)
        model.refresh()
        rows = model.rows(
            "SELECT first_seen_at, seen_raw_at, published_at FROM facts"
        )
        moved = [r for r in rows if r["seen_raw_at"] != r["first_seen_at"]]
        placed = all(r["first_seen_at"] == r["published_at"] for r in moved)
        self.check(
            "catch-up stories move to their published time",
            f"{len(moved)} moved",
            len(moved) == n * OVERRUN and placed,
        )

        # A short hiccup, then simulated sleep.
        self.feeds.publish("hiccup")
        took = await self.wait_for_all("hiccup", 30)
        self.check(
            "normal polling resumes", f"{took:.0f} s", self.seen("hiccup") == n
        )
        wall = time.time() - 2700
        self.gate.note_clocks(wall, 100.0, wall + 2705, 105.0)
        self.check(
            "a clock difference reads as sleep",
            self.gate.offline_reason,
            not self.gate.is_online,
        )
        self.feeds.publish("slept")
        took = await self.wait_for_all("slept", 30)
        self.check(
            "every feed catches up after sleep",
            f"{took:.0f} s",
            self.seen("slept") == n,
        )
        gaps = self.conn.execute(
            "SELECT reason, round(epoch(gap_end - gap_start) / 60) "
            "FROM lake.raw.collection_gaps WHERE reason = 'sleep'"
        ).fetchone()
        self.check(
            "the sleep gap has its true length",
            gaps,
            gaps is not None and gaps[0] == "sleep" and gaps[1] >= 44,
        )
        server = await self.moving_the_laptop(server)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.shutdown()
        server.server_close()
        self.conn.close()
        self.write_report()
        failed = sum(not row[2] for row in self.rows)
        print(f"{failed} failed; wrote {REPORT}")
        return 1 if failed else 0

    async def until(self, ready: Callable[[], bool]) -> None:
        for _ in range(1200):
            if ready():
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("the drill waited 60 s for a step")

    async def catch_up_finished(self, limit: float) -> float:
        started = time.monotonic()
        running = self.pipeline.catch_up_task
        if running is not None:
            await asyncio.wait_for(asyncio.shield(running), limit)
        return time.monotonic() - started

    async def moving_the_laptop(
        self, server: ThreadingHTTPServer
    ) -> ThreadingHTTPServer:
        """Sleep, a slow failing probe, a return, a drop during catch-up."""
        n = self.feeds.n_feeds
        await self.catch_up_finished(120)
        # The short drop below is under this, as a real short drop would be.
        self.settings.poll.catch_up_min_gap_seconds = 60.0
        reads_before = self.feeds.sitemap_reads

        # The network goes as the lid closes. A failing feed starts a probe
        # that is still running when the clock check sees the sleep.
        server.shutdown()
        server.server_close()
        self.up = False
        self.probe_delay = SLOW_PROBE_SECONDS
        await self.until(lambda: self.probes_running > 0)
        wall = time.time() - 2700
        slept_from = datetime.fromtimestamp(wall, UTC)
        self.gate.note_clocks(wall, 100.0, wall + 2705, 105.0)
        for _ in range(OVERRUN):
            self.feeds.publish("away", slept_from + timedelta(minutes=1))
        await asyncio.sleep(SLOW_PROBE_SECONDS + 1)
        self.check(
            "a slow failing probe leaves the outage's start at the sleep",
            self.gate.offline_since,
            self.gate.offline_since == slept_from
            and self.gate.offline_reason == "sleep",
        )

        # The network returns, and catch-up starts reading.
        self.probe_delay = 0.0
        self.feeds.sitemap_delay = SITEMAP_DELAY_SECONDS
        server = serve(self.feeds, self.port)
        self.up = True
        await self.until(lambda: self.feeds.sitemap_reads >= reads_before + 20)
        running = self.pipeline.catch_up_task

        # A short drop during catch-up.
        server.shutdown()
        server.server_close()
        self.up = False
        await asyncio.sleep(SHORT_DROP_SECONDS)
        self.check(
            "catch-up is still owed during a short drop",
            self.poller.catch_up_since,
            self.poller.catch_up_since == slept_from,
        )
        server = serve(self.feeds, self.port)
        self.up = True
        took = await self.catch_up_finished(120)
        self.feeds.sitemap_delay = 0.0
        self.check(
            "catch-up finishes after a drop under the catch-up minimum",
            f"{took:.0f} s",
            self.poller.catch_up_since is None
            and self.poller.last_catch_up is not None
            and self.poller.last_catch_up["since"] == slept_from
            and self.poller.last_catch_up["gave_up"] == [],
        )
        self.check(
            "one run lasted through the drop",
            "same task",
            self.pipeline.catch_up_task is running,
        )
        self.check(
            "every story from the sleep is read",
            f"{self.seen('away')} of {n * OVERRUN}",
            self.seen("away") == n * OVERRUN,
        )
        gaps = self.conn.execute(
            "SELECT reason, round(epoch(gap_end - gap_start) / 60) "
            "FROM lake.raw.collection_gaps WHERE gap_start = $start",
            {"start": slept_from},
        ).fetchall()
        self.check(
            "the gap row starts at the sleep",
            gaps,
            len(gaps) == 1 and gaps[0][0] == "sleep" and gaps[0][1] >= 44,
        )
        await asyncio.sleep(self.settings.poll.resume_stagger_seconds + 5)
        health = self.poller.health()
        self.check(
            "no feed is penalized by the sequence",
            health,
            health["healthy"] == n,
        )
        return server

    def write_report(self) -> None:
        failed = sum(not row[2] for row in self.rows)
        lines = [
            "# Outage drill",
            "",
            f"Run {datetime.now(UTC):%Y-%m-%d %H:%M} UTC on a scratch lake.",
            f"{self.feeds.n_feeds} feeds on a local server, "
            f"{self.outage:.0f} s offline, poll cadence shortened to 3 s.",
            f"{len(self.rows) - failed} of {len(self.rows)} checks passed.",
            "",
            "The local server stopped and refused connections for the outage.",
            "Sleep was simulated with a clock difference and has not been",
            "exercised here by closing the lid.",
            "",
            "| Check | Seen | Result |",
            "|---|---|---|",
        ]
        for what, seen, ok in self.rows:
            lines.append(f"| {what} | {seen} | {'pass' if ok else 'FAIL'} |")
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feeds", type=int, default=200)
    parser.add_argument("--outage", type=float, default=20.0)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    drill = Drill(args.feeds, args.outage, args.port)
    raise SystemExit(asyncio.run(drill.run()))


if __name__ == "__main__":
    main()
