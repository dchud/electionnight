"""MediaWiki API access and a small parser for rendered HTML tables.

Page text is treated as data. Nothing read from a page changes what this
code requests, apart from the article titles it looks up for Wikidata ids.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx

from monitor.logs import get

log = get(__name__)

API_URL = "https://en.wikipedia.org/w/api.php"
ARTICLE_URL = "https://en.wikipedia.org/wiki/"
# Wikimedia answers 403 to a User-Agent with no contact, so this one names the
# project's repository.
USER_AGENT = (
    "ElectionNightMonitor/1.0 (https://github.com/dchud/electionnight; "
    "reference data build for a personal election news monitor) python-httpx"
)
MIN_INTERVAL_SECONDS = 1.0
MAX_ATTEMPTS = 5
TITLES_PER_QUERY = 50

SKIP_TAGS = {"style", "script"}
VOID_TAGS = {"br", "hr", "img", "link", "meta", "input", "wbr"}
BLOCK_TAGS = {"br", "p", "div", "li", "ul", "ol", "dd", "dt"}


def article_url(title: str, anchor: str | None = None) -> str:
    url = ARTICLE_URL + title.replace(" ", "_")
    return f"{url}#{anchor}" if anchor else url


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


@dataclass
class Link:
    title: str
    label: str = ""


@dataclass
class Item:
    """A list entry inside a table cell."""

    text: str
    links: list[Link] = field(default_factory=list)


@dataclass
class Cell:
    text: str = ""
    links: list[Link] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    header: bool = False


@dataclass
class Table:
    heading: str
    rows: list[list[Cell]] = field(default_factory=list)


@dataclass
class _OpenCell:
    header: bool
    rowspan: int
    colspan: int
    text: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    item_text: list[str] | None = None
    item_links: list[Link] = field(default_factory=list)


@dataclass
class _OpenTable:
    heading: str
    raw: list[list[_OpenCell]] = field(default_factory=list)
    row: list[_OpenCell] | None = None
    cell: _OpenCell | None = None


def _span(attrs: dict[str, str | None], name: str) -> int:
    match = re.match(r"\d+", attrs.get(name) or "")
    return max(int(match.group()), 1) if match else 1


def _is_hidden(attrs: dict[str, str | None]) -> bool:
    style = (attrs.get("style") or "").replace(" ", "").lower()
    if "display:none" in style:
        return True
    classes = (attrs.get("class") or "").split()
    return "reference" in classes or "mw-empty-elt" in classes


def _to_grid(table: _OpenTable) -> Table:
    """Expand rowspan and colspan so every row has aligned columns."""
    grid: list[list[Cell]] = []
    carry: dict[int, tuple[int, Cell]] = {}
    for raw_row in table.raw:
        row: list[Cell] = []
        queue = list(raw_row)
        column = 0
        while queue or column in carry:
            if column in carry:
                remaining, cell = carry[column]
                row.append(cell)
                if remaining <= 1:
                    del carry[column]
                else:
                    carry[column] = (remaining - 1, cell)
                column += 1
                continue
            opened = queue.pop(0)
            cell = Cell(
                text=clean_text(" ".join(opened.text)),
                links=opened.links,
                items=opened.items,
                header=opened.header,
            )
            for _ in range(opened.colspan):
                row.append(cell)
                if opened.rowspan > 1:
                    carry[column] = (opened.rowspan - 1, cell)
                column += 1
        grid.append(row)
    return Table(heading=table.heading, rows=grid)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self.stack: list[_OpenTable] = []
        self.heading = ""
        self.heading_text: list[str] | None = None
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.link: Link | None = None
        self.link_text: list[str] = []

    @property
    def _cell(self) -> _OpenCell | None:
        return self.stack[-1].cell if self.stack else None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if self.skip_tag is not None:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in VOID_TAGS:
            if tag == "br":
                self._write(" ")
            return
        if tag in SKIP_TAGS or _is_hidden(values):
            self.skip_tag = tag
            self.skip_depth = 1
            return
        if tag in {"h1", "h2", "h3", "h4"}:
            self.heading_text = []
        elif tag == "table":
            self.stack.append(_OpenTable(heading=self.heading))
        elif not self.stack:
            return
        elif tag == "tr":
            self._close_row()
            self.stack[-1].row = []
        elif tag in {"td", "th"}:
            self._close_cell()
            top = self.stack[-1]
            if top.row is None:
                top.row = []
            top.cell = _OpenCell(
                header=tag == "th",
                rowspan=_span(values, "rowspan"),
                colspan=_span(values, "colspan"),
            )
            top.row.append(top.cell)
        elif tag == "li" and self._cell is not None:
            self._close_item()
            self._cell.item_text = []
            self._cell.item_links = []
        elif tag == "a" and self._cell is not None:
            href = values.get("href") or ""
            title = values.get("title") or ""
            if href.startswith("/wiki/") and title:
                self.link = Link(title=title)
                self.link_text = []
        elif tag in BLOCK_TAGS:
            self._write(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_tag is not None:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in {"h1", "h2", "h3", "h4"} and self.heading_text is not None:
            self.heading = clean_text(" ".join(self.heading_text))
            self.heading_text = None
        elif tag == "table" and self.stack:
            self._close_row()
            finished = self.stack.pop()
            self.tables.append(_to_grid(finished))
        elif not self.stack:
            return
        elif tag == "tr":
            self._close_row()
        elif tag in {"td", "th"}:
            self._close_cell()
        elif tag == "li":
            self._close_item()
        elif tag == "a" and self.link is not None:
            self.link.label = clean_text(" ".join(self.link_text))
            cell = self._cell
            if cell is not None:
                cell.links.append(self.link)
                if cell.item_text is not None:
                    cell.item_links.append(self.link)
            self.link = None
        elif tag in BLOCK_TAGS:
            self._write(" ")

    def handle_data(self, data: str) -> None:
        if self.skip_tag is not None:
            return
        if self.heading_text is not None:
            self.heading_text.append(data)
            return
        self._write(data)

    def _write(self, data: str) -> None:
        cell = self._cell
        if cell is None:
            return
        cell.text.append(data)
        if cell.item_text is not None:
            cell.item_text.append(data)
        if self.link is not None:
            self.link_text.append(data)

    def _close_item(self) -> None:
        cell = self._cell
        if cell is None or cell.item_text is None:
            return
        text = clean_text("".join(cell.item_text))
        if text:
            cell.items.append(Item(text=text, links=cell.item_links))
        cell.item_text = None
        cell.item_links = []

    def _close_cell(self) -> None:
        if not self.stack:
            return
        self._close_item()
        self.stack[-1].cell = None

    def _close_row(self) -> None:
        if not self.stack:
            return
        self._close_cell()
        top = self.stack[-1]
        if top.row is not None:
            top.raw.append(top.row)
        top.row = None


def parse_tables(html: str) -> list[Table]:
    """Return every table in the HTML, inner tables before outer ones."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return parser.tables


class WikiClient:
    """Sequential MediaWiki API client at no more than one request a second."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
        )
        self._last_request = 0.0

    def _pause(self) -> None:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, **params: Any) -> dict[str, Any]:
        query = {
            "format": "json",
            "formatversion": 2,
            "maxlag": 5,
            **params,
        }
        for attempt in range(MAX_ATTEMPTS):
            self._pause()
            response = self.client.get(API_URL, params=query)
            if response.status_code in {429, 500, 502, 503, 504}:
                delay = float(response.headers.get("retry-after") or 5)
                log.warning(
                    "wiki_retry",
                    status=response.status_code,
                    attempt=attempt,
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            error = payload.get("error")
            if error and error.get("code") == "maxlag":
                time.sleep(float(response.headers.get("retry-after") or 5))
                continue
            if error:
                raise RuntimeError(f"wikipedia: {error.get('code')}")
            return payload
        raise RuntimeError("wikipedia: retries exhausted")

    def page(self, title: str) -> tuple[str, int]:
        """Return the rendered HTML and revision id of a page."""
        payload = self.get(
            action="parse",
            page=title,
            prop="text|revid",
            redirects=1,
            disableeditsection=1,
            disablelimitreport=1,
        )
        parse = payload["parse"]
        return parse["text"], int(parse["revid"])

    def wikidata_ids(self, titles: Iterable[str]) -> dict[str, str]:
        """Map article titles to Wikidata ids, following redirects."""
        unique = sorted({title for title in titles if title})
        found: dict[str, str] = {}
        for start in range(0, len(unique), TITLES_PER_QUERY):
            chunk = unique[start : start + TITLES_PER_QUERY]
            payload = self.get(
                action="query",
                prop="pageprops",
                ppprop="wikibase_item",
                titles="|".join(chunk),
                redirects=1,
            )
            query = payload.get("query", {})
            aliases = {title: title for title in chunk}
            for step in query.get("normalized", []) + query.get(
                "redirects", []
            ):
                for original, current in list(aliases.items()):
                    if current == step["from"]:
                        # A redirect into a section is about a topic, not a
                        # person with an article of their own.
                        aliases[original] = (
                            "" if step.get("tofragment") else step["to"]
                        )
            by_title = {
                page.get("title"): page.get("pageprops", {}).get(
                    "wikibase_item"
                )
                for page in query.get("pages", [])
            }
            for original, resolved in aliases.items():
                qid = by_title.get(resolved)
                if qid:
                    found[original] = qid
        return found
