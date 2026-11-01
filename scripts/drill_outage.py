#!/usr/bin/env python3
"""Take the network away from a real poller, then give it back.

Serves generated feeds from a local server and runs the real poller, writer,
and read model on a scratch lake. Taking the server down refuses connections,
as a dropped network does. Sleep is simulated by feeding the gate a clock
difference, because the monotonic clock only stops during real system sleep.

    uv run python scripts/drill_outage.py
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from monitor.ingest.network import NetworkGate
from monitor.ingest.poller import Poller
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect, init_lake
from monitor.settings import Paths, Settings
from monitor.web.readmodel import ReadModel

ROOT = Path("tmp/drill-outage")
REPORT = Path("reports/outage_drill.md")
PORT = 8765
ITEMS_KEPT = 40


class Feeds:
    """Generated stories, each with a published time."""

    def __init__(self, n_feeds: int) -> None:
        self.n_feeds = n_feeds
        self.stories: dict[int, list[tuple[str, datetime]]] = {
            feed: [] for feed in range(n_feeds)
        }
        self.counter = 0
        self.lock = threading.Lock()

    def publish(self, label: str, at: datetime | None = None) -> None:
        with self.lock:
            for feed in range(self.n_feeds):
                self.counter += 1
                self.stories[feed].append(
                    (f"{label}-{self.counter}", at or datetime.now(UTC))
                )

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


def serve(feeds: Feeds) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = feeds.body(int(self.path.rsplit("/", 1)[1]))
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.allow_reuse_address = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Drill:
    def __init__(self, n_feeds: int, outage: float) -> None:
        shutil.rmtree(ROOT, ignore_errors=True)
        self.settings = Settings(
            paths=Paths.under(ROOT, config=Path("config"))
        )
        self.settings.poll.tier_seconds = [3, 3, 3]
        self.settings.poll.jitter_pct = 0
        self.settings.poll.probe_seconds = 1.0
        self.settings.poll.resume_stagger_seconds = 5.0
        self.settings.poll.min_gap_seconds = 1.0
        init_lake(self.settings)
        self.conn = connect(self.settings)
        self.writer = BatchWriter(self.conn, self.settings)
        self.feeds = Feeds(n_feeds)
        self.outage = outage
        self.up = True
        self.rows: list[tuple[str, str, bool]] = []
        self.gate = NetworkGate(
            self.settings, probe=self.probe, on_restored=self.restored
        )
        sources = [
            Source(
                source_id=f"s{n}",
                name=f"S{n}",
                homepage="https://example.com/",
                feed_urls=[f"http://127.0.0.1:{PORT}/{n}"],
                poll_tier=(n % 3) + 1,
            )
            for n in range(n_feeds)
        ]
        self.poller = Poller(
            self.settings, self.writer, sources, set(), gate=self.gate
        )

    async def probe(self) -> bool:
        return self.up

    async def restored(self, gap: dict[str, Any]) -> None:
        seconds = (gap["gap_end"] - gap["gap_start"]).total_seconds()
        if seconds >= self.settings.poll.min_gap_seconds:
            self.writer.add("raw.collection_gaps", [gap])
            self.writer.flush()

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

    async def wait_for_all(self, label: str, limit: float) -> float:
        want = self.feeds.n_feeds
        started = time.monotonic()
        while time.monotonic() - started < limit:
            if self.seen(label) >= want:
                break
            await asyncio.sleep(0.5)
        return time.monotonic() - started

    async def run(self) -> int:
        n = self.feeds.n_feeds
        self.feeds.publish("before")
        server = serve(self.feeds)
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
        server = serve(self.feeds)
        self.up = True
        took = await self.wait_for_all("during", 30)
        self.check(
            f"every feed catches up after {self.outage:.0f} s offline",
            f"{took:.0f} s, {self.seen('during')} of {n} stories",
            self.seen("during") == n,
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
            len(moved) == n and placed,
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
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.shutdown()
        server.server_close()
        self.conn.close()
        self.write_report()
        failed = sum(not row[2] for row in self.rows)
        print(f"{failed} failed; wrote {REPORT}")
        return 1 if failed else 0

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
    args = parser.parse_args()
    raise SystemExit(asyncio.run(Drill(args.feeds, args.outage).run()))


if __name__ == "__main__":
    main()
