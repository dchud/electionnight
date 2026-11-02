"""How the lake's tables relate: a declared list, and a map drawn from it.

DuckLake has no foreign keys, so nothing enforces these relations. The code
that writes each table keeps them. They are declared here and not inferred from
column names, because a name match can be false: `call_id` on `nlp.race_calls`
identifies a race call, and on `nlp.llm_calls` it identifies a model call.

`RELATIONS` covers every key between lake tables. The map draws the ones marked
`on_map`, and the list on the page shows all of them. A table with no relation
on the map still appears, in its schema's box of other tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape

from markupsafe import Markup

from monitor.lake import TABLES
from monitor.rollups import FACTS_SQL, LATE_SQL, MENTIONS_SQL, ROLLUPS

COLUMN_RE = re.compile(r"(\w+)\s+[A-Z]+(?:\[\])?")
LAKE_TABLE_RE = re.compile(r"\blake\.(\w+\.\w+)")

# How many records on each side, counting the newest row per record key.
ONE_TO_ONE = "one to one"
MANY_TO_ONE = "many to one"
ONE_TO_MANY = "one to many"
MANY_TO_MANY = "many to many"

# How `table.column` holds the key.
VALUE = "value"
LIST = "list"
JSON_OBJECT = "JSON"
TIME_RANGE = "time range"


def columns_of(table: str) -> set[str]:
    """The columns `lake.TABLES` declares for a table, with `created_at`."""
    return {*COLUMN_RE.findall(TABLES[table]), "created_at"}


def rollup_inputs() -> set[str]:
    """The tables outside gold that the rollups' SQL reads."""
    statements = [LATE_SQL, FACTS_SQL, MENTIONS_SQL]
    statements += [rollup.sql for rollup in ROLLUPS]
    named = {
        table
        for statement in statements
        for table in LAKE_TABLE_RE.findall(statement)
    }
    return {table for table in named if not table.startswith("gold.")}


@dataclass(frozen=True)
class Relation:
    """`table.column` holds values of `target.target_column`.

    A `TIME_RANGE` relation compares times instead, and `condition` gives the
    comparison.
    """

    table: str
    column: str
    target: str
    target_column: str
    kind: str
    condition: str = ""
    note: str = ""
    held_as: str = VALUE
    on_map: bool = True

    @property
    def source_text(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def target_text(self) -> str:
        return f"{self.target}.{self.target_column}"


def _to_item(
    table: str,
    kind: str,
    column: str = "item_id",
    condition: str = "",
    note: str = "",
    held_as: str = VALUE,
    on_map: bool = True,
) -> Relation:
    return Relation(
        table,
        column,
        "raw.items",
        "item_id",
        kind,
        condition,
        note,
        held_as,
        on_map,
    )


def _to_source(table: str) -> Relation:
    return Relation(
        table, "source_id", "ref.sources", "source_id", MANY_TO_ONE
    )


def _to_entity(table: str, column: str = "entity_id") -> Relation:
    return Relation(table, column, "ref.entities", "entity_id", MANY_TO_ONE)


def _to_storyline(
    table: str,
    column: str = "storyline_id",
    condition: str = "",
    note: str = "",
    on_map: bool = True,
) -> Relation:
    return Relation(
        table,
        column,
        "nlp.storylines",
        "storyline_id",
        MANY_TO_ONE,
        condition,
        note,
        VALUE,
        on_map,
    )


def _to_state(
    table: str,
    column: str = "state_usps",
    kind: str = MANY_TO_ONE,
    condition: str = "",
    note: str = "",
    held_as: str = VALUE,
    on_map: bool = True,
) -> Relation:
    return Relation(
        table,
        column,
        "ref.states",
        "usps",
        kind,
        condition,
        note,
        held_as,
        on_map,
    )


def _to_county(table: str) -> Relation:
    return Relation(table, "county_fips", "ref.counties", "fips", MANY_TO_ONE)


def _to_feed(
    table: str, column: str = "feed_url", condition: str = ""
) -> Relation:
    return Relation(
        table,
        column,
        "raw.fetches",
        "feed_url",
        MANY_TO_MANY,
        condition,
        on_map=False,
    )


CITATIONS_NOTE = "A JSON object from reference number to item id."

RELATIONS: tuple[Relation, ...] = (
    _to_item("raw.sightings", MANY_TO_ONE),
    _to_item("raw.article_text", ONE_TO_ONE),
    _to_item(
        "raw.archive_index",
        MANY_TO_ONE,
        "key",
        condition="kind = 'pages'",
        note="A row of kind feeds holds a feed URL in key.",
    ),
    _to_item("nlp.race_calls", MANY_TO_ONE),
    _to_item("nlp.mentions", MANY_TO_ONE),
    _to_item("nlp.places", MANY_TO_ONE),
    _to_item("nlp.storyline_assign", ONE_TO_ONE),
    _to_item("nlp.cluster_decisions", MANY_TO_ONE),
    _to_item("nlp.dup_groups", ONE_TO_ONE),
    _to_item("nlp.classify_a", ONE_TO_ONE),
    _to_item("nlp.lang", ONE_TO_ONE),
    _to_item("nlp.embeddings", ONE_TO_ONE),
    _to_item("nlp.extract_b", ONE_TO_ONE),
    _to_item("nlp.rejected_quotes", MANY_TO_ONE),
    _to_item("nlp.parser_runs", MANY_TO_ONE),
    _to_item(
        "nlp.llm_traces",
        MANY_TO_MANY,
        "item_ids",
        note="One call covers many items, and an item can be in many calls.",
        held_as=LIST,
    ),
    _to_item(
        "nlp.llm_traces",
        MANY_TO_ONE,
        "batch_id",
        condition="purpose = 'extract_b'",
        note="A retried call repeats the batch_id.",
        on_map=False,
    ),
    _to_item(
        "nlp.briefs",
        MANY_TO_MANY,
        "citations",
        note=CITATIONS_NOTE,
        held_as=JSON_OBJECT,
        on_map=False,
    ),
    _to_item(
        "nlp.storylines",
        MANY_TO_MANY,
        "citations",
        note=CITATIONS_NOTE,
        held_as=JSON_OBJECT,
        on_map=False,
    ),
    Relation(
        "raw.collection_gaps",
        "gap_end",
        "raw.items",
        "first_seen_at",
        MANY_TO_MANY,
        condition="first_seen_at BETWEEN gap_end "
        "AND gap_end + INTERVAL 15 MINUTE",
        note="The read model dates an item seen just after a gap by when it "
        "was published.",
        held_as=TIME_RANGE,
        on_map=False,
    ),
    _to_source("raw.items"),
    _to_source("raw.fetches"),
    _to_source("raw.sightings"),
    _to_source("raw.archive_index"),
    _to_source("nlp.storyline_assign"),
    _to_source("gold.source_bins"),
    _to_feed("raw.items"),
    _to_feed("raw.sightings"),
    _to_feed("raw.archive_index", "key", "kind = 'feeds'"),
    Relation(
        "ref.sources",
        "feed_urls",
        "raw.fetches",
        "feed_url",
        MANY_TO_MANY,
        note="Text split on |. A sitemap's URL is only in raw.fetches.",
        held_as=LIST,
        on_map=False,
    ),
    _to_entity("nlp.mentions"),
    _to_entity("nlp.race_calls", "winner_entity_id"),
    _to_entity("gold.entity_bins"),
    _to_entity("gold.entity_lean_bins"),
    _to_entity("gold.comention_edges", "entity_a"),
    _to_entity("gold.comention_edges", "entity_b"),
    Relation(
        "ref.races",
        "candidates",
        "ref.entities",
        "entity_id",
        ONE_TO_MANY,
        note="The entities whose race_key names this race.",
        held_as=LIST,
        on_map=False,
    ),
    _to_storyline("nlp.storyline_assign"),
    _to_storyline(
        "nlp.storylines",
        "merged_into",
        note="A merged storyline names the storyline that absorbed it.",
    ),
    _to_storyline(
        "nlp.cluster_decisions",
        "nearest_id",
        condition="stage = 'storylines'",
        note="The nearest storyline when the decision was made.",
    ),
    Relation(
        "nlp.cluster_decisions",
        "nearest_id",
        "nlp.dup_groups",
        "dup_group_id",
        MANY_TO_MANY,
        condition="stage = 'dedupe'",
        note="The nearest item's duplicate group, not the item. A group has "
        "one record per item in it.",
    ),
    _to_storyline("gold.storyline_bins"),
    _to_storyline("gold.lean_storyline_bins"),
    _to_storyline(
        "nlp.llm_traces",
        "batch_id",
        condition="purpose = 'storylines'",
        note="A relabeled storyline repeats the batch_id.",
        on_map=False,
    ),
    Relation(
        "nlp.race_calls",
        "race_key",
        "ref.races",
        "race_key",
        MANY_TO_ONE,
        condition="race_key NOT LIKE '%-M-%'",
        note="A ballot measure's key is built from the model's wording and "
        "is not looked up.",
    ),
    Relation("ref.entities", "race_key", "ref.races", "race_key", MANY_TO_ONE),
    Relation(
        "nlp.extract_b",
        "raw",
        "ref.races",
        "race_key",
        MANY_TO_MANY,
        note="The race_keys list inside raw. A ballot measure's key is not "
        "looked up.",
        held_as=JSON_OBJECT,
        on_map=False,
    ),
    Relation(
        "raw.fetch_details", "fetch_id", "raw.fetches", "fetch_id", ONE_TO_ONE
    ),
    Relation(
        "nlp.brief_stats",
        "brief_id",
        "nlp.briefs",
        "brief_id",
        ONE_TO_ONE,
        condition="sentences_kept > 0",
        note="A brief with no sentence left writes statistics and no brief.",
    ),
    Relation(
        "nlp.llm_traces",
        "batch_id",
        "nlp.briefs",
        "brief_id",
        MANY_TO_ONE,
        condition="purpose = 'briefs'",
        note="A failed brief leaves a trace and no brief.",
        on_map=False,
    ),
    Relation(
        "nlp.llm_traces", "call_id", "nlp.llm_calls", "call_id", ONE_TO_ONE
    ),
    Relation(
        "nlp.llm_traces",
        "prompt_hash",
        "nlp.llm_prompts",
        "prompt_hash",
        MANY_TO_ONE,
    ),
    Relation(
        "nlp.classify_a",
        "batch_id",
        "nlp.llm_traces",
        "batch_id",
        MANY_TO_MANY,
        note="Empty on a keyword row. A retried call repeats the batch_id.",
    ),
    _to_state("ref.counties"),
    _to_state("ref.places"),
    _to_state(
        "nlp.places",
        condition="method <> 'llm'",
        note="A row of method llm holds the model's text, unchecked.",
    ),
    _to_state("gold.state_bins"),
    _to_state(
        "nlp.classify_a",
        "states",
        MANY_TO_MANY,
        note="Checked against the state list when written.",
        held_as=LIST,
        on_map=False,
    ),
    _to_state(
        "nlp.storylines",
        "states",
        MANY_TO_MANY,
        note="The most frequent states of the storyline's items.",
        held_as=LIST,
        on_map=False,
    ),
    _to_state("ref.sources", "home_state", on_map=False),
    _to_state(
        "ref.races",
        "state",
        note="A territory's race can name a state with no row in ref.states.",
        on_map=False,
    ),
    _to_state(
        "ref.entities",
        "state",
        condition="origin <> 'llm'",
        note="An entity the model named holds the model's text.",
        on_map=False,
    ),
    _to_county("nlp.places"),
    _to_county("gold.county_bins"),
    Relation("nlp.places", "place_geoid", "ref.places", "geoid", MANY_TO_ONE),
    Relation(
        "gold.entity_lean_bins",
        "lean_label",
        "ref.sources",
        "lean_label",
        MANY_TO_MANY,
        on_map=False,
    ),
    Relation(
        "gold.lean_storyline_bins",
        "lean_label",
        "ref.sources",
        "lean_label",
        MANY_TO_MANY,
        on_map=False,
    ),
    Relation(
        "gold.topic_bins",
        "topic",
        "nlp.classify_a",
        "topics",
        MANY_TO_MANY,
        note="One element of the list.",
        on_map=False,
    ),
    Relation(
        "gold.bin_counts",
        "level",
        "nlp.classify_a",
        "primary_level",
        MANY_TO_MANY,
        note="none when an item has no classification.",
        on_map=False,
    ),
)


@dataclass(frozen=True)
class Card:
    """Tables in one schema that share a key, under one heading.

    `targets` decides membership: a table is in the card if it is a target, or
    holds a target's column under the same name. `hubs` are outlined. `keys`
    are the columns the heading names, so rows do not repeat them, and each
    table in the card holds at least one of them. A line leaving a card through
    its bottom edge needs its hub there, which `hub_last` gives.
    """

    name: str
    schema: str
    heading: str
    targets: tuple[str, ...]
    hubs: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    note: str = ""
    hub_last: bool = False


CARDS = (
    Card(
        "sources",
        "ref",
        "source_id",
        ("ref.sources",),
        ("ref.sources",),
        ("source_id",),
        hub_last=True,
    ),
    Card(
        "people",
        "ref",
        "entity_id and race_key",
        ("ref.entities", "ref.races"),
        ("ref.entities", "ref.races"),
        ("entity_id", "race_key"),
    ),
    Card(
        "geography",
        "ref",
        "usps = state_usps",
        ("ref.states", "ref.counties", "ref.places"),
        ("ref.states",),
        ("usps", "state_usps"),
    ),
    Card(
        "raw_items",
        "raw",
        "item_id",
        ("raw.items",),
        ("raw.items",),
        ("item_id",),
    ),
    Card(
        "archive",
        "raw",
        "key, when kind = 'pages'",
        ("raw.archive_index",),
        (),
        ("key",),
        "key holds an item_id.",
    ),
    Card(
        "fetches",
        "raw",
        "fetch_id",
        ("raw.fetches",),
        ("raw.fetches",),
        ("fetch_id",),
    ),
    Card(
        "nlp_items",
        "nlp",
        "item_id, joined to raw.items",
        ("raw.items",),
        (),
        ("item_id",),
    ),
    Card(
        "stories",
        "nlp",
        "storyline_id",
        ("nlp.storylines",),
        ("nlp.storylines",),
        ("storyline_id",),
    ),
    Card(
        "calls",
        "nlp",
        "call_id and prompt_hash",
        ("nlp.llm_traces", "nlp.llm_calls", "nlp.llm_prompts"),
        ("nlp.llm_traces",),
        ("call_id", "prompt_hash"),
    ),
    Card(
        "briefs",
        "nlp",
        "brief_id",
        ("nlp.briefs",),
        ("nlp.briefs",),
        ("brief_id",),
    ),
)
HEADINGS = {
    "ref": "ref: reference tables",
    "raw": "raw: what was collected",
    "nlp": "nlp: what the stages produced",
    "gold": "gold: 5-minute bins",
}
SIDE_HEADING = "nlp, continued"
ALONE_HEADING = "stands alone: no shared key"
NO_KEY_HEADING = "no shared key"
OTHER_HEADING = "other tables"
DERIVED_HEADING = "derived by rollups"


def _map_relations(table: str) -> list[Relation]:
    return [r for r in RELATIONS if r.table == table and r.on_map]


def card_members() -> dict[str, list[str]]:
    """Tables per card, in `RELATIONS` order, with the hub tables first."""
    order = {table: n for n, table in enumerate(TABLES)}
    for number, relation in enumerate(reversed(RELATIONS)):
        if relation.on_map:
            order[relation.table] = -number - 1
    members: dict[str, list[str]] = {card.name: [] for card in CARDS}
    for table in sorted(TABLES, key=lambda name: order[name]):
        card = _card_for(table)
        if card:
            members[card.name].append(table)
    for card in CARDS:
        hubs = [t for t in card.targets if t in members[card.name]]
        rest = [t for t in members[card.name] if t not in hubs]
        members[card.name] = rest + hubs if card.hub_last else hubs + rest
    return members


def _card_for(table: str) -> Card | None:
    schema = table.split(".")[0]
    targets = {
        r.target
        for r in _map_relations(table)
        if r.held_as == VALUE and r.column == r.target_column
    }
    for card in CARDS:
        if card.schema != schema:
            continue
        if table in card.targets or targets & set(card.targets):
            return card
    return None


def other_tables(schema: str) -> list[str]:
    """A schema's tables with no card. Every gold table is derived instead.

    A table in a schema with no column of its own stands with nlp's.
    """
    if schema == "gold":
        return []
    return [
        table
        for table in TABLES
        if _card_for(table) is None
        and (
            table.split(".")[0] == schema
            or (schema == "nlp" and table.split(".")[0] not in HEADINGS)
        )
    ]


def other_heading(tables: list[str]) -> str:
    """What is true of every table in a box of tables with no card."""
    related = [r for r in RELATIONS if {r.table, r.target} & set(tables)]
    if any(r.held_as != TIME_RANGE for r in related):
        return OTHER_HEADING
    return NO_KEY_HEADING if related else ALONE_HEADING


def other_notes(tables: list[str]) -> tuple[str, ...]:
    """A line for each table in the box that joins another by time range."""
    notes = []
    for relation in RELATIONS:
        if relation.table in tables and relation.held_as == TIME_RANGE:
            note = f"joined to {relation.target} by time range"
            prefix = f"{_short(relation.table)} " if len(tables) > 1 else ""
            notes.append(prefix + note)
    return tuple(notes)


def gold_tables() -> list[str]:
    return [table for table in TABLES if table.startswith("gold.")]


def row_keys(table: str, keys: tuple[str, ...] = ()) -> str:
    """The key columns to print on a table's row, without the card's own."""
    columns = [
        r.target_column
        for r in RELATIONS
        if r.target == table and r.on_map and r.held_as == VALUE
    ]
    columns += [
        f"{r.column} (list)" if r.held_as == LIST else r.column
        for r in _map_relations(table)
    ]
    return ", ".join(c for c in dict.fromkeys(columns) if c not in keys)


# The map's geometry, in the units of its viewBox.
WIDTH = 1240
ROW = 25
ROW_WRAPPED = 41
HEAD = 24
PAD = 6
GAP = 10
NOTE_LINE = 18
RAW_X, RAW_W = 22, 236
NLP_X, NLP_W = 326, 316
SIDE_X, SIDE_W = 692, 240
GOLD_X, GOLD_W = 962, 266
PEOPLE_INSET = 60
BAND_Y = 28
SCHEMA_ROOM = 23
BELOW_BAND = 41
BELOW_CARDS = 24
BELOW_LIST = 18
BELOW_DERIVED = 28
CHANNEL_RACE, CHANNEL_ENTITY, CHANNEL_GEO = 651, 665, 679
CHANNEL_LIST = 672
CHANNEL_LEFT = 10
CHANNEL_RIGHT = 947
LIST_X = RAW_X + RAW_W + 18
TRUNK_X = RAW_X + RAW_W + 40
COMB_X = NLP_X - 10
TRUNK_RISE = 5
LIST_DROP = 7
ABOVE_TRUNK = 15
# Estimates for one character of a table name and of a row's keys.
NAME_CHAR = 8.2
KEYS_CHAR = 6.6
ROW_TEXT_PAD = 24


def _keys_fit(table: str, keys: tuple[str, ...], width: int) -> bool:
    used = len(_short(table)) * NAME_CHAR
    used += len(row_keys(table, keys)) * KEYS_CHAR
    return used + ROW_TEXT_PAD <= width


@dataclass
class Box:
    x: int
    y: int
    w: int
    rows: list[str]
    heading: str
    keys: tuple[str, ...] = ()
    hubs: tuple[str, ...] = ()
    css: str = "tm-card"
    notes: tuple[str, ...] = ()

    def row_h(self, table: str) -> int:
        """A row's height. Keys that do not fit beside the name go below it."""
        return ROW if _keys_fit(table, self.keys, self.w) else ROW_WRAPPED

    def row_top(self, table: str) -> int:
        top = self.y + HEAD
        for row in self.rows:
            if row == table:
                return top
            top += self.row_h(row)
        raise KeyError(table)

    @property
    def h(self) -> int:
        rows = sum(self.row_h(row) for row in self.rows)
        return HEAD + rows + NOTE_LINE * len(self.notes) + PAD

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def right(self) -> int:
        return self.x + self.w

    def row_y(self, table: str) -> float:
        """The middle of a table's row, or of its first line if wrapped."""
        return self.row_top(table) + ROW / 2


def _short(table: str) -> str:
    schema, name = table.split(".", 1)
    return name if schema in HEADINGS else table


def _box_svg(box: Box) -> list[str]:
    parts = [
        f'<rect class="{box.css}" x="{box.x}" y="{box.y}" width="{box.w}" '
        f'height="{box.h}" rx="6"/>',
        f'<text class="tm-head" x="{box.x + 8}" y="{box.y + 17}">'
        f"{escape(box.heading)}</text>",
    ]
    for table in box.rows:
        y = box.row_top(table)
        height = box.row_h(table)
        css = "tm-row tm-hub" if table in box.hubs else "tm-row"
        keys = escape(row_keys(table, box.keys))
        wrapped = height != ROW
        keys_y = y + height - 8 if wrapped else y + 17
        parts.append(
            f'<a href="?table={escape(table)}#detail" '
            f'aria-label="{escape(table)}">'
            f'<rect class="{css}" x="{box.x + 4}" y="{y + 1}" '
            f'width="{box.w - 8}" height="{height - 2}" rx="3"/>'
            f'<text class="tm-name" x="{box.x + 10}" y="{y + 18}">'
            f"{escape(_short(table))}</text>"
            f'<text class="tm-keys" x="{box.right - 10}" y="{keys_y}" '
            f'text-anchor="end">{keys}</text></a>'
        )
    notes_y = box.y + HEAD + sum(box.row_h(row) for row in box.rows)
    for number, note in enumerate(box.notes):
        parts.append(
            f'<text class="tm-keys" x="{box.x + 10}" '
            f'y="{notes_y + NOTE_LINE * number + 14}">{escape(note)}</text>'
        )
    return parts


def _line(css: str, path: str, *markers: str) -> str:
    ends = " ".join(
        f'marker-{end}="url(#tm-{name})"'
        for end, name in zip(("end", "start"), markers, strict=False)
    )
    return f'<path class="{css}" {ends} d="{path}"/>'


def _label(x: float, y: float, text: str, anchor: str = "start") -> str:
    return (
        f'<text class="tm-label" x="{x}" y="{y}" text-anchor="{anchor}">'
        f"{escape(text)}</text>"
    )


LEGEND_ROWS = (
    ("tm-trunk", "the item_id trunk"),
    ("tm-key", "a value the code keeps equal"),
    ("tm-many", "many to many, through a list"),
    ("tm-derived", "rollups read it to derive gold"),
)
LEGEND_NOTE = "Small text: more keys. The list has all."
LEGEND_ROW = 24


def _legend_bottom(y: int) -> int:
    return y + 10 + len(LEGEND_ROWS) * LEGEND_ROW + 6


def _legend(x: int, y: int) -> list[str]:
    """What each line style means, in words on the map."""
    parts = ['<g class="tm-legend">']
    for number, (css, text) in enumerate(LEGEND_ROWS):
        row_y = y + 10 + number * LEGEND_ROW
        parts.append(f'<path class="{css}" d="M{x} {row_y} H{x + 46}"/>')
        parts.append(_label(x + 56, row_y + 5, text))
    parts.append(
        _label(x, y + 10 + len(LEGEND_ROWS) * LEGEND_ROW + 2, LEGEND_NOTE)
    )
    parts.append("</g>")
    return parts


def _stack(y: int, boxes: list[Box]) -> int:
    """Place boxes top to bottom. Returns the bottom of the last one."""
    for box in boxes:
        box.y = y
        y = box.bottom + GAP
    return y - GAP


def _bus_paths(
    rows: list[tuple[Box, str]], edge: str, channel: int, bottom: float
) -> list[str]:
    """A tooth from each row to a channel, and the channel down to `bottom`."""
    paths = [
        f"M{getattr(box, edge)} {box.row_y(table)} H{channel}"
        for box, table in rows
    ]
    if rows:
        top = min(box.row_y(table) for box, table in rows)
        paths.append(f"M{channel} {top} V{bottom}")
    return paths


def _derived_lines(
    boxes: list[Box], gold: Box, derived_y: float, ends_x: int
) -> list[str]:
    """A line from each table the rollups read, ending on the gold box."""
    read = rollup_inputs()
    inputs = [
        (box, table) for box in boxes for table in box.rows if table in read
    ]
    left = [(b, t) for b, t in inputs if b.x == RAW_X]
    side = [(b, t) for b, t in inputs if b.x == SIDE_X]
    middle = [(b, t) for b, t in inputs if b.x not in {RAW_X, SIDE_X}]
    lines = []
    if left or middle:
        start = CHANNEL_LEFT if left else COMB_X
        paths = _bus_paths(left, "x", CHANNEL_LEFT, derived_y)
        paths += _bus_paths(middle, "x", COMB_X, derived_y)
        paths.append(f"M{start} {derived_y} H{ends_x} V{gold.bottom}")
        lines.append(_line("tm-derived", " ".join(paths), "derived"))
    if side:
        head_y = gold.y + HEAD / 2
        paths = [
            f"M{box.right} {box.row_y(table)} H{CHANNEL_RIGHT}"
            for box, table in side
        ]
        top = min(head_y, *(box.row_y(table) for box, table in side))
        bottom = max(box.row_y(table) for box, table in side)
        if top < head_y:
            paths.append(f"M{CHANNEL_RIGHT} {top} V{head_y}")
        paths.append(f"M{CHANNEL_RIGHT} {bottom} V{head_y} H{GOLD_X}")
        lines.append(_line("tm-derived", " ".join(paths), "derived"))
    return lines


@lru_cache(maxsize=1)
def table_map() -> Markup:
    """The whole map as one SVG, sized by what each box holds."""
    members = card_members()

    def card_box(name: str, x: int, w: int) -> Box:
        card = next(card for card in CARDS if card.name == name)
        notes = (card.note,) if card.note else ()
        return Box(
            x,
            0,
            w,
            members[name],
            card.heading,
            card.keys,
            card.hubs,
            notes=notes,
        )

    def others_box(schema: str, x: int, w: int) -> Box:
        tables = other_tables(schema)
        return Box(
            x,
            0,
            w,
            tables,
            other_heading(tables),
            css="tm-alone",
            notes=other_notes(tables),
        )

    sources = card_box("sources", RAW_X, RAW_W)
    people = card_box("people", NLP_X + PEOPLE_INSET, NLP_W - PEOPLE_INSET)
    geography = card_box("geography", SIDE_X, SIDE_W)
    ref_others = others_box("ref", people.x, people.w)
    for box in (sources, people, geography):
        box.y = BAND_Y
    ref_others.y = people.bottom + GAP
    band_bottom = max(
        sources.bottom,
        people.bottom,
        geography.bottom,
        ref_others.bottom if ref_others.rows else 0,
        _legend_bottom(BAND_Y),
    )
    main_y = band_bottom + BELOW_BAND

    raw_items = card_box("raw_items", RAW_X, RAW_W)
    raw_others = others_box("raw", RAW_X, RAW_W)
    raw_boxes = [
        raw_items,
        card_box("archive", RAW_X, RAW_W),
        card_box("fetches", RAW_X, RAW_W),
        raw_others,
    ]
    raw_boxes = [box for box in raw_boxes if box.rows]
    raw_bottom = _stack(main_y, raw_boxes)
    nlp = card_box("nlp_items", NLP_X, NLP_W)
    nlp.y = main_y
    # The storyline card sits so its hub row lines up with the row joined.
    assign_y = nlp.row_y("nlp.storyline_assign")
    stories = card_box("stories", SIDE_X, SIDE_W)
    stories.y = int(assign_y - HEAD - ROW / 2)
    nlp_others = others_box("nlp", SIDE_X, SIDE_W)
    nlp_others.y = stories.y - GAP - nlp_others.h
    below = [
        card_box("calls", SIDE_X, SIDE_W),
        card_box("briefs", SIDE_X, SIDE_W),
    ]
    # With too many to fit above the storyline card, they go below the rest.
    if nlp_others.y < geography.bottom + GAP + SCHEMA_ROOM:
        below.append(nlp_others)
    side_bottom = _stack(stories.bottom + GAP, below)
    side_top = stories.y
    if nlp_others.rows:
        side_top = min(side_top, nlp_others.y)
    gold = Box(
        GOLD_X, main_y, GOLD_W, gold_tables(), DERIVED_HEADING, css="tm-gold"
    )

    list_y = max(nlp.bottom, side_bottom, gold.bottom) + BELOW_CARDS
    derived_y = max(list_y, raw_bottom) + BELOW_LIST
    height = derived_y + BELOW_DERIVED

    calls = below[0]
    boxes = [
        sources,
        people,
        geography,
        *raw_boxes,
        nlp,
        stories,
        *below,
        gold,
    ]
    if ref_others.rows:
        boxes.append(ref_others)
    if nlp_others.rows and not any(box is nlp_others for box in below):
        boxes.append(nlp_others)

    sources_x = RAW_X + RAW_W - 26
    sources_row_bottom = sources.bottom - PAD - 1
    items_y = raw_items.row_y("raw.items")
    calls_y = nlp.row_y("nlp.race_calls")
    mentions_y = nlp.row_y("nlp.mentions")
    places_y = nlp.row_y("nlp.places")
    entity_y = people.row_y("ref.entities")
    race_y = people.row_y("ref.races")
    state_y = geography.row_y("ref.states")
    traces_y = calls.row_y("nlp.llm_traces")
    head_y = nlp.y + HEAD / 2
    lines = [
        _line(
            "tm-trunk",
            f"M{raw_items.right} {items_y - TRUNK_RISE} H{TRUNK_X} "
            f"V{head_y} H{NLP_X}",
            "trunk",
        ),
        _line(
            "tm-key",
            f"M{sources_x} {sources_row_bottom} "
            f"V{raw_items.row_top('raw.items') + 1}",
            "key",
        ),
        _line(
            "tm-key",
            f"M{people.right} {entity_y} H{CHANNEL_ENTITY} "
            f"V{mentions_y} H{nlp.right}",
            "key",
        ),
        _line(
            "tm-key",
            f"M{people.right} {entity_y} H{CHANNEL_ENTITY} "
            f"V{calls_y + 5} H{nlp.right}",
            "key",
        ),
        _line(
            "tm-key",
            f"M{people.right} {race_y} H{CHANNEL_RACE} "
            f"V{calls_y - 5} H{nlp.right}",
            "key",
        ),
        _line(
            "tm-key",
            f"M{geography.x} {state_y} H{CHANNEL_GEO} "
            f"V{places_y} H{nlp.right}",
            "key",
        ),
        _line(
            "tm-key",
            f"M{SIDE_X} {assign_y} H{nlp.right}",
            "key",
        ),
        _line(
            "tm-many",
            f"M{SIDE_X} {traces_y} H{CHANNEL_LIST} V{list_y} "
            f"H{LIST_X} V{items_y + LIST_DROP} H{raw_items.right}",
            "key",
            "key",
        ),
        *_derived_lines(boxes, gold, derived_y, GOLD_X + 110),
    ]
    labels = [
        _label(
            sources_x - 8,
            (sources.bottom + main_y - SCHEMA_ROOM) / 2 + 5,
            "source_id",
            "end",
        ),
        _label(
            (raw_items.right + NLP_X) / 2,
            head_y - ABOVE_TRUNK,
            "item_id",
            "middle",
        ),
        _label(
            NLP_X + 8,
            list_y - LIST_DROP,
            "nlp.llm_traces.item_ids is a list: many to many",
        ),
        _label(
            RAW_X + 122,
            derived_y + 16,
            "Rollups derive every gold table from raw and nlp rows, with lean "
            "labels from ref.sources.",
        ),
    ]
    legend = _legend(GOLD_X, BAND_Y)
    headings = [
        (HEADINGS["ref"], RAW_X, BAND_Y - 9),
        (HEADINGS["raw"], RAW_X, main_y - 9),
        (HEADINGS["nlp"], NLP_X, main_y - 9),
        (SIDE_HEADING, SIDE_X, side_top - 9),
        (HEADINGS["gold"], GOLD_X, main_y - 9),
    ]
    parts = [
        f'<svg class="tm" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {WIDTH} {height}" role="group" '
        'aria-label="How the lake tables relate">',
        STYLE,
        DEFS,
        f'<rect class="tm-bg" width="{WIDTH}" height="{height}"/>',
    ]
    for text, x, y in headings:
        parts.append(
            f'<text class="tm-schema" x="{x}" y="{y}">{escape(text)}</text>'
        )
    for box in boxes:
        parts += _box_svg(box)
    parts += ['<g class="tm-lines">', *lines, "</g>", *labels, *legend]
    parts.append("</svg>")
    return Markup("".join(parts))


STYLE = """<style>
.tm-bg { fill: var(--surface-1, #fcfcfb); }
.tm-card { fill: var(--surface-2, #efefec);
  stroke: var(--line-strong, #8f8e87); }
.tm-gold { fill: var(--surface-2, #efefec);
  stroke: var(--line-strong, #8f8e87); stroke-dasharray: 7 4; }
.tm-alone { fill: none; stroke: var(--line-strong, #8f8e87);
  stroke-dasharray: 2 4; }
.tm-row { fill: var(--surface-1, #fcfcfb); stroke: var(--line, #a9a9a3); }
.tm-hub { stroke: var(--accent, #6250d6); stroke-width: 2.5; }
a:hover .tm-row, a:focus .tm-row { stroke: var(--text-1, #0b0b0b);
  stroke-width: 2.5; }
.tm-schema { fill: var(--text-1, #0b0b0b);
  font: var(--weight-heading, 550) 17px ui-sans-serif, system-ui,
    sans-serif; }
.tm-head { fill: var(--text-1, #0b0b0b);
  font: var(--weight-label, 500) 13.5px ui-monospace, Menlo, monospace; }
.tm-name { fill: var(--text-1, #0b0b0b);
  font: var(--weight-label, 500) 15px ui-sans-serif, system-ui,
    sans-serif; }
.tm-keys { fill: var(--text-2, #52514e);
  font: 400 13.5px ui-sans-serif, system-ui, sans-serif; }
.tm-label, .tm-legend { fill: var(--text-2, #52514e);
  font: 400 14px ui-sans-serif, system-ui, sans-serif; }
.tm-trunk { fill: none; stroke: var(--accent, #6250d6); stroke-width: 7; }
.tm-key { fill: none; stroke: var(--text-2, #52514e); stroke-width: 2.5; }
.tm-many { fill: none; stroke: var(--text-2, #52514e); stroke-width: 2.5;
  stroke-dasharray: 10 3 2 3; }
.tm-derived { fill: none; stroke: var(--text-3, #6f6e69); stroke-width: 3;
  stroke-dasharray: 9 6; }
.tm-head-key { fill: var(--text-2, #52514e); }
.tm-head-trunk { fill: var(--accent, #6250d6); }
.tm-head-derived { fill: var(--text-3, #6f6e69); }
</style>"""
DEFS = """<defs>
<marker id="tm-key" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5"
  markerHeight="5" orient="auto-start-reverse">
  <path class="tm-head-key" d="M0 0 L10 5 L0 10 Z"/></marker>
<marker id="tm-trunk" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="3"
  markerHeight="3" orient="auto-start-reverse">
  <path class="tm-head-trunk" d="M0 0 L10 5 L0 10 Z"/></marker>
<marker id="tm-derived" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4"
  markerHeight="4" orient="auto-start-reverse">
  <path class="tm-head-derived" d="M0 0 L10 5 L0 10 Z"/></marker>
</defs>"""


def relation_rows() -> list[dict[str, str]]:
    """Every relation as text, for the list below the map."""
    return [
        {
            "source": r.source_text,
            "target": r.target_text,
            "kind": r.kind,
            "held_as": r.held_as,
            "condition": r.condition,
            "note": r.note,
            "on_map": "yes" if r.on_map else "no",
        }
        for r in RELATIONS
    ]
