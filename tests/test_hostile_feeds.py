"""Feed and sitemap parsing with hostile bodies and hostile field values."""

import time
from datetime import UTC, datetime

from monitor.ingest.feeds import (
    RAW_ENTRY_MAX_CHARS,
    parse_feed,
    parse_news_sitemap,
    raw_entry,
    strip_html,
    struct_to_datetime,
)

LINK = "https://example.com/a"


def rss(item_xml: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        f"<item>{item_xml}</item></channel></rss>"
    ).encode()


def news_sitemap(loc: str, title: str, date: str) -> bytes:
    return (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        f"<url><loc>{loc}</loc><news:news><news:title>{title}</news:title>"
        f"<news:publication_date>{date}</news:publication_date>"
        "</news:news></url></urlset>"
    ).encode()


def test_a_body_naming_a_local_file_is_not_opened(tmp_path):
    planted = tmp_path / "planted.xml"
    planted.write_bytes(rss(f"<title>From disk</title><link>{LINK}</link>"))
    assert parse_feed(str(planted).encode()).entries == []
    assert parse_feed(str(planted)).entries == []


def test_feed_bytes_still_parse():
    feed = parse_feed(rss(f"<title>Polls close</title><link>{LINK}</link>"))
    assert [entry.title for entry in feed.entries] == ["Polls close"]


def test_tag_stripping_stays_fast_on_unclosed_brackets():
    started = time.perf_counter()
    assert strip_html("<" * 160_000).startswith("<")
    assert time.perf_counter() - started < 2.0


def test_hostile_summary_does_not_stall_the_feed_parser():
    body = rss(
        f"<title>x</title><link>{LINK}</link>"
        "<description><![CDATA[" + "<" * 160_000 + "]]></description>"
    )
    started = time.perf_counter()
    parse_feed(body)
    assert time.perf_counter() - started < 2.0


def test_tag_stripping_still_removes_tags():
    assert strip_html("<p>Ohio <b>votes</b> &amp; counts</p>") == (
        "Ohio votes & counts"
    )


def test_categories_are_capped_in_length_and_number():
    tags = "".join(
        f"<category>{n}{'x' * 200_000}</category>" for n in range(30)
    )
    entry = parse_feed(
        rss(f"<title>a</title><link>{LINK}</link>{tags}")
    ).entries[0]
    assert len(entry.categories) == 20
    assert max(len(term) for term in entry.categories) <= 100


def test_an_entry_with_an_oversized_link_is_dropped():
    long_link = LINK + "/" + "L" * 1_000_000
    body = rss(f"<title>a</title><link>{long_link}</link>")
    assert parse_feed(body).entries == []


def test_a_long_ordinary_link_is_kept():
    link = LINK + "/" + "L" * 1500
    entry = parse_feed(rss(f"<title>a</title><link>{link}</link>")).entries[0]
    assert entry.url == link


def test_a_sitemap_entry_with_an_oversized_link_is_dropped():
    body = news_sitemap(
        LINK + "/" + "L" * 1_000_000, "Title", "2026-11-03T20:00:00Z"
    )
    assert parse_news_sitemap(body).entries == []


def test_raw_entry_size_covers_every_field():
    entry = {
        "id": "i" * 1_000_000,
        "published": "p" * 1_000_000,
        "updated": "u" * 1_000_000,
        "title": "t" * 1_000_000,
        "link": "l" * 1_000_000,
        "author": "a" * 1_000_000,
        "tags": [{"term": "c" * 1_000_000} for _ in range(50)],
        "summary": "s" * 1_000_000,
        "content": [{"value": "v" * 1_000_000}],
    }
    raw = raw_entry(entry)
    assert raw["truncated"] is True
    used = sum(
        len(str(value)) for name, value in raw.items() if name != "truncated"
    )
    assert used <= RAW_ENTRY_MAX_CHARS


def test_a_small_raw_entry_is_not_marked_truncated():
    raw = raw_entry({"title": "Short", "summary": "Also short"})
    assert raw["title"] == "Short"
    assert "truncated" not in raw


def test_sitemap_raw_title_is_capped():
    entry = parse_news_sitemap(
        news_sitemap(LINK, "T" * 1_000_000, "2026-11-03T20:00:00Z")
    ).entries[0]
    assert len(entry.title) == 500
    assert len(entry.raw["title"]) <= 500


def test_a_date_outside_the_plausible_range_is_dropped():
    for date in (
        "Mon, 01 Jan 0001 00:00:00 GMT",
        "Sat, 01 Jan 1000 00:00:00 GMT",
        "Fri, 31 Dec 9999 23:59:59 GMT",
    ):
        body = rss(
            f"<title>a</title><link>{LINK}</link><pubDate>{date}</pubDate>"
        )
        assert parse_feed(body).entries[0].published_at is None, date


def test_a_plausible_date_is_kept():
    body = rss(
        f"<title>a</title><link>{LINK}</link>"
        "<pubDate>Tue, 03 Nov 2026 20:00:00 GMT</pubDate>"
    )
    assert parse_feed(body).entries[0].published_at == datetime(
        2026, 11, 3, 20, tzinfo=UTC
    )


def test_struct_dates_outside_the_range_are_none():
    assert struct_to_datetime((1, 1, 1, 0, 0, 0, 0, 1, 0)) is None


def test_sitemap_dates_outside_the_range_do_not_raise():
    for date in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
        entry = parse_news_sitemap(news_sitemap(LINK, "T", date)).entries[0]
        assert entry.published_at is None, date
