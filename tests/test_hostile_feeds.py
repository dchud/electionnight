"""Feed and sitemap parsing with hostile bodies and hostile field values."""

import time
from datetime import UTC, datetime, timedelta
from datetime import date as calendar_day

import httpx
import pytest

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


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("2026-11-04", calendar_day(2026, 11, 4)),
        ("2026-11-03T20:30:00", calendar_day(2026, 11, 3)),
        ("Tue, 03 Nov 2026 20:30:00 -0000", calendar_day(2026, 11, 3)),
        ("Tue, 03 Nov 2026 20:30:00", calendar_day(2026, 11, 3)),
    ],
)
def test_a_date_with_no_zone_gets_no_invented_time(text, day):
    entry = parse_news_sitemap(news_sitemap(LINK, "T", text)).entries[0]
    assert entry.published_at is None
    assert entry.rough_date == day


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-04T01:30:00Z",
        "2026-11-03T20:30:00-05:00",
        "Tue, 03 Nov 2026 20:30:00 -0500",
        "Wed, 04 Nov 2026 01:30:00 GMT",
    ],
)
def test_a_date_with_a_zone_is_exact(text):
    entry = parse_news_sitemap(news_sitemap(LINK, "T", text)).entries[0]
    assert entry.published_at == datetime(2026, 11, 4, 1, 30, tzinfo=UTC)
    assert entry.rough_date is None


def test_a_date_that_cannot_be_read_is_undated():
    entry = parse_news_sitemap(news_sitemap(LINK, "T", "soon")).entries[0]
    assert entry.published_at is None
    assert entry.rough_date is None


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("2026-11-04T01:30:00Z", True),
        ("2026-11-03T20:00:00Z", False),
        ("2026-11-04", True),
        ("2026-11-03", True),
        ("2026-11-02", False),
        ("2026-11-03T20:30:00", True),
        ("2026-11-01T20:30:00", False),
        ("Tue, 03 Nov 2026 20:30:00 -0500", True),
        ("Tue, 03 Nov 2026 12:00:00 -0500", False),
        ("soon", False),
    ],
)
def test_the_time_filter_keeps_rough_dates_within_a_day(text, kept):
    from monitor.ingest.feeds import published_since

    since = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
    entry = parse_news_sitemap(news_sitemap(LINK, "T", text)).entries[0]
    assert published_since(entry, since) is kept


async def test_a_rough_date_reaches_the_lake_with_no_published_time(settings):
    from monitor.ingest.network import NetworkGate
    from monitor.ingest.poller import FeedState, Poller
    from monitor.ingest.sources import Source
    from monitor.lake import BatchWriter, connect

    since = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
    body = news_sitemap(LINK, "T", "2026-11-04")
    delivered = []
    conn = connect(settings)
    source = Source("s", "S", "https://example.com/", ["u"])
    poller = Poller(
        settings,
        BatchWriter(conn, settings),
        [source],
        set(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=body)
            )
        ),
        gate=NetworkGate(settings),
        on_new_items=delivered.extend,
    )
    new = await poller.poll_once(
        FeedState(source, "https://example.com/n.xml"),
        parse_news_sitemap,
        since - timedelta(minutes=20),
    )
    conn.close()
    assert new == 1
    assert delivered[0]["published_at"] is None


NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"
IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"
VIDEO_NS = "http://www.google.com/schemas/sitemap-video/1.1"


def sitemap_url(inner: str, news_ns: str = NEWS_NS) -> bytes:
    return (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        f'xmlns:news="{news_ns}" xmlns:image="{IMAGE_NS}" '
        f'xmlns:video="{VIDEO_NS}"><url>{inner}</url></urlset>'
    ).encode()


NEWS_BLOCK = (
    "<news:news><news:publication><news:language>es</news:language>"
    "</news:publication><news:publication_date>2026-11-04T01:30:00Z"
    "</news:publication_date><news:title>Polls close</news:title></news:news>"
)


@pytest.mark.parametrize(
    "caption",
    [
        "<image:title>Photo credit AP</image:title>",
        "<image:title></image:title>",
    ],
)
def test_an_image_title_is_not_the_story_title(caption):
    inner = (
        f"<loc>{LINK}</loc><image:image>"
        f"<image:loc>https://example.com/a.jpg</image:loc>{caption}"
        f"</image:image>{NEWS_BLOCK}"
    )
    entry = parse_news_sitemap(sitemap_url(inner)).entries[0]
    assert entry.title == "Polls close"
    assert entry.url == LINK


def test_an_image_address_is_not_the_story_address():
    inner = (
        "<image:image><image:loc>https://example.com/a.jpg</image:loc>"
        f"</image:image><loc>{LINK}</loc>{NEWS_BLOCK}"
    )
    assert parse_news_sitemap(sitemap_url(inner)).entries[0].url == LINK


def test_a_video_date_and_language_are_not_the_story_date_and_language():
    inner = (
        f"<loc>{LINK}</loc><video:video><video:title>A clip</video:title>"
        "<video:publication_date>2020-01-01T00:00:00Z"
        "</video:publication_date><video:language>de</video:language>"
        f"</video:video>{NEWS_BLOCK}"
    )
    entry = parse_news_sitemap(sitemap_url(inner)).entries[0]
    assert entry.title == "Polls close"
    assert entry.language == "es"
    assert entry.published_at == datetime(2026, 11, 4, 1, 30, tzinfo=UTC)


def test_a_url_with_only_an_image_title_has_no_story():
    inner = (
        f"<loc>{LINK}</loc><image:image>"
        "<image:title>A photo</image:title></image:image>"
    )
    assert parse_news_sitemap(sitemap_url(inner)).entries == []


@pytest.mark.parametrize(
    "news_ns",
    [
        NEWS_NS,
        "https://www.google.com/schemas/sitemap-news/0.9",
        "http://www.google.com/schemas/sitemap-news/0.9/",
    ],
)
def test_the_news_namespace_is_read_in_its_common_spellings(news_ns):
    inner = f"<loc>{LINK}</loc>{NEWS_BLOCK}"
    entry = parse_news_sitemap(sitemap_url(inner, news_ns)).entries[0]
    assert entry.title == "Polls close"
