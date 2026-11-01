"""OPML round-trip, for curating the feed list in a feed reader."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from monitor.ingest.sources import load_sources, write_sources
from monitor.settings import Settings


def export_opml(settings: Settings, path: Path) -> int:
    """Write enabled feeds grouped by scope. Returns the feed count."""
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "Election Night Monitor sources"
    body = ET.SubElement(root, "body")
    groups: dict[str, ET.Element] = {}
    count = 0
    for source in load_sources(settings):
        if not source.enabled:
            continue
        group = groups.get(source.scope)
        if group is None:
            group = ET.SubElement(
                body, "outline", text=source.scope, title=source.scope
            )
            groups[source.scope] = group
        for url in source.feed_urls:
            ET.SubElement(
                group,
                "outline",
                type="rss",
                text=source.name,
                title=source.name,
                xmlUrl=url,
                htmlUrl=source.homepage,
                category=source.source_id,
            )
            count += 1
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return count


def import_opml(settings: Settings, path: Path) -> str:
    """Make `sources.csv` match the OPML's feed set.

    A feed is matched to its outlet by the `category` attribute, then by
    homepage host. Outlets left with no feeds are disabled. Feeds that match
    no outlet are reported and not added.
    """
    feeds_by_source: dict[str, list[str]] = defaultdict(list)
    sources = load_sources(settings)
    known = {source.source_id for source in sources}
    by_host = {
        (urlsplit(source.homepage).hostname or "").removeprefix(
            "www."
        ): source.source_id
        for source in sources
    }
    unmatched: list[str] = []
    for outline in ET.parse(path).iter("outline"):
        url = outline.get("xmlUrl")
        if not url:
            continue
        source_id = outline.get("category") or ""
        if source_id not in known:
            home = outline.get("htmlUrl") or url
            host = (urlsplit(home).hostname or "").removeprefix("www.")
            source_id = by_host.get(host, "")
        if source_id:
            feeds_by_source[source_id].append(url)
        else:
            unmatched.append(url)
    changed = 0
    for source in sources:
        feeds = feeds_by_source.get(source.source_id, [])
        if set(feeds) != set(source.feed_urls if source.enabled else []):
            changed += 1
        source.feed_urls = feeds
        source.enabled = bool(feeds)
    write_sources(settings, sources)
    message = f"{changed} outlets changed"
    if unmatched:
        message += f"; {len(unmatched)} feeds matched no outlet: " + ", ".join(
            unmatched
        )
    return message
