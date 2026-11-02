"""How the lake's tables relate: a declared list, and a map drawn from it.

DuckLake has no foreign keys, so nothing enforces these relations. The code
that writes each table keeps them. They are declared here and not inferred from
column names, because a name match can be false: `call_id` on `nlp.race_calls`
identifies a race call, and on `nlp.llm_calls` it identifies a model call.

A table in `lake.TABLES` with no relation here still appears on the map, in its
schema's list of tables that stand alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape

from markupsafe import Markup

from monitor.lake import TABLES

COLUMN_RE = re.compile(r"(\w+)\s+[A-Z]+(?:\[\])?")


def columns_of(table: str) -> set[str]:
    """The columns `lake.TABLES` declares for a table, with `created_at`."""
    return {*COLUMN_RE.findall(TABLES[table]), "created_at"}


@dataclass(frozen=True)
class Relation:
    """`table.column` holds values of `target.target_column`."""

    table: str
    column: str
    target: str
    target_column: str
    condition: str = ""
    note: str = ""
    many_to_many: bool = False

    @property
    def source_text(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def target_text(self) -> str:
        return f"{self.target}.{self.target_column}"


def _to_item(
    table: str, column: str = "item_id", condition: str = "", note: str = ""
) -> Relation:
    return Relation(table, column, "raw.items", "item_id", condition, note)


def _to_source(table: str) -> Relation:
    return Relation(table, "source_id", "ref.sources", "source_id")


def _to_entity(table: str, column: str = "entity_id") -> Relation:
    return Relation(table, column, "ref.entities", "entity_id")


def _to_storyline(
    table: str,
    column: str = "storyline_id",
    condition: str = "",
    note: str = "",
) -> Relation:
    return Relation(
        table, column, "nlp.storylines", "storyline_id", condition, note
    )


def _to_state(table: str) -> Relation:
    return Relation(table, "state_usps", "ref.states", "usps")


def _to_county(table: str) -> Relation:
    return Relation(table, "county_fips", "ref.counties", "fips")


RELATIONS: tuple[Relation, ...] = (
    _to_item("raw.sightings"),
    _to_item("raw.article_text"),
    _to_item(
        "raw.archive_index",
        "key",
        condition="kind = 'pages'",
        note="A row of kind `feeds` holds a feed URL in `key`.",
    ),
    _to_item("nlp.race_calls"),
    _to_item("nlp.mentions"),
    _to_item("nlp.places"),
    _to_item("nlp.storyline_assign"),
    _to_item("nlp.cluster_decisions"),
    _to_item("nlp.dup_groups"),
    _to_item("nlp.classify_a"),
    _to_item("nlp.lang"),
    _to_item("nlp.embeddings"),
    _to_item("nlp.extract_b"),
    _to_item("nlp.rejected_quotes"),
    _to_item("nlp.parser_runs"),
    Relation(
        "nlp.llm_traces",
        "item_ids",
        "raw.items",
        "item_id",
        note="A list: one call covers many items, and an item can be in many "
        "calls.",
        many_to_many=True,
    ),
    _to_source("raw.items"),
    _to_source("raw.fetches"),
    _to_source("raw.sightings"),
    _to_source("raw.archive_index"),
    _to_source("nlp.storyline_assign"),
    _to_source("gold.source_bins"),
    _to_entity("nlp.mentions"),
    _to_entity("nlp.race_calls", "winner_entity_id"),
    _to_entity("gold.entity_bins"),
    _to_entity("gold.entity_lean_bins"),
    _to_entity("gold.comention_edges", "entity_a"),
    _to_entity("gold.comention_edges", "entity_b"),
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
        condition="stage = 'dedupe'",
        note="The nearest item's duplicate group, not the item.",
    ),
    _to_storyline("gold.storyline_bins"),
    _to_storyline("gold.lean_storyline_bins"),
    Relation("nlp.race_calls", "race_key", "ref.races", "race_key"),
    Relation("ref.entities", "race_key", "ref.races", "race_key"),
    Relation("raw.fetch_details", "fetch_id", "raw.fetches", "fetch_id"),
    Relation("nlp.brief_stats", "brief_id", "nlp.briefs", "brief_id"),
    Relation("nlp.llm_traces", "call_id", "nlp.llm_calls", "call_id"),
    Relation(
        "nlp.llm_traces", "prompt_hash", "nlp.llm_prompts", "prompt_hash"
    ),
    Relation("nlp.classify_a", "batch_id", "nlp.llm_traces", "batch_id"),
    _to_state("ref.counties"),
    _to_state("ref.places"),
    _to_state("nlp.places"),
    _to_state("gold.state_bins"),
    _to_county("nlp.places"),
    _to_county("gold.county_bins"),
    Relation("nlp.places", "place_geoid", "ref.places", "geoid"),
)


@dataclass(frozen=True)
class Card:
    """Tables in one schema that share a key, under one heading.

    `targets` decides membership: a table is in the card if it is a target or
    has a relation to one. `hubs` are outlined. `keys` are the columns the
    heading already names, so rows do not repeat them.
    """

    name: str
    schema: str
    heading: str
    targets: tuple[str, ...]
    hubs: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()


CARDS = (
    Card(
        "sources",
        "ref",
        "source_id",
        ("ref.sources",),
        ("ref.sources",),
        ("source_id",),
    ),
    Card(
        "people",
        "ref",
        "entity_id and race_key",
        ("ref.entities", "ref.races"),
        ("ref.entities", "ref.races"),
    ),
    Card(
        "geography",
        "ref",
        "usps and fips",
        ("ref.states", "ref.counties", "ref.places"),
        ("ref.states",),
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
ALONE_HEADING = "stands alone: no shared key"
DERIVED_HEADING = "derived by rollups"


def _own_relations(table: str) -> list[Relation]:
    return [r for r in RELATIONS if r.table == table]


def card_members() -> dict[str, list[str]]:
    """Tables per card, in `RELATIONS` order, with the hub tables first."""
    order = {table: n for n, table in enumerate(TABLES)}
    for number, relation in enumerate(reversed(RELATIONS)):
        order[relation.table] = -number - 1
    members: dict[str, list[str]] = {card.name: [] for card in CARDS}
    for table in sorted(TABLES, key=lambda name: order[name]):
        card = _card_for(table)
        if card:
            members[card.name].append(table)
    for card in CARDS:
        hubs = [t for t in card.targets if t in members[card.name]]
        rest = [t for t in members[card.name] if t not in hubs]
        members[card.name] = hubs + rest
    return members


def _card_for(table: str) -> Card | None:
    schema = table.split(".")[0]
    targets = {r.target for r in _own_relations(table) if not r.many_to_many}
    for card in CARDS:
        if card.schema != schema:
            continue
        if table in card.targets or targets & set(card.targets):
            return card
    return None


def alone_tables(schema: str) -> list[str]:
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


def gold_tables() -> list[str]:
    return [table for table in TABLES if table.startswith("gold.")]


def row_keys(table: str, keys: tuple[str, ...] = ()) -> str:
    """The key columns to print on a table's row, without the card's own."""
    columns = [
        r.target_column
        for r in RELATIONS
        if r.target == table and not r.many_to_many
    ]
    columns += [
        f"{r.column} (list)" if r.many_to_many else r.column
        for r in _own_relations(table)
    ]
    return ", ".join(c for c in dict.fromkeys(columns) if c not in keys)


# The map's geometry, in the units of its viewBox.
WIDTH = 1240
ROW = 25
HEAD = 24
PAD = 6
GAP = 10
RAW_X, RAW_W = 12, 240
NLP_X, NLP_W = 312, 330
SIDE_X, SIDE_W = 692, 240
GOLD_X, GOLD_W = 962, 266
BAND_Y = 28
MAIN_Y = 172
BELOW_BAND = 39
CHANNEL_RACE, CHANNEL_ENTITY, CHANNEL_GEO = 651, 665, 679
CHANNEL_LIST = 672


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

    @property
    def h(self) -> int:
        return HEAD + ROW * len(self.rows) + PAD

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def right(self) -> int:
        return self.x + self.w

    def row_y(self, table: str) -> float:
        """The middle of a table's row."""
        return self.y + HEAD + ROW * self.rows.index(table) + ROW / 2


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
    for number, table in enumerate(box.rows):
        y = box.y + HEAD + ROW * number
        css = "tm-row tm-hub" if table in box.hubs else "tm-row"
        keys = escape(row_keys(table, box.keys))
        parts.append(
            f'<a href="?table={escape(table)}#detail" '
            f'aria-label="{escape(table)}">'
            f'<rect class="{css}" x="{box.x + 4}" y="{y + 1}" '
            f'width="{box.w - 8}" height="{ROW - 2}" rx="3"/>'
            f'<text class="tm-name" x="{box.x + 10}" y="{y + 18}">'
            f"{escape(_short(table))}</text>"
            f'<text class="tm-keys" x="{box.right - 10}" y="{y + 17}" '
            f'text-anchor="end">{keys}</text></a>'
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


def _legend(x: int, y: int) -> list[str]:
    """What each line style means, in words on the map."""
    rows = (
        ("tm-trunk", "the item_id trunk"),
        ("tm-key", "a shared key the code keeps"),
        ("tm-many", "many to many, through a list"),
        ("tm-derived", "derived, not joined"),
    )
    parts = []
    for number, (css, text) in enumerate(rows):
        row_y = y + 10 + number * 24
        parts.append(f'<path class="{css}" d="M{x} {row_y} H{x + 46}"/>')
        parts.append(_label(x + 56, row_y + 5, text))
    parts.append(
        _label(
            x, y + 10 + len(rows) * 24 + 2, "Small text: a table's other keys."
        )
    )
    return parts


def _stack(x: int, y: int, w: int, boxes: list[Box]) -> int:
    """Place boxes top to bottom. Returns the bottom of the last one."""
    for box in boxes:
        box.x, box.y, box.w = x, y, w
        y = box.bottom + GAP
    return y - GAP


@lru_cache(maxsize=1)
def table_map() -> Markup:
    """The whole map as one SVG, sized by how many tables each card holds."""
    members = card_members()
    cards = {
        card.name: Box(
            0, 0, 0, members[card.name], card.heading, card.keys, card.hubs
        )
        for card in CARDS
    }

    def alone_box(schema: str) -> Box:
        return Box(
            0, 0, 0, alone_tables(schema), ALONE_HEADING, css="tm-alone"
        )

    cards["sources"].x, cards["sources"].y = RAW_X, BAND_Y
    cards["sources"].w = RAW_W
    cards["people"].x, cards["people"].y = NLP_X + 60, BAND_Y
    cards["people"].w = NLP_W - 60
    cards["geography"].x, cards["geography"].y = SIDE_X, BAND_Y
    cards["geography"].w = SIDE_W

    ref_alone = alone_box("ref")
    ref_alone.x, ref_alone.w = cards["people"].x, cards["people"].w
    ref_alone.y = cards["people"].bottom + GAP
    band_bottom = max(
        cards["geography"].bottom,
        ref_alone.bottom if ref_alone.rows else 0,
    )
    main_y = max(MAIN_Y, band_bottom + BELOW_BAND)

    raw_alone = alone_box("raw")
    raw_bottom = _stack(
        RAW_X, main_y, RAW_W, [cards["raw_items"], cards["fetches"], raw_alone]
    )
    nlp = cards["nlp_items"]
    nlp.x, nlp.y, nlp.w = NLP_X, main_y, NLP_W
    # The storyline card sits so its hub row lines up with the row joined.
    stories = cards["stories"]
    stories.x, stories.w = SIDE_X, SIDE_W
    stories.y = int(nlp.row_y("nlp.storyline_assign") - HEAD - ROW / 2)
    nlp_alone = alone_box("nlp")
    nlp_alone.x, nlp_alone.w = SIDE_X, SIDE_W
    nlp_alone.y = stories.y - GAP - nlp_alone.h
    below = [cards["calls"], cards["briefs"]]
    # With too many to fit above the storyline card, they go below the rest.
    if nlp_alone.y < band_bottom + GAP:
        below.append(nlp_alone)
    side_bottom = _stack(SIDE_X, stories.bottom + GAP, SIDE_W, below)
    side_top = min(stories.y, nlp_alone.y)
    gold = Box(
        GOLD_X, main_y, GOLD_W, gold_tables(), DERIVED_HEADING, css="tm-gold"
    )

    list_y = max(nlp.bottom, side_bottom, gold.bottom) + 16
    derived_y = list_y + 18
    height = derived_y + 28
    bracket_x = SIDE_X + SIDE_W + 10

    boxes = [
        *cards.values(),
        gold,
        *([ref_alone] if ref_alone.rows else []),
        *([raw_alone] if raw_alone.rows else []),
        *([nlp_alone] if nlp_alone.rows else []),
    ]
    people = cards["people"]
    geography = cards["geography"]
    raw_items = cards["raw_items"]
    calls = cards["calls"]
    sources_x = RAW_X + RAW_W - 26
    trunk_y = raw_items.row_y("raw.items")
    calls_y = nlp.row_y("nlp.race_calls")
    mentions_y = nlp.row_y("nlp.mentions")
    places_y = nlp.row_y("nlp.places")
    assign_y = nlp.row_y("nlp.storyline_assign")
    entity_y = people.row_y("ref.entities")
    race_y = people.row_y("ref.races")
    state_y = geography.row_y("ref.states")
    traces_y = calls.row_y("nlp.llm_traces")
    lines = [
        _line(
            "tm-trunk",
            f"M{RAW_X + RAW_W} {trunk_y} H{NLP_X}",
            "trunk",
        ),
        _line(
            "tm-key",
            f"M{sources_x} {cards['sources'].bottom} V{raw_items.y}",
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
            f"M{CHANNEL_ENTITY} {calls_y + 5} H{nlp.right}",
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
            f"H{RAW_X + RAW_W + 30} V{raw_items.bottom - 20} "
            f"H{RAW_X + RAW_W}",
            "key",
            "key",
        ),
        f'<path class="tm-derived" d="M{SIDE_X + SIDE_W} {side_top} '
        f'H{bracket_x} V{side_bottom} H{SIDE_X + SIDE_W}"/>',
        _line(
            "tm-derived",
            f"M{bracket_x} {gold.y + 110} H{GOLD_X}",
            "derived",
        ),
        _line(
            "tm-derived",
            f"M{RAW_X + 110} {raw_bottom} V{derived_y} "
            f"H{GOLD_X + 110} V{gold.bottom}",
            "derived",
        ),
    ]
    labels = [
        _label(sources_x - 8, BAND_Y + 100, "source_id", "end"),
        _label(RAW_X + RAW_W + 6, trunk_y - 8, "item_id"),
        _label(
            NLP_X + 8,
            list_y - 3,
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
        ("ref", RAW_X, BAND_Y - 9),
        ("raw", RAW_X, main_y - 9),
        ("nlp", NLP_X, main_y - 9),
        ("gold", GOLD_X, main_y - 9),
    ]
    parts = [
        f'<svg class="tm" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {WIDTH} {height}" role="group" '
        'aria-label="How the lake tables relate">',
        STYLE,
        DEFS,
        f'<rect class="tm-bg" width="{WIDTH}" height="{height}"/>',
    ]
    for schema, x, y in headings:
        parts.append(
            f'<text class="tm-schema" x="{x}" y="{y}">'
            f"{escape(HEADINGS[schema])}</text>"
        )
    for box in boxes:
        parts += _box_svg(box)
    parts += [*lines, *labels, *legend, "</svg>"]
    return Markup("".join(parts))


STYLE = """<style>
.tm-bg { fill: var(--surface-1, #fcfcfb); }
.tm-card { fill: var(--surface-2, #efefec); stroke: var(--text-3, #6f6e69);
  stroke-width: 1.5; }
.tm-gold { fill: var(--surface-2, #efefec); stroke: var(--text-3, #6f6e69);
  stroke-width: 1.5; stroke-dasharray: 7 4; }
.tm-alone { fill: none; stroke: var(--text-3, #6f6e69); stroke-width: 1.5;
  stroke-dasharray: 2 4; }
.tm-row { fill: var(--surface-1, #fcfcfb); stroke: var(--line, #a9a9a3); }
.tm-hub { stroke: var(--accent, #6250d6); stroke-width: 2.5; }
a:hover .tm-row, a:focus .tm-row { stroke: var(--text-1, #0b0b0b);
  stroke-width: 2.5; }
.tm-schema { fill: var(--text-1, #0b0b0b);
  font: 700 17px ui-sans-serif, system-ui, sans-serif; }
.tm-head { fill: var(--text-1, #0b0b0b);
  font: 650 13px ui-monospace, Menlo, monospace; }
.tm-name { fill: var(--text-1, #0b0b0b);
  font: 600 15px ui-sans-serif, system-ui, sans-serif; }
.tm-keys { fill: var(--text-2, #52514e);
  font: 400 12px ui-sans-serif, system-ui, sans-serif; }
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
    """Every relation as text, for the list beside the map."""
    return [
        {
            "source": r.source_text,
            "target": r.target_text,
            "kind": "many to many" if r.many_to_many else "many to one",
            "condition": r.condition,
            "note": r.note,
        }
        for r in RELATIONS
    ]
