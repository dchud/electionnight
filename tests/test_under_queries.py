"""The query gallery, against a scratch lake."""

import json
from datetime import timedelta
from urllib.parse import quote

import pytest
from test_under import NOW, add_rows, app_client, classified, item, seed

from monitor.lake import connect
from monitor.web.under import GATE_BUSY, console_available, result_cell
from monitor.web.under_queries import (
    PRODUCERS,
    QUERIES,
    SHAPES,
    gallery,
)
from monitor.web.under_worker import check_console_sql, open_lake

TAIL = "THE-END-OF-THE-FULL-ARTICLE"
LONG_TEXT = "word " * 2000 + TAIL


def vector(first):
    return [first] + [0.1] * 383


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
        check_console_sql(conn, query.text(max(0, newer - 1), newer))
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
    assert "SELECT field, count(*) AS entries" in html
    assert "Open in the SQL console" in html
    assert "<th>field</th>" in html


def test_english_and_spanish_stories_match_across_languages(view):
    ran = results_of(view)["vector_english_spanish"]
    assert ran.first["english_title"] == "Senator wins in Georgia"
    assert ran.first["spanish_title"] == "Senador gana en Georgia"
    assert ran.first["cosine"] > 0.9


def test_the_latest_row_query_gives_two_different_sums(view):
    row = results_of(view)["latest_naive_sum"].first
    # Three rows for one bin, with counts 1, 2, and 3.
    assert row["rows_stored"] == 3
    assert row["distinct_bins"] == 1
    assert row["naive_sum"] == 6
    assert row["latest_row_sum"] in (1, 2, 3)


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
