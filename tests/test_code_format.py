"""Code as the views show it: colored, and exactly the text it was."""

import json
import re

import pytest
from markupsafe import escape
from test_sql_format import HOSTILE, ROOT, strip_tags

from monitor.web import code_format
from monitor.web.code_format import language_of, render_code
from monitor.web.sql_format import CSS_CLASSES, MAX_FORMATTED_CHARS

PYTHON = '''@cached
class Reader(Base):
    """Reads one thing."""

    def read(self, limit=10, rate=2.5):
        # Stop early.
        if limit is None:
            raise ValueError("no limit")
        return len(self.rows) > 1
'''
JAVASCRIPT = """const limit = 10;
// Stop early.
function read(rows) {
  return rows.length > limit ? "many" : null;
}
"""
JSON_TEXT = json.dumps(
    {"title": "A story", "count": 3, "share": 0.5, "ok": True, "none": None},
    indent=2,
)
SQL = "SELECT count(*) AS n FROM lake.raw.items -- every row\nWHERE a = 'b'"
LONG_LINE_SQL = (
    "SELECT a, b, c FROM lake.raw.items WHERE a = 1 " + "AND b = 2 " * 9
)
SOURCES = {
    "python": PYTHON,
    "javascript": JAVASCRIPT,
    "json": JSON_TEXT,
    "sql": SQL,
}
LANGUAGES = list(SOURCES)
ALLOWED_TAG = re.compile(r'<span class="code-\w+">|</span>')


def span(kind, text):
    return f'<span class="code-{kind}">{escape(text)}</span>'


@pytest.mark.parametrize(
    "kind,text",
    [
        ("function", "@cached"),
        ("keyword", "class"),
        ("function", "Reader"),
        ("string", '"""Reads one thing."""'),
        ("keyword", "def"),
        ("function", "read"),
        ("number", "10"),
        ("number", "2.5"),
        ("comment", "# Stop early."),
        ("keyword", "is"),
        ("keyword", "None"),
        ("function", "ValueError"),
        ("string", '"no limit"'),
        ("function", "len"),
    ],
)
def test_python_is_colored(kind, text):
    assert span(kind, text) in render_code(PYTHON, "python")


def test_a_python_name_and_self_stay_uncolored():
    markup = render_code(PYTHON, "python")
    assert ">limit<" not in markup
    assert ">self<" not in markup


@pytest.mark.parametrize(
    "kind,text",
    [
        ("keyword", "const"),
        ("number", "10"),
        ("comment", "// Stop early."),
        ("keyword", "function"),
        ("keyword", "return"),
        ("string", '"many"'),
        ("keyword", "null"),
    ],
)
def test_javascript_is_colored(kind, text):
    assert span(kind, text) in render_code(JAVASCRIPT, "javascript")


@pytest.mark.parametrize(
    "kind,text",
    [
        ("identifier", '"title"'),
        ("string", '"A story"'),
        ("number", "3"),
        ("number", "0.5"),
        ("keyword", "true"),
        ("keyword", "null"),
    ],
)
def test_json_is_colored(kind, text):
    assert span(kind, text) in render_code(JSON_TEXT, "json")


def test_stated_sql_is_colored():
    assert span("keyword", "SELECT") in render_code(SQL, "sql")


@pytest.mark.parametrize("language", [*LANGUAGES, None])
@pytest.mark.parametrize(
    "shape",
    [
        "{}",
        "\n{}\n",
        "\n\n  {}  \n\n",
        "{}\n\n\n",
        "\t{}\t",
        "﻿{}",
        "{}\r\n",
        "{}\r",
        "{}\n# é 中 🗳 & < > ' \"",
    ],
)
def test_the_text_stays_exact(language, shape):
    text = shape.format(SOURCES.get(language, PYTHON))
    assert strip_tags(render_code(text, language)) == text


def test_indented_json_stays_exact():
    text = json.dumps(
        {"a": [1, {"b": "é\n<&>"}], "c": {}, "d": []},
        indent=4,
        ensure_ascii=False,
    )
    markup = render_code(text, "json")
    assert "<span" in markup
    assert strip_tags(markup) == text


def test_a_long_line_of_sql_keeps_its_one_line():
    assert strip_tags(render_code(LONG_LINE_SQL, "sql")) == LONG_LINE_SQL


@pytest.mark.parametrize(
    "language,text",
    [
        ("python", f"x = '{HOSTILE}'  # {HOSTILE}\n{HOSTILE}\n"),
        ("python", f'"""{HOSTILE}"""\ndef {HOSTILE}(): pass\n'),
        ("javascript", f'const a = "{HOSTILE}"; // {HOSTILE}\n/*{HOSTILE}*/'),
        ("javascript", f"const a = `{HOSTILE}${{{HOSTILE}}}`; {HOSTILE}"),
        ("json", json.dumps({HOSTILE: HOSTILE, "a": [HOSTILE]})),
        ("json", HOSTILE),
        ("sql", f"SELECT '{HOSTILE}' -- {HOSTILE}"),
        (None, HOSTILE),
    ],
)
def test_no_token_lets_markup_through(language, text):
    markup = str(render_code(text, language))
    assert "<script>" not in markup
    assert all(
        ALLOWED_TAG.fullmatch(tag) for tag in re.findall(r"<[^>]*>?", markup)
    )
    assert strip_tags(markup) == text


@pytest.mark.parametrize(
    "text",
    [
        '{"cut": "in the mid',
        "<item><title>A feed entry</title></item>",
        "No raw entry is stored.",
        "{'a': 1}",
        "[" * MAX_FORMATTED_CHARS,
        "",
    ],
)
def test_json_that_does_not_parse_is_plain(text):
    assert render_code(text, "json") == escape(text)


@pytest.mark.parametrize("language", LANGUAGES)
def test_text_over_the_limit_is_plain(language):
    text = SOURCES[language] * (MAX_FORMATTED_CHARS // len(SOURCES[language]))
    if language == "json":
        text = json.dumps(["a" * MAX_FORMATTED_CHARS])
    else:
        text += "\n" + SOURCES[language]
    assert len(text) > MAX_FORMATTED_CHARS
    assert render_code(text, language) == escape(text)


@pytest.mark.parametrize("language", [None, "", "xml", "Python", "text"])
def test_text_with_no_stated_language_is_plain(language):
    assert render_code(JSON_TEXT, language) == escape(JSON_TEXT)
    assert render_code(PYTHON, language) == escape(PYTHON)


def test_no_text_is_empty():
    assert render_code(None, "python") == ""
    assert render_code("", "python") == ""


def test_a_lexer_that_fails_leaves_the_text_plain(monkeypatch):
    class Fails:
        def get_tokens_unprocessed(self, _text):
            raise RuntimeError("no")

    monkeypatch.setitem(code_format.LEXERS, "python", Fails())
    assert render_code(PYTHON, "python") == escape(PYTHON)


def test_a_lexer_that_drops_text_leaves_the_text_plain(monkeypatch):
    class Drops:
        def get_tokens_unprocessed(self, text):
            yield 0, code_format.Keyword, text[1:]

    monkeypatch.setitem(code_format.LEXERS, "python", Drops())
    assert render_code(PYTHON, "python") == escape(PYTHON)


@pytest.mark.parametrize(
    "name,language",
    [
        ("src/monitor/analytics.py", "python"),
        ("src/monitor/web/static/js/under.js", "javascript"),
        ("config/races.csv", None),
        ("Procfile", None),
    ],
)
def test_a_file_extension_states_the_language(name, language):
    assert language_of(name) == language


@pytest.mark.parametrize("css", CSS_CLASSES.values())
def test_the_compiled_stylesheet_has_each_token(css):
    compiled = (ROOT / "src/monitor/web/static/css/app.css").read_text(
        encoding="utf-8"
    )
    assert f".{css}" in compiled
    assert f"--{css}" in compiled


def test_the_raw_view_sits_on_the_surface_the_colors_were_chosen_for():
    css = (ROOT / "src/monitor/web/static/css/input.css").read_text(
        encoding="utf-8"
    )
    rule = css[css.index("pre.raw {") :]
    assert "background: var(--surface-2)" in rule[: rule.index("}")]
