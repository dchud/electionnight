"""SQL as the teaching views show it: colored, and the statement that ran."""

import html
import json
import re
import threading
import time
from pathlib import Path

import duckdb
import pytest
from test_under import app_client

from monitor.lake import connect
from monitor.web import sql_format
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
# DuckDB reads each of these differently from a tokenizer that knows only plain
# strings and flat comments, and each is long enough to get line breaks.
ADVERSARIAL = [
    "SELECT $q$ one FROM two WHERE three ORDER BY four $q$ AS text_with_it, "
    "1 AS another_column",
    "SELECT $q$ ' $q$ AS v, 1 AS padding_to_make_it_long_enough "
    "-- ' FROM secret WHERE x = 0",
    "SELECT $a1$ FROM two WHERE three $a1$ AS v, 'padding padding padding "
    "padding padding padding' AS p",
    "SELECT E'it\\'s one FROM two WHERE three ORDER BY four' AS text_with_it, "
    "1 AS another",
    "SELECT E'\\'', 1 AS padding_to_make_this_statement_long_enough "
    "-- ' FROM secret WHERE x = 0",
    "SELECT 1 AS a /* outer /* inner */ still comment FROM secret WHERE x = 1 "
    "*/, 2 AS padding_to_be_long_enough",
    "SELECT 'it''s FROM two WHERE three ORDER BY four' AS value, "
    "1 AS padding_padding_padding_padding",
    'SELECT "a FROM b""c" AS "x WHERE y", \'padding padding padding padding '
    "padding padding' AS p FROM t",
    "SELECT $$ one FROM two WHERE three $$ AS v, 'padding padding padding "
    "padding padding padding' AS p",
    "SELECT $$ ' $$ AS v, 'padding padding padding padding padding padding' "
    "AS p FROM t WHERE a = 1",
    "SELECT $first, $1, a->>'$.b', 'padding padding padding padding padding "
    "padding' AS p FROM t WHERE c = $second",
    "SELECT x'4142' AS v, 'padding padding padding padding padding padding "
    "padding' AS p FROM t",
    "SELECT 1 AS padding_padding_padding_padding_padding_padding_padding "
    "-- note\rFROM secret WHERE x = 0",
    "SELECT a, b, 'padding padding padding padding' AS pad FROM t WHERE a "
    "IS NOT DISTINCT FROM b ORDER BY a",
    "SELECT extract(year FROM d) AS y, trim(both ' ' FROM x) AS z, "
    "substring(a FROM 1 FOR 2) AS s FROM t ORDER BY a",
    "SELECT * EXCEPT (a) FROM t WHERE b = 1 EXCEPT SELECT * EXCLUDE (a) "
    "FROM t LIMIT 1",
    "SELECT 'no end FROM x WHERE y ORDER BY z padding padding padding "
    "padding padding padding padding",
    "SELECT /* no end FROM x WHERE y ORDER BY z padding padding padding "
    "padding padding padding",
    'SELECT "no end FROM x WHERE y ORDER BY z padding padding padding '
    "padding padding padding padding",
    "SELECT $q$ no end FROM x WHERE y ORDER BY z padding padding padding "
    "padding padding padding",
]


def strip_tags(markup):
    return html.unescape(re.sub(r"<[^>]+>", "", str(markup)))


def spaced(text):
    return " ".join(text.split())


def kinds(sql):
    return {token.text: token.kind for token in tokenize(sql)}


def serialized(sql):
    """DuckDB's parse of a statement, with no positions in it.

    A statement that does not parse is its kind of error.
    """
    text = (
        duckdb.connect()
        .execute("SELECT json_serialize_sql(?)", [sql])
        .fetchone()[0]
    )
    tree = json.loads(text)
    if tree.get("error"):
        return tree["error_type"]
    return without_positions(tree)


def without_positions(value):
    if isinstance(value, dict):
        return {
            key: without_positions(item)
            for key, item in value.items()
            if not key.startswith("query_location")
        }
    if isinstance(value, list):
        return [without_positions(item) for item in value]
    return value


def literal_tokens(sql):
    """The exact text of each string and comment, as DuckDB reads them."""
    encoded = sql.encode("utf-8")
    marks = duckdb.tokenize(sql)
    ends = [start for start, _ in marks[1:]] + [len(encoded)]
    return [
        encoded[start:end].decode("utf-8").rstrip()
        for (start, kind), end in zip(marks, ends, strict=False)
        if kind in {duckdb.token_type.string_const, duckdb.token_type.comment}
    ]


@pytest.mark.parametrize("sql", GALLERY + ODD + ADVERSARIAL)
def test_every_character_is_in_one_token(sql):
    assert "".join(token.text for token in tokenize(sql)) == sql


@pytest.mark.parametrize("sql", GALLERY + ODD + ADVERSARIAL)
def test_the_text_stays_the_statement_that_ran(sql):
    assert spaced(strip_tags(render_sql(sql))) == spaced(sql)


@pytest.mark.parametrize("sql", GALLERY + ODD + ADVERSARIAL)
def test_the_displayed_text_parses_the_same(sql):
    assert serialized(strip_tags(render_sql(sql))) == serialized(sql)


@pytest.mark.parametrize("sql", GALLERY + ODD + ADVERSARIAL)
def test_strings_and_comments_keep_their_exact_text(sql):
    assert literal_tokens(strip_tags(render_sql(sql))) == literal_tokens(sql)


def test_the_gallery_really_parses_so_comparing_it_proves_something():
    assert not any(isinstance(serialized(sql), str) for sql in GALLERY)


def test_comparing_parses_catches_a_break_inside_a_string():
    sql = "SELECT $q$ one FROM two $q$ AS v"
    assert serialized(sql.replace(" FROM", "\nFROM")) != serialized(sql)


def test_comparing_parses_catches_a_comment_turned_into_code():
    sql = "SELECT 1 AS a -- ' FROM secret WHERE x = 0"
    assert serialized(sql.replace(" FROM", "\nFROM")) != serialized(sql)


def test_comparing_parses_ignores_whitespace_between_tokens():
    sql = "SELECT 1 AS a FROM t WHERE a = 1"
    assert serialized(sql.replace(" FROM", "\nFROM")) == serialized(sql)


def test_comparing_literals_catches_a_changed_comment():
    sql = "SELECT 1 /* a FROM b */"
    assert literal_tokens(sql.replace(" FROM", "\nFROM")) != literal_tokens(
        sql
    )


def test_a_copied_comment_stays_a_comment():
    sql = ADVERSARIAL[4]
    conn = duckdb.connect()
    conn.execute("CREATE TABLE secret AS SELECT 42 AS x")
    copied = strip_tags(render_sql(sql))
    assert conn.execute(copied).fetchall() == conn.execute(sql).fetchall()


def test_a_tagged_string_keeps_its_value():
    sql = ADVERSARIAL[0]
    copied = strip_tags(render_sql(sql))
    conn = duckdb.connect()
    assert conn.execute(copied).fetchall() == conn.execute(sql).fetchall()


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q.key)
def test_a_hand_formatted_query_keeps_its_layout(query):
    sql = query.text(1, 2, 0.5)
    assert strip_tags(render_sql(sql)) == sql


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q.key)
def test_text_copied_from_a_block_parses_the_same(query):
    sql = query.text(1, 2, 0.5)
    assert serialized(strip_tags(render_sql(sql))) == serialized(sql)


def test_a_long_copied_line_breaks_and_parses_the_same():
    copied = strip_tags(render_sql(ONE_LINE))
    assert "\n" in copied
    assert serialized(copied) == serialized(ONE_LINE)


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


@pytest.mark.parametrize(
    ("sql", "kept"),
    [
        (
            "SELECT a, b, 'padding padding padding padding' AS pad FROM t "
            "WHERE a IS NOT DISTINCT FROM b ORDER BY a",
            "WHERE a IS NOT DISTINCT FROM b",
        ),
        (
            "SELECT a, b, 'padding padding padding padding' AS pad FROM t "
            "WHERE a IS DISTINCT FROM b ORDER BY a",
            "WHERE a IS DISTINCT FROM b",
        ),
        (
            "SELECT extract(year FROM d) AS y, 'padding padding padding "
            "padding padding' AS p FROM t ORDER BY a",
            "extract(year FROM d)",
        ),
        (
            "SELECT a FROM (SELECT extract(year FROM d) AS a FROM t) x "
            "WHERE a > 2 ORDER BY a -- padding",
            "extract(year FROM d)",
        ),
        (
            "SELECT trim(both ' ' FROM x) AS z, substring(a FROM 1 FOR 2) AS "
            "s, 'padding padding' AS p FROM t",
            "substring(a FROM 1 FOR 2)",
        ),
        (
            "SELECT * EXCEPT (a) FROM t WHERE b = 1 ORDER BY c LIMIT 1 "
            "-- padding padding padding padding",
            "SELECT * EXCEPT (a)",
        ),
    ],
)
def test_a_from_that_is_not_a_clause_stays_on_its_line(sql, kept):
    assert kept in strip_tags(render_sql(sql))


def test_a_clause_after_a_distinct_comparison_still_breaks():
    lines = strip_tags(
        render_sql(
            "SELECT a, b, 'padding padding padding padding' AS pad FROM t "
            "WHERE a IS DISTINCT FROM b ORDER BY a"
        )
    ).splitlines()
    assert [line.split()[0] for line in lines] == [
        "SELECT",
        "FROM",
        "WHERE",
        "ORDER",
    ]


@pytest.mark.parametrize(
    ("sql", "starts"),
    [
        (
            "SELECT a FROM t NATURAL JOIN u ANTI JOIN v POSITIONAL JOIN w "
            "LEFT ANTI JOIN x ASOF LEFT JOIN y",
            [
                "SELECT",
                "FROM",
                "NATURAL",
                "ANTI",
                "POSITIONAL",
                "LEFT",
                "ASOF",
            ],
        ),
    ],
)
def test_a_join_starts_at_its_first_word(sql, starts):
    lines = strip_tags(render_sql(sql)).splitlines()
    assert [line.split()[0] for line in lines] == starts


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
        "SELECT round(a), lake.raw.items.title, 'it''s', 1.5, $p, title, "
        "ducklake_list_files('lake') -- note"
    )
    assert found["SELECT"] == KEYWORD
    assert found["round"] == FUNCTION
    assert found["lake.raw.items.title"] == IDENTIFIER
    assert found["title"] == IDENTIFIER
    assert found["'it''s'"] == STRING
    assert found["1.5"] == NUMBER
    assert found["$p"] == PARAM
    assert found["ducklake_list_files"] == FUNCTION
    assert found["-- note"] == COMMENT


def test_a_keyword_used_as_a_function_is_a_function():
    assert kinds("left(a, 1)")["left"] == FUNCTION
    assert kinds("a LEFT JOIN b")["LEFT"] == KEYWORD
    assert kinds("a IN(1)")["IN"] == KEYWORD


@pytest.mark.parametrize(
    "word",
    [
        "version",
        "text",
        "hour",
        "minute",
        "second",
        "date",
        "time",
        "timestamp",
        "key",
        "kind",
        "set",
        "first",
        "last",
        "row",
        "rows",
        "range",
        "current",
        "to",
    ],
)
def test_a_column_name_stays_an_identifier(word):
    assert kinds(f"SELECT {word}, a FROM t")[word] == IDENTIFIER
    assert (
        kinds(f"SELECT {word.upper()}, a FROM t")[word.upper()] == IDENTIFIER
    )


def test_a_qualified_name_is_one_identifier_even_with_a_keyword_in_it():
    assert kinds("SELECT t.from, t.order FROM t")["t.from"] == IDENTIFIER
    assert kinds("SELECT t.* FROM t")["t.*"] == IDENTIFIER


@pytest.mark.parametrize(
    ("sql", "words"),
    [
        ("ORDER BY a NULLS LAST", ["NULLS", "LAST"]),
        ("ORDER BY a NULLS FIRST", ["NULLS", "FIRST"]),
        ("a AT TIME ZONE 'UTC'", ["AT", "TIME", "ZONE"]),
        (
            "OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
            ["ROWS", "BETWEEN", "UNBOUNDED", "CURRENT", "ROW"],
        ),
    ],
)
def test_a_word_beside_its_partner_is_a_keyword(sql, words):
    found = kinds(sql)
    assert all(found[word] == KEYWORD for word in words)


def test_only_a_short_list_of_words_are_keywords():
    assert len(sql_format.KEYWORDS) < 80
    assert not {"VERSION", "TEXT", "HOUR", "DATE"} & sql_format.KEYWORDS


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


HOSTILE = "</pre><script>alert(1)</script><img src=x onerror=alert(1)>"


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT '{HOSTILE}'",
        f'SELECT "{HOSTILE}"',
        f"SELECT 1 -- {HOSTILE}",
        f"SELECT /* {HOSTILE} */ 1",
        f"SELECT $${HOSTILE}$$",
        f"SELECT $q${HOSTILE}$q$",
        f"SELECT E'{HOSTILE}'",
        f"SELECT $name{HOSTILE}",
        f"SELECT {HOSTILE} FROM t",
        f"{HOSTILE}",
        "x" * 60_000 + HOSTILE,
    ],
)
def test_no_token_type_lets_markup_through(sql):
    markup = str(render_sql(sql))
    tags = re.findall(r"<[^>]*>?", markup)
    allowed = re.compile(r'<span class="code-\w+">|</span>')
    assert all(allowed.fullmatch(tag) for tag in tags), tags
    assert spaced(strip_tags(markup)) == spaced(sql)


def test_text_the_tokenizer_cannot_read_is_shown_escaped(monkeypatch):
    def fails(_sql):
        raise RuntimeError("no")

    monkeypatch.setattr(sql_format.duckdb, "tokenize", fails)
    markup = str(render_sql("SELECT '<b>'"))
    assert markup == "SELECT &#39;&lt;b&gt;&#39;"


def test_a_lone_surrogate_is_shown_escaped():
    markup = str(render_sql("SELECT '\udc80 <b>'"))
    assert "<span" not in markup
    assert "&lt;b&gt;" in markup


def test_huge_text_is_shown_escaped_and_uncolored():
    sql = "SELECT " + "(" * (sql_format.MAX_FORMATTED_CHARS + 1)
    markup = str(render_sql(sql))
    assert "<span" not in markup
    assert markup == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + "(" * 99_990,
        "SELECT " + "( " * 49_990,
        "SELECT " + "(" * 49_990,
        "SELECT " + "( " * 24_990,
        "SELECT" + " FROM" * 9_990,
        "SELECT" + " LEFT" * 9_990,
        "SELECT" + " GROUP" * 8_300,
        "SELECT " + "(SELECT " * 6_200,
        "SELECT " + "(" * 12_000 + " a" * 18_000,
        "SELECT " + "'" * 99_990,
        "SELECT /*" + "*" * 99_990,
        "SELECT " + "a." * 49_990,
    ],
    ids=lambda sql: f"{sql[7:14]!r}x{len(sql)}",
)
def test_adversarial_input_at_the_byte_limit_stays_fast(sql):
    started = time.perf_counter()
    markup = render_sql(sql)
    assert time.perf_counter() - started < 1.0
    assert spaced(strip_tags(markup)) == spaced(sql)


def test_formatting_needs_no_connection(monkeypatch):
    def fails(*_args, **_kwargs):
        raise AssertionError("no connection is needed")

    monkeypatch.setattr(sql_format.duckdb, "connect", fails)
    assert 'class="code-keyword">SELECT' in str(render_sql("SELECT 1"))


def test_formatting_works_from_several_threads():
    failures = []

    def format_many():
        for _ in range(200):
            if strip_tags(render_sql(ONE_LINE)).count("\n") != 7:
                failures.append("changed")

    threads = [threading.Thread(target=format_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []


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
    assert '<span class="code-keyword">SELECT</span>' in page
    assert '<span class="code-identifier">lake.raw.fetches</span>' in page


def test_the_gallery_is_colored(seeded):
    page = seeded.get("/under/queries").text
    assert '<span class="code-keyword">SELECT</span>' in page
    assert '<span class="code-comment">-- raw_entry is JSON.' in page


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
    assert '<span class="code-keyword">SELECT</span>' in echo
    assert "&lt;script&gt;" in echo
