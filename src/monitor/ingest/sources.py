"""The canonical source list in `config/sources.csv`."""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from monitor.settings import Settings

COLUMNS = [
    "source_id",
    "name",
    "homepage",
    "feed_urls",
    "scope",
    "home_state",
    "country",
    "language",
    "lean_label",
    "lean_source",
    "lean_source_url",
    "lean_retrieved_on",
    "reliability_note",
    "ownership",
    "state_affiliated",
    "paywall",
    "poll_tier",
    "enabled",
    "notes",
]

TIER_INDEX = {1: 0, 2: 1, 3: 2}


@dataclass
class Source:
    source_id: str
    name: str
    homepage: str
    feed_urls: list[str] = field(default_factory=list)
    scope: str = "national"
    home_state: str = ""
    country: str = "US"
    language: str = "en"
    lean_label: str = "Unrated"
    lean_source: str = ""
    lean_source_url: str = ""
    lean_retrieved_on: str = ""
    reliability_note: str = ""
    ownership: str = ""
    state_affiliated: bool = False
    paywall: str = "none"
    poll_tier: int = 3
    enabled: bool = True
    notes: str = ""

    def to_row(self) -> dict[str, str]:
        row = asdict(self)
        row["feed_urls"] = "|".join(self.feed_urls)
        row["state_affiliated"] = str(self.state_affiliated).lower()
        row["enabled"] = str(self.enabled).lower()
        row["poll_tier"] = str(self.poll_tier)
        return row


def slugify(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}


def source_from_row(row: dict[str, str]) -> Source:
    feeds = [url for url in (row.get("feed_urls") or "").split("|") if url]
    return Source(
        source_id=row["source_id"],
        name=row["name"],
        homepage=row["homepage"],
        feed_urls=feeds,
        scope=row.get("scope") or "national",
        home_state=row.get("home_state") or "",
        country=row.get("country") or "US",
        language=row.get("language") or "en",
        lean_label=row.get("lean_label") or "Unrated",
        lean_source=row.get("lean_source") or "",
        lean_source_url=row.get("lean_source_url") or "",
        lean_retrieved_on=row.get("lean_retrieved_on") or "",
        reliability_note=row.get("reliability_note") or "",
        ownership=row.get("ownership") or "",
        state_affiliated=parse_bool(row.get("state_affiliated")),
        paywall=row.get("paywall") or "none",
        poll_tier=int(row.get("poll_tier") or 3),
        enabled=parse_bool(row.get("enabled")),
        notes=row.get("notes") or "",
    )


def sources_path(settings: Settings) -> Path:
    return settings.paths.config / "sources.csv"


def load_sources(settings: Settings) -> list[Source]:
    path = sources_path(settings)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [source_from_row(row) for row in csv.DictReader(handle)]


def load_news_sitemaps(settings: Settings) -> dict[str, str]:
    """News sitemap per outlet, for catching up after a collection gap."""
    path = settings.paths.config / "news_sitemaps.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["source_id"]: row["url"] for row in csv.DictReader(handle)}


def outlet_for_language(sources: list[Source], language: str) -> Source | None:
    """The first of these outlets that publishes in this language."""
    wanted = language[:2].lower()
    if not wanted:
        return None
    for source in sources:
        if source.language[:2].lower() == wanted:
            return source
    return None


def write_sources(settings: Settings, sources: list[Source]) -> None:
    path = sources_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for source in sources:
            writer.writerow(source.to_row())


def default_lean(row: dict[str, str]) -> str:
    """Starting label for a seed row.

    The seed's `lean_hint` is an unverified expectation, so it is never shown.
    US outlets start as `Unrated` and non-US outlets as `NA`.
    """
    return "Unrated" if (row.get("lean_hint") or "") != "NA" else "NA"


def seed_to_source(row: dict[str, str]) -> Source:
    return Source(
        source_id=slugify(row["name"]),
        name=row["name"],
        homepage=row["homepage"],
        scope=row["scope"],
        home_state=row.get("home_state") or "",
        country=row.get("country") or "US",
        language=row.get("language") or "en",
        lean_label=default_lean(row),
        state_affiliated=parse_bool(row.get("state_affiliated")),
        paywall=row.get("paywall") or "none",
        poll_tier=int(row.get("poll_tier") or 3),
        enabled=False,
        notes=row.get("notes") or "",
    )
