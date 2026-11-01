"""Tells an outage of the whole network apart from one failing feed.

A request can fail because one site is down, or because the laptop was asleep,
is between networks, or sits behind a Wi-Fi login page. Only the first counts
against a feed. The rest hold collection until the network returns, then every
feed polls again at once.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

TICK_SECONDS = 5.0

Probe = Callable[[], Awaitable[bool]]
OnRestored = Callable[[dict[str, Any]], Awaitable[None]]


async def _always_up() -> bool:
    return True


def https_probe(settings: Settings) -> Probe:
    """True when any probe host answers over HTTPS.

    Any status counts. A Wi-Fi login page cannot pass the certificate check.
    """
    client: httpx.AsyncClient | None = None

    async def one(url: str) -> bool:
        nonlocal client
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.poll.probe_timeout_seconds),
                headers={"User-Agent": settings.poll.user_agent},
            )
        try:
            await client.head(url)
        except httpx.HTTPError:
            return False
        return True

    async def probe() -> bool:
        urls = settings.poll.probe_urls
        return any(await asyncio.gather(*(one(url) for url in urls)))

    return probe


class NetworkGate:
    """Holds network work while offline and says when the network returns."""

    def __init__(
        self,
        settings: Settings,
        probe: Probe | None = None,
        on_restored: OnRestored | None = None,
    ) -> None:
        self.settings = settings
        self.probe = probe or _always_up
        self.on_restored = on_restored
        self.online = asyncio.Event()
        self.online.set()
        self.offline_since: datetime | None = None
        self.offline_reason: str | None = None
        self.last_gap: dict[str, Any] | None = None
        self._resume = asyncio.Event()
        self._lock = asyncio.Lock()
        self._confirmed_at = -math.inf
        self._last_probe = -math.inf

    @property
    def is_online(self) -> bool:
        return self.online.is_set()

    async def reachable(self) -> bool:
        """Whether a failed request should count against its site.

        Failing feeds share one probe, and a recent success is reused.
        """
        if not self.is_online:
            return False
        async with self._lock:
            if not self.is_online:
                return False
            age = time.monotonic() - self._confirmed_at
            if age < self.settings.poll.probe_cache_seconds:
                return True
            if await self.probe():
                self._confirmed_at = time.monotonic()
                return True
            self._go_offline("network", datetime.now(UTC))
            return False

    async def wait_online(self) -> bool:
        """Wait for the network. True if this call had to wait."""
        if self.is_online:
            return False
        await self.online.wait()
        return True

    async def sleep_or_resume(self, delay: float) -> bool:
        """Wait out a delay. True if the network returned during it."""
        resume = self._resume
        try:
            await asyncio.wait_for(resume.wait(), delay)
        except TimeoutError:
            return False
        return True

    def _go_offline(self, reason: str, since: datetime) -> None:
        self.online.clear()
        self.offline_since = since
        self.offline_reason = reason
        log.warning("network_offline", reason=reason, since=since.isoformat())

    async def _go_online(self) -> None:
        now = datetime.now(UTC)
        since = self.offline_since or now
        gap = {
            "gap_start": since,
            "gap_end": now,
            "reason": self.offline_reason,
        }
        if self.on_restored is not None:
            await self.on_restored(gap)
        self.last_gap = gap
        self.offline_since = None
        self.offline_reason = None
        self._confirmed_at = time.monotonic()
        self.online.set()
        resume, self._resume = self._resume, asyncio.Event()
        resume.set()
        log.info(
            "network_restored",
            offline_seconds=round((now - since).total_seconds()),
            reason=gap["reason"],
        )

    def note_clocks(
        self, wall: float, mono: float, now_wall: float, now_mono: float
    ) -> None:
        """The monotonic clock stops while macOS sleeps; the wall clock runs.

        A difference between them means the laptop slept.
        """
        slept = (now_wall - wall) - (now_mono - mono)
        if slept < self.settings.poll.sleep_gap_seconds:
            return
        since = datetime.fromtimestamp(wall, UTC)
        log.info("system_slept", seconds=round(slept))
        if self.is_online:
            self._go_offline("sleep", since)
        elif self.offline_since is None or since < self.offline_since:
            self.offline_since = since
            self.offline_reason = "sleep"

    async def check_restored(self) -> None:
        """Probe while offline, no more often than the configured rate."""
        if self.is_online:
            return
        now = time.monotonic()
        if now - self._last_probe < self.settings.poll.probe_seconds:
            return
        self._last_probe = now
        if await self.probe():
            await self._go_online()

    async def watch(self) -> None:
        wall, mono = time.time(), time.monotonic()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            now_wall, now_mono = time.time(), time.monotonic()
            self.note_clocks(wall, mono, now_wall, now_mono)
            wall, mono = now_wall, now_mono
            await self.check_restored()

    def status(self) -> dict[str, Any]:
        return {
            "online": self.is_online,
            "offline_since": self.offline_since,
            "reason": self.offline_reason,
            "last_gap": self.last_gap,
        }
