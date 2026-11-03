"""Code as the teaching views show it: colored, and exactly the text it was.

A view states a block's language, and nothing is guessed from its content. JSON
that does not parse, text over the size limit, and text with no stated language
are shown escaped and uncolored.

Only spans are added, so an exact copy stays exact. Everything is escaped,
because a block can hold feed text and model output. This runs on the server,
so no script is needed to read a block. The colors are the ones SQL uses.
"""

from __future__ import annotations

import json
from itertools import groupby
from pathlib import PurePath
from typing import Any

from markupsafe import Markup
from pygments.lexers.data import JsonLexer
from pygments.lexers.javascript import JavascriptLexer
from pygments.lexers.python import PythonLexer
from pygments.token import Comment, Keyword, Name, Number, Operator, String

from monitor.web.sql_format import (
    COMMENT,
    FUNCTION,
    IDENTIFIER,
    KEYWORD,
    MAX_FORMATTED_CHARS,
    NUMBER,
    OTHER,
    STRING,
    Token,
    plain,
    tokenize,
    tokens_html,
)

PYTHON = "python"
JAVASCRIPT = "javascript"
JSON = "json"
SQL = "sql"

LEXERS: dict[str, Any] = {
    PYTHON: PythonLexer(),
    JAVASCRIPT: JavascriptLexer(),
    JSON: JsonLexer(),
}
SUFFIXES = {".py": PYTHON, ".js": JAVASCRIPT}

# The most specific type listed here names a token. Any other token, such as a
# plain name or an operator, stays uncolored.
TOKEN_KINDS = {
    Comment: COMMENT,
    String: STRING,
    Number: NUMBER,
    Keyword: KEYWORD,
    Operator.Word: KEYWORD,
    Name.Function: FUNCTION,
    Name.Class: FUNCTION,
    Name.Decorator: FUNCTION,
    Name.Exception: FUNCTION,
    Name.Builtin: FUNCTION,
    Name.Builtin.Pseudo: OTHER,
    # The JSON lexer's name for a key.
    Name.Tag: IDENTIFIER,
}


def language_of(file_name: str) -> str | None:
    """The language a file's extension states, if it states one."""
    return SUFFIXES.get(PurePath(file_name).suffix)


def _kind(token_type: Any) -> str:
    while token_type is not None:
        if token_type in TOKEN_KINDS:
            return TOKEN_KINDS[token_type]
        token_type = token_type.parent
    return OTHER


def _lexed(lexer: Any, text: str) -> list[Token]:
    """The lexer's tokens, with neighbors of one kind joined.

    The lexer's unprocessed form is used because its usual form changes line
    endings and removes a byte order mark.
    """
    runs = groupby(
        lexer.get_tokens_unprocessed(text), key=lambda token: _kind(token[1])
    )
    return [
        Token(kind, "".join(token[2] for token in run)) for kind, run in runs
    ]


def code_tokens(text: str, language: str | None) -> list[Token]:
    """Every character of `text`, in order, in exactly one token."""
    if language == SQL:
        return tokenize(text)
    lexer = LEXERS.get(language or "")
    if lexer is None or len(text) > MAX_FORMATTED_CHARS:
        return plain(text)
    try:
        if language == JSON:
            json.loads(text)
        tokens = _lexed(lexer, text)
    except Exception:
        return plain(text)
    if "".join(token.text for token in tokens) != text:
        return plain(text)
    return tokens


def render_code(text: str | None, language: str | None = None) -> Markup:
    """The text as escaped HTML, for inside a `pre` element."""
    return tokens_html(code_tokens(text or "", language))
