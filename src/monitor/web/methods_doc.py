"""The Methods document, rendered from `docs/methods.md`.

The Markdown stays the single source. Rendering gives each heading an anchor
for the page's navigation, and inlines each diagram so it follows the theme
and projector tokens. Raw HTML in the Markdown is still escaped.

Math is TeX between `$` or `$$`, which GitHub also renders. It is taken out
before Markdown runs, because `_` and `*` in math would otherwise become
emphasis, and put back afterward for KaTeX to draw in the browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"
DIAGRAMS_DIR = DOCS_DIR / "diagrams"
DIAGRAM_SRC_RE = re.compile(r"^diagrams/([a-z0-9-]+)\.svg$")
VALUES_HEADING = "## Values in this document"
VALUE_ROW_RE = re.compile(
    r"^\| [^|]+ \| `(?P<path>[A-Za-z0-9_.-]+)` \| (?P<stated>[^|]+?) \|$",
    re.M,
)


# Code is matched first, so math inside it stays code. An inline `$` must touch
# its text and must not close before a digit, so a price is not math.
MATH_RE = re.compile(
    r"(?P<fence>^ {0,3}```.*?^ {0,3}```[ \t]*$)"
    r"|(?P<code>`[^`\n]*`)"
    r"|\$\$(?P<display>.+?)\$\$"
    r"|(?<![\\$\w])\$(?P<inline>[^\s$](?:[^$\n]*[^\s$])?)\$(?![\d$])",
    re.M | re.S,
)
MATH_HOLD = "MATHHOLD{}END"
HELD_DISPLAY_RE = re.compile(r"<p>(MATHHOLD\d+END)</p>")
HELD_RE = re.compile(r"MATHHOLD\d+END")
HEADING_RE = re.compile(r"^(#{1,6}) (.+)$")


@dataclass
class Heading:
    level: int
    text: str
    anchor: str


@dataclass
class Document:
    html: Markup
    headings: list[Heading] = field(default_factory=list)
    has_math: bool = False


def diagram(name: str) -> Markup:
    """One hand-written SVG, or nothing if the file is missing."""
    if not re.fullmatch(r"[a-z0-9-]+", name):
        return Markup("")
    try:
        text = (DIAGRAMS_DIR / f"{name}.svg").read_text(encoding="utf-8")
    except OSError:
        return Markup("")
    return Markup(f'<span class="doc-diagram">{text}</span>')


def slug(text: str, taken: set[str]) -> str:
    """A stable anchor. A repeated heading gets a number."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
    anchor = base
    count = 1
    while anchor in taken:
        count += 1
        anchor = f"{base}-{count}"
    taken.add(anchor)
    return anchor


def _image_rule(
    self: Any, tokens: list[Any], index: int, options: Any, env: Any
) -> str:
    token = tokens[index]
    match = DIAGRAM_SRC_RE.match(token.attrGet("src") or "")
    if match:
        inlined = diagram(match.group(1))
        if inlined:
            return str(inlined)
    return str(self.renderToken(tokens, index, options, env))


def hold_math(text: str) -> tuple[str, dict[str, str]]:
    """`text` with each formula replaced by a token Markdown leaves alone."""
    held: dict[str, str] = {}

    def hold(match: re.Match[str]) -> str:
        if match.group("fence") or match.group("code"):
            return match.group(0)
        token = MATH_HOLD.format(len(held))
        if match.group("display") is not None:
            tex = escape(match.group("display").strip())
            held[token] = f'<div class="math-display">\\[{tex}\\]</div>'
        else:
            tex = escape(match.group("inline"))
            held[token] = f'<span class="math-inline">\\({tex}\\)</span>'
        return token

    return MATH_RE.sub(hold, text), held


def restore_math(html: str, held: dict[str, str]) -> str:
    """Each formula back in the HTML, where KaTeX finds its delimiters."""
    if not held:
        return html
    html = HELD_DISPLAY_RE.sub(lambda match: held[match.group(1)], html)
    return HELD_RE.sub(lambda match: held[match.group(0)], html)


def render(text: str) -> Document:
    """The document's HTML and its headings, in order."""
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    markdown.add_render_rule("image", _image_rule)
    source, held = hold_math(text)
    tokens = markdown.parse(source)
    headings: list[Heading] = []
    taken: set[str] = set()
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        title = tokens[index + 1].content
        anchor = slug(title, taken)
        token.attrSet("id", anchor)
        headings.append(Heading(int(token.tag[1]), title, anchor))
    html = markdown.renderer.render(tokens, markdown.options, {})
    return Document(
        Markup(restore_math(html, held)), headings, has_math=bool(held)
    )


def section_text(text: str, anchor: str) -> str:
    """The Markdown of one section, up to the next heading of its level."""
    lines = text.splitlines(keepends=True)
    taken: set[str] = set()
    fenced = False
    start: int | None = None
    level = 0
    for number, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = HEADING_RE.match(line)
        if fenced or not match:
            continue
        depth = len(match.group(1))
        if start is not None and depth <= level:
            return "".join(lines[start:number])
        found = slug(match.group(2).strip(), taken)
        if start is None and found == anchor:
            start, level = number, depth
    return "".join(lines[start:]) if start is not None else ""


def sections_html(text: str, anchors: list[str]) -> Document:
    """The named sections of the document, rendered together."""
    parts = [section_text(text, anchor) for anchor in anchors]
    return render("\n".join(part for part in parts if part))


def stated_values(text: str) -> list[tuple[str, str]]:
    """Each `(where it lives, stated value)` row in the values table."""
    _, _, table = text.partition(VALUES_HEADING)
    return [
        (match.group("path"), match.group("stated").strip())
        for match in VALUE_ROW_RE.finditer(table)
    ]


def value_text(value: Any) -> str:
    """A value in the form the document states it."""
    if isinstance(value, list | tuple):
        return ", ".join(str(part) for part in value)
    return str(value)


def setting_at(settings: Any, path: str) -> Any:
    """The value at a dotted settings path."""
    value = settings
    for part in path.split("."):
        value = getattr(value, part)
    return value


def drifted(text: str, settings: Any) -> list[str]:
    """Settings in service that differ from what the document states.

    Constants are checked by a test instead, because importing every parser
    here would load models the web process does not use.
    """
    lines = []
    for path, stated in stated_values(text):
        if path.startswith("monitor."):
            continue
        actual = value_text(setting_at(settings, path))
        if actual != stated:
            lines.append(f"{path} is {actual}, not {stated}")
    return lines
