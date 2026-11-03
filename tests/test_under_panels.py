"""Each dashboard card's link, and the page that shows what is behind it."""

import html
import inspect
import math
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient
from test_under import add_rows, app_client, classified, item, seed

from monitor.lake import connect
from monitor.web.app import create_app
from monitor.web.panels import Panels
from monitor.web.readmodel import ReadModel
from monitor.web.routes import sse
from monitor.web.under import console_available, et_label
from monitor.web.under_panels import (
    CARDS,
    CARDS_BY_KEY,
    CODE_LABEL,
    CONSOLE_VERIFIED,
    LINKED_CARDS,
    NEEDS_VISIBLE_SEQ,
    NOT_OFFERED,
    Recorded,
    RecordingModel,
    capture_card,
    combined,
    lake_form,
    literal,
    source_of,
    with_literals,
)
from monitor.web.under_worker import check_console_sql

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "src/monitor/web/templates/pages/home.html"
QUERY = "window=3h&focus=battleground&level=state&copies=all"
FILTER_SETS = ["", "window=3h", QUERY]
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
            "nlp.storylines": [
                {
                    "storyline_id": "gone",
                    "version": 1,
                    "label": "Merged away",
                    "status": "merged",
                    "merged_into": "middle",
                },
                {
                    "storyline_id": "middle",
                    "version": 1,
                    "label": "Merged too",
                    "status": "merged",
                    "merged_into": "survivor",
                },
                {
                    "storyline_id": "survivor",
                    "version": 1,
                    "label": "The survivor",
                    "status": "active",
                },
            ],
            "raw.items": [item("merged-item", now())],
            "nlp.classify_a": [classified("merged-item")],
            "nlp.storyline_assign": [
                {
                    "item_id": "merged-item",
                    "storyline_id": "gone",
                    "assigned_at": now(),
                }
            ],
        },
    )


@pytest.fixture(scope="module")
def filled(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("panels"))
    fill(settings)
    return settings


@pytest.fixture(scope="module")
def client(filled):
    with app_client(filled) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def web(client):
    return client.app.state.web


def strip_tags(markup):
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def shown_blocks(page):
    """Each statement shown, as text with its whitespace normalized."""
    return [
        " ".join(strip_tags(block).split())
        for block in re.findall(
            r'<pre class="u-sql">(.*?)</pre>', page, flags=re.S
        )
    ]


def filters_for(web, query):
    class Asked:
        query_params = dict(
            pair.split("=", 1) for pair in query.split("&") if pair
        )

    return web.filters(Asked)


# The control on the dashboard.


def test_every_card_on_the_home_page_has_the_control():
    template = HOME.read_text(encoding="utf-8")
    articles = re.findall(
        r'<article class="panel[^"]*" id="panel-(\w+)">(.*?)</article>',
        template,
        flags=re.S,
    )
    assert len(articles) >= 11
    for panel, body in articles:
        head = body[: body.index("</div>")]
        assert "card.link(" in head, panel


def test_a_rendered_home_page_has_one_link_per_card_with_the_filters(client):
    page = client.get(f"/?{QUERY}").text
    for card in LINKED_CARDS:
        matches = re.findall(
            rf'data-card-link="{card.key}" href="([^"]+)">(\w+)</a>', page
        )
        assert len(matches) == 1, card.key
        href, label = matches[0]
        assert href.startswith(f"/under/panels/{card.key}?")
        assert html.unescape(href).split("?")[1].split("#")[
            0
        ] in html.unescape(page)
        assert label == card.label
        for part in QUERY.split("&"):
            assert part in html.unescape(href)


def test_the_header_row_has_no_link_and_is_in_the_list(client):
    page = client.get("/").text
    assert 'data-card-link="kpis"' not in page
    assert "/under/panels/kpis" in client.get("/under/panels").text


def test_a_card_gets_one_label_and_these_are_the_code_ones():
    assert {card.key for card in CARDS if card.label == CODE_LABEL} == {
        "brief",
        "tone",
    }


def test_a_code_link_goes_to_the_code_section(client):
    page = client.get("/").text
    assert re.search(
        r'data-card-link="brief" href="/under/panels/brief\?[^"]*#code"', page
    )
    assert re.search(
        r'data-card-link="wire" href="/under/panels/wire\?[^"#]*"', page
    )


# The list, and getting back.


def test_the_list_names_every_card_and_is_in_the_navigation(client):
    page = client.get(f"/under/panels?{QUERY}").text
    assert 'href="/under/panels?' in page
    for card in CARDS:
        assert f"/under/panels/{card.key}?" in page
        assert card.title in html.unescape(page)
    assert 'href="/?' in page


@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_each_page_links_back_with_the_same_filters(client, card):
    page = html.unescape(client.get(f"/under/panels/{card.key}?{QUERY}").text)
    back = re.search(r'href="/\?([^"]*)">Back to the dashboard', page).group(1)
    assert set(back.split("&")) == set(QUERY.split("&"))
    assert f"/under/panels?{QUERY}"[:14] in page


def test_an_unknown_card_is_a_404(client):
    assert client.get("/under/panels/no-such-card").status_code == 404


# The SQL shown is what ran.


@pytest.mark.parametrize("query", FILTER_SETS)
@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_the_page_shows_what_the_wrapper_recorded(client, web, card, query):
    filters = filters_for(web, query)
    recorder, _, failures = capture_card(
        web.settings, web.model, card, filters
    )
    assert failures == []
    assert recorder.statements
    page = client.get(f"/under/panels/{card.key}?{query}").text
    blocks = shown_blocks(page)
    for statement in combined(recorder.statements):
        assert " ".join(statement.sql.split()) in blocks, statement.name


@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_changed_filters_change_the_statement_or_its_parameters(web, card):
    def statements(query):
        recorder, _, _ = capture_card(
            web.settings, web.model, card, filters_for(web, query)
        )
        return [(s.name, s.sql, repr(s.params)) for s in recorder.statements]

    if card.key in {"brief", "kpis"}:
        return
    assert statements("window=3h&level=state") != statements("window=1h")


# A stream never finishes in a test client, so no request here covers it.
STREAM_ROUTE = "/events"


def watched(name, real, called, inside):
    """A `Panels` method that notes a call no other method of it made."""

    def spy(self, *args, **kwargs):
        if getattr(inside, "active", False):
            return real(self, *args, **kwargs)
        called.add(name)
        inside.active = True
        try:
            return real(self, *args, **kwargs)
        finally:
            inside.active = False

    return spy


@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_a_cards_method_list_is_what_its_routes_call(
    client, monkeypatch, card
):
    """Every public method is watched, so one the list omits fails this."""
    called = set()
    inside = threading.local()
    for name, value in vars(Panels).items():
        if inspect.isfunction(value) and not name.startswith("_"):
            monkeypatch.setattr(
                Panels, name, watched(name, value, called, inside)
            )
    listed = [m for m in card.methods if m.route != STREAM_ROUTE]
    for route in {method.route for method in listed}:
        assert client.get(route).status_code == 200
    assert called == {method.name for method in listed}


def test_the_one_method_on_the_stream_is_what_the_stream_calls():
    streamed = [
        (card.key, method.name, method.route)
        for card in CARDS
        for method in card.methods
        if method.route == STREAM_ROUTE
    ]
    assert streamed == [("wire", "wire_recent", "/events")]
    assert [route.path for route in sse.router.routes] == [STREAM_ROUTE]
    assert "web.panels.wire_recent" in inspect.getsource(sse)


@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_each_method_gives_the_card_its_real_data(web, card):
    as_of = (now() - timedelta(minutes=1)).isoformat()
    filters = filters_for(web, f"as_of={as_of}")
    _, captured, failures = capture_card(
        web.settings, web.model, card, filters
    )
    assert failures == []
    for method in card.methods:
        assert captured[method.name] == method.call(web.panels, filters)


def test_the_dashboard_route_runs_the_statement_the_page_shows(
    client, web, monkeypatch
):
    ran = []
    real = ReadModel.rows

    def recording(self, sql, params=None):
        ran.append(sql.strip())
        return real(self, sql, params)

    web.model.cache._data.clear()
    monkeypatch.setattr(ReadModel, "rows", recording)
    assert client.get(f"/frag/rising?{QUERY}").status_code == 200
    from_route = {" ".join(sql.split()) for sql in ran}
    monkeypatch.undo()
    blocks = shown_blocks(client.get(f"/under/panels/rising?{QUERY}").text)
    assert from_route
    assert from_route <= set(blocks)


def test_a_cached_answer_is_said_to_be_one(client, web):
    web.model.cache._data.clear()
    as_of = (now() - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    path = f"/under/panels/stream?window=24h&as_of={as_of}"
    first = client.get(path).text
    second = client.get(path).text
    assert "Ran in" in first
    assert "came from the read model's cache" in second
    assert "Ran in" not in second
    assert shown_blocks(first)[0] == shown_blocks(second)[0]


def test_a_capture_adds_nothing_to_the_slow_mode_timings(client, web):
    web.model.cache._data.clear()
    before = len(web.model.query_times)
    client.get("/under/panels/stream?window=24h")
    assert len(web.model.query_times) == before


def test_the_recorder_passes_other_attributes_through(web):
    recorder = RecordingModel(web.model)
    assert recorder.snapshot is web.model.snapshot
    assert recorder.read_status() == web.model.read_status()


# What is not SQL, and the code.


@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_each_page_says_what_is_not_sql_and_explains_the_layers(client, card):
    page = html.unescape(client.get(f"/under/panels/{card.key}").text)
    assert card.not_sql
    for note in card.not_sql:
        assert note in page
    assert "These queries do not read Parquet" in page
    assert "/methods#the-read-model" in page


@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_each_link_to_methods_lands_on_a_heading(client, card):
    methods = client.get("/methods").text
    for link in card.links:
        if link.href.startswith("/methods#"):
            assert f'id="{link.href.split("#")[1]}"' in methods, link.href


@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_code_is_read_from_the_source_with_its_file_and_line(client, card):
    page = html.unescape(client.get(f"/under/panels/{card.key}").text)
    for ref in card.code:
        assert ref.name in page
    for path, line in re.findall(
        r"(src/monitor/[\w/]+\.py):(\d+)</code>", page
    ):
        lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        assert lines[int(line) - 1].lstrip().startswith(("def ", "async def "))


def test_the_tone_page_shows_the_constants_and_the_real_function(client):
    page = client.get("/under/panels/tone").text
    assert ">5<" in page
    page = strip_tags(page)
    assert "def tone_aggregate(" in page
    assert "src/monitor/analytics.py:" in page
    assert "TONE_HIDE_BELOW" in page


def test_the_brief_page_shows_the_prompt_and_the_validator(client):
    page = strip_tags(client.get("/under/panels/brief").text)
    assert "def validate_structured(" in page
    assert "You write short briefings" in page
    assert "async def write(" in page


def test_code_comes_only_from_the_named_functions(client):
    page = client.get("/under/panels/tone").text
    code = strip_tags(
        page[page.index('id="code"') : page.index('id="local-tables"')]
    )
    assert "def tone_aggregate(" in code
    assert "def coverage_share(" not in code
    assert "def velocity_score(" not in code
    assert "api_key" not in code.lower()


def test_source_text_is_escaped(client):
    page = client.get("/under/panels/map").text
    code = page[page.index('id="code"') : page.index('id="local-tables"')]
    assert "-&gt;" in code
    assert "->" not in code
    assert "-> float" in strip_tags(code)


def test_source_is_colored_as_the_file_extension_states(client):
    page = client.get("/under/panels/tone").text
    code = page[page.index('id="code"') : page.index('id="local-tables"')]
    assert (
        '<span class="code-keyword">def</span> '
        '<span class="code-function">tone_aggregate</span>'
    ) in code


def test_each_source_block_is_exactly_the_function(client):
    page = client.get("/under/panels/tone").text
    shown = [
        strip_tags(block)
        for block in re.findall(
            r'<pre class="u-pre">(.*?)</pre>', page, flags=re.S
        )
    ]
    for ref in CARDS_BY_KEY["tone"].code:
        assert source_of(ref)["source"] in shown


def test_a_long_value_is_colored_only_if_it_states_a_language(client):
    page = client.get("/under/panels/brief").text
    values = page[
        page.index("Values it uses") : page.index('id="local-tables"')
    ]
    blocks = dict(
        re.findall(
            r"<summary><code>(\w+)</code>.*?<pre class=\"u-pre\">(.*?)</pre>",
            values,
            flags=re.S,
        )
    )
    assert '<span class="code-keyword">SELECT</span>' in blocks["ITEMS_SQL"]
    assert (
        '<span class="code-keyword">SELECT</span>' in blocks["STORYLINES_SQL"]
    )
    assert "<span" not in blocks["SYSTEM"]


# Three layers.


def test_a_page_shows_how_each_local_table_it_names_is_built(client):
    page = html.unescape(client.get("/under/panels/wire").text)
    tables = page[page.index('id="local-tables"') :]
    assert "<code>facts</code>" in tables
    tables_text = strip_tags(tables)
    assert "<code>sources</code>" in tables
    assert "FROM lake.raw.items i" in tables_text
    assert "FROM lake.ref.sources" in tables_text
    assert "<code>storyline_versions</code>" not in tables


# Parameters and results.


def test_a_long_parameter_list_is_shortened(web, monkeypatch):
    from monitor.web.under_panels import shown_params

    text = shown_params({"keys": [f"key-{n}" for n in range(30)]})
    assert "30 values" in text
    assert "key-29" not in text


def test_no_stored_text_runs_past_the_cut_in_a_result(lake_template, tmp_path):
    settings = clone_lake(lake_template, tmp_path)
    fill(settings)
    marker = "PASTTHECUT"
    add_rows(
        settings,
        {
            "nlp.briefs": [
                {
                    "brief_id": "long",
                    "markdown": "m" * 200 + marker,
                    "citations": "{}",
                    "focus": "all",
                }
            ],
        },
    )
    with app_client(settings) as fresh:
        page = fresh.get("/under/panels/brief").text
    assert marker not in page


def test_a_result_value_is_escaped(client):
    page = client.get("/under/panels/wire?window=24h").text
    assert "<script>alert" not in page


# A lake form of each statement, through the console's own check.


def test_a_literal_is_quoted_and_checked():
    assert literal("it's") == "'it''s'"
    assert literal(3) == "3"
    assert literal(True) == "TRUE"
    assert literal(None) == "NULL"
    assert literal(["a", "b'"]) == "['a', 'b''']"
    assert literal([]) == "CAST([] AS VARCHAR[])"
    assert literal(datetime(2026, 11, 3, tzinfo=UTC)).startswith("TIMESTAMPTZ")
    assert literal(object()) is None


def test_only_a_parameter_token_is_replaced():
    sql = "SELECT '$a' AS text, $a AS value -- $a\nFROM t"
    assert with_literals(sql, {"a": 1}) == (
        "SELECT '$a' AS text, 1 AS value -- $a\nFROM t"
    )
    assert with_literals("SELECT $missing", {}) is None


def same(first, second):
    if isinstance(first, float) or isinstance(second, float):
        if first is None or second is None:
            return first == second
        return math.isclose(float(first), float(second), abs_tol=1e-5)
    if isinstance(first, list | tuple) and isinstance(second, list | tuple):
        return len(first) == len(second) and all(
            same(a, b) for a, b in zip(first, second, strict=True)
        )
    if isinstance(first, datetime):
        moment = (
            second
            if isinstance(second, datetime)
            else datetime.fromisoformat(second)
        )
        return first == moment
    return first == second


def rows_match(recorded, console):
    """A console row and a recorded row hold the same values, in any order."""
    columns = console["columns"]
    if len(recorded) != len(console["rows"]):
        return False
    wanted = [[row[name] for name in columns] for row in recorded]
    unmatched = list(console["rows"])
    for row in wanted:
        for other in unmatched:
            if same(row, other):
                unmatched.remove(other)
                break
        else:
            return False
    return True


@pytest.mark.parametrize("query", FILTER_SETS)
@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_every_offered_form_passes_the_console_check(web, filled, card, query):
    recorder, _, _ = capture_card(
        web.settings, web.model, card, filters_for(web, query)
    )
    conn = connect(filled, read_only=True)
    try:
        for statement in combined(recorder.statements):
            if statement.name in CONSOLE_VERIFIED:
                form = lake_form(statement, web.model.snapshot.snapshot_id)
                assert form is not None, statement.name
                check_console_sql(conn, form)
    finally:
        conn.close()


@needs_sandbox
@pytest.mark.parametrize("query", ["window=24h", QUERY])
@pytest.mark.parametrize("card", CARDS, ids=lambda card: card.key)
def test_every_offered_form_gives_the_panels_rows_in_the_console(
    web, card, query
):
    as_of = now().isoformat()
    filters = filters_for(web, f"{query}&as_of={as_of}")
    recorder, _, _ = capture_card(web.settings, web.model, card, filters)
    checked = 0
    for statement in combined(recorder.statements):
        if statement.name not in CONSOLE_VERIFIED:
            continue
        form = lake_form(statement, web.model.snapshot.snapshot_id)
        result = web.under.console(form)
        assert result.get("status") == "ok", (statement.name, result)
        assert rows_match(statement.rows, result), statement.name
        checked += 1
    assert checked or card.key == "kpis"


def test_a_form_reads_the_merge_chain_the_read_model_follows(web, filled):
    statement = next(
        s
        for s in combined(
            capture_card(
                web.settings,
                web.model,
                CARDS_BY_KEY["wire"],
                filters_for(web, ""),
            )[0].statements
        )
        if s.name == "wire"
    )
    form = lake_form(statement, web.model.snapshot.snapshot_id)
    assert "WITH RECURSIVE" in form
    assert "merged_hops" in form
    assert "AT (VERSION =>" in form


def test_comparing_rows_catches_a_changed_value():
    console = {"columns": ["a"], "rows": [[1], [2]]}
    assert rows_match([{"a": 2}, {"a": 1}], console)
    assert not rows_match([{"a": 1}, {"a": 3}], console)
    assert not rows_match([{"a": 1}], console)


@needs_sandbox
def test_a_form_follows_merges_as_the_read_model_does(web):
    held = web.model.rows(
        "SELECT storyline_id FROM facts WHERE item_id = 'merged-item'"
    )
    assert held == [{"storyline_id": "survivor"}]
    statement = Recorded(
        "held",
        "SELECT storyline_id FROM facts WHERE item_id = 'merged-item'",
        {},
        held,
        None,
        False,
    )
    form = lake_form(statement, web.model.snapshot.snapshot_id)
    result = web.under.console(form)
    assert result["rows"] == [["survivor"]]


def test_a_statement_with_no_lake_form_says_why_and_offers_no_link(client):
    page = html.unescape(client.get("/under/panels/wire").text)
    assert NEEDS_VISIBLE_SEQ in page
    assert page.count("Open a lake form in the SQL console") >= 1


@pytest.mark.parametrize(
    ("card", "statement"),
    [("map", "map_top_story"), ("calls", "race_calls"), ("brief", "brief")],
)
def test_a_statement_that_can_differ_says_why_and_has_no_link(
    client, card, statement
):
    page = html.unescape(client.get(f"/under/panels/{card}").text)
    start = page.index(f"<code>{statement}</code>, from")
    section = page[start : page.index('id="code"', start)].split(
        'class="u-call"'
    )[0]
    assert NOT_OFFERED[statement] in section
    assert "Open a lake form" not in section


def test_a_tie_can_change_the_top_story_so_it_is_not_offered():
    assert NOT_OFFERED.keys().isdisjoint(CONSOLE_VERIFIED)


def test_the_console_link_fills_the_box_without_running(
    client, web, monkeypatch
):
    page = client.get("/under/panels/stream").text
    link = re.search(r'href="(/under/sql\?sql=[^"]+)"', page)
    assert link
    from monitor.web import under as under_module

    def fails(*_args, **_kwargs):
        raise AssertionError("opening the console runs nothing")

    monkeypatch.setattr(under_module, "run_worker", fails)
    console = client.get(html.unescape(link.group(1))).text
    box = html.unescape(
        console[console.index("<textarea") : console.index("</textarea>")]
    )
    assert "WITH RECURSIVE" in box


# What these pages say.


def page_text(client, path):
    return " ".join(strip_tags(client.get(path).text).split())


def test_the_stream_page_says_what_the_browser_computes(client):
    page = page_text(client, "/under/panels/stream")
    assert "It sums n per bin and storyline there" in page
    assert "each storyline's share of its bin" in page
    assert "computes no figure" not in page


def test_the_map_page_says_what_focus_does_in_each_part(client):
    page = page_text(client, "/under/panels/map")
    assert "On the map, Focus dims the states outside it" in page
    assert "In States rising, Focus chooses which states are listed" in page
    assert "Focus only dims" not in page


def test_the_list_of_cards_defines_its_two_labels(client):
    page = page_text(client, "/under/panels")
    assert (
        "Code marks a card where a model or a statistical formula produces "
        "what is on screen. SQL marks the others, whose pages list any "
        "arithmetic done after the query." in page
    )


def test_the_briefing_page_says_it_shows_the_newest_brief(client):
    page = page_text(client, "/under/panels/brief")
    assert "This page shows the newest brief, with $offset = 0" in page


def test_a_lake_form_says_rows_can_tie(client):
    page = page_text(client, "/under/panels/rising")
    assert "Open a lake form" in page
    assert (
        "Rows written in the same commit with the same key tie on the latest "
        "row, and either may be returned" in page
    )


def test_a_request_from_elsewhere_is_offered_no_lake_form(client, filled):
    assert "/under/sql?sql=" in client.get("/under/panels/rising").text
    with TestClient(
        create_app(filled),
        base_url="http://testserver",
        client=("192.168.1.30", 50000),
    ) as remote:
        page = remote.get("/under/panels/rising").text
    assert "/under/sql?sql=" not in page
    assert "Open a lake form" not in page
    assert "no lake form is offered here" in page
    assert remote.get("/under/sql").status_code == 403


@pytest.mark.parametrize(
    "path", ["/under/panels/rising", "/under/queries", "/under/data-model"]
)
def test_a_page_with_sql_says_once_that_line_breaks_are_added(client, path):
    page = client.get(path).text
    assert 'class="u-sql"' in page
    assert page.count("Line breaks are added for reading.") == 1


def test_a_page_with_no_sql_does_not_mention_line_breaks(client):
    page = client.get("/under/panels").text
    assert 'class="u-sql"' not in page
    assert "Line breaks are added for reading." not in page


def nav_cards_link(page):
    return html.unescape(re.search(r'<a href="(/under/panels[^"]*)"', page)[1])


@pytest.mark.parametrize("path", ["/under/panels", "/under/panels/rising"])
def test_the_cards_navigation_link_keeps_the_filters(client, path):
    link = nav_cards_link(client.get(f"{path}?{QUERY}").text)
    assert "window=3h" in link
    assert "focus=battleground" in link


def test_the_cards_navigation_link_adds_no_filters_to_another_view(client):
    assert nav_cards_link(client.get("/under/sql").text) == "/under/panels"


def test_a_repeated_statement_says_which_methods_ran_it():
    def ran(method):
        return Recorded(
            "focus_sets",
            "SELECT 1",
            None,
            [],
            1.0,
            False,
            method=method,
            runs_by_method={method: 1},
        )

    merged = combined([ran("map_data")] * 3 + [ran("states_rising")] * 2)
    assert len(merged) == 1
    assert merged[0].runs == 5
    assert merged[0].runs_by_method == {"map_data": 3, "states_rising": 2}


def test_the_race_call_page_states_the_rules_that_apply(client):
    page = client.get(f"/under/panels/calls?{QUERY}").text
    assert "decided what its sentence means" in page
    assert "the article to name the candidate" in page
    assert "language for its status" not in page


def test_the_map_page_credits_each_run_to_its_method(client):
    page = client.get(f"/under/panels/map?{QUERY}").text
    assert "The method ran this" not in page
    runs = re.findall(
        r"This ran (\d+) times: (\d+) from <code>map_data</code>, "
        r"(\d+) from <code>states_rising</code>\.",
        page,
    )
    assert runs
    for total, first, second in runs:
        assert int(total) == int(first) + int(second)


def test_a_replayed_card_page_says_which_moment_it_shows(client):
    as_of = quote((now() - timedelta(hours=1)).isoformat())
    stamp = et_label(now() - timedelta(hours=1))[:6]
    response = client.get(f"/under/panels/rising?as_of={as_of}")
    match = re.search(
        r"<p[^>]*data-replay-moment[^>]*>([^<]*)</p>", response.text
    )
    assert match
    assert stamp in match[1]
    assert "Back returns to the replay at that moment" in match[1]
    back = html.unescape(
        re.search(r'<a class="text-link" href="(/\?[^"]*)"', response.text)[1]
    )
    assert "as_of=" in back


def test_a_live_card_page_mentions_no_moment(client):
    assert "data-replay-moment" not in client.get("/under/panels/rising").text
