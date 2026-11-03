"""The DuckLake view, against a scratch lake."""

import html
import json
import re
import sqlite3

import pytest
from test_under import NOW, add_rows, app_client, item, seed

from monitor.lake import TABLES, connect
from monitor.maintenance import run_maintenance
from monitor.web.under import GATE_BUSY, Disclosed, Ran
from monitor.web.under_lake import (
    CATALOG_TABLES,
    NO_SOURCE,
    TABLE_SOURCES,
    catalog_counts_sql,
    lake_view,
    sample_cell,
    schema_tree,
    statistic,
)

TAIL = "THE-END-OF-THE-FULL-ARTICLE"
LONG_TEXT = "word " * 2000 + TAIL


@pytest.fixture(scope="module")
def lake_settings(lake_template, tmp_path_factory):
    from conftest import clone_lake

    settings = clone_lake(lake_template, tmp_path_factory.mktemp("lake"))
    seed(settings)
    add_rows(
        settings,
        {
            "raw.article_text": [
                {
                    "item_id": "cap1",
                    "fetched_at": NOW,
                    "http_status": 200,
                    "text": LONG_TEXT,
                    "n_chars": len(LONG_TEXT),
                    "extractor": "trafilatura",
                    "paywalled": False,
                }
            ],
            "raw.items": [
                item(
                    "longfeed",
                    NOW,
                    raw_entry=json.dumps({"content": LONG_TEXT}),
                )
            ],
            "nlp.llm_traces": [
                {
                    "call_id": "long",
                    "purpose": "extract_b",
                    "item_ids": ["cap1"],
                    "request_text": json.dumps({"text": LONG_TEXT}),
                    "response_text": "{}",
                }
            ],
            "nlp.embeddings": [
                {
                    "item_id": f"dim{n}",
                    "model": "m",
                    "vec": [0.25] * 384,
                    "text_basis": "title+summary",
                    "dim": n,
                }
                for n in (9, 10)
            ],
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


def page_text(page):
    """A page's text as read, with tags and escapes gone."""
    return html.unescape(re.sub(r"<[^>]+>", "", page))


def direct(settings, sql, params=None):
    conn = connect(settings, read_only=True)
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


def test_the_view_lists_every_table_in_the_catalog(client):
    html = client.get("/under/lake").text
    for table in TABLES:
        assert f"?table={table}#detail" in html, table
    assert f"Every lake table: {len(TABLES)}" in html


def test_every_table_has_a_listed_source():
    assert set(TABLES) == set(TABLE_SOURCES)


def test_a_new_table_appears_with_no_view_change(settings):
    conn = connect(settings)
    conn.execute(
        "CREATE TABLE lake.raw.brand_new (name TEXT, created_at TIMESTAMPTZ)"
    )
    conn.execute("INSERT INTO lake.raw.brand_new VALUES ('first', now())")
    conn.close()
    with app_client(settings) as test_client:
        html = test_client.get("/under/lake?table=raw.brand_new").text
    assert "?table=raw.brand_new#detail" in html
    assert NO_SOURCE in html
    assert ">first<" in html
    assert "no key" in html


def test_schema_matches_a_direct_catalog_query(lake_settings, under):
    data = lake_view(under, {"table": "nlp.embeddings"})
    rows = direct(
        lake_settings,
        "SELECT c.column_name, c.column_type, c.parent_column "
        "FROM __ducklake_metadata_lake.main.ducklake_column c "
        "JOIN __ducklake_metadata_lake.main.ducklake_table t "
        "USING (table_id) WHERE t.table_name = 'embeddings' "
        "AND c.end_snapshot IS NULL ORDER BY c.column_order",
    )
    schema = data["detail"]["schema"]
    assert [(c["column_name"], c["column_type"]) for c in schema] == [
        (r[0], r[1]) for r in rows
    ]
    by_name = {c["column_name"]: c for c in schema}
    assert by_name["vec"]["column_type"] == "list"
    assert by_name["element"]["depth"] == 1
    assert by_name["element"]["column_type"].startswith("float")
    assert by_name["model"]["depth"] == 0


def test_counts_match_direct_queries(lake_settings, under):
    data = lake_view(under, {"table": "raw.items"})
    detail = data["detail"]
    rows = direct(
        lake_settings,
        "SELECT count(*), count(DISTINCT item_id) FROM lake.raw.items",
    )[0]
    assert detail["row"]["total_rows"] == rows[0]
    assert detail["key_scan"].first["n_rows"] == rows[0]
    assert detail["key_scan"].first["n_keys"] == rows[1]


def stored_where(settings, table):
    """Rows in the catalog and in files, from each row's own `filename`."""
    return direct(
        settings,
        "SELECT count(*) FILTER "
        "(WHERE filename LIKE 'ducklake_inlined_data%'), "
        "count(*) FILTER (WHERE filename LIKE '%.parquet') "
        f"FROM lake.{table}",
    )[0]


def test_inlined_and_file_rows_match_where_rows_sit(settings):
    conn = connect(settings)
    conn.execute(
        "INSERT INTO lake.raw.fetches BY NAME "
        "SELECT 'small-' || i AS fetch_id, now() AS created_at "
        "FROM range(5) t(i)"
    )
    conn.execute(
        "INSERT INTO lake.raw.fetches BY NAME "
        "SELECT 'large-' || i AS fetch_id, now() AS created_at "
        "FROM range(2500) t(i)"
    )
    conn.close()
    with app_client(settings) as test_client:
        under = test_client.app.state.web.under
        row = lake_view(under, {"table": "raw.fetches"})["detail"]["row"]
        assert stored_where(settings, "raw.fetches") == (5, 2500)
        assert (row["inlined_rows"], row["parquet_rows"]) == (5, 2500)
        assert row["total_rows"] == 2505
        conn = connect(settings)
        run_maintenance(conn)
        conn.close()
        under._cache.clear()
        row = lake_view(under, {"table": "raw.fetches"})["detail"]["row"]
    assert stored_where(settings, "raw.fetches") == (0, 2505)
    assert (row["inlined_rows"], row["parquet_rows"]) == (0, 2505)


def test_nulls_on_a_list_element_count_null_and_empty_lists(settings):
    conn = connect(settings)
    conn.execute(
        "INSERT INTO lake.raw.items BY NAME "
        "SELECT 'i' || i AS item_id, CASE i % 3 WHEN 0 THEN NULL "
        "WHEN 1 THEN []::VARCHAR[] ELSE ['a', 'b'] END AS feed_categories, "
        "now() AS created_at FROM range(3000) t(i)"
    )
    conn.close()
    with app_client(settings) as test_client:
        under = test_client.app.state.web.under
        detail = lake_view(under, {"table": "raw.items"})["detail"]
        html_page = test_client.get("/under/lake?table=raw.items").text
    by_name = {c["column_name"]: c for c in detail["schema"]}
    element = by_name["element"]
    # 1,000 NULL lists, 1,000 empty lists, and 1,000 lists of two values.
    assert element["n_values"] == 2000
    assert element["n_nulls"] == 2000
    assert not by_name["feed_categories"]["has_stats"]
    assert (
        "Nulls counts NULL lists, empty lists, and null elements" in html_page
    )
    assert "not by these totals" in html_page


def test_a_table_with_no_list_has_no_note_on_lists(client):
    page = client.get("/under/lake?table=raw.fetch_details").text
    assert "Nulls counts NULL lists" not in page
    assert "not by these totals" in page


def test_a_failed_column_read_says_the_scan_did_not_finish(client, under):
    real = under.catalog

    def failing(sql, params=None):
        if "column_order, column_name" in sql:
            return Ran([], Disclosed(sql, "catalog", params), "disk I/O error")
        return real(sql, params)

    under.catalog = failing
    try:
        response = client.get("/under/lake?table=raw.items")
    finally:
        under.catalog = real
    assert response.status_code == 200
    assert "A scan did not finish" in response.text
    assert "disk I/O error" in response.text
    assert "This table has no rows." not in response.text
    assert ">Retry</button>" in response.text


def test_a_catalog_count_matches_a_direct_query(lake_settings, under):
    data = lake_view(under, {})
    counts = {row["name"]: row["n"] for row in data["catalog"]["rows"]}
    snapshots = direct(
        lake_settings,
        "SELECT count(*) FROM __ducklake_metadata_lake.main.ducklake_snapshot",
    )[0][0]
    assert counts["ducklake_snapshot"] == snapshots


def test_the_catalog_list_matches_a_new_catalog(settings):
    with sqlite3.connect(settings.paths.catalog) as catalog:
        names = {
            row[0]
            for row in catalog.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not row[0].startswith("ducklake_inlined_data_")
            or row[0] == "ducklake_inlined_data_tables"
        }
    assert names == {name for name, _ in CATALOG_TABLES}
    assert "UNION ALL" in catalog_counts_sql()


def test_numeric_statistics_sort_as_numbers(under):
    data = lake_view(under, {"table": "nlp.embeddings"})
    by_name = {c["column_name"]: c for c in data["detail"]["schema"]}
    # As text, "10" sorts before "9".
    assert by_name["dim"]["smallest"] == 9
    assert by_name["dim"]["largest"] == 384
    assert by_name["dim"]["has_stats"]


def test_statistic_chooses_the_form_by_column_type():
    row = {
        "column_type": "int32",
        "min_number": 9.0,
        "min_text": "10",
    }
    assert statistic(row, "min") == 9.0
    text_row = {"column_type": "varchar", "min_text": "x" * 90}
    assert statistic(text_row, "min") == "x" * 40


def test_schema_tree_gives_each_column_its_depth():
    rows = [
        {"column_id": 1, "parent_column": None},
        {"column_id": 2, "parent_column": 1},
        {"column_id": 3, "parent_column": 2},
    ]
    assert [row["depth"] for row in schema_tree(rows)] == [0, 1, 2]


@pytest.mark.parametrize(
    "table", ["raw.article_text", "raw.items", "nlp.llm_traces"]
)
def test_no_full_article_text_on_the_page(client, table):
    html = client.get(f"/under/lake?table={table}").text
    assert TAIL not in html
    assert "word word" in html


def test_sample_cuts_text_in_the_query(under):
    detail = lake_view(under, {"table": "raw.article_text"})["detail"]
    assert 'left("text", 160)' in detail["sample"].query.sql
    for row in detail["sample"].rows:
        assert len(row["text"] or "") <= 160


def test_a_vector_shows_its_length_and_first_values(client):
    html = client.get("/under/lake?table=nlp.embeddings").text
    assert "384 values; first: 0.25, 0.25, 0.25" in html
    assert "0.25, 0.25, 0.25, 0.25" not in html


def test_a_sample_names_a_file_without_its_directory(client):
    html = client.get("/under/lake?table=nlp.embeddings").text
    section = html[html.index("Newest rows") :]
    assert "lake/nlp/embeddings/" not in section.split("Show query")[0]
    assert ".parquet" in section


def test_the_newest_rows_are_one_table_with_a_line_per_row(client):
    html = client.get("/under/lake?table=nlp.embeddings").text
    section = html[html.index("Newest rows") :].split("Show query")[0]
    assert section.count("<table") == 1
    head, body = section.split("</thead>")
    for column in ("item_id", "_snapshot", "_file"):
        assert f"<th>{column}</th>" in head
    columns = head.count("<th>")
    lines = body.split("</tbody>")[0].split("<tr>")[1:]
    assert lines
    assert all(line.count("<td") == columns for line in lines)


def test_the_key_scan_query_sits_with_the_key_count(client):
    html = client.get("/under/lake?table=nlp.embeddings").text
    section = html[html.index('id="detail"') :]
    assert (
        section.index("Distinct keys, scan")
        < section.index("Show query for distinct keys")
        < section.index("Newest rows")
    )
    assert "Show query for newest rows" in section
    assert "for distinct keys" not in section[section.index("Newest rows") :]


def test_sample_cell_formats_a_list_and_a_float():
    assert sample_cell({"length": 2, "first": [0.123456, 1.5]}) == (
        "2 values; first: 0.1235, 1.5"
    )
    assert sample_cell(None) == "NULL"
    assert sample_cell({"length": None, "first": None}) == "NULL"
    assert sample_cell({"length": 0, "first": []}) == "0 values; first: "
    assert len(sample_cell("x" * 900)) == 160


def test_each_query_is_disclosed(client):
    page = client.get("/under/lake?table=nlp.embeddings").text
    text = page_text(page)
    for part in (
        "ducklake_file_column_stats",
        "parent_column",
        "ducklake_table_info",
        "ducklake_snapshot_changes",
        "count(DISTINCT item_id)",
    ):
        assert part in text, part
    assert page.count("Show query") >= 4


def test_a_table_name_outside_the_catalog_is_ignored(client):
    html = client.get(
        "/under/lake", params={"table": "raw.items; DROP TABLE x"}
    ).text
    assert "DROP TABLE x" not in html
    assert '<h2 class="u-section-title">raw.items</h2>' in html


def test_a_failed_scan_is_not_an_empty_table(client, under):
    under._cache.clear()
    assert under._gate.acquire(timeout=30)
    try:
        html = client.get("/under/lake?table=raw.items").text
    finally:
        under._gate.release()
        under._cache.clear()
    assert "This table has no rows." not in html
    assert GATE_BUSY in html
    assert ">Retry</button>" in html


def test_the_view_links_instead_of_repeating(client):
    html = client.get("/under/lake?table=raw.items").text
    assert 'href="/under/inventory"' in html
    assert 'href="/under/storage"' in html
    assert 'href="/under/data-model"' in html


def test_the_view_is_in_the_navigation(client):
    html = client.get("/under").text
    assert 'href="/under/lake"' in html
    assert 'href="/under/queries"' in html


def test_the_page_inlines_the_diagram(client):
    assert "<svg" in client.get("/under/lake").text
