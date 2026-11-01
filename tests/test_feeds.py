from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from monitor.ingest.discover import (
    find_alternate_links,
    rank_feed,
    validate_feed,
)
from monitor.ingest.feeds import (
    canonicalize_url,
    parse_feed,
    strip_html,
)
from monitor.ingest.opml import export_opml, import_opml
from monitor.ingest.sources import (
    Source,
    load_sources,
    seed_to_source,
    slugify,
    write_sources,
)
from monitor.settings import Paths, Settings

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://WWW.Example.COM/a/?utm_source=x&id=1#frag",
            "https://www.example.com/a?id=1",
        ),
        ("https://example.com/a?fbclid=1&gclid=2", "https://example.com/a"),
        ("https://example.com/a?cmpid=abc&b=2", "https://example.com/a?b=2"),
        ("https://example.com/story/amp/", "https://example.com/story"),
        ("https://example.com/amp/story", "https://example.com/story"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/a?b=1&c=2", "https://example.com/a?b=1&c=2"),
    ],
)
def test_canonicalize_url(raw, expected):
    assert canonicalize_url(raw) == expected


def test_strip_html_removes_tags_and_entities():
    assert strip_html("<p>Vote &amp; count\n <b>now</b></p>") == (
        "Vote & count now"
    )


@pytest.mark.parametrize("name", ["rss2.xml", "atom.xml"])
def test_parse_feed_reads_entries(name):
    feed = parse_feed((FIXTURES / name).read_bytes())
    assert len(feed.entries) == 5
    first = feed.entries[0]
    assert first.title.startswith("Story 1")
    assert first.published_at is not None
    assert "utm_source" not in first.canonical_url
    assert first.summary


def test_parse_feed_tolerates_malformed_input():
    feed = parse_feed((FIXTURES / "malformed.xml").read_bytes())
    assert feed.bozo is True
    assert [e.title for e in feed.entries] == ["Only readable entry"]


def test_validate_feed_rules():
    feed = parse_feed((FIXTURES / "rss2.xml").read_bytes())
    newest = max(e.published_at for e in feed.entries)
    assert validate_feed(feed, "national", newest)[0] is True
    late = newest + timedelta(days=4)
    assert validate_feed(feed, "national", late)[0] is False
    assert validate_feed(feed, "local", late)[0] is True
    assert validate_feed(feed, "local", newest + timedelta(days=8))[0] is False
    feed.entries = feed.entries[:4]
    assert validate_feed(feed, "national", newest)[0] is False


def test_find_alternate_links_resolves_relative_urls():
    page = """<html><head>
      <link rel="alternate" type="application/rss+xml" href="/politics/feed">
      <link rel="alternate" type="application/atom+xml"
            href="https://x.org/a.atom">
      <link rel="stylesheet" href="/x.css">
    </head></html>"""
    assert find_alternate_links(page, "https://x.org/news") == [
        "https://x.org/politics/feed",
        "https://x.org/a.atom",
    ]


def test_rank_feed_prefers_politics():
    urls = ["https://x.org/feed", "https://x.org/politics/feed"]
    assert sorted(urls, key=rank_feed)[0].endswith("politics/feed")


def test_seed_row_never_carries_lean_hint():
    row = {
        "name": "La Opinión",
        "homepage": "https://laopinion.com",
        "scope": "local",
        "home_state": "CA",
        "country": "US",
        "language": "es",
        "lean_hint": "LL",
        "state_affiliated": "false",
        "paywall": "none",
        "poll_tier": "2",
        "notes": "",
    }
    source = seed_to_source(row)
    assert source.lean_label == "Unrated"
    assert source.source_id == slugify("La Opinión") == "la-opinion"
    assert seed_to_source({**row, "lean_hint": "NA"}).lean_label == "NA"


def test_opml_round_trip_keeps_feed_set(tmp_path):
    settings = Settings(paths=Paths.under(tmp_path, config=tmp_path))
    write_sources(
        settings,
        [
            Source(
                "a",
                "A News",
                "https://a.org",
                ["https://a.org/feed", "https://a.org/politics/feed"],
            ),
            Source("b", "B News", "https://b.org", ["https://b.org/rss"]),
            Source("c", "C News", "https://c.org", [], enabled=False),
        ],
    )
    path = tmp_path / "out.opml"
    assert export_opml(settings, path) == 3
    message = import_opml(settings, path)
    assert message.startswith("0 outlets changed")
    loaded = {s.source_id: s for s in load_sources(settings)}
    assert set(loaded["a"].feed_urls) == {
        "https://a.org/feed",
        "https://a.org/politics/feed",
    }
    assert loaded["c"].enabled is False


def test_opml_import_disables_outlets_with_no_feeds(tmp_path):
    settings = Settings(paths=Paths.under(tmp_path, config=tmp_path))
    write_sources(
        settings,
        [
            Source("a", "A", "https://a.org", ["https://a.org/feed"]),
            Source("b", "B", "https://b.org", ["https://b.org/rss"]),
        ],
    )
    path = tmp_path / "in.opml"
    path.write_text(
        '<opml version="2.0"><body><outline type="rss" text="A" '
        'xmlUrl="https://a.org/feed" htmlUrl="https://a.org"/>'
        '<outline type="rss" text="Z" xmlUrl="https://z.org/feed"/>'
        "</body></opml>"
    )
    message = import_opml(settings, path)
    assert "1 feeds matched no outlet" in message
    loaded = {s.source_id: s for s in load_sources(settings)}
    assert loaded["a"].enabled is True
    assert loaded["b"].enabled is False


def test_fixture_dates_are_recent_enough_to_exercise_rules():
    feed = parse_feed((FIXTURES / "atom.xml").read_bytes())
    newest = max(e.published_at for e in feed.entries)
    assert newest < datetime(2030, 1, 1, tzinfo=UTC)
