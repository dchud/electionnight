"""SQL as the teaching views show it: colored, and broken into lines if long.

DuckDB's own tokenizer decides where a string or a comment starts and ends, so
this reads a statement the way the engine that ran it does. It needs no
connection. This runs on the server, so no script is needed to read a query.

It changes only whitespace between tokens, and only in a statement that is one
long line, so the text on the page stays the statement that ran. Everything is
escaped, because the SQL console echoes what a person typed.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
from markupsafe import Markup, escape

# A statement of one line at most this long stays on one line.
LONG_LINE_CHARS = 72
INDENT = "  "
# Longer text is shown escaped and uncolored, so its cost stays small.
MAX_FORMATTED_CHARS = 50_000

COMMENT = "comment"
STRING = "string"
NUMBER = "number"
KEYWORD = "keyword"
FUNCTION = "function"
IDENTIFIER = "identifier"
PARAM = "param"
SPACE = "space"
OTHER = "other"
WORD = "word"

# One set of colors, for every language the views color.
CSS_CLASSES = {
    COMMENT: "code-comment",
    STRING: "code-string",
    NUMBER: "code-number",
    KEYWORD: "code-keyword",
    FUNCTION: "code-function",
    IDENTIFIER: "code-identifier",
    PARAM: "code-param",
}

TOKEN_KINDS = {
    duckdb.token_type.comment: COMMENT,
    duckdb.token_type.string_const: STRING,
    duckdb.token_type.numeric_const: NUMBER,
    duckdb.token_type.operator: OTHER,
    duckdb.token_type.keyword: WORD,
    duckdb.token_type.identifier: WORD,
}

# Clause and operator words. A word that can name a column, such as `version`,
# `text`, or `hour`, is not here, so it stays an identifier.
KEYWORDS = frozenset(
    """
    ALL AND ANALYZE ANTI ANY AS ASC ASOF AT BETWEEN BY CASE CAST CROSS DESC
    DISTINCT ELSE END EXCEPT EXCLUDE EXISTS EXPLAIN FALSE FILTER FOLLOWING FROM
    FULL GROUP HAVING ILIKE IN INNER INTERSECT INTERVAL IS JOIN LATERAL LEFT
    LIKE LIMIT MATERIALIZED NATURAL NOT NULL NULLS OFFSET ON OR ORDER OUTER
    OVER PARTITION PIVOT POSITIONAL PRECEDING QUALIFY RECURSIVE REPLACE RIGHT
    SELECT SEMI SOME THEN TRUE UNBOUNDED UNION UNPIVOT USING VALUES WHEN WHERE
    WINDOW WITH WITHIN
    """.split()
)
# These can name a column, so each is a keyword only beside its partner.
KEYWORD_AFTER = {
    "FIRST": "NULLS",
    "LAST": "NULLS",
    "ROW": "CURRENT",
    "TIME": "AT",
    "ZONE": "TIME",
}
KEYWORD_BEFORE = {"CURRENT": "ROW", "RANGE": "BETWEEN", "ROWS": "BETWEEN"}
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
    "ANTI": {"JOIN"},
    "SEMI": {"JOIN"},
    "NATURAL": {"JOIN", "LEFT", "RIGHT", "FULL", "INNER"},
    "POSITIONAL": {"JOIN"},
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
        "NATURAL",
        "POSITIONAL",
    }
)
SET_OPERATIONS = frozenset({"EXCEPT", "INTERSECT", "UNION"})


@dataclass(frozen=True)
class Token:
    kind: str
    text: str


def _scanned(sql: str) -> list[Token]:
    """DuckDB's tokens, with the whitespace after each as its own token."""
    encoded = sql.encode("utf-8")
    marks = duckdb.tokenize(sql)
    # DuckDB reports each token's start as a byte offset.
    starts = [start for start, _ in marks] + [len(encoded)]
    tokens = []
    if starts[0]:
        tokens.append(Token(SPACE, encoded[: starts[0]].decode("utf-8")))
    for (start, token_type), end in zip(marks, starts[1:], strict=True):
        text = encoded[start:end].decode("utf-8")
        body = text.rstrip()
        if body == ".":
            tokens.append(Token(OTHER, body))
        elif body:
            tokens.append(Token(TOKEN_KINDS[token_type], body))
        if len(body) < len(text):
            tokens.append(Token(SPACE, text[len(body) :]))
    return tokens


def _word_kind(word: str, before: str, after: str, attached: bool) -> str:
    """What a word is, given the code on each side of it.

    `attached` says that nothing separates the word from what follows it.
    """
    called = after == "("
    if '"' in word:
        return IDENTIFIER
    if "." in word:
        return FUNCTION if called else IDENTIFIER
    upper = word.upper()
    if upper in KEYWORDS:
        if called and attached and upper not in NOT_FUNCTIONS:
            return FUNCTION
        return KEYWORD
    if called:
        return FUNCTION
    if before == KEYWORD_AFTER.get(upper) or after == KEYWORD_BEFORE.get(
        upper
    ):
        return KEYWORD
    return IDENTIFIER


def _is_name_part(token: Token) -> bool:
    return token.kind == WORD or token.text == "*"


def _classified(scanned: list[Token]) -> list[Token]:
    """Name each word, joining `a.b.c`, `t.*`, `$name`, and `$1` into one."""
    tokens: list[Token] = []
    index = 0
    before = ""
    while index < len(scanned):
        token = scanned[index]
        index += 1
        if token.text == "$" and index < len(scanned):
            following = scanned[index]
            if following.kind in {WORD, NUMBER}:
                tokens.append(Token(PARAM, token.text + following.text))
                index += 1
                before = ""
                continue
        if token.kind != WORD:
            tokens.append(token)
            if token.kind not in {SPACE, COMMENT}:
                before = ""
            continue
        word = token.text
        while (
            index + 1 < len(scanned)
            and scanned[index].text == "."
            and _is_name_part(scanned[index + 1])
        ):
            word += "." + scanned[index + 1].text
            index += 2
        attached = index >= len(scanned) or scanned[index].kind != SPACE
        ahead = index if attached else index + 1
        after = scanned[ahead].text.upper() if ahead < len(scanned) else ""
        kind = _word_kind(word, before, after, attached)
        tokens.append(Token(kind, word))
        before = word.upper()
    return tokens


def plain(text: str) -> list[Token]:
    """Text as one uncolored token."""
    return [Token(OTHER, text)] if text else []


def tokenize(sql: str) -> list[Token]:
    """Every character of `sql`, in order, in exactly one token.

    Text that is very long, or that the tokenizer cannot read, is one uncolored
    token, because showing a statement matters more than coloring it.
    """
    if len(sql) > MAX_FORMATTED_CHARS:
        return plain(sql)
    try:
        tokens = _classified(_scanned(sql))
    except Exception:
        return plain(sql)
    if "".join(token.text for token in tokens) != sql:
        return plain(sql)
    return tokens


def _neighbors(tokens: list[Token]) -> tuple[list[str], list[str]]:
    """Each token's nearest code before and after it, in capitals.

    Spaces and comments do not count.
    """
    before = [""] * len(tokens)
    after = [""] * len(tokens)
    last = ""
    for index, token in enumerate(tokens):
        before[index] = last
        if token.kind not in {SPACE, COMMENT}:
            last = token.text.upper()
    last = ""
    for index in range(len(tokens) - 1, -1, -1):
        after[index] = last
        if tokens[index].kind not in {SPACE, COMMENT}:
            last = tokens[index].text.upper()
    return before, after


def _opens_clause(token: Token, before: str, after: str) -> bool:
    upper = token.text.upper()
    if token.kind != KEYWORD or upper not in CLAUSE_STARTS:
        return False
    if before in JOIN_WORDS:
        return False
    # `IS DISTINCT FROM` compares two values.
    if upper == "FROM" and before == "DISTINCT":
        return False
    # `SELECT * EXCEPT (a)` names columns to leave out.
    if upper in SET_OPERATIONS and before.endswith("*"):
        return False
    following = CLAUSE_STARTS[upper]
    return following is None or after in following


def break_clauses(tokens: list[Token]) -> list[Token]:
    """Put each major clause on its own line, using the space before it.

    Inside parentheses this applies only to a subquery, so the `ORDER BY` of a
    window and the `FROM` of `EXTRACT(year FROM d)` stay on their line. No
    space is added where none was.
    """
    before, after = _neighbors(tokens)
    result: list[Token] = []
    stack: list[bool] = []
    not_subqueries = 0
    for index, token in enumerate(tokens):
        if token.kind == OTHER and token.text == "(":
            subquery = after[index] in {"SELECT", "WITH"}
            stack.append(subquery)
            not_subqueries += not subquery
        elif token.kind == OTHER and token.text == ")" and stack:
            not_subqueries -= not stack.pop()
        following = index + 1
        if (
            token.kind == SPACE
            and not not_subqueries
            and following < len(tokens)
            and _opens_clause(
                tokens[following], before[following], after[following]
            )
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


def tokens_html(tokens: list[Token]) -> Markup:
    """Tokens as escaped HTML, each kind in the span of its color."""
    parts = []
    for token in tokens:
        css = CSS_CLASSES.get(token.kind)
        text = escape(token.text)
        parts.append(
            f'<span class="{css}">{text}</span>' if css else str(text)
        )
    return Markup("".join(parts))


def render_sql(sql: str | None) -> Markup:
    """The statement as escaped HTML, for inside a `pre` element."""
    return tokens_html(laid_out(sql or ""))
