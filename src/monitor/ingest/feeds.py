"""Feed parsing, URL canonicalization, and entry normalization."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

SUMMARY_MAX_CHARS = 2000

TRACKING_PARAMS = {"fbclid", "gclid", "cmpid", "ocid", "ref", "smid"}
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def canonicalize_url(url: str) -> str:
    """Normalize a URL so copies of one article share one form.

    Lowercases the host; removes tracking parameters, fragments, default
    ports, trailing slashes, and AMP path segments.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in {80, 443}:
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    path = re.sub(r"/amp(?=/|$)", "", path)
    path = re.sub(r"\.amp(?=\.html$|$)", "", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in TRACKING_PARAMS
        and key.lower() != "outputtype"
    ]
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def strip_html(text: str | None) -> str:
    """Remove tags and collapse whitespace."""
    if not text:
        return ""
    without_tags = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", html.unescape(without_tags)).strip()


def struct_to_datetime(value: struct_time | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


@dataclass
class FeedEntry:
    url: str
    canonical_url: str
    guid: str
    title: str
    summary: str
    author: str
    published_at: datetime | None
    language: str
    categories: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedFeed:
    entries: list[FeedEntry]
    title: str
    bozo: bool
    language: str


def _entry_date(entry: Any) -> datetime | None:
    return struct_to_datetime(
        entry.get("published_parsed") or entry.get("updated_parsed")
    )


def parse_feed(content: bytes | str) -> ParsedFeed:
    """Parse feed bytes. Entries without a link or title are skipped."""
    parsed = feedparser.parse(content)
    feed_language = (parsed.feed.get("language") or "")[:2].lower()
    entries: list[FeedEntry] = []
    for entry in parsed.entries:
        url = (entry.get("link") or "").strip()
        title = strip_html(entry.get("title"))
        if not url or not title or not url.startswith("http"):
            continue
        summary = strip_html(entry.get("summary"))[:SUMMARY_MAX_CHARS]
        categories = [
            term
            for term in (tag.get("term") for tag in entry.get("tags", []))
            if term
        ]
        entries.append(
            FeedEntry(
                url=url,
                canonical_url=canonicalize_url(url),
                guid=(entry.get("id") or url)[:500],
                title=title[:500],
                summary=summary,
                author=(entry.get("author") or "")[:200],
                published_at=_entry_date(entry),
                language=feed_language,
                categories=categories[:20],
                raw={
                    "id": entry.get("id"),
                    "published": entry.get("published"),
                    "updated": entry.get("updated"),
                },
            )
        )
    return ParsedFeed(
        entries=entries,
        title=strip_html(parsed.feed.get("title")),
        bozo=bool(parsed.bozo),
        language=feed_language,
    )


def newest_entry_at(feed: ParsedFeed) -> datetime | None:
    dates = [e.published_at for e in feed.entries if e.published_at]
    return max(dates) if dates else None
