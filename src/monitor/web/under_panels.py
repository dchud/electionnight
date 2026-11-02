"""What is behind each dashboard card: the SQL that ran, and what is not SQL.

The SQL is not a copy. A card's real `Panels` methods run on a small proxy of
the read model, which records each statement, its parameters, and its row
count. The code shown is read from the running source, by name. Only the named
functions and values are read.
"""

from __future__ import annotations

import importlib
import inspect
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from markupsafe import Markup

import monitor
from monitor.settings import Settings
from monitor.web.filters import Filters
from monitor.web.methods_doc import Document, sections_html
from monitor.web.panels import Panels
from monitor.web.readmodel import (
    CHANGED_IDS,
    COPIED_TABLES,
    FACTS_SELECT,
    INCREMENTAL_TABLES,
    MERGE_HOPS,
    ReadModel,
    _freeze,
)
from monitor.web.reference import METHODS_PATH
from monitor.web.sql_format import IDENTIFIER, PARAM, tokenize
from monitor.web.under import CONSOLE_MAX_ROWS, Under
from monitor.web.under_worker import CONSOLE_MAX_CELL_CHARS

SQL_LABEL = "SQL"
CODE_LABEL = "Code"
SHOWN_ROWS = 3
SHOWN_COLUMNS = 8
SHOWN_CHARS = 80
MAX_CONSOLE_SQL = 90_000
LONG_AGO = "TIMESTAMPTZ '2000-01-01 00:00:00+00'"
LAKE_TABLE_RE = re.compile(r"\blake\.(\w+\.\w+)(\s+[a-z]\b)?")
MERGED_SQL = f"""merged_hops AS (
    SELECT storyline_id AS from_id, merged_into AS to_id, 1 AS hops
    FROM storylines
    WHERE merged_into IS NOT NULL AND merged_into <> storyline_id
    UNION ALL
    SELECT h.from_id, s.merged_into, h.hops + 1
    FROM merged_hops h JOIN storylines s ON h.to_id = s.storyline_id
    WHERE s.merged_into IS NOT NULL AND s.merged_into <> s.storyline_id
      AND h.hops < {MERGE_HOPS}
),
merged AS (
    SELECT from_id, arg_max(to_id, hops) AS to_id FROM merged_hops GROUP BY 1
)"""


@dataclass(frozen=True)
class CodeRef:
    """A function or value, named by module and attribute path."""

    module: str
    name: str


@dataclass(frozen=True)
class MethodRef:
    """A `Panels` method, and the dashboard route that calls it."""

    name: str
    route: str
    call: Callable[[Panels, Filters], Any]


@dataclass(frozen=True)
class Link:
    label: str
    href: str


@dataclass(frozen=True)
class Card:
    key: str
    title: str
    label: str
    methods: tuple[MethodRef, ...]
    not_sql: tuple[str, ...]
    code: tuple[CodeRef, ...] = ()
    values: tuple[CodeRef, ...] = ()
    links: tuple[Link, ...] = ()


ANALYTICS = "monitor.analytics"
PANELS = "monitor.web.panels"
SPECS = "monitor.web.charts.specs"
VEGA_NOTE = (
    "A Vega-Lite specification draws the chart in the browser from these "
    "rows. It decides marks, scales, and colors, and it computes no figure."
)
STREAM_NOTE = (
    "A Vega-Lite specification draws the chart in the browser from these "
    "rows. It sums n per bin and storyline there. The 100% view also computes "
    "each storyline's share of its bin there, so no SQL result holds that "
    "share."
)
TRANSLATED_NOTE = (
    "A headline in another language shows the classifier model's English "
    "translation when it has one, so that text comes from a model."
)
TRACE = Link("Trace one article", "/under/trace")
MODELS = Link("Models and their prompts", "/under/models")
GAPS = (
    "Poll-closing lines come from config/poll_closings.yaml, and offline "
    "lines from raw.collection_gaps. Python keeps those inside the window."
)

CARDS: tuple[Card, ...] = (
    Card(
        "wire",
        "Live Wire",
        SQL_LABEL,
        (
            MethodRef("wire", "/frag/wire", lambda p, f: p.wire(f)),
            MethodRef(
                "wire_recent",
                "/events",
                lambda p, f: p.wire_recent(
                    f, p.model.refresh_seq - 1, p.model.refresh_seq
                ),
            ),
        ),
        (
            "The first rows come from a query. After that, new rows are "
            "pushed over a server-sent event stream. Each push runs "
            "wire_recent, the same query with a condition on visible_seq, the "
            "refresh in which an item last became relevant.",
            "decorate_item in Python adds the lean glyph and label, and "
            "clears a link that is not http or https.",
            TRANSLATED_NOTE,
        ),
        (CodeRef(PANELS, "decorate_item"), CodeRef(PANELS, "safe_url")),
        links=(Link("Copies", "/methods#copies"), TRACE),
    ),
    Card(
        "rising",
        "Rising Storylines",
        SQL_LABEL,
        (MethodRef("rising", "/frag/rising", lambda p, f: p.rising(f)),),
        (
            "The score and the rule are in the SQL. The same formula is in "
            "analytics.velocity_score below, which a test holds to the SQL.",
            "Python builds each sparkline from the bin counts, and turns "
            "counts per lean into shares with coverage_share.",
            "Each label and summary was written by the bulk model when the "
            "storyline was labeled, then checked for citations. The query "
            "only reads what was stored.",
        ),
        (
            CodeRef(ANALYTICS, "velocity_score"),
            CodeRef(ANALYTICS, "is_rising"),
            CodeRef(ANALYTICS, "is_new"),
            CodeRef(ANALYTICS, "coverage_share"),
            CodeRef("monitor.parsers.storylines", "StorylineParser._label"),
        ),
        (
            CodeRef(ANALYTICS, "SHORT_WINDOW_MINUTES"),
            CodeRef(ANALYTICS, "TRAILING_WINDOW_MINUTES"),
            CodeRef(ANALYTICS, "RISING_SCORE"),
            CodeRef(ANALYTICS, "RISING_MIN_OBSERVED"),
            CodeRef("monitor.parsers.storylines", "NAMING_SYSTEM"),
        ),
        (
            Link("Velocity and rising", "/methods#velocity-and-rising"),
            Link("Storylines", "/methods#storylines"),
            MODELS,
        ),
    ),
    Card(
        "brief",
        "Briefing",
        CODE_LABEL,
        (
            MethodRef(
                "latest_brief", "/frag/brief", lambda p, f: p.latest_brief(f)
            ),
            MethodRef(
                "brief_count", "/frag/brief", lambda p, f: p.brief_count(f)
            ),
        ),
        (
            "The brief model wrote this text. The SQL only fetches the newest "
            "stored brief and counts them.",
            "The writer sends the model items and storylines from the two "
            "queries below, then keeps a sentence only if it cites an item it "
            "was given. A sentence with no valid citation is dropped.",
            "A citation is checked to be a supplied item. It is not checked "
            "to support the claim.",
            "This page shows the newest brief, with $offset = 0. The card's "
            "Earlier button runs the same statement with a larger offset.",
        ),
        (
            CodeRef("monitor.parsers.briefs", "BriefWriter.write"),
            CodeRef("monitor.parsers.briefs", "BriefWriter._gather"),
            CodeRef("monitor.llm.citations", "validate_structured"),
        ),
        (
            CodeRef("monitor.parsers.briefs", "SYSTEM"),
            CodeRef("monitor.parsers.briefs", "ITEMS_SQL"),
            CodeRef("monitor.parsers.briefs", "STORYLINES_SQL"),
        ),
        (
            Link("Rollups and briefs", "/methods#rollups-and-briefs"),
            Link("The brief prompt", "/methods#briefs"),
            MODELS,
            TRACE,
        ),
    ),
    Card(
        "map",
        "US Map",
        SQL_LABEL,
        (
            MethodRef(
                "map_data",
                "/api/data/map",
                lambda p, f: p.map_data(f),
            ),
            MethodRef(
                "states_rising",
                "/frag/states-rising",
                lambda p, f: p.states_rising(f),
            ),
        ),
        (
            "SQL returns counts per state. Python computes the attention "
            "index and the mean tone from them, in the functions below.",
            "The browser fetches these figures as JSON and joins them to the "
            "state shapes in map.js. D3 draws the map, and no SQL draws it.",
            "On the map, Focus dims the states outside it and does not "
            "change a value. In States rising, Focus chooses which states are "
            "listed, and it does not change a listed state's figure.",
        ),
        (
            CodeRef(ANALYTICS, "attention_index"),
            CodeRef(ANALYTICS, "tone_aggregate"),
            CodeRef(ANALYTICS, "t_975"),
        ),
        (
            CodeRef(ANALYTICS, "ATTENTION_PSEUDO_COUNT"),
            CodeRef(PANELS, "BASELINE_START"),
            CodeRef(PANELS, "BASELINE_END"),
        ),
        (
            Link("State attention index", "/methods#state-attention-index"),
            Link("States rising", "/methods#states-rising"),
            Link("Tone aggregates", "/methods#tone-aggregates"),
        ),
    ),
    Card(
        "calls",
        "Race Calls in the News",
        SQL_LABEL,
        (
            MethodRef(
                "race_calls", "/frag/race-calls", lambda p, f: p.race_calls(f)
            ),
        ),
        (
            "The bulk model extracted each claim from an article and decided "
            "what its sentence means. Code then requires the evidence to "
            "appear verbatim, the article to name the candidate, the contest "
            "not to be labeled a primary, and the item to come from after the "
            "first polls closed in that state. The query also drops items "
            "from before the first close nationally. Once a final claim names "
            "a caller, or two outlets report one candidate's final claim, the "
            "race shows only its final claims, whatever filters are on.",
            "These are claims in news articles, not official results.",
        ),
        (CodeRef("monitor.parsers.race_calls", "call_problem"),),
        links=(
            Link("Race calls in the news", "/methods#race-calls-in-the-news"),
            MODELS,
        ),
    ),
    Card(
        "stream",
        "Storyline Stream",
        SQL_LABEL,
        (
            MethodRef(
                "stream_rows",
                "/api/data/stream",
                lambda p, f: p.stream_rows(f),
            ),
            MethodRef(
                "poll_rules", "/api/data/stream", lambda p, f: p.poll_rules(f)
            ),
        ),
        (STREAM_NOTE, GAPS),
        (CodeRef(SPECS, "stream"),),
        links=(Link("Volume", "/methods#volume"),),
    ),
    Card(
        "heat",
        "Topic Heat",
        SQL_LABEL,
        (
            MethodRef(
                "topic_heat",
                "/api/data/topic_heat",
                lambda p, f: p.topic_heat(f),
            ),
        ),
        (
            VEGA_NOTE,
            "Python rounds the start of the span down to a whole 15-minute "
            "bin before the query, so no bin is counted in part.",
        ),
        (CodeRef(PANELS, "whole_bin"), CodeRef(SPECS, "topic_heat")),
        (CodeRef(PANELS, "TOPIC_HEAT_SPAN"),),
    ),
    Card(
        "bump",
        "Entity Rank",
        SQL_LABEL,
        (
            MethodRef("bump", "/api/data/bump", lambda p, f: p.bump(f)),
            MethodRef(
                "poll_rules", "/api/data/bump", lambda p, f: p.poll_rules(f)
            ),
        ),
        (
            VEGA_NOTE,
            GAPS,
            "Python chooses the bin width before the query: 15 minutes, or 60 "
            "in a window over 6 hours.",
        ),
        (CodeRef(SPECS, "bump"),),
        (
            CodeRef(PANELS, "BUMP_BIN_SHORT"),
            CodeRef(PANELS, "BUMP_BIN_LONG"),
            CodeRef(PANELS, "BUMP_LONG_SPAN"),
        ),
    ),
    Card(
        "tone",
        "Tone by Outlet Lean",
        CODE_LABEL,
        (
            MethodRef(
                "tone_by_lean",
                "/api/data/tone_by_lean",
                lambda p, f: p.tone_by_lean(f),
            ),
        ),
        (
            "SQL returns only the sum of tones, the sum of their squares, and "
            "the count. Each point and interval on screen is computed in "
            "Python by tone_aggregate, which also hides a cell under 5 "
            "observations and flags one under 15.",
            "Each tone is a score the bulk model gave a mention, kept only if "
            "its evidence passed the checks in code.",
            VEGA_NOTE,
        ),
        (
            CodeRef(ANALYTICS, "tone_aggregate"),
            CodeRef(ANALYTICS, "t_975"),
            CodeRef(PANELS, "Panels.tone_by_lean"),
            CodeRef(SPECS, "tone_by_lean"),
        ),
        (
            CodeRef(ANALYTICS, "TONE_HIDE_BELOW"),
            CodeRef(ANALYTICS, "TONE_LOW_N_BELOW"),
            CodeRef(ANALYTICS, "TONE_MIN"),
            CodeRef(ANALYTICS, "TONE_MAX"),
            CodeRef(ANALYTICS, "Z_975"),
        ),
        (
            Link("Tone aggregates", "/methods#tone-aggregates"),
            Link("Tone caveats", "/methods#tone-caveats"),
        ),
    ),
    Card(
        "matrix",
        "Coverage by Lean",
        SQL_LABEL,
        (
            MethodRef(
                "coverage_matrix",
                "/api/data/coverage_matrix",
                lambda p, f: p.coverage_matrix(f),
            ),
        ),
        (
            "SQL returns counts per storyline and lean. Python divides them "
            "into shares, computes divergence, keeps the 12 largest "
            "storylines, and sorts them by divergence.",
            VEGA_NOTE,
        ),
        (
            CodeRef(ANALYTICS, "coverage_share"),
            CodeRef(ANALYTICS, "divergence"),
            CodeRef(PANELS, "Panels.coverage_matrix"),
        ),
        links=(Link("Coverage by lean", "/methods#coverage-by-lean"),),
    ),
    Card(
        "global",
        "Global Press",
        SQL_LABEL,
        (
            MethodRef(
                "global_press",
                "/api/data/global_press",
                lambda p, f: p.global_press(f),
            ),
            MethodRef(
                "latest_translated",
                "/frag/translated",
                lambda p, f: p.latest_translated(f),
            ),
        ),
        (VEGA_NOTE, TRANSLATED_NOTE),
        (CodeRef(SPECS, "global_press"),),
        links=(Link("Language detection", "/methods#language-detection"),),
    ),
    Card(
        "kpis",
        "Header figures",
        SQL_LABEL,
        (MethodRef("kpis", "/frag/kpis", lambda p, f: p.kpis(f)),),
        (
            "Feed health comes from the pipeline's status file, not from SQL, "
            "and it is not shown in replay.",
            "The next poll close comes from config/poll_closings.yaml.",
            "Python divides the recent count into a rate per minute, and "
            "draws the sparkline from the bins.",
        ),
        (
            CodeRef(PANELS, "bin_series"),
            CodeRef(PANELS, "sparkline_points"),
        ),
        (
            CodeRef(PANELS, "KPI_RECENT_MINUTES"),
            CodeRef(PANELS, "SPARK_BINS"),
        ),
    ),
)
CARDS_BY_KEY = {card.key: card for card in CARDS}
# The header row of figures has no card head to hold a link, so `/under/panels`
# lists it and it carries no link of its own.
LINKED_CARDS = tuple(card for card in CARDS if card.key != "kpis")
# Statements whose lake form gave the same rows as the panel, on a seeded lake,
# in a test that runs each one through the console.
CONSOLE_VERIFIED = frozenset(
    {
        "wire",
        "translated",
        "rising",
        "rising_bins",
        "lean_share",
        "brief_count",
        "map_window",
        "map_total",
        "attention_baseline",
        "attention_baseline_total",
        "states_rising",
        "states_rising_total",
        "stream",
        "collection_gaps",
        "topic_heat",
        "bump",
        "tone_by_lean",
        "coverage_matrix",
        "global_press",
        "kpi_bins",
        "kpi_recent",
        "kpi_active",
        "focus_races",
        "focus_states",
    }
)
# Why a statement is not offered, where the two runs can honestly differ.
NOT_OFFERED = {
    "brief": "It selects the brief's whole text, and the console cuts a cell "
    f"at {CONSOLE_MAX_CELL_CHARS:,} characters, so the rows can differ.",
    "map_top_story": "Its ranking has no tie-break, so when two storylines "
    "tie in a state, two runs can name different ones.",
    "race_calls": "It uses any_value and mode, which can choose a different "
    "value on another scan of the same rows.",
}
NEEDS_VISIBLE_SEQ = (
    "It uses visible_seq, which only the read model keeps: each refresh "
    "numbers the items that became relevant, and the lake has no such column."
)
NOT_VERIFIED = (
    "No test has compared this statement's lake form with the panel."
)
TOO_MANY_ROWS = (
    f"It returns over {CONSOLE_MAX_ROWS:,} rows, the console's cap."
)
UNKNOWN_PARAM = "A parameter could not be written as a literal."
TOO_LONG = "Its lake form is too long for the console's byte limit."


def link_targets() -> dict[str, dict[str, str]]:
    """Each card's link label and where it goes on the panel page."""
    return {
        card.key: {
            "label": card.label,
            "href": f"/under/panels/{card.key}",
            "anchor": "#code" if card.label == CODE_LABEL else "",
        }
        for card in LINKED_CARDS
    }


@dataclass
class Recorded:
    """One statement a panel method ran, as it ran."""

    name: str
    sql: str
    params: dict[str, Any]
    rows: list[Any]
    ms: int | None
    cached: bool
    runs: int = 1
    method: str = ""
    runs_by_method: dict[str, int] = field(default_factory=dict)
    console_sql: str = ""
    console_reason: str = ""
    local_tables: list[str] = field(default_factory=list)


class RecordingModel:
    """The read model, plus a record of each statement that reaches it.

    A statement missing from the cache is run with `rows`, so a capture adds
    nothing to the query times behind the dashboard's slow mode. Its result
    goes in the cache by the read model's own key, and a cached answer is said
    to be one.
    """

    def __init__(self, model: ReadModel) -> None:
        self._model = model
        self.statements: list[Recorded] = []
        self.current_method = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def _note(
        self,
        name: str,
        sql: str,
        params: dict[str, Any] | None,
        rows: list[Any],
        ms: int | None,
        cached: bool,
    ) -> None:
        self.statements.append(
            Recorded(
                name,
                sql.strip(),
                dict(params or {}),
                rows,
                ms,
                cached,
                method=self.current_method,
                runs_by_method={self.current_method: 1},
            )
        )

    def query(
        self, name: str, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        frozen = tuple(
            sorted(
                (key, _freeze(value)) for key, value in (params or {}).items()
            )
        )
        key = (self._model.snapshot.snapshot_id, name, sql, frozen)
        cached = self._model.cache.get(key)
        if cached is not None:
            self._note(name, sql, params, cached, None, True)
            return cached
        started = time.monotonic()
        rows = self._model.rows(sql, params)
        self._model.cache.put(key, rows)
        ms = int((time.monotonic() - started) * 1000)
        self._note(name, sql, params, rows, ms, False)
        return rows

    def rows(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        rows = self._model.rows(sql, params)
        ms = int((time.monotonic() - started) * 1000)
        self._note(self.current_method, sql, params, rows, ms, False)
        return rows

    def column(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        started = time.monotonic()
        values = self._model.column(sql, params)
        ms = int((time.monotonic() - started) * 1000)
        self._note(self.current_method, sql, params, values, ms, False)
        return values


def capture_card(
    settings: Settings, model: ReadModel, card: Card, filters: Filters
) -> tuple[RecordingModel, dict[str, Any], list[str]]:
    """Run a card's real methods. Returns the record, results, and failures."""
    recorder = RecordingModel(model)
    panels = Panels(settings, cast(ReadModel, recorder))
    results: dict[str, Any] = {}
    failures: list[str] = []
    for method in card.methods:
        recorder.current_method = method.name
        try:
            results[method.name] = method.call(panels, filters)
        except Exception as error:
            failures.append(f"{method.name}: {str(error)[:300]}")
    return recorder, results, failures


def literal(value: Any) -> str | None:
    """A checked SQL literal for a parameter, or None if it has no form."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, datetime):
        return f"TIMESTAMPTZ '{value.isoformat()}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, list | tuple):
        if not value:
            return "CAST([] AS VARCHAR[])"
        parts = [literal(part) for part in value]
        if any(part is None for part in parts):
            return None
        return "[" + ", ".join(cast(list[str], parts)) + "]"
    return None


def with_literals(sql: str, params: dict[str, Any]) -> str | None:
    """`sql` with each `$name` replaced. Strings and comments are untouched."""
    parts = []
    for token in tokenize(sql):
        if token.kind != PARAM:
            parts.append(token.text)
            continue
        name = token.text[1:]
        text = literal(params[name]) if name in params else None
        if text is None:
            return None
        parts.append(text)
    return "".join(parts)


def local_table_sql() -> dict[str, str]:
    """How each local table is built from the lake, with no time limit."""
    tables = dict(COPIED_TABLES)
    for name, (_, sql) in INCREMENTAL_TABLES.items():
        tables[name] = sql.replace("$since", LONG_AGO)
    tables["facts"] = FACTS_SELECT.format(
        changed="SELECT item_id FROM lake.raw.items"
    )
    return tables


def local_tables_in(sql: str) -> list[str]:
    known = local_table_sql()
    named = {
        token.text
        for token in tokenize(sql)
        if token.kind == IDENTIFIER and token.text in known
    }
    return [name for name in known if name in named]


def pinned(sql: str, snapshot: int) -> str:
    """Every lake table in `sql`, read at one snapshot."""

    def pin(match: re.Match[str]) -> str:
        alias = match.group(2) or ""
        return f"lake.{match.group(1)}{alias} AT (VERSION => {snapshot})"

    return LAKE_TABLE_RE.sub(pin, sql)


def lake_form(statement: Recorded, snapshot: int) -> str | None:
    """A statement that reads the lake and asks for what the panel asked.

    The local tables become common table expressions over the lake, at the
    snapshot the read model holds, and each parameter becomes a literal.
    """
    body = with_literals(statement.sql, statement.params)
    if body is None:
        return None
    tables = local_table_sql()
    names = local_tables_in(statement.sql)
    parts = []
    if "facts" in names:
        if "storylines" not in names:
            names = ["storylines", *names]
        parts += [
            f"storylines AS (\n{pinned(tables['storylines'], snapshot)}\n)",
            MERGED_SQL,
            "facts AS (\nSELECT f.* REPLACE (coalesce(m.to_id, "
            "f.storyline_id) AS storyline_id)\nFROM (\n"
            f"{pinned(tables['facts'], snapshot)}\n) f\n"
            "LEFT JOIN merged m ON f.storyline_id = m.from_id\n)",
        ]
    for name in names:
        if name not in {"facts", "storylines"} or "facts" not in names:
            parts.append(f"{name} AS (\n{pinned(tables[name], snapshot)}\n)")
    head = ("WITH RECURSIVE " + ",\n".join(parts) + "\n") if parts else ""
    return f"{head}SELECT * FROM (\n{body}\n) AS panel_rows"


def offer_console(statement: Recorded, snapshot: int) -> None:
    """Set the statement's lake form, or the reason it has none to offer."""
    if "visible_seq" in statement.sql:
        statement.console_reason = NEEDS_VISIBLE_SEQ
    elif statement.name in NOT_OFFERED:
        statement.console_reason = NOT_OFFERED[statement.name]
    elif statement.name not in CONSOLE_VERIFIED:
        statement.console_reason = NOT_VERIFIED
    elif len(statement.rows) > CONSOLE_MAX_ROWS:
        statement.console_reason = TOO_MANY_ROWS
    else:
        form = lake_form(statement, snapshot)
        if form is None:
            statement.console_reason = UNKNOWN_PARAM
        elif len(form) > MAX_CONSOLE_SQL:
            statement.console_reason = TOO_LONG
        else:
            statement.console_sql = form


def combined(statements: list[Recorded]) -> list[Recorded]:
    """One entry per distinct statement, in order, with how often it ran."""
    seen: dict[tuple[str, str, str], Recorded] = {}
    for statement in statements:
        key = (statement.name, statement.sql, repr(statement.params))
        if key in seen:
            kept = seen[key]
            kept.runs += 1
            kept.cached = kept.cached and statement.cached
            kept.runs_by_method[statement.method] = (
                kept.runs_by_method.get(statement.method, 0) + 1
            )
        else:
            seen[key] = statement
    return list(seen.values())


def trimmed(value: Any) -> str:
    text = "NULL" if value is None else str(value)
    return text[:SHOWN_CHARS] + ("..." if len(text) > SHOWN_CHARS else "")


def shown_rows(rows: list[Any]) -> tuple[list[str], list[list[str]]]:
    """A few rows, with few columns and every value cut."""
    head = rows[:SHOWN_ROWS]
    if not head or not isinstance(head[0], dict):
        return ["value"], [[trimmed(value)] for value in head]
    columns = list(head[0])[:SHOWN_COLUMNS]
    return columns, [[trimmed(row[name]) for name in columns] for row in head]


def shown_params(params: dict[str, Any]) -> str:
    """Parameters as text. A long list shows its length and first values."""
    parts = []
    for name, value in sorted(params.items()):
        if isinstance(value, list | tuple) and len(value) > 8:
            first = ", ".join(trimmed(part) for part in list(value)[:4])
            text = f"[{first}, ...] ({len(value)} values)"
        elif isinstance(value, datetime):
            text = value.astimezone(UTC).isoformat()
        else:
            text = trimmed(value)
        parts.append(f"${name} = {text}")
    return "; ".join(parts)


def source_of(ref: CodeRef) -> dict[str, Any]:
    """A named function's source, read now from the file it is running from."""
    target: Any = importlib.import_module(ref.module)
    for part in ref.name.split("."):
        target = getattr(target, part)
    file_name = inspect.getsourcefile(target)
    lines, first = inspect.getsourcelines(target)
    root = Path(monitor.__file__).resolve().parent
    path = Path(file_name or "").resolve()
    return {
        "name": ref.name,
        "file": f"src/monitor/{path.relative_to(root)}",
        "line": first,
        "source": "".join(lines).rstrip(),
    }


def value_of(ref: CodeRef) -> dict[str, Any]:
    """A named constant or prompt, read now from its module."""
    module = importlib.import_module(ref.module)
    value = getattr(module, ref.name)
    return {
        "name": ref.name,
        "module": ref.module,
        "text": value if isinstance(value, str) else repr(value),
        "long": isinstance(value, str) and len(value) > 80,
    }


def table_definition(name: str) -> dict[str, str]:
    """How the read model fills one local table, with the SQL it runs."""
    if name == "facts":
        return {
            "name": name,
            "how": "One row per item. Each refresh rereads the items with a "
            "row newer than its watermark and replaces them, and then points "
            "items in a merged storyline at the survivor.",
            "sql": FACTS_SELECT.format(changed=CHANGED_IDS).strip(),
        }
    if name in COPIED_TABLES:
        return {
            "name": name,
            "how": "Copied whole from the lake on each refresh.",
            "sql": COPIED_TABLES[name],
        }
    return {
        "name": name,
        "how": "Rows newer than the watermark replace the rows with the same "
        f"{INCREMENTAL_TABLES[name][0]}.",
        "sql": INCREMENTAL_TABLES[name][1],
    }


def methods_sections(card: Card) -> Document:
    """The Methods sections this card links to, rendered with their math."""
    anchors = [
        link.href.split("#")[1]
        for link in card.links
        if link.href.startswith("/methods#")
    ]
    if not anchors:
        return Document(Markup(""))
    try:
        text = METHODS_PATH.read_text(encoding="utf-8")
    except OSError:
        return Document(Markup(""))
    return sections_html(text, anchors)


def panels_list(filters: Filters) -> dict[str, Any]:
    return {
        "cards": CARDS,
        "back_href": f"/?{filters.query_string()}",
        "sql_label": SQL_LABEL,
        "code_label": CODE_LABEL,
    }


def panel_view(
    under: Under, key: str, filters: Filters
) -> dict[str, Any] | None:
    """Everything the page for one card shows. None for an unknown card."""
    card = CARDS_BY_KEY.get(key)
    if card is None:
        return None
    recorder, _, failures = capture_card(
        under.settings, under.model, card, filters
    )
    statements = combined(recorder.statements)
    snapshot = under.snapshot_id
    local: list[str] = []
    for statement in statements:
        offer_console(statement, snapshot)
        statement.local_tables = local_tables_in(statement.sql)
        local += [t for t in statement.local_tables if t not in local]
    entries = []
    for statement in statements:
        columns, rows = shown_rows(statement.rows)
        entries.append(
            {
                "statement": statement,
                "n_rows": len(statement.rows),
                "params_text": shown_params(statement.params),
                "columns": columns,
                "rows": rows,
            }
        )
    sections = methods_sections(card)
    return {
        "card": card,
        "back_href": f"/?{filters.query_string()}",
        "methods_html": sections.html,
        "has_math": sections.has_math,
        "entries": entries,
        "failures": failures,
        "code": [source_of(ref) for ref in card.code],
        "values": [value_of(ref) for ref in card.values],
        "tables": [table_definition(name) for name in local],
        "at_snapshot": snapshot,
        "console_max_rows": CONSOLE_MAX_ROWS,
    }
