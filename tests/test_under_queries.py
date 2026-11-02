"""The query gallery, against a scratch lake."""

import html
import json
import re
from datetime import timedelta
from urllib.parse import quote

import pytest
from test_under import NOW, add_rows, app_client, classified, item, seed

from monitor.ingest.feeds import parse_feed, parse_news_sitemap
from monitor.lake import connect
from monitor.settings import Settings
from monitor.web.under import (
    CHILD_LIMIT_SECONDS,
    GATE_BUSY,
    Disclosed,
    Ran,
    console_available,
    result_cell,
    run_worker,
)
from monitor.web.under_queries import (
    PRODUCERS,
    QUERIES,
    SHAPES,
    SNAPSHOTS_BACK,
    choose_snapshots,
    gallery,
)
from monitor.web.under_worker import check_console_sql, open_lake

TAIL = "THE-END-OF-THE-FULL-ARTICLE"
LONG_TEXT = "word " * 2000 + TAIL


THRESHOLD = Settings().nlp.relevance_threshold
BY_KEY = {query.key: query for query in QUERIES}


def vector(first):
    return [first] + [0.1] * 383


def rows_of(settings, key, older=0, newer=0):
    """One query's rows, read directly from a scratch lake."""
    conn = connect(settings, read_only=True)
    try:
        result = conn.execute(BY_KEY[key].text(older, newer, THRESHOLD))
        names = [column[0] for column in result.description]
        return [
            dict(zip(names, row, strict=True)) for row in result.fetchall()
        ]
    finally:
        conn.close()


def page_text(page):
    return html.unescape(re.sub(r"<[^>]+>", "", page))


@pytest.fixture(scope="module")
def lake_settings(lake_template, tmp_path_factory):
    from conftest import clone_lake

    settings = clone_lake(lake_template, tmp_path_factory.mktemp("gallery"))
    seed(settings)
    rows = {
        "raw.items": [
            item("english", NOW, title="Senator wins in Georgia"),
            item(
                "spanish", NOW, title="Senador gana en Georgia", language="es"
            ),
            item(
                "longfeed",
                NOW,
                raw_entry=json.dumps(
                    {"title": "T", "tags": ["Politics"], "content": LONG_TEXT}
                ),
            ),
        ],
        "nlp.lang": [
            {
                "item_id": item_id,
                "language": language,
                "confidence": 0.99,
                "method": "feed+detected",
            }
            for item_id, language in (("english", "en"), ("spanish", "es"))
        ],
        "nlp.classify_a": [
            classified("english", topics=["results"], states=["GA", "FL"]),
            classified("spanish", headline_en="Senator wins in Georgia"),
            classified("english", method="keywords", created_at=NOW),
        ],
        "nlp.embeddings": [
            {
                "item_id": item_id,
                "model": "test-model",
                "vec": vector(value),
                "text_basis": "title+summary",
                "dim": 384,
                "parser": "embed",
                "parser_version": "1",
            }
            for item_id, value in (("english", 0.9), ("spanish", 0.88))
        ],
        "raw.article_text": [
            {
                "item_id": "english",
                "fetched_at": NOW,
                "http_status": 200,
                "text": LONG_TEXT,
                "n_chars": len(LONG_TEXT),
            }
        ],
        "nlp.extract_b": [
            {
                "item_id": "english",
                "summary_en": "A summary.",
                "article_tone": -0.5,
                "text_basis": "full_text",
                "parser": "extract_b",
                "parser_version": "1",
            }
        ],
        "nlp.briefs": [
            {
                "brief_id": "brief-one",
                "focus": "all",
                "markdown": "A brief.",
                "citations": json.dumps({"1": "english"}),
                "model": "claude-sonnet-5-5",
            }
        ],
        "nlp.brief_stats": [
            {
                "brief_id": "brief-one",
                "sentences_kept": 3,
                "sentences_dropped": 1,
                "citations_dropped": 0,
            }
        ],
        "nlp.rejected_quotes": [
            {
                "item_id": "english",
                "kind": "tone",
                "quote": "A quote",
                "reason": "no quote",
            }
        ],
        "nlp.storylines": [
            {
                "storyline_id": "s1",
                "version": 2,
                "label": "Georgia race, later",
                "status": "active",
            }
        ],
        "gold.comention_edges": [
            {
                "bin_start": NOW,
                "entity_a": "e1",
                "entity_b": "e2",
                "n": 2,
            }
        ],
    }
    add_rows(settings, rows)
    add_rows(
        settings,
        {
            "nlp.mentions": [
                {
                    "mention_id": f"m{n}",
                    "item_id": "english",
                    "surface": "Name",
                    "entity_id": entity,
                    "tone": -1.0,
                    "created_at": NOW + timedelta(seconds=n),
                }
                for n, entity in enumerate(
                    ["e1", "e2", "e1", "e1", "e1", "e1"]
                )
            ],
            "ref.entities": [{"entity_id": "e2", "canonical_name": "Ann Lee"}],
        },
    )
    return settings


@pytest.fixture(scope="module")
def client(lake_settings):
    with app_client(lake_settings) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def under(client):
    return client.app.state.web.under


@pytest.fixture(scope="module")
def view(under):
    return gallery(under, "shape")


def results_of(view):
    return {
        member["query"].key: member["ran"]
        for group in view["groups"]
        for member in group["members"]
    }


def test_each_query_has_every_part():
    keys = [query.key for query in QUERIES]
    assert len(keys) == len(set(keys))
    for query in QUERIES:
        assert query.question.endswith("?"), query.key
        assert query.sql.strip(), query.key
        assert query.interesting.endswith("."), query.key
        assert query.not_mean.endswith("."), query.key
        assert query.shape in SHAPES, query.key
        assert query.producer in PRODUCERS, query.key


def test_every_shape_and_producer_has_a_query():
    assert {query.shape for query in QUERIES} == set(SHAPES)
    assert {query.producer for query in QUERIES} == set(PRODUCERS)


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q.key)
def test_a_query_passes_the_console_allowlist(lake_settings, under, query):
    conn = open_lake(
        {
            "catalog": str(lake_settings.paths.catalog.resolve()),
            "data_path": str(lake_settings.paths.data.resolve()),
        }
    )
    try:
        newer = under.snapshot_id
        check_console_sql(
            conn, query.text(max(0, newer - 1), newer, THRESHOLD)
        )
    finally:
        conn.close()


@pytest.mark.skipif(
    not console_available(), reason="The console needs the macOS sandbox"
)
@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q.key)
def test_a_query_runs_in_the_console(under, view, query):
    sql = results_of(view)[query.key].query.sql
    result = under.console(sql)
    assert result.get("status") == "ok", result.get("rejected") or result.get(
        "error"
    )


def test_every_query_runs_on_a_scratch_lake(view):
    assert view["errors"] == []
    assert sum(len(group["members"]) for group in view["groups"]) == len(
        QUERIES
    )


def test_grouping_by_producer_shows_each_query_once(under):
    data = gallery(under, "producer")
    assert data["by"] == "producer"
    keys = [m["query"].key for g in data["groups"] for m in g["members"]]
    assert sorted(keys) == sorted(query.key for query in QUERIES)
    titles = {group["title"] for group in data["groups"]}
    assert PRODUCERS["keywords"] in titles
    assert PRODUCERS["briefs"] in titles


def test_an_unknown_grouping_falls_back_to_shape(under):
    assert gallery(under, "nonsense")["by"] == "shape"


def test_no_query_returns_full_article_text(view):
    for key, ran in results_of(view).items():
        assert TAIL not in json.dumps(ran.rows, default=str), key


def test_the_page_holds_no_full_article_text_or_whole_vector(client):
    for by in ("shape", "producer"):
        html = client.get(f"/under/queries?by={by}").text
        assert TAIL not in html
        assert "word word word word word word word" not in html
        assert "0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1" not in html


def test_the_page_shows_each_part_of_a_query(client):
    html = client.get("/under/queries").text
    query = QUERIES[0]
    assert query.question in html
    assert "Why it is interesting" in html
    assert "What it does not mean" in html
    assert "SELECT field, count(*) AS entries" in page_text(html)
    assert "Open in the SQL console" in html
    assert "<th>field</th>" in html


def test_english_and_spanish_stories_match_across_languages(view):
    ran = results_of(view)["vector_english_spanish"]
    assert ran.first["english_title"] == "Senator wins in Georgia"
    assert ran.first["spanish_title"] == "Senador gana en Georgia"
    assert ran.first["cosine"] > 0.9


def test_the_latest_row_query_takes_the_newest_row(settings):
    add_rows(
        settings,
        {
            "gold.source_bins": [
                {
                    "bin_start": NOW,
                    "source_id": "kept",
                    "n_items": count,
                    "n_relevant": count,
                    "created_at": NOW + timedelta(minutes=minutes),
                }
                # The newest row is neither the largest nor the last written.
                for count, minutes in ((5, 0), (2, 2), (9, 1))
            ]
            + [
                {
                    "bin_start": NOW,
                    "source_id": "vanished",
                    "n_items": count,
                    "n_relevant": count,
                    "created_at": NOW + timedelta(minutes=minutes),
                }
                for count, minutes in ((4, 0), (0, 1))
            ],
        },
    )
    row = rows_of(settings, "latest_naive_sum")[0]
    assert row["rows_stored"] == 5
    assert row["naive_sum"] == 20
    assert row["latest_row_sum"] == 2
    assert row["distinct_bins"] == 1


def fields_query_rows(settings, entries):
    add_rows(
        settings,
        {
            "raw.items": [
                item(f"entry{n}", NOW, raw_entry=entry)
                for n, entry in enumerate(entries)
            ]
        },
    )
    return rows_of(settings, "feed_fields")


FEED_WITH_EXTRAS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:x="urn:x">
<channel><title>t</title><item>
<title>A story about the Georgia race</title><link>https://e.com/a</link>
<guid>g1</guid><description>A summary</description>
<category>Politics</category>
<media:content url="https://e.com/i.jpg" medium="image"/>
<x:section>Metro</x:section><comments>https://e.com/c</comments>
<pubDate>Sun, 01 Nov 2026 18:00:00 GMT</pubDate>
</item></channel></rss>"""
SITEMAP = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
 xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>
<loc>https://e.com/b</loc><news:news><news:publication_date>2026-11-01
</news:publication_date><news:title>A title</news:title>
<news:publication><news:language>en</news:language></news:publication>
</news:news></url></urlset>"""


def test_raw_entry_holds_the_fixed_fields_the_gallery_states(settings):
    feed_raw = parse_feed(FEED_WITH_EXTRAS).entries[0].raw
    sitemap_raw = parse_news_sitemap(SITEMAP).entries[0].raw
    assert len(feed_raw) == 9
    assert len(sitemap_raw) == 4
    # Fields the feed sent that raw_entry does not keep.
    assert not {"media_content", "x_section", "comments"} & set(feed_raw)
    assert b"x:section" in FEED_WITH_EXTRAS
    interesting = BY_KEY["feed_fields"].interesting
    assert "nine from a feed entry" in interesting
    assert "four from a sitemap row" in interesting
    assert "archive files" in interesting
    assert "whatever a feed sent" not in interesting
    assert "raw_entry" in BY_KEY["feed_fields"].question
    long_entry = (
        parse_feed(FEED_WITH_EXTRAS.replace(b"A summary", b"word " * 3000))
        .entries[0]
        .raw
    )
    assert long_entry["truncated"] is True
    rows = fields_query_rows(
        settings,
        [
            json.dumps(feed_raw),
            json.dumps(sitemap_raw),
            json.dumps(long_entry),
        ],
    )
    fields = {row["field"] for row in rows}
    assert fields == set(feed_raw) | set(sitemap_raw) | {"truncated"}
    assert {row["field"]: row["entries"] for row in rows}["truncated"] == 1


@pytest.mark.parametrize("key", ["list_topics", "list_state_pairs"])
def test_a_relevant_story_meets_the_threshold(settings, key):
    add_rows(
        settings,
        {
            "nlp.classify_a": [
                classified(
                    "above",
                    relevance_conf=THRESHOLD,
                    topics=["turnout"],
                    states=["FL", "GA"],
                ),
                classified(
                    "below",
                    relevance_conf=THRESHOLD - 0.01,
                    topics=["turnout"],
                    states=["FL", "GA"],
                ),
            ]
        },
    )
    assert [row["items"] for row in rows_of(settings, key)] == [1]
    sql = BY_KEY[key].sql
    assert "relevance_conf >= {threshold}" in sql
    assert "Relevant means election_relevant" in sql


def test_each_path_counts_relevant_by_the_same_rule(settings):
    add_rows(
        settings,
        {
            "nlp.classify_a": [
                classified("above", relevance_conf=0.9),
                classified("below", relevance_conf=0.2),
            ]
        },
    )
    row = rows_of(settings, "keyword_share")[0]
    assert row["items"] == 2
    assert row["relevant"] == 1


def embedded(item_id, values, minutes):
    return {
        "item_id": item_id,
        "model": "m",
        "vec": values,
        "text_basis": "title+summary",
        "dim": len(values),
        "created_at": NOW + timedelta(minutes=minutes),
    }


def test_neighbors_leave_out_the_story_and_count_each_item_once(settings):
    add_rows(
        settings,
        {
            "raw.items": [
                item(name, NOW, title=f"Title {name}")
                for name in ("newest", "near", "far")
            ],
            "nlp.embeddings": [
                embedded("far", [0.0, 1.0], 0),
                # The older vector for this item would rank it first.
                embedded("near", [1.0, 0.0], 1),
                embedded("near", [1.0, 1.0], 2),
                embedded("newest", [1.0, 0.0], 3),
            ],
        },
    )
    rows = rows_of(settings, "vector_neighbors")
    assert [row["item_id"] for row in rows] == ["near", "far"]
    assert rows[0]["cosine"] == pytest.approx(0.707, abs=0.001)


def test_language_pairs_search_only_the_newest_500_on_each_side(settings):
    conn = connect(settings)
    conn.execute(
        "INSERT INTO lake.raw.items BY NAME SELECT 'en' || i AS item_id, "
        "'en' || i AS title, now() AS created_at FROM range(501) t(i)"
    )
    conn.execute(
        "INSERT INTO lake.nlp.lang BY NAME SELECT 'en' || i AS item_id, "
        "'en' AS language, now() AS created_at FROM range(501) t(i)"
    )
    # en0 is the oldest. It matches the Spanish story exactly, and the rest
    # do not, so it would be the top pair if it were read.
    conn.execute(
        "INSERT INTO lake.nlp.embeddings BY NAME SELECT 'en' || i AS item_id, "
        "CASE WHEN i = 0 THEN [1.0, 0.0]::FLOAT[] ELSE [1.0, 1.0]::FLOAT[] "
        "END AS vec, TIMESTAMPTZ '2026-11-01 12:00:00+00' + "
        "(i || ' minutes')::INTERVAL AS created_at FROM range(501) t(i)"
    )
    conn.execute(
        "INSERT INTO lake.raw.items BY NAME SELECT 'es0' AS item_id, "
        "'es0' AS title, now() AS created_at"
    )
    conn.execute(
        "INSERT INTO lake.nlp.lang BY NAME SELECT 'es0' AS item_id, "
        "'es' AS language, now() AS created_at"
    )
    conn.execute(
        "INSERT INTO lake.nlp.embeddings BY NAME SELECT 'es0' AS item_id, "
        "[1.0, 0.0]::FLOAT[] AS vec, now() AS created_at"
    )
    conn.close()
    rows = rows_of(settings, "vector_english_spanish")
    assert rows
    assert all(row["english_title"] != "en0" for row in rows)
    assert rows[0]["cosine"] == pytest.approx(0.707, abs=0.001)
    assert "newest 500" in BY_KEY["vector_english_spanish"].question
    assert "newest 500" in BY_KEY["vector_english_spanish"].not_mean


def test_degree_leaves_out_an_edge_that_vanished(settings):
    edge = {"bin_start": NOW, "entity_a": "e1", "entity_b": "e2"}
    add_rows(
        settings,
        {
            "gold.comention_edges": [
                {**edge, "n": 3, "created_at": NOW},
                {**edge, "n": 0, "created_at": NOW + timedelta(minutes=1)},
                {
                    "bin_start": NOW,
                    "entity_a": "e1",
                    "entity_b": "e3",
                    "n": 1,
                    "created_at": NOW,
                },
            ],
            "ref.entities": [
                {"entity_id": "e1", "canonical_name": "Ann"},
                {"entity_id": "e3", "canonical_name": "Cal"},
            ],
        },
    )
    rows = rows_of(settings, "graph_degree")
    assert {row["entity"]: row["neighbors"] for row in rows} == {
        "Ann": 1,
        "Cal": 1,
    }
    assert {row["entity"]: row["co_mentions"] for row in rows} == {
        "Ann": 1,
        "Cal": 1,
    }


def test_entity_tone_ranks_by_mentions_not_by_toned_mentions(settings):
    add_rows(
        settings,
        {
            "nlp.classify_a": [
                classified(f"i{n}", is_opinion=False) for n in range(100)
            ],
            "nlp.mentions": [
                {
                    "mention_id": f"named-{n}",
                    "item_id": f"i{n}",
                    "entity_id": "often",
                    "tone": 1.0 if n < 6 else None,
                }
                for n in range(100)
            ]
            + [
                {
                    "mention_id": f"toned-{n}",
                    "item_id": f"i{n}",
                    "entity_id": "seldom",
                    "tone": 0.5,
                }
                for n in range(10)
            ],
            "ref.entities": [
                {"entity_id": "often", "canonical_name": "Named often"},
                {"entity_id": "seldom", "canonical_name": "Named seldom"},
            ],
        },
    )
    rows = rows_of(settings, "extract_entity_tone")
    assert rows[0]["entity"] == "Named often"
    assert rows[0]["mentions"] == 100
    assert rows[0]["n"] == 6
    assert rows[1]["entity"] == "Named seldom"


def resolved_then_unresolved(settings):
    add_rows(
        settings,
        {
            "nlp.classify_a": [classified("i1", is_opinion=False)],
            "nlp.mentions": [
                {
                    "mention_id": f"m{n}",
                    "item_id": "i1",
                    "entity_id": entity,
                    "tone": 1.0,
                    "created_at": NOW,
                }
                for n, entity in enumerate(("e1", "e2"))
            ],
            "ref.entities": [
                {"entity_id": "e1", "canonical_name": "Ann"},
                {"entity_id": "e2", "canonical_name": "Bob"},
            ],
        },
    )
    add_rows(
        settings,
        {
            "nlp.mentions": [
                {
                    "mention_id": "m1",
                    "item_id": "i1",
                    "entity_id": None,
                    "tone": 1.0,
                    "created_at": NOW + timedelta(minutes=1),
                }
            ]
        },
    )


def test_a_mention_whose_newest_row_has_no_entity_does_not_count(settings):
    resolved_then_unresolved(settings)
    assert rows_of(settings, "graph_pairs") == []


def test_agreement_counts_an_item_once_by_each_paths_newest_row(settings):
    add_rows(
        settings,
        {
            "nlp.classify_a": [
                classified(
                    "both", method="keywords", created_at=NOW, topics=[]
                ),
                classified(
                    "both",
                    method="llm",
                    election_relevant=True,
                    created_at=NOW + timedelta(minutes=1),
                ),
                classified(
                    "both",
                    method="llm",
                    election_relevant=False,
                    created_at=NOW + timedelta(minutes=2),
                ),
            ]
        },
    )
    rows = rows_of(settings, "keyword_agreement")
    assert rows == [
        {"keywords_relevant": True, "model_relevant": False, "items": 1}
    ]


def test_storyline_versions_sit_with_the_labels():
    assert BY_KEY["latest_versions"].producer == "labels"
    assert "Storyline labels" in PRODUCERS["labels"]
    assert PRODUCERS["classify"].startswith("Classification")


def test_the_extraction_note_names_what_the_dashboard_means():
    from monitor.web.readmodel import FACTS_SELECT

    note = BY_KEY["extract_basis"].not_mean
    assert "The state map uses a mean of this same value" in note
    assert "`summary+text`" in note
    assert "x.article_tone AS tone" in FACTS_SELECT


def test_table_changes_is_honest_about_a_merged_file(settings):
    conn = connect(settings)
    for n in range(4):
        conn.execute(
            "INSERT INTO lake.nlp.storylines BY NAME "
            f"SELECT 's{n}-' || i AS storyline_id, 1 AS version, "
            "'L' AS label, now() AS created_at FROM range(2500) t(i)"
        )
    newest = conn.execute(
        "SELECT max(snapshot_id) FROM lake.snapshots()"
    ).fetchone()[0]
    conn.execute("CALL ducklake_merge_adjacent_files('lake')").fetchall()
    plan = conn.execute(
        "EXPLAIN ANALYZE SELECT count(*) FROM ducklake_table_changes("
        f"'lake', 'nlp', 'storylines', {newest}, {newest})"
    ).fetchall()[0][1]
    conn.close()
    # The one merged file holds all 10,000 rows, and two commits are asked for.
    assert "Total Files Read: 1" in plan
    query = BY_KEY["time_changes"]
    assert "without a scan" not in query.sql
    assert "can hold the whole table" in query.not_mean


def expire_snapshots(settings, versions):
    conn = connect(settings)
    listing = ", ".join(str(version) for version in versions)
    conn.execute(
        f"CALL ducklake_expire_snapshots('lake', versions => [{listing}])"
    ).fetchall()
    conn.close()


def test_snapshots_are_chosen_from_those_that_exist():
    assert choose_snapshots([9, 7, 4]).older == 4
    assert choose_snapshots([9, 7, 4]).newer == 9
    assert "snapshot 4 with snapshot 9" in choose_snapshots([9, 7, 4]).note
    only = choose_snapshots([9])
    assert (only.older, only.newer) == (9, 9)
    assert "Only snapshot 9" in only.note
    assert choose_snapshots([]).error


def test_time_queries_survive_a_pruned_snapshot(settings):
    for version in range(4):
        add_rows(
            settings,
            {
                "nlp.storylines": [
                    {"storyline_id": "s1", "version": version, "label": "L"}
                ]
            },
        )
    conn = connect(settings, read_only=True)
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT snapshot_id FROM lake.snapshots() ORDER BY 1 DESC"
        ).fetchall()
    ]
    conn.close()
    newest = ids[0]
    # A plain count back from the newest would land on a missing snapshot.
    expire_snapshots(settings, [newest - 1, newest - 2])
    with app_client(settings) as test_client:
        under = test_client.app.state.web.under
        results = results_of(gallery(under, "shape"))
    for key in ("time_two_snapshots", "time_changes"):
        assert results[key].ok, results[key].error
    older = results["time_two_snapshots"].rows[0]["version"]
    assert older not in (newest - 1, newest - 2)
    assert older < newest


def test_time_queries_say_so_when_one_snapshot_exists(settings):
    add_rows(
        settings,
        {"nlp.storylines": [{"storyline_id": "s1", "version": 1}]},
    )
    conn = connect(settings)
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT snapshot_id FROM lake.snapshots() ORDER BY 1"
        ).fetchall()
    ]
    conn.close()
    expire_snapshots(settings, ids[:-1])
    with app_client(settings) as test_client:
        page = test_client.get("/under/queries").text
    assert f"Only snapshot {ids[-1]} still exists" in page


def test_time_queries_say_so_when_snapshots_cannot_be_read(client, under):
    real = under.catalog

    def failing(sql, params=None):
        if "ducklake_snapshot" in sql and "begin_snapshot" in sql:
            return Ran([], Disclosed(sql, "catalog", params), "disk I/O error")
        return real(sql, params)

    under.catalog = failing
    under._cache.clear()
    try:
        page = client.get("/under/queries").text
    finally:
        under.catalog = real
        under._cache.clear()
    assert "The list of snapshots could not be read" in page
    assert "disk I/O error" in page
    # A query that names no snapshot still ran.
    assert "<th>field</th>" in page


def test_the_comparison_reaches_back_at_most_the_stated_distance():
    ids = list(range(500, 500 - SNAPSHOTS_BACK - 1, -1))
    pair = choose_snapshots(ids)
    assert pair.newer - pair.older == SNAPSHOTS_BACK


def test_vector_shape_shows_a_length_and_first_values(view):
    rows = results_of(view)["vector_shape"].rows
    row = next(r for r in rows if r["model"] == "test-model")
    assert row["vector_length"] == 384
    assert len(row["first_values"]) == 4


def test_time_travel_compares_two_commits(lake_settings, under):
    conn = connect(lake_settings)
    newest = conn.execute(
        "SELECT max(snapshot_id) FROM lake.snapshots()"
    ).fetchone()[0]
    conn.close()
    under._cache.clear()
    ran = results_of(gallery(under, "shape"))["time_two_snapshots"]
    assert f"AT (VERSION => {newest})" in ran.query.sql
    assert ran.ok
    assert {row["read_at"] for row in ran.rows} == {"older", "newer"}


def test_the_console_link_opens_a_query_without_running_it(client, view):
    sql = results_of(view)["vector_shape"].query.sql
    html = client.get(f"/under/sql?sql={quote(sql)}").text
    assert "FROM lake.nlp.embeddings" in html
    assert "ms in the query" not in html


def test_a_busy_gate_says_so_and_offers_a_retry(client, under):
    under._cache.clear()
    assert under._gate.acquire(timeout=30)
    try:
        html = client.get("/under/queries").text
    finally:
        under._gate.release()
        under._cache.clear()
    assert GATE_BUSY in html
    assert ">Retry</button>" in html
    assert "This query returned no rows." not in html


def test_result_cell_formats_values():
    assert result_cell(0.8289999961853027) == "0.829"
    assert result_cell(None) == "NULL"
    assert result_cell([0.123456, 2.0]) == "[0.1235, 2]"
    assert "(9 values)" in result_cell(list(range(9)))
    assert len(result_cell("x" * 900)) == 203


@pytest.mark.slow
def test_the_language_pairs_query_fits_its_memory_at_600k_rows(settings):
    """Run in the child, under the same limits as the page, at scale."""
    conn = connect(settings)
    for table, select in (
        (
            "raw.items",
            "SELECT 'i' || i AS item_id, 'T' || i AS title, "
            "now() AS created_at",
        ),
        (
            "nlp.lang",
            "SELECT 'i' || i AS item_id, CASE WHEN i % 20 = 0 THEN 'es' "
            "ELSE 'en' END AS language, now() AS created_at",
        ),
        (
            "nlp.embeddings",
            "SELECT 'i' || i AS item_id, "
            "[random()::FLOAT FOR x IN range(384)] AS vec, "
            "TIMESTAMPTZ '2026-11-01 12:00:00+00' + "
            "(i || ' seconds')::INTERVAL AS created_at",
        ),
    ):
        conn.execute(
            f"INSERT INTO lake.{table} BY NAME {select} "
            "FROM range(600000) t(i)"
        )
    conn.close()
    outcome = run_worker(
        {
            "catalog": str(settings.paths.catalog.resolve()),
            "data_path": str(settings.paths.data.resolve()),
            "mode": "scan",
            "max_rows": 15,
            "statements": [
                {
                    "name": key,
                    "sql": BY_KEY[key].text(0, 0, THRESHOLD),
                    "params": None,
                }
                for key in ("vector_english_spanish", "vector_neighbors")
            ],
        },
        CHILD_LIMIT_SECONDS,
    )
    assert not outcome.timed_out
    records = {record["name"]: record for record in outcome.records}
    for key in ("vector_english_spanish", "vector_neighbors"):
        assert "error" not in records[key], records[key]
        assert len(records[key]["rows"]) == 8
