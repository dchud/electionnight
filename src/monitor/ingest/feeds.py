"""Feed parsing, URL canonicalization, and entry normalization."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

import feedparser

SUMMARY_MAX_CHARS = 2000
RAW_ENTRY_MAX_CHARS = 8000
SITEMAP_MAX_BYTES = 20_000_000

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


def raw_entry(entry: Any) -> dict[str, Any]:
    """The entry as the feed sent it, up to a size cap."""
    contents = entry.get("content") or []
    raw: dict[str, Any] = {
        "id": entry.get("id"),
        "published": entry.get("published"),
        "updated": entry.get("updated"),
        "title": entry.get("title"),
        "link": entry.get("link"),
        "author": entry.get("author"),
        "tags": [tag.get("term") for tag in entry.get("tags", [])],
        "summary": entry.get("summary"),
        "content": contents[0].get("value") if contents else None,
    }
    used = sum(len(str(value)) for value in raw.values() if value)
    if used <= RAW_ENTRY_MAX_CHARS:
        return raw
    room = RAW_ENTRY_MAX_CHARS
    for name in ("summary", "content"):
        text = raw[name] or ""
        raw[name] = text[: max(room // 2, 0)]
    raw["truncated"] = True
    return raw


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
                raw=raw_entry(entry),
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


def _local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _child_text(element: ElementTree.Element, name: str) -> str:
    """Text of the first descendant with this local name."""
    for child in element.iter():
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _sitemap_date(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sitemap_root(content: bytes) -> ElementTree.Element | None:
    if len(content) > SITEMAP_MAX_BYTES:
        return None
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None


def parse_news_sitemap(content: bytes | str) -> ParsedFeed:
    """Parse a Google News sitemap. It has titles and dates, not summaries."""
    data = content.encode() if isinstance(content, str) else content
    root = _sitemap_root(data)
    if root is None:
        return ParsedFeed(entries=[], title="", bozo=True, language="")
    entries: list[FeedEntry] = []
    for node in root:
        if _local_name(node.tag) != "url":
            continue
        url = _child_text(node, "loc")
        title = strip_html(_child_text(node, "title"))
        if not url.startswith("http") or not title:
            continue
        date = _child_text(node, "publication_date")
        language = _child_text(node, "language")[:2].lower()
        entries.append(
            FeedEntry(
                url=url,
                canonical_url=canonicalize_url(url),
                guid=url[:500],
                title=title[:500],
                summary="",
                author="",
                published_at=_sitemap_date(date),
                language=language,
                categories=[],
                raw={
                    "loc": url,
                    "title": title,
                    "publication_date": date,
                    "language": language,
                },
            )
        )
    return ParsedFeed(entries=entries, title="", bozo=False, language="")


def sitemap_children(content: bytes) -> list[str]:
    """Sitemap files listed by a sitemap index, news files first."""
    root = _sitemap_root(content)
    if root is None or _local_name(root.tag) != "sitemapindex":
        return []
    urls = [
        _child_text(node, "loc")
        for node in root
        if _local_name(node.tag) == "sitemap"
    ]
    urls = [url for url in urls if url.startswith("http")]
    news = [url for url in urls if "news" in url.lower()]
    return news or urls


def same_site(parent: str, child: str) -> bool:
    """Whether a listed URL stays on the site that listed it."""
    parent_host = (urlsplit(parent).hostname or "").lower()
    child_host = (urlsplit(child).hostname or "").lower()
    base = ".".join(parent_host.split(".")[-2:])
    return child_host == base or child_host.endswith(f".{base}")
