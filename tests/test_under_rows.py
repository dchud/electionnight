"""A table's rows, opened in place: the query, the pagers, and the cuts."""

import html
import json
import re
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient
from test_under import add_rows, app_client, item, seed

from monitor.lake import TABLES, BatchWriter, connect, init_lake
from monitor.web import under as under_module
from monitor.web.app import create_app
from monitor.web.under import console_available
from monitor.web.under_rows import (
    MAX_PAGE,
    PAGE_ROWS,
    SHOWN_CHARS,
    rows_sql,
    rows_view,
    table_columns,
)
from monitor.web.under_worker import check_console_sql

MARKER = "PASTTHECUT"
LONG = "a" * 200 + MARKER
HOSTILE = "<script>alert(1)</script>"
FOURTH = 0.875
needs_sandbox = pytest.mark.skipif(
    not console_available(), reason="needs the macOS sandbox"
)


def now():
    return datetime.now(UTC)


def fill(settings):
    seed(settings)
    add_rows(
        settings,
        {
            "raw.items": [
                item(
                    "cut-item",
                    now(),
                    title=HOSTILE,
                    summary=LONG,
                    raw_entry=json.dumps({"summary": LONG}),
                    feed_categories=[LONG],
                )
            ],
            "raw.article_text": [{"item_id": "cut-item", "text": LONG}],
            "nlp.llm_traces": [
                {
                    "call_id": "cut-call",
                    "request_text": LONG,
                    "response_text": LONG,
                    "item_ids": [LONG, "cut-item"],
                }
            ],
            "nlp.embeddings": [
                {
                    "item_id": "cut-item",
                    "vec": [0.25, 0.25, 0.25, FOURTH],
                    "dim": 4,
                }
            ],
        },
    )


@pytest.fixture(scope="module")
def filled(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("rows"))
    fill(settings)
    return settings


@pytest.fixture(scope="module")
def client(filled):
    with app_client(filled) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def under(client):
    return client.app.state.web.under


def strip_tags(markup):
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def shown_sql(page):
    block = page[page.index('<pre class="u-sql">') :]
    return strip_tags(block[: block.index("</pre>")])


def rows_page(client, table, mode="recent", **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return client.get(f"/under/rows?table={table}&mode={mode}&{query}")


def cell_values(data, column):
    number = data["columns"].index(column)
    return [row[number]["text"] for row in data["rows"]]


# Every table, in both modes, with no change to any view.


@pytest.mark.parametrize("mode", ["recent", "sample"])
@pytest.mark.parametrize("table", TABLES)
def test_every_table_works_and_its_query_passes_the_console_check(
    client, under, filled, table, mode
):
    response = rows_page(client, table, mode)
    assert response.status_code == 200
    assert '<pre class="u-sql">' in response.text
    assert "These are rows" in response.text
    conn = connect(filled, read_only=True)
    try:
        check_console_sql(conn, shown_sql(response.text))
    finally:
        conn.close()


def test_a_new_table_works_with_no_view_change(
    lake_template, tmp_path, monkeypatch
):
    monkeypatch.setitem(TABLES, "nlp.brand_new", "name TEXT")
    settings = clone_lake(lake_template, tmp_path)
    init_lake(settings)
    conn = connect(settings)
    conn.execute(
        "INSERT INTO lake.nlp.brand_new (name, created_at) "
        "VALUES ('first', now())"
    )
    conn.close()
    with app_client(settings) as fresh:
        for mode in ("recent", "sample"):
            page = rows_page(fresh, "nlp.brand_new", mode).text
            assert "first" in page
        inventory = fresh.get("/under/inventory").text
    assert 'data-rows-table="nlp.brand_new"' in inventory


def test_a_table_with_no_time_column_offers_only_the_sample(
    lake_template, tmp_path, monkeypatch
):
    monkeypatch.setitem(TABLES, "nlp.no_time", "name TEXT")
    settings = clone_lake(lake_template, tmp_path)
    conn = connect(settings)
    conn.execute("CREATE TABLE lake.nlp.no_time (name TEXT)")
    conn.execute("INSERT INTO lake.nlp.no_time VALUES ('only')")
    conn.close()
    with app_client(settings) as fresh:
        recent = rows_page(fresh, "nlp.no_time", "recent").text
        sample = rows_page(fresh, "nlp.no_time", "sample").text
    assert "has no <code>created_at</code> column" in recent
    assert "Show a sample" in recent
    assert "ORDER BY created_at" not in recent
    assert "only" in sample


# Where the panel appears.


@pytest.mark.parametrize(
    "path",
    [
        "/under/lake?table=raw.items",
        "/under/inventory",
        "/under/data-model?table=raw.items",
        "/under/storage?table=raw.items",
    ],
)
def test_a_view_of_a_table_offers_both_controls(client, path):
    page = client.get(path).text
    for mode in ("recent", "sample"):
        assert f'data-rows-open="{mode}" data-rows-table="raw.items"' in page
    assert 'data-rows-body="raw.items"' in page


def test_inventory_offers_the_panel_for_every_table(client):
    page = client.get("/under/inventory").text
    for table in TABLES:
        assert f'data-rows-body="{table}"' in page


# The query shown is the one that ran.


@pytest.mark.parametrize("mode", ["recent", "sample"])
@pytest.mark.parametrize(
    "table", ["raw.items", "nlp.embeddings", "gold.bin_counts"]
)
def test_the_query_shown_is_the_query_that_ran(
    client, monkeypatch, table, mode
):
    ran = []
    real = under_module.run_worker

    def recording(request, *args, **kwargs):
        ran.extend(request["statements"])
        return real(request, *args, **kwargs)

    monkeypatch.setattr(under_module, "run_worker", recording)
    page = rows_page(client, table, mode, page=1).text
    statements = {statement["name"]: statement["sql"] for statement in ran}
    assert shown_sql(page) == statements["rows"]
    assert statements["count"] in strip_tags(page)
    assert statements["rows"].startswith("SELECT ")
    assert all(statement["params"] is None for statement in ran)


@needs_sandbox
@pytest.mark.parametrize("mode", ["recent", "sample"])
@pytest.mark.parametrize("table", ["raw.items", "nlp.llm_traces"])
def test_the_console_returns_the_rows_shown_after_more_rows_arrive(
    lake_template, tmp_path, table, mode
):
    settings = clone_lake(lake_template, tmp_path)
    fill(settings)
    with app_client(settings) as fresh:
        fresh_under = fresh.app.state.web.under
        data = rows_view(fresh_under, {"table": table, "mode": mode})
        add_rows(
            settings,
            {
                "raw.items": [item("later", now())],
                "nlp.llm_traces": [
                    {"call_id": "later", "item_ids": ["later"]}
                ],
            },
        )
        console = fresh_under.console(data["query"].sql)
    assert console.get("status") == "ok", console
    number = console["columns"].index("_snapshot")
    assert {row[number] for row in console["rows"]} <= {
        int(text) for text in cell_values(data, "_snapshot")
    }
    assert len(console["rows"]) == len(data["rows"])
    first = data["columns"][0]
    place = console["columns"].index(first)
    assert [str(row[place]) for row in console["rows"]] == [
        str(value)[: SHOWN_CHARS + 1] for value in cell_values(data, first)
    ]


def test_the_console_link_fills_the_box_without_running(client, monkeypatch):
    page = rows_page(client, "raw.items", "recent").text
    sql = shown_sql(page)
    link = re.search(r'href="(/under/sql\?sql=[^"]+)"', page).group(1)
    assert parse_qs(urlsplit(html.unescape(link)).query)["sql"] == [sql]

    def fails(*_args, **_kwargs):
        raise AssertionError("opening the console runs nothing")

    monkeypatch.setattr(under_module, "run_worker", fails)
    console = client.get(html.unescape(link)).text
    box = console[console.index("<textarea") : console.index("</textarea>")]
    assert sql in html.unescape(box)


def test_a_request_from_elsewhere_gets_no_console_link(filled):
    remote = TestClient(
        create_app(filled),
        base_url="http://testserver",
        client=("192.168.1.30", 50000),
    )
    with remote as test_client:
        page = rows_page(test_client, "raw.items", "recent").text
    assert "These are rows" in page
    assert "Open in SQL console" not in page
    assert "data-copy-sql" in page


# Recent rows, across the end of a table.


@pytest.fixture(scope="module")
def paged(lake_template, tmp_path_factory):
    """60 rows written together, so `created_at` ties, and 3 written later."""
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("paged"))
    add_rows(
        settings,
        {"raw.items": [item(f"tied-{n:02}", now()) for n in range(60)]},
    )
    add_rows(
        settings,
        {"raw.items": [item(f"newest-{n}", now()) for n in range(3)]},
    )
    with app_client(settings) as test_client:
        yield test_client


def view(client, **params):
    return rows_view(client.app.state.web.under, params)


def test_pages_cover_every_row_once_and_newest_first(paged):
    seen = []
    for page in (1, 2, 3):
        data = view(paged, table="raw.items", mode="recent", page=str(page))
        seen += cell_values(data, "item_id")
    assert len(seen) == 63
    assert len(set(seen)) == 63
    assert all(name.startswith("newest") for name in seen[:3])


def test_the_pagers_say_what_is_showing_and_stop_at_the_ends(paged):
    first = rows_page(paged, "raw.items", "recent", page=1).text
    assert "Rows 1 to 25 of 63" in first
    assert re.search(
        r'<button type="button" class="u-button" disabled>Newer', first
    )
    assert "data-rows-go" in first and ">Older</button>" in first
    last = rows_page(paged, "raw.items", "recent", page=3).text
    assert "Rows 51 to 63 of 63" in last
    assert re.search(
        r'<button type="button" class="u-button" disabled>Older', last
    )


def test_a_page_past_the_last_row_stops_cleanly_with_no_scan(
    paged, monkeypatch
):
    def fails(*_args, **_kwargs):
        raise AssertionError("no scan is needed past the end")

    monkeypatch.setattr(under_module, "run_worker", fails)
    page = rows_page(paged, "raw.items", "recent", page=4)
    assert page.status_code == 200
    assert "No rows on this page" in page.text
    assert "63 rows" in page.text
    assert "page=3" in page.text
    assert "Last page" in page.text


def test_a_page_past_the_end_of_a_pinned_snapshot_stops_cleanly(paged):
    under = paged.app.state.web.under
    older = under.snapshot_id - 1
    data = view(
        paged, table="raw.items", mode="recent", page="3", at=str(older)
    )
    assert data["status"] == "ok"
    assert data["last_row"] == 60
    assert data["older_url"] is None


def test_an_empty_table_says_so(paged):
    page = rows_page(paged, "nlp.health", "recent").text
    assert "This table has no rows" in page
    page = rows_page(paged, "nlp.health", "sample").text
    assert "This table has no rows" in page


def test_a_pager_keeps_the_snapshot_so_new_rows_do_not_move_the_page(paged):
    under = paged.app.state.web.under
    data = view(paged, table="raw.items", mode="recent", page="1")
    assert f"at={under.snapshot_id}" in data["older_url"]
    assert f"AT (VERSION => {under.snapshot_id})" in data["query"].sql


# A sample.


def spread_lake(settings):
    """Rows in three Parquet files, then rows inlined in the catalog."""
    conn = connect(settings)
    for batch in range(3):
        conn.execute(
            "INSERT INTO lake.raw.items (item_id, source_id, created_at) "
            f"SELECT 'file{batch}-' || i, 's', now() FROM range(2500) t(i)"
        )
    writer = BatchWriter(conn, settings)
    writer.add(
        "raw.items",
        [{"item_id": f"inlined-{n}", "source_id": "s"} for n in range(900)],
    )
    writer.flush()
    conn.close()


@pytest.fixture(scope="module")
def spread(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("spread"))
    spread_lake(settings)
    with app_client(settings) as test_client:
        yield test_client


def test_the_same_seed_gives_the_same_rows_and_another_seed_differs(spread):
    def picked(number):
        data = view(spread, table="raw.items", mode="sample", page=str(number))
        return cell_values(data, "item_id")

    assert picked(3) == picked(3)
    assert picked(3) != picked(4)
    assert len(picked(3)) == PAGE_ROWS


def test_a_sample_reaches_every_file_and_the_inlined_rows(spread):
    groups = set()
    files = set()
    for number in range(1, 13):
        data = view(spread, table="raw.items", mode="sample", page=str(number))
        groups |= {name.split("-")[0] for name in cell_values(data, "item_id")}
        files |= set(cell_values(data, "_file"))
    assert groups == {"file0", "file1", "file2", "inlined"}
    assert any(name.startswith("ducklake_inlined_data") for name in files)
    assert any(name.endswith(".parquet") for name in files)


def test_a_sample_of_a_small_table_shows_every_row(paged):
    conn_rows = view(paged, table="ref.states", mode="sample")
    assert conn_rows["total"] == len(conn_rows["rows"])


def test_a_sample_is_not_a_reservoir_sample(spread):
    data = view(spread, table="raw.items", mode="sample", page="5")
    assert "ORDER BY hash(rowid, 5)" in data["query"].sql
    assert "USING SAMPLE" not in data["query"].sql


def test_a_sample_pager_names_the_next_seed(spread):
    data = view(spread, table="raw.items", mode="sample", page="5")
    assert "page=6" in data["another_url"]
    assert "page=4" in data["newer_url"]
    first = view(spread, table="raw.items", mode="sample", page="1")
    assert first["newer_url"] is None


# Bad values never reach SQL.


@pytest.mark.parametrize(
    "query",
    [
        "table=no.such",
        "table=raw.items;DROP",
        "table=raw.items%27%20OR%201=1",
        "table=",
        "",
        "table=raw.items&mode=other",
        "table=raw.items&page=0",
        "table=raw.items&page=-1",
        "table=raw.items&page=abc",
        "table=raw.items&page=1.5",
        "table=raw.items&page=1%20OR%201=1",
        f"table=raw.items&page={MAX_PAGE + 1}",
        "table=raw.items&page=99999999999999999999",
        "table=raw.items&at=abc",
        "table=raw.items&at=0",
        "table=raw.items&at=999999999",
        "table=raw.items&at=1;DROP",
    ],
)
def test_a_bad_value_gets_a_clean_error_and_no_scan(
    client, monkeypatch, query
):
    def fails(*_args, **_kwargs):
        raise AssertionError("a bad value starts no child process")

    monkeypatch.setattr(under_module, "run_worker", fails)
    response = client.get(f"/under/rows?{query}")
    assert response.status_code == 400
    assert "banner-warn" in response.text
    assert "<pre" not in response.text
    assert "DROP" not in response.text


def test_a_large_seed_within_the_bound_works(client):
    page = rows_page(client, "raw.items", "sample", page=MAX_PAGE).text
    assert f"hash(rowid, {MAX_PAGE})" in strip_tags(page)
    assert "Another sample" in page


# Escaping, and what the SQL cuts.


def test_results_are_escaped(client):
    page = rows_page(client, "raw.items", "recent").text
    assert HOSTILE not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


@pytest.mark.parametrize("mode", ["recent", "sample"])
@pytest.mark.parametrize(
    "table",
    ["raw.article_text", "nlp.llm_traces", "raw.items", "nlp.embeddings"],
)
def test_a_cut_column_never_leaks_past_the_cut(
    under, client, monkeypatch, table, mode
):
    outputs = []
    real = under_module.run_worker

    def recording(request, *args, **kwargs):
        outcome = real(request, *args, **kwargs)
        outputs.append(json.dumps(outcome.records))
        return outcome

    monkeypatch.setattr(under_module, "run_worker", recording)
    page = rows_page(client, table, mode).text
    assert MARKER not in page
    assert MARKER not in "".join(outputs), "it left the child process"


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("raw.article_text", ["text"]),
        ("nlp.llm_traces", ["request_text", "response_text", "item_ids"]),
        ("raw.items", ["summary", "raw_entry", "feed_categories"]),
    ],
)
def test_the_panel_says_which_columns_were_cut(under, table, columns):
    data = rows_view(under, {"table": table, "mode": "recent"})
    for column in columns:
        assert column in data["cut_columns"]
    number = data["columns"].index(columns[0])
    cut_cells = [row[number] for row in data["rows"] if row[number]["cut"]]
    assert cut_cells
    assert all(len(cell["text"]) <= SHOWN_CHARS for cell in cut_cells)


def test_the_cut_is_marked_on_the_cell_and_named_in_the_caveat(client):
    page = rows_page(client, "raw.article_text", "recent").text
    assert '<span class="u-rows-cut">cut</span>' in page
    assert "Cut on this page: <code>text</code>" in page
    assert "The SQL console can read stored text up to 4,000" in page


def test_a_value_of_exactly_the_shown_length_is_not_called_cut(
    lake_template, tmp_path
):
    settings = clone_lake(lake_template, tmp_path)
    add_rows(
        settings,
        {
            "raw.article_text": [
                {"item_id": "fits", "text": "b" * SHOWN_CHARS},
            ]
        },
    )
    with app_client(settings) as fresh:
        data = view(fresh, table="raw.article_text", mode="recent")
    assert data["cut_columns"] == []
    assert cell_values(data, "text") == ["b" * SHOWN_CHARS]


def test_a_vector_shows_its_length_and_first_values_only(client):
    page = rows_page(client, "nlp.embeddings", "recent").text
    assert "4 values; first: 0.25, 0.25, 0.25" in page
    assert str(FOURTH) not in page


def test_the_cut_is_in_the_sql(under, filled):
    columns, _ = table_columns(under, "raw.article_text")
    sql = rows_sql(
        "raw.article_text", columns.rows, "recent", 1, under.snapshot_id
    )
    assert f'left("text", {SHOWN_CHARS + 1}) AS "text"' in sql
    assert 'SELECT "text"' not in sql


# What the panel says.


def test_the_recent_caveat_names_the_column_and_what_it_is_not(client):
    page = strip_tags(rows_page(client, "nlp.mentions", "recent").text)
    assert "one record can have several rows" in page
    assert "A record here is one mention_id" in page
    assert "Most recent means by created_at" in page
    assert "a write time, not a publication time" in page
    assert "backlog" in page


def test_the_sample_caveat_says_it_is_a_sample_of_rows(client):
    page = strip_tags(rows_page(client, "nlp.mentions", "sample").text)
    assert "one record can have several rows" in page
    assert "a sample of rows, not of stories" in page
    assert "hash(rowid, 1)" in page


# Bounds.


def test_a_busy_gate_returns_at_once_with_a_retry(under, client, monkeypatch):
    def fails(*_args, **_kwargs):
        raise AssertionError("a busy gate starts no child process")

    monkeypatch.setattr(under_module, "run_worker", fails)
    assert under._gate.acquire(timeout=30)
    try:
        started = time.monotonic()
        page = rows_page(client, "raw.items", "recent").text
        assert time.monotonic() - started < 2
    finally:
        under._gate.release()
    assert "Another scan is running" in page
    assert "Retry" in page
    assert "page=1" in page


def test_nothing_is_kept_in_the_cache(under, client):
    before = len(under._cache)
    rows_page(client, "raw.article_text", "recent", page=1)
    rows_page(client, "raw.article_text", "sample", page=2)
    kept = [
        key for key in list(under._cache)[before:] if key and key[0] == "scan"
    ]
    assert kept == []


def test_a_child_that_stops_says_so_in_the_panel(client, monkeypatch):
    monkeypatch.setattr(
        under_module,
        "run_worker",
        lambda *_args, **_kwargs: under_module.ChildOutcome(timed_out=True),
    )
    page = rows_page(client, "raw.items", "recent").text
    assert "These rows were not read" in page
    assert "stopped at 10 s" in page
    assert "Retry" in page
    assert '<pre class="u-sql">' in page


# The copy button on every statement.


@pytest.mark.parametrize(
    "path",
    [
        "/under",
        "/under/lake?table=raw.items",
        "/under/inventory",
        "/under/storage",
        "/under/data-model",
        "/under/models",
        "/under/feeds",
        "/under/costs",
        "/under/queries",
        "/under/coverage",
        "/under/records",
        "/under/trace/old1",
        "/under/panels/wire",
    ],
)
def test_every_sql_block_has_a_copy_button(client, path):
    page = client.get(path).text
    blocks = page.count('<pre class="u-sql">')
    assert blocks > 0
    assert page.count("data-copy-sql") == blocks


def test_the_console_echo_has_a_copy_button(client):
    page = client.post(
        "/under/sql",
        data={"sql": "SELECT 1 AS one"},
        headers={"Origin": "http://127.0.0.1:8000"},
    ).text
    echo = page[page.index('<div class="u-sql-block">') :]
    assert "data-copy-sql" in echo


def test_the_button_sits_outside_the_text_it_copies(client):
    page = client.get("/under/queries").text
    for block in re.findall(r'<pre class="u-sql">.*?</pre>', page, flags=re.S):
        assert "Copy" not in block.replace("Copyright", "")


# Only the rows the sentence is true of.


@pytest.mark.parametrize(
    ("table", "shown"),
    [
        ("raw.items", True),
        ("nlp.mentions", True),
        ("ref.sources", False),
        ("gold.bin_counts", False),
        ("nlp.maintenance_runs", False),
    ],
)
def test_the_backlog_sentence_appears_where_a_first_fetch_brings_rows(
    client, table, shown
):
    page = html.unescape(rows_page(client, table).text)
    assert "a write time, not a publication time" in page
    assert ("backlog from a feed's first fetch" in page) is shown


# A pinned snapshot.


def latest_snapshot(settings):
    conn = connect(settings, read_only=True)
    try:
        return conn.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchone()[0]
    finally:
        conn.close()


def pinned_lake(lake_template, tmp_path):
    """10 rows at a snapshot, and 60 more written after it."""
    settings = clone_lake(lake_template, tmp_path)
    add_rows(
        settings,
        {"raw.items": [item(f"early-{n}", now()) for n in range(10)]},
    )
    pinned = latest_snapshot(settings)
    add_rows(
        settings,
        {"raw.items": [item(f"later-{n}", now()) for n in range(60)]},
    )
    return settings, pinned


def test_past_the_end_of_a_pinned_snapshot_uses_the_count_at_it(
    lake_template, tmp_path
):
    settings, pinned = pinned_lake(lake_template, tmp_path)
    with app_client(settings) as fresh:
        page = rows_page(fresh, "raw.items", page=50, at=pinned).text
        assert f"has 10 rows at snapshot {pinned}" in page
        last = html.unescape(re.search(r'data-rows-go="([^"]+)"', page)[1])
        assert parse_qs(urlsplit(last).query)["page"] == ["1"]
        assert "Rows 1 to 10 of 10" in fresh.get(last).text


def test_a_pinned_page_after_a_prune_says_the_snapshot_was_removed(
    lake_template, tmp_path
):
    settings, pinned = pinned_lake(lake_template, tmp_path)
    conn = connect(settings)
    conn.execute(
        f"CALL ducklake_expire_snapshots('lake', versions => [{pinned}])"
    ).fetchall()
    conn.close()
    with app_client(settings) as fresh:
        for page_number in (1, 50):
            page = rows_page(
                fresh, "raw.items", page=page_number, at=pinned
            ).text
            assert f"Snapshot {pinned} was removed from the lake" in page
            assert "Close this and reopen" in page
            assert "data-rows-close" in page
            assert "Retry" not in page


# A value of any type is cut in the SQL.


def test_a_new_table_with_nested_columns_is_cut_in_the_sql(
    lake_template, tmp_path, monkeypatch
):
    monkeypatch.setitem(TABLES, "nlp.nested", "box TEXT")
    settings = clone_lake(lake_template, tmp_path)
    conn = connect(settings)
    conn.execute(
        "CREATE TABLE lake.nlp.nested (box STRUCT(note VARCHAR), "
        "lookup MAP(VARCHAR, VARCHAR), deep VARCHAR[][], "
        "boxes STRUCT(note VARCHAR)[], amount INTEGER, created_at TIMESTAMPTZ)"
    )
    conn.execute(
        "INSERT INTO lake.nlp.nested VALUES "
        "({'note': $long}, MAP {'key': $long}, [[$long]], [{'note': $long}], "
        "7, now())",
        {"long": LONG},
    )
    conn.close()
    with app_client(settings) as fresh:
        page = html.unescape(rows_page(fresh, "nlp.nested").text)
    assert MARKER not in page
    statement = strip_tags(page)
    for name in ("box", "lookup"):
        assert (
            f'left(CAST("{name}" AS VARCHAR), {SHOWN_CHARS + 1})' in statement
        )
    assert (
        "Cut on this page: <code>box</code>, <code>lookup</code>, "
        "<code>deep</code>, <code>boxes</code>." in page
    )
    assert ">7</td>" in page
    assert 'CAST("amount"' not in statement
    assert 'CAST("created_at"' not in statement
