"""SQL as the teaching views show it: colored, and broken into lines if long.

This runs on the server, so no script is needed to read a query. It changes
only whitespace, and only in a statement that is one long line, so the text on
the page stays the statement that ran. Everything is escaped, because the SQL
console echoes what a person typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markupsafe import Markup, escape

# A statement of one line at most this long stays on one line.
LONG_LINE_CHARS = 72
INDENT = "  "

COMMENT = "comment"
STRING = "string"
NUMBER = "number"
KEYWORD = "keyword"
FUNCTION = "function"
IDENTIFIER = "identifier"
NAME = "name"
PARAM = "param"
SPACE = "space"
OTHER = "other"

CSS_CLASSES = {
    COMMENT: "sql-comment",
    STRING: "sql-string",
    NUMBER: "sql-number",
    KEYWORD: "sql-keyword",
    FUNCTION: "sql-function",
    IDENTIFIER: "sql-identifier",
    PARAM: "sql-param",
}

KEYWORDS = frozenset(
    """
    ALL AND ANY ANTI AS ASC ASOF AT BETWEEN BY CASE CAST COLLATE CROSS CUBE
    CURRENT DESC DISTINCT ELSE END ESCAPE EXCEPT EXCLUDE EXISTS EXPLAIN FALSE
    FILTER FOLLOWING FOR FROM FULL GROUP GROUPING HAVING ILIKE IN INNER
    INTERSECT INTERVAL IS JOIN LATERAL LEFT LIKE LIMIT MATERIALIZED NATURAL
    NOT NULL NULLS OFFSET ON OR ORDER OUTER OVER PARTITION PIVOT PRECEDING
    QUALIFY RANGE RECURSIVE REPLACE RIGHT ROLLUP ROW ROWS SELECT SEMI SET
    SOME THEN TO TRUE UNBOUNDED UNION UNNEST UNPIVOT USING VALUES VERSION WHEN
    WHERE WINDOW WITH WITHIN
    BIGINT BLOB BOOLEAN DATE DOUBLE FLOAT HOUR HUGEINT INT INTEGER JSON MINUTE
    SECOND TEXT TIME TIMESTAMP TIMESTAMPTZ VARCHAR
    """.split()
)
# These take a parenthesis without being functions.
NOT_FUNCTIONS = frozenset(
    """
    ALL AND ANY AS AT BETWEEN BY ELSE EXCEPT EXCLUDE EXISTS FILTER FROM HAVING
    IN INTERSECT JOIN LATERAL LIMIT MATERIALIZED NOT ON OR OVER QUALIFY
    RECURSIVE SELECT SOME THEN UNION USING VALUES WHEN WHERE WITH WITHIN
    """.split()
)
# Words that start a line in a long statement. Each needs the next word listed
# with it, if any: `ORDER BY` starts a clause, and a column named `order` does
# not.
CLAUSE_STARTS = {
    "FROM": None,
    "WHERE": None,
    "HAVING": None,
    "QUALIFY": None,
    "LIMIT": None,
    "OFFSET": None,
    "JOIN": None,
    "GROUP": {"BY"},
    "ORDER": {"BY"},
    "UNION": None,
    "EXCEPT": None,
    "INTERSECT": None,
    "LEFT": {"JOIN", "OUTER", "ANTI", "SEMI"},
    "RIGHT": {"JOIN", "OUTER"},
    "FULL": {"JOIN", "OUTER"},
    "INNER": {"JOIN"},
    "CROSS": {"JOIN"},
    "ASOF": {"JOIN", "LEFT"},
}
# A join's first word starts the line, and the words after it stay with it.
JOIN_WORDS = frozenset(
    {
        "LEFT",
        "RIGHT",
        "FULL",
        "INNER",
        "CROSS",
        "OUTER",
        "ASOF",
        "ANTI",
        "SEMI",
    }
)

_QUOTED = r'"(?:[^"]|"")*"?'
_WORD = r"[A-Za-z_][\w$]*"
_PART = rf"(?:{_QUOTED}|{_WORD})"
TOKEN_RE = re.compile(
    rf"""
    (?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
    |(?P<string>'(?:[^']|'')*'?|\$\$.*?(?:\$\$|\Z))
    |(?P<param>\$[A-Za-z_]\w*|\$\d+)
    |(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
    |(?P<word>{_PART}(?:\.(?:{_PART}|\*))*)
    |(?P<space>\s+)
    |(?P<other>->>|->|::|<=|>=|<>|!=|\|\||=>|:=|.)
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str


def _word_kind(word: str, following: str) -> str:
    """What a word is, given the text right after it."""
    called = following.lstrip().startswith("(")
    if '"' in word:
        return IDENTIFIER
    if "." in word:
        return FUNCTION if called else IDENTIFIER
    upper = word.upper()
    if upper in KEYWORDS:
        attached = following.startswith("(")
        if attached and upper not in NOT_FUNCTIONS:
            return FUNCTION
        return KEYWORD
    return FUNCTION if called else NAME


def tokenize(sql: str) -> list[Token]:
    """Every character of `sql`, in order, in exactly one token."""
    tokens = []
    for match in TOKEN_RE.finditer(sql):
        kind = match.lastgroup or OTHER
        text = match.group()
        if kind == "word":
            kind = _word_kind(text, sql[match.end() : match.end() + 40])
        tokens.append(Token(kind, text))
    return tokens


def _next_word(tokens: list[Token], index: int) -> str:
    for token in tokens[index + 1 :]:
        if token.kind != SPACE:
            return token.text.upper()
    return ""


def _previous_word(tokens: list[Token], index: int) -> str:
    for token in reversed(tokens[:index]):
        if token.kind != SPACE:
            return token.text.upper()
    return ""


def _opens_clause(tokens: list[Token], index: int) -> bool:
    token = tokens[index]
    upper = token.text.upper()
    if token.kind != KEYWORD or upper not in CLAUSE_STARTS:
        return False
    if _previous_word(tokens, index) in JOIN_WORDS:
        return False
    following = CLAUSE_STARTS[upper]
    return following is None or _next_word(tokens, index) in following


def break_clauses(tokens: list[Token]) -> list[Token]:
    """Put each major clause on its own line, using the space before it.

    Inside parentheses this applies only to a subquery, so the `ORDER BY` of a
    window stays on its line. No space is added where none was.
    """
    result: list[Token] = []
    stack: list[bool] = []
    for index, token in enumerate(tokens):
        if token.text == "(" and token.kind == OTHER:
            stack.append(_next_word(tokens, index) in {"SELECT", "WITH"})
        elif token.text == ")" and token.kind == OTHER and stack:
            stack.pop()
        if (
            token.kind == SPACE
            and all(stack)
            and index + 1 < len(tokens)
            and _opens_clause(tokens, index + 1)
        ):
            result.append(Token(SPACE, "\n" + INDENT * len(stack)))
        else:
            result.append(token)
    return result


def _is_one_long_line(sql: str) -> bool:
    return "\n" not in sql.strip() and len(sql.strip()) > LONG_LINE_CHARS


def laid_out(sql: str) -> list[Token]:
    """The tokens to show. A long single line gets breaks before clauses."""
    tokens = tokenize(sql)
    return break_clauses(tokens) if _is_one_long_line(sql) else tokens


def render_sql(sql: str | None) -> Markup:
    """The statement as escaped HTML, for inside a `pre` element."""
    parts = []
    for token in laid_out(sql or ""):
        css = CSS_CLASSES.get(token.kind)
        text = escape(token.text)
        parts.append(
            f'<span class="{css}">{text}</span>' if css else str(text)
        )
    return Markup("".join(parts))
