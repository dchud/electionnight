"""The Methods document, rendered from `docs/methods.md`.

The Markdown stays the single source. Rendering gives each heading an anchor
for the page's navigation, and inlines each diagram so it follows the theme
and projector tokens. Raw HTML in the Markdown is still escaped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markupsafe import Markup

DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"
DIAGRAMS_DIR = DOCS_DIR / "diagrams"
DIAGRAM_SRC_RE = re.compile(r"^diagrams/([a-z0-9-]+)\.svg$")
VALUES_HEADING = "## Values in this document"
VALUE_ROW_RE = re.compile(
    r"^\| [^|]+ \| `(?P<path>[A-Za-z0-9_.-]+)` \| (?P<stated>[^|]+?) \|$",
    re.M,
)


@dataclass
class Heading:
    level: int
    text: str
    anchor: str


@dataclass
class Document:
    html: Markup
    headings: list[Heading] = field(default_factory=list)


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


def render(text: str) -> Document:
    """The document's HTML and its headings, in order."""
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    markdown.add_render_rule("image", _image_rule)
    tokens = markdown.parse(text)
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
    return Document(Markup(html), headings)


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
