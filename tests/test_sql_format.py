"""SQL as the teaching views show it: colored, and the statement that ran."""

import html
import re
from pathlib import Path

import duckdb
import pytest
from test_under import app_client

from monitor.lake import connect
from monitor.web.sql_format import (
    COMMENT,
    CSS_CLASSES,
    FUNCTION,
    IDENTIFIER,
    KEYWORD,
    NUMBER,
    PARAM,
    STRING,
    laid_out,
    render_sql,
    tokenize,
)
from monitor.web.under import console_available
from monitor.web.under_lake import catalog_counts_sql
from monitor.web.under_queries import QUERIES

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/monitor/web/static/css/input.css").read_text(
    encoding="utf-8"
)
GALLERY = [query.text(1, 2, 0.5) for query in QUERIES]
ONE_LINE = (
    "SELECT count(*) AS n_rows, left(title, 3) AS start, "
    "row_number() OVER (PARTITION BY a ORDER BY b) AS position "
    "FROM lake.raw.items i LEFT JOIN lake.nlp.lang l USING (item_id) "
    "WHERE i.title = 'it''s' AND i.raw_entry ->> '$.tags' = $tag "
    "GROUP BY ALL HAVING count(*) > 1 ORDER BY n_rows DESC LIMIT 3"
)
ODD = [
    ONE_LINE,
    catalog_counts_sql(),
    "",
    "   ",
    "SELECT 'no end to this string <script>alert(1)</script>",
    'SELECT "no end to this name',
    "SELECT 1 FROM t -- FROM a comment, and 'not a string",
    "SELECT /* WHERE x */ 1 /* no end",
    "SELECT $$ dollars $$, $1, $name, a::INT, b->>'c', d->'e', 1.5e3",
    'SELECT "a""b".c, t.*, lake.raw.items.title FROM "my table"',
    "SELECT 1 FROM (SELECT x FROM y WHERE z ORDER BY a) t ORDER BY 1",
    "SELECT a<b&&c>d FROM t",
    "SELECT é, 日本 FROM t WHERE x = '日本'",
]


def strip_tags(markup):
    return html.unescape(re.sub(r"<[^>]+>", "", str(markup)))


def spaced(text):
    return " ".join(text.split())


def kinds(sql):
    return {token.text: token.kind for token in tokenize(sql)}


@pytest.mark.parametrize("sql", GALLERY + ODD)
def test_every_character_is_in_one_token(sql):
    assert "".join(token.text for token in tokenize(sql)) == sql


@pytest.mark.parametrize("sql", GALLERY + ODD)
def test_the_text_stays_the_statement_that_ran(sql):
    assert spaced(strip_tags(render_sql(sql))) == spaced(sql)


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q.key)
def test_a_hand_formatted_query_keeps_its_layout(query):
    sql = query.text(1, 2, 0.5)
    assert strip_tags(render_sql(sql)) == sql


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q.key)
def test_text_copied_from_a_block_is_one_valid_statement(query):
    copied = strip_tags(render_sql(query.text(1, 2, 0.5)))
    assert len(duckdb.connect().extract_statements(copied)) == 1


def test_a_long_copied_line_stays_valid_after_its_breaks():
    copied = strip_tags(render_sql(ONE_LINE))
    assert "\n" in copied
    assert len(duckdb.connect().extract_statements(copied)) == 1


def test_a_long_single_line_breaks_before_major_clauses():
    lines = strip_tags(render_sql(ONE_LINE)).splitlines()
    starts = [line.split()[0] for line in lines]
    assert starts == [
        "SELECT",
        "FROM",
        "LEFT",
        "WHERE",
        "GROUP",
        "HAVING",
        "ORDER",
        "LIMIT",
    ]


def test_a_join_stays_with_the_word_before_it():
    assert "LEFT JOIN lake.nlp.lang" in strip_tags(render_sql(ONE_LINE))


def test_a_window_keeps_its_order_by_on_its_line():
    text = strip_tags(render_sql(ONE_LINE))
    assert "OVER (PARTITION BY a ORDER BY b)" in text


def test_a_subquery_breaks_and_indents():
    sql = (
        "SELECT a FROM (SELECT a FROM lake.raw.items WHERE a > 1 ORDER BY a) "
        "t WHERE a > 2 ORDER BY a"
    )
    lines = strip_tags(render_sql(sql)).splitlines()
    assert "  FROM lake.raw.items" in lines
    assert "  WHERE a > 1" in lines
    assert "WHERE a > 2" in [line.split(" ORDER")[0] for line in lines]


def test_a_short_line_stays_on_one_line():
    assert "\n" not in strip_tags(render_sql("SELECT 1 FROM t WHERE a"))


def test_no_space_is_added_where_none_was():
    sql = "SELECT a, b, c, d, e, f, g, h, i, j, k, l, m, n, o, p, q (a)FROM t"
    assert ")FROM" in strip_tags(render_sql(sql))


def test_a_keyword_in_a_string_or_comment_does_not_break_a_line():
    sql = (
        "SELECT 'a FROM b WHERE c ORDER BY d GROUP BY e LIMIT 1, long enough' "
        "AS value -- FROM here"
    )
    assert strip_tags(render_sql(sql)) == sql


def test_tokens_get_their_kinds():
    found = kinds(
        "SELECT round(a), lake.raw.items.title, 'it''s', 1.5, $p, "
        "ducklake_list_files('lake') -- note"
    )
    assert found["SELECT"] == KEYWORD
    assert found["round"] == FUNCTION
    assert found["lake.raw.items.title"] == IDENTIFIER
    assert found["'it''s'"] == STRING
    assert found["1.5"] == NUMBER
    assert found["$p"] == PARAM
    assert found["ducklake_list_files"] == FUNCTION
    assert found["-- note"] == COMMENT


def test_a_keyword_used_as_a_function_is_a_function():
    assert kinds("left(a, 1)")["left"] == FUNCTION
    assert kinds("a LEFT JOIN b")["LEFT"] == KEYWORD
    assert kinds("a IN(1)")["IN"] == KEYWORD


def test_operators_stay_whole():
    texts = [token.text for token in tokenize("a->>'b' ->'c' ::INT >= 1")]
    assert "->>" in texts
    assert "->" in texts
    assert "::" in texts
    assert ">=" in texts


def test_an_unterminated_string_takes_the_rest_of_the_statement():
    sql = "SELECT 'no end </pre><script>alert(1)</script>"
    tokens = tokenize(sql)
    assert tokens[-1].kind == STRING
    assert tokens[-1].text.endswith("</script>")
    markup = str(render_sql(sql))
    assert "<script>" not in markup
    assert "</pre>" not in markup
    assert "&lt;script&gt;" in markup


def test_everything_is_escaped():
    for sql in ("<img src=x onerror=alert(1)>", '"<b>"', "'<b>'", "-- <b>"):
        markup = str(render_sql(sql))
        assert "<img" not in markup
        assert "<b>" not in markup


def test_nothing_renders_as_nothing():
    assert str(render_sql(None)) == ""
    assert laid_out("") == []


def luminance(color):
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def block(selector):
    body = CSS[CSS.index(selector + " {") :]
    body = body[body.index("{") + 1 : body.index("}")]
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", body))


LIGHT = block(":root")
DARK = {**LIGHT, **block(':root[data-theme="dark"]')}
PROJECTOR_LIGHT = {**LIGHT, **block(':root[data-projector="1"]')}
PROJECTOR_DARK = {
    **DARK,
    **block(':root[data-projector="1"]'),
    **block(':root[data-projector="1"][data-theme="dark"]'),
}
THEMES = {
    "light": LIGHT,
    "dark": DARK,
    "projector": PROJECTOR_LIGHT,
    "projector dark": PROJECTOR_DARK,
}


def test_the_sql_block_sits_on_the_second_surface():
    rule = CSS[CSS.index(".u-sql, .u-pre {") :]
    assert "background: var(--surface-2)" in rule[: rule.index("}")]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("css", CSS_CLASSES.values())
def test_each_token_color_meets_contrast(theme, css):
    tokens = THEMES[theme]
    color = tokens[f"--{css}"]
    assert contrast(color, tokens["--surface-2"]) >= 4.5, (theme, css, color)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("css", CSS_CLASSES.values())
def test_a_token_color_is_not_a_party_color(theme, css):
    tokens = THEMES[theme]
    color = tokens[f"--{css}"].lower()
    assert color not in {tokens["--party-dem"], tokens["--party-rep"]}
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    # Blue or red that dominates would read as a party.
    assert not (blue > 1.6 * max(red, green)), (theme, css, color)
    assert not (red > 2.5 * max(green, blue)), (theme, css, color)


@pytest.mark.parametrize("css", CSS_CLASSES.values())
def test_each_token_has_a_style(css):
    assert f".{css} " in CSS


@pytest.fixture
def seeded(settings):
    conn = connect(settings)
    conn.execute(
        "INSERT INTO lake.raw.fetches BY NAME "
        "SELECT 'f' AS fetch_id, now() AS created_at"
    )
    conn.close()
    with app_client(settings) as client:
        yield client


def test_a_disclosed_query_is_colored(seeded):
    page = seeded.get("/under/lake?table=raw.fetches").text
    assert '<pre class="u-sql">' in page
    assert '<span class="sql-keyword">SELECT</span>' in page
    assert '<span class="sql-identifier">lake.raw.fetches</span>' in page


def test_the_gallery_is_colored(seeded):
    page = seeded.get("/under/queries").text
    assert '<span class="sql-keyword">SELECT</span>' in page
    assert '<span class="sql-comment">-- raw_entry is JSON.' in page


@pytest.mark.skipif(
    not console_available(), reason="The console needs the macOS sandbox"
)
def test_the_console_echoes_a_colored_escaped_statement(seeded):
    typed = "SELECT '<script>alert(1)</script>' AS x"
    page = seeded.post(
        "/under/sql",
        data={"sql": typed},
        headers={"Origin": "http://127.0.0.1:8000"},
    ).text
    assert "<script>alert(1)</script>" not in page
    box = page[page.index("<textarea") : page.index("</textarea>")]
    assert "<span" not in box
    echo = page[page.index('<pre class="u-sql">') :]
    echo = echo[: echo.index("</pre>")]
    assert '<span class="sql-keyword">SELECT</span>' in echo
    assert "&lt;script&gt;" in echo
