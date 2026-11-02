"""The table map: its declared relations, and the diagram drawn from them."""

import asyncio
import csv
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb
import httpx
import numpy as np
import pytest
import yaml
from markupsafe import escape
from test_under import add_rows, app_client, item, seed, source

from monitor.archive import ArchiveWriter
from monitor.ingest.poller import Poller
from monitor.ingest.sources import Source
from monitor.lake import TABLES, BatchWriter, connect
from monitor.llm.budget import Budget
from monitor.llm.client import LlmClient
from monitor.llm.schemas import Brief, PlaceB
from monitor.orchestrator import Database
from monitor.parsers import extract_b
from monitor.parsers.base import Item
from monitor.parsers.briefs import BriefInput, BriefWriter
from monitor.parsers.dedupe import DedupeIndex, DedupeParser
from monitor.parsers.geo import Gazetteer
from monitor.parsers.storylines import StorylineEngine, StorylineParser
from monitor.rollups import MENTIONS_SQL, RollupEngine
from monitor.settings import Settings
from monitor.web import under_map
from monitor.web.readmodel import FACTS_SELECT
from monitor.web.under import TABLE_KEYS
from monitor.web.under_map import (
    CARDS,
    DERIVED_HEADING,
    RELATIONS,
    card_members,
    columns_of,
    gold_tables,
    relation_rows,
    row_keys,
    table_map,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "feeds"
FEED_URL = "https://example.com/feed.xml"
ROLLUP_READS = {
    "raw.items",
    "ref.sources",
    "nlp.classify_a",
    "nlp.dup_groups",
    "nlp.extract_b",
    "nlp.mentions",
    "nlp.places",
    "nlp.storyline_assign",
    "nlp.storylines",
}
DRAWN_KEYS = {
    ("ref.sources", "raw.items"),
    ("ref.entities", "nlp.mentions"),
    ("ref.entities", "nlp.race_calls"),
    ("ref.races", "nlp.race_calls"),
    ("ref.states", "nlp.places"),
    ("nlp.storylines", "nlp.storyline_assign"),
}
# How far a line's end can sit from the row it lands on: a row is inset from
# its card by 4, and a row's rectangle stops 1 short of its neighbor's.
ROW_INSET = 5
ROW_EDGE = 1


@pytest.fixture
def fresh_map():
    table_map.cache_clear()
    yield
    table_map.cache_clear()


def link(table):
    return f'href="?table={table}#detail"'


def relation(source, target):
    return next(
        r for r in RELATIONS if r.source_text == source and r.target == target
    )


# What the drawn map says, read from the SVG and nothing else.


@dataclass
class Rect:
    x: float
    y: float
    right: float
    bottom: float
    css: str = ""
    heading: str = ""

    def holds(self, x, y):
        return self.x < x < self.right and self.y < y < self.bottom


@dataclass
class Drawn:
    width: int
    height: int
    rows: dict[str, Rect]
    boxes: list[Rect]
    lines: list[tuple[str, list[list[tuple[float, float]]]]]
    schema_headings: list[tuple[str, float, float]]
    labels: list[tuple[str, float, float, str]]

    def tables_in(self, box):
        return tuple(
            table
            for table, row in self.rows.items()
            if box.x <= row.x and row.right <= box.right
            if box.y <= row.y and row.bottom <= box.bottom
        )

    def box_of(self, table):
        row = self.rows[table]
        return next(
            box
            for box in self.boxes
            if box.x <= row.x and row.right <= box.right
            if box.y <= row.y and row.bottom <= box.bottom
        )

    def landed(self, point):
        """The row a point is on, or the box if it is on an edge between rows.

        A box with one row is that row.
        """
        x, y = point
        hits = [
            table
            for table, row in self.rows.items()
            if row.x - ROW_INSET <= x <= row.right + ROW_INSET
            and row.y - ROW_EDGE <= y <= row.bottom + ROW_EDGE
        ]
        if len(hits) > 1:
            return hits
        if hits:
            return hits[0]
        for box in self.boxes:
            on_side = x in {box.x, box.right} and box.y <= y <= box.bottom
            on_end = y in {box.y, box.bottom} and box.x <= x <= box.right
            if on_side or on_end:
                tables = self.tables_in(box)
                return tables[0] if len(tables) == 1 else tables
        return None


def polylines(path):
    """Each `M` run of a path as its points. Only M, H, and V are used."""
    runs = []
    x = y = 0.0
    for command, first, second in re.findall(
        r"([MHV])\s*(-?[\d.]+)(?:\s+(-?[\d.]+))?", path
    ):
        if command == "M":
            x, y = float(first), float(second)
            runs.append([(x, y)])
        elif command == "H":
            x = float(first)
            runs[-1].append((x, y))
        else:
            y = float(first)
            runs[-1].append((x, y))
    return runs


def read_map(page):
    width, height = re.search(r'viewBox="0 0 (\d+) (\d+)"', page).groups()
    rows = {
        table: Rect(
            float(x), float(y), float(x) + float(w), float(y) + float(h)
        )
        for table, x, y, w, h in re.findall(
            r'<a href="\?table=([^#]+)#detail"[^>]*><rect class="[^"]*" '
            r'x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"',
            page,
        )
    }
    boxes = [
        Rect(
            float(x),
            float(y),
            float(x) + float(w),
            float(y) + float(h),
            css,
            heading,
        )
        for css, x, y, w, h, heading in re.findall(
            r'<rect class="(tm-(?:card|alone|gold))" x="(\d+)" y="(\d+)" '
            r'width="(\d+)" height="(\d+)" rx="6"/>'
            r'<text class="tm-head"[^>]*>([^<]*)</text>',
            page,
        )
    ]
    # Everything after the legend's own markup is the map's.
    body = re.sub(r'<g class="tm-legend">.*?</g>', "", page, flags=re.S)
    lines = [
        (css, polylines(path))
        for css, path in re.findall(
            r'<path class="(tm-(?:trunk|key|many|derived))"[^>]*? d="([^"]+)"',
            body,
        )
    ]
    schema_headings = [
        (text, float(x), float(y))
        for x, y, text in re.findall(
            r'<text class="tm-schema" x="([\d.]+)" y="([\d.]+)">([^<]*)',
            page,
        )
    ]
    labels = [
        (text, float(x), float(y), anchor)
        for x, y, anchor, text in re.findall(
            r'<text class="tm-label" x="([\d.]+)" y="([\d.]+)" '
            r'text-anchor="(\w+)">([^<]*)',
            body,
        )
    ]
    return Drawn(
        int(width), int(height), rows, boxes, lines, schema_headings, labels
    )


def declared():
    return {
        (
            row["source"].rsplit(".", 1)[0],
            row["target"].rsplit(".", 1)[0],
            row["kind"],
        )
        for row in relation_rows()
    }


def derived_problems(drawn, runs):
    """A derived line starts at what rollups read and ends on the gold box."""
    problems = []
    gold = tuple(gold_tables())
    starts = []
    for run in runs:
        where = drawn.landed(run[0])
        if where is None:
            continue
        starts.append(where)
        if where not in ROLLUP_READS:
            problems.append(f"derived line starts on {where}, not read")
        end = run[-1]
        on_gold = drawn.landed(end) == gold
        joined = any(
            other is not run and on_polyline(end, other) for other in runs
        )
        if not on_gold and not joined:
            problems.append(f"derived line from {where} stops at {end}")
    end = runs[-1][-1]
    if drawn.landed(end) != gold:
        problems.append(
            f"derived line ends on {drawn.landed(end)}, not the gold box"
        )
    return problems, starts


def on_polyline(point, run):
    x, y = point
    for (ax, ay), (bx, by) in zip(run, run[1:], strict=False):
        if min(ax, bx) <= x <= max(ax, bx) and min(ay, by) <= y <= max(ay, by):
            return True
    return False


def line_problems(page):
    """How the drawn lines differ from what is declared. Empty when true."""
    drawn = read_map(page)
    relations = declared()
    problems = []
    keys = set()
    rollup_starts = []
    for css, runs in drawn.lines:
        if css == "tm-derived":
            found, starts = derived_problems(drawn, runs)
            problems += found
            rollup_starts += starts
            continue
        if len(runs) != 1:
            problems.append(f"{css} has {len(runs)} runs")
            continue
        start = drawn.landed(runs[0][0])
        end = drawn.landed(runs[0][-1])
        if css == "tm-trunk":
            if start != "raw.items":
                problems.append(f"the trunk starts on {start}")
            if not isinstance(end, tuple) or not all(
                (table, "raw.items") in {(a, b) for a, b, _ in relations}
                for table in end
            ):
                problems.append(f"the trunk ends on {end}")
        elif css == "tm-many":
            if (start, end, "many to many") not in relations:
                problems.append(f"the list line joins {start} and {end}")
        else:
            keys.add((start, end))
            if (end, start) not in {(a, b) for a, b, _ in relations}:
                problems.append(f"a key line joins {start} and {end}")
    if keys != DRAWN_KEYS:
        problems.append(f"key lines drawn: {sorted(keys, key=str)}")
    if set(rollup_starts) != ROLLUP_READS:
        problems.append(f"derived lines start at: {rollup_starts}")
    return problems


def through_problems(page):
    """Segments that cross a box neither of a line's ends is in."""
    drawn = read_map(page)
    problems = []
    for css, runs in drawn.lines:
        for run in runs:
            ends = [run[0], run[-1]]
            for (ax, ay), (bx, by) in zip(run, run[1:], strict=False):
                for box in drawn.boxes:
                    if any(
                        box.x <= x <= box.right and box.y <= y <= box.bottom
                        for x, y in ends
                    ):
                        continue
                    crosses = (
                        min(ax, bx) < box.right
                        and max(ax, bx) > box.x
                        and min(ay, by) < box.bottom
                        and max(ay, by) > box.y
                    )
                    if crosses:
                        problems.append(f"{css} {run} crosses {box}")
    return problems


def clip_problems(page):
    """Anything drawn outside the view box, or on top of something else."""
    drawn = read_map(page)
    problems = []
    for box in drawn.boxes:
        if box.right > drawn.width or box.bottom > drawn.height:
            problems.append(f"{box} is outside {drawn.width}x{drawn.height}")
    for table, row in drawn.rows.items():
        if row.bottom > drawn.height:
            problems.append(f"{table} is cut off at {drawn.height}")
    for css, runs in drawn.lines:
        for run in runs:
            for x, y in run:
                if not (0 <= x <= drawn.width and 0 <= y <= drawn.height):
                    problems.append(f"{css} reaches {x}, {y}")
    for number, first in enumerate(drawn.boxes):
        for second in drawn.boxes[number + 1 :]:
            if (
                first.x < second.right
                and second.x < first.right
                and first.y < second.bottom
                and second.y < first.bottom
            ):
                problems.append(f"{first} overlaps {second}")
    for text, x, y, _ in drawn.labels:
        if y > drawn.height or x > drawn.width:
            problems.append(f"the label {text!r} is outside the view box")
        for box in drawn.boxes:
            if box.holds(x, y):
                problems.append(f"the label {text!r} is inside {box}")
    return problems


# The drawn lines, against the declarations.


def test_each_drawn_line_lands_on_what_is_declared():
    assert line_problems(str(table_map())) == []


def test_no_line_crosses_a_box_it_does_not_end_in():
    assert through_problems(str(table_map())) == []


def shifted(page, pattern, replacement):
    """The page with one line's path changed."""
    changed, count = re.subn(pattern, replacement, page, count=1)
    assert count == 1, pattern
    return changed


@pytest.mark.parametrize(
    ("css", "number", "problem"),
    [
        ("tm-many", 1, "the list line joins"),
        ("tm-key", 2, "a key line joins"),
    ],
)
def test_a_line_moved_to_a_neighbor_row_is_caught(css, number, problem):
    page = str(table_map())
    drawn = [
        match
        for match in re.finditer(
            rf'<path class="{css}"[^>]*? d="([^"]+)"', page
        )
    ]
    path = drawn[number - 1].group(1)
    x, y = polylines(path)[0][-1]
    last_vertical = list(re.finditer(r"V([\d.]+)", path))[-1]
    moved = (
        path[: last_vertical.start()]
        + f"V{float(last_vertical.group(1)) + 25}"
        + path[last_vertical.end() :]
    )
    assert (x, y) != polylines(moved)[0][-1]
    page = page.replace(path, moved, 1)
    assert any(problem in found for found in line_problems(page))


def test_a_derived_line_moved_to_an_unread_row_is_caught():
    page = str(table_map())
    drawn = read_map(page)
    runs = next(runs for css, runs in drawn.lines if css == "tm-derived")
    tooth = next(run for run in runs if drawn.landed(run[0]) == "nlp.mentions")
    (x, y), (end, _) = tooth
    unread = drawn.rows["nlp.lang"].y + 11.5
    changed = page.replace(
        f"M{x:g} {y:g} H{end:g}", f"M{x:g} {unread:g} H{end:g}", 1
    )
    assert any(
        "nlp.lang, not read" in found for found in line_problems(changed)
    )


def test_a_box_edge_between_rows_is_not_a_row():
    drawn = read_map(str(table_map()))
    nlp = drawn.box_of("nlp.mentions")
    assert drawn.landed((nlp.x, nlp.y + 12)) == drawn.tables_in(nlp)
    assert drawn.landed((nlp.x + 40, nlp.y - 40)) is None


def test_rollup_inputs_are_what_the_rollups_sql_reads():
    assert under_map.rollup_inputs() == ROLLUP_READS


def test_the_trunk_ends_on_the_card_heading_not_on_a_row():
    drawn = read_map(str(table_map()))
    runs = next(runs for css, runs in drawn.lines if css == "tm-trunk")
    end = drawn.landed(runs[0][-1])
    assert isinstance(end, tuple)
    assert "nlp.race_calls" in end


@pytest.mark.parametrize("table", TABLES)
def test_every_table_is_on_the_map_once_and_links_to_its_entry(table):
    assert str(table_map()).count(link(table)) == 1


@pytest.mark.parametrize("relation", RELATIONS, ids=lambda r: r.source_text)
def test_a_relation_names_columns_that_exist(relation):
    assert relation.table in TABLES
    assert relation.target in TABLES
    assert relation.column in columns_of(relation.table)
    assert relation.target_column in columns_of(relation.target)


def test_columns_are_read_from_the_declaration():
    assert "item_ids" in columns_of("nlp.llm_traces")
    assert "no_such_column" not in columns_of("nlp.llm_traces")
    assert "created_at" in columns_of("raw.items")


@pytest.mark.parametrize(
    ("key", "hub", "other_columns"),
    [
        ("item_id", "raw.items", ()),
        ("source_id", "ref.sources", ()),
        ("entity_id", "ref.entities", ("winner_entity_id",)),
        ("storyline_id", "nlp.storylines", ()),
        ("race_key", "ref.races", ()),
    ],
)
def test_a_table_with_a_hub_key_declares_the_relation(key, hub, other_columns):
    holders = {
        table for table in TABLES if table != hub and key in columns_of(table)
    }
    declared_tables = {
        r.table for r in RELATIONS if r.target == hub and r.column == key
    }
    assert holders == declared_tables
    for column in other_columns:
        assert any(r.column == column and r.target == hub for r in RELATIONS)


def test_the_hub_counts_match_the_tables():
    count = {
        key: sum(key in columns_of(table) for table in TABLES)
        for key in ("item_id", "source_id", "entity_id", "storyline_id")
    }
    assert count == {
        "item_id": 15,
        "source_id": 7,
        "entity_id": 4,
        "storyline_id": 4,
    }


def test_call_id_is_not_declared_across_its_two_meanings():
    for declared_relation in RELATIONS:
        pair = {declared_relation.table, declared_relation.target}
        assert not (
            "nlp.race_calls" in pair
            and pair & {"nlp.llm_calls", "nlp.llm_traces"}
        )
    assert "call_id" in columns_of("nlp.race_calls")
    assert "call_id" in columns_of("nlp.llm_calls")


# Each relation's kind, from what the writers do.


def record_key(table):
    return [part.strip() for part in TABLE_KEYS[table][0].split(",")]


def expected_kind(table, column, target, target_column, held_as):
    one_source = record_key(table) == [column]
    one_target = record_key(target) == [target_column]
    if held_as == "list":
        return "many to many" if not one_target else None
    return {
        (True, True): "one to one",
        (False, True): "many to one",
        (True, False): "one to many",
        (False, False): "many to many",
    }[(one_source, one_target)]


@pytest.mark.parametrize(
    "row", relation_rows(), ids=lambda row: f"{row['source']}>{row['target']}"
)
def test_a_kind_agrees_with_each_tables_record_key(row):
    table, column = row["source"].rsplit(".", 1)
    target, target_column = row["target"].rsplit(".", 1)
    held_as = row.get("held_as", "value")
    if held_as in {"list", "JSON"}:
        assert row["kind"].endswith("to many")
        return
    assert row["kind"] == expected_kind(
        table, column, target, target_column, held_as
    )


async def test_one_batch_id_can_have_two_traces(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    client = LlmClient(
        settings,
        writer,
        Budget(settings),
        client=FakeAnthropic(["not json", '{"paragraphs": []}']),
    )
    await client.structured(
        "classify_a",
        Brief,
        "system",
        "user",
        batch_id="batch1",
        item_ids=["a"],
    )
    writer.flush()
    traces = conn.execute(
        "SELECT count(*) FROM lake.nlp.llm_traces WHERE batch_id = 'batch1'"
    ).fetchone()[0]
    conn.close()
    assert traces == 2
    row = next(
        row
        for row in relation_rows()
        if row["source"] == "nlp.classify_a.batch_id"
    )
    assert row["kind"] == "many to many"


def test_a_group_has_many_rows_so_nearest_id_is_many_to_many():
    assert record_key("nlp.dup_groups") == ["item_id"]
    row = next(
        row
        for row in relation_rows()
        if row["target"] == "nlp.dup_groups.dup_group_id"
    )
    assert row["kind"] == "many to many"


@pytest.mark.parametrize(
    "source",
    [
        "raw.fetch_details.fetch_id",
        "nlp.brief_stats.brief_id",
        "nlp.llm_traces.call_id",
    ],
)
def test_these_relations_are_one_to_one(source):
    row = next(row for row in relation_rows() if row["source"] == source)
    assert row["kind"] == "one to one"


# What the list covers.

LIST_MUST_HOLD = [
    ("ref.races.candidates", "ref.entities.entity_id"),
    ("nlp.classify_a.states", "ref.states.usps"),
    ("nlp.storylines.states", "ref.states.usps"),
    ("nlp.briefs.citations", "raw.items.item_id"),
    ("nlp.storylines.citations", "raw.items.item_id"),
    ("nlp.llm_traces.batch_id", "raw.items.item_id"),
    ("nlp.llm_traces.batch_id", "nlp.storylines.storyline_id"),
    ("nlp.llm_traces.batch_id", "nlp.briefs.brief_id"),
    ("raw.items.feed_url", "raw.fetches.feed_url"),
    ("raw.sightings.feed_url", "raw.fetches.feed_url"),
    ("raw.archive_index.key", "raw.fetches.feed_url"),
    ("ref.sources.feed_urls", "raw.fetches.feed_url"),
    ("ref.sources.home_state", "ref.states.usps"),
    ("ref.races.state", "ref.states.usps"),
    ("ref.entities.state", "ref.states.usps"),
    ("nlp.extract_b.raw", "ref.races.race_key"),
    ("raw.collection_gaps.gap_end", "raw.items.first_seen_at"),
]
NOT_KEYS = {
    "raw.items.feed_categories",
    "raw.items.raw_entry",
    "nlp.classify_a.levels",
    "nlp.classify_a.race_types",
    "nlp.classify_a.topics",
    "nlp.classify_a.free_tags",
    "nlp.extract_b.topics_refined",
    "nlp.embeddings.vec",
    "nlp.storylines.keywords",
    "nlp.storylines.centroid",
    "nlp.health.stages",
    "ref.entities.aliases",
}


@pytest.mark.parametrize(("source", "target"), LIST_MUST_HOLD)
def test_the_list_holds_each_key_the_writers_keep(source, target):
    pairs = {(row["source"], row["target"]) for row in relation_rows()}
    assert (source, target) in pairs


def test_a_list_or_json_column_is_a_key_or_named_as_not_one():
    held = {r.source_text for r in RELATIONS if r.held_as in {"list", "JSON"}}
    found = {
        f"{table}.{column}"
        for table, columns in TABLES.items()
        for column, kind in re.findall(r"(\w+)\s+([A-Z]+(?:\[\])?)", columns)
        if kind.endswith("[]") or kind == "JSON"
    }
    assert found - held == NOT_KEYS


def test_only_key_lines_for_declared_relations_are_on_the_map():
    on_map = {
        (r.target, r.table)
        for r in RELATIONS
        if r.on_map and r.held_as == "value"
    }
    assert on_map >= DRAWN_KEYS


def test_the_list_marks_how_a_key_is_held():
    rows = {(r["source"], r["target"]): r for r in relation_rows()}
    assert (
        rows[("ref.races.candidates", "ref.entities.entity_id")]["held_as"]
        == "list"
    )
    assert (
        rows[("nlp.briefs.citations", "raw.items.item_id")]["held_as"]
        == "JSON"
    )
    assert (
        rows[("raw.collection_gaps.gap_end", "raw.items.first_seen_at")][
            "held_as"
        ]
        == "time range"
    )


def test_config_values_are_the_keys_the_list_says():
    races = yaml.safe_load((ROOT / "config/races.yaml").read_text())["races"]
    assert all(
        re.fullmatch(r"[0-9a-f]{16}", candidate)
        for race in races
        for candidate in race.get("candidates") or []
    )
    states = {race["state"] for race in races}
    assert all(re.fullmatch(r"[A-Z]{2}", state) for state in states)
    with (ROOT / "config/sources.csv").open(encoding="utf-8") as handle:
        homes = {row["home_state"] for row in csv.DictReader(handle)}
    assert all(re.fullmatch(r"[A-Z]{2}", home) for home in homes if home)


async def test_a_writer_puts_each_kind_of_id_in_batch_id(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    calls = []

    class Stop(Exception):
        pass

    class Capturing:
        def should_attempt(self):
            return True

        async def structured(self, purpose, schema, *args, **kwargs):
            calls.append((purpose, kwargs["batch_id"]))
            if purpose == "extract_b":
                raise Stop
            return schema.model_validate(ANSWERS[purpose])

    parser = extract_b.ExtractParser.__new__(extract_b.ExtractParser)
    parser.llm = Capturing()
    parser.system = "system"
    parser._user_message = lambda _item, _text: "user"
    with pytest.raises(Stop):
        await parser._call(Item("item-1"), "text")
    assert calls == [("extract_b", "item-1")]
    calls.clear()

    story_parser = StorylineParser(settings, None, writer, Capturing())
    vector = np.ones(384, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    now = datetime.now(UTC)
    storyline, _ = story_parser.engine.assign(
        "item-1", vector, "Title", "s", now, [], None
    )
    await story_parser._label(storyline)
    assert calls == [("storylines", storyline.storyline_id)]
    calls.clear()

    brief_writer = BriefWriter(settings, None, writer, Capturing())
    brief_writer._gather = fake_gather
    await brief_writer.write(now=now)
    assert [purpose for purpose, _ in calls] == ["briefs"]
    stats = writer._buffers["nlp.brief_stats"]
    assert stats[0]["brief_id"] == calls[0][1]
    conn.close()
    for purpose, target in (
        ("extract_b", "raw.items"),
        ("storylines", "nlp.storylines"),
        ("briefs", "nlp.briefs"),
    ):
        found = relation("nlp.llm_traces.batch_id", target)
        assert found.condition == f"purpose = '{purpose}'"


ANSWERS = {
    "storylines": {
        "label": "A label",
        "summary": "A claim.[1]",
        "citations": ["1"],
    },
    "briefs": {
        "paragraphs": [
            {"sentences": [{"text": "A claim.", "citations": ["1"]}]}
        ]
    },
}


async def fake_gather(start, end, focus):
    items = [
        {"ref": str(n), "headline": "h", "summary": "s", "outlet": "o"}
        for n in range(1, 8)
    ]
    references = {str(n): f"item{n}" for n in range(1, 8)}
    return BriefInput(items, [], references)


# Conditions a relation holds under.


def test_a_place_from_the_model_holds_its_text_so_the_key_is_conditional():
    parser = extract_b.ExtractParser.__new__(extract_b.ExtractParser)
    parser.sources = {}
    parser.name = "extract_b"
    parser.version = "1"
    parser.gazetteer = Gazetteer([], [])
    thing = SimpleNamespace(item_id="i1", source_id="s")
    rows = parser._place_rows(
        thing,
        [
            PlaceB(
                name="Atlanta",
                place_type="city",
                state="Georgia",
                district=None,
            )
        ],
        [],
    )
    assert [(r["method"], r["state_usps"]) for r in rows] == [
        ("llm", "Georgia")
    ]
    assert relation("nlp.places.state_usps", "ref.states").condition == (
        "method <> 'llm'"
    )


def test_a_ballot_measure_key_is_not_looked_up_so_the_key_is_conditional():
    parser = extract_b.ExtractParser.__new__(extract_b.ExtractParser)
    parser.known_races = {"2026-GA-SEN"}
    key = parser._race_key("CA", "Proposition 50 ballot measure", None)
    assert "-M-" in key
    assert key not in parser.known_races
    assert relation("nlp.race_calls.race_key", "ref.races").condition == (
        "race_key NOT LIKE '%-M-%'"
    )


async def test_a_brief_with_no_sentence_writes_stats_and_no_brief(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)

    class Rejected:
        async def structured(self, *args, **kwargs):
            return Brief.model_validate(
                {
                    "paragraphs": [
                        {
                            "sentences": [
                                {"text": "A claim.", "citations": ["999"]}
                            ]
                        }
                    ]
                }
            )

    brief_writer = BriefWriter(settings, None, writer, Rejected())
    brief_writer._gather = fake_gather
    await brief_writer.write(now=datetime.now(UTC))
    conn.close()
    assert writer._buffers["nlp.brief_stats"][0]["sentences_kept"] == 0
    assert "nlp.briefs" not in writer._buffers
    assert relation("nlp.brief_stats.brief_id", "nlp.briefs").condition == (
        "sentences_kept > 0"
    )


def test_nearest_id_holds_a_storyline_at_the_storyline_stage():
    engine = StorylineEngine(Settings())
    vector = np.ones(384, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    now = datetime.now(UTC)
    first, _ = engine.assign("a", vector, "T", "s", now, [], None)
    engine.assign("b", vector, "T", "s", now, [], None)
    assert engine.last_decision["nearest_id"] == first.storyline_id
    found = next(
        r
        for r in RELATIONS
        if r.column == "nearest_id" and r.target == "nlp.storylines"
    )
    assert found.condition == "stage = 'storylines'"


def test_nearest_id_holds_a_group_at_the_dedupe_stage():
    index = DedupeIndex(Settings())
    vector = np.ones(384, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    now = datetime.now(UTC)
    index.add(None, vector, now, "2026-11-03", "group-1", "item-1")
    index.find(None, vector, now, "2026-11-03", "item-2")
    assert index.last_nearest[0] == "group-1"
    found = next(
        r
        for r in RELATIONS
        if r.column == "nearest_id" and r.target == "nlp.dup_groups"
    )
    assert found.condition == "stage = 'dedupe'"


# Cards and their headings.


def test_each_table_in_a_card_holds_a_column_its_heading_names():
    members = card_members()
    for card in CARDS:
        assert all(key in card.heading for key in card.keys), card.name
        for table in members[card.name]:
            assert columns_of(table) & set(card.keys), (card.name, table)


def test_the_geography_heading_does_not_call_fips_a_shared_key():
    card = next(card for card in CARDS if card.name == "geography")
    assert "fips" not in card.heading
    assert "fips" in columns_of("ref.states")
    assert "fips" in columns_of("ref.counties")
    joined = {
        (r.table, r.target)
        for r in RELATIONS
        if r.column == "fips" or r.target_column == "fips"
    }
    assert ("ref.counties", "ref.states") not in joined
    assert ("ref.states", "ref.counties") not in joined


def test_a_conditional_key_says_so_on_the_map():
    page = str(table_map())
    drawn = read_map(page)
    box = drawn.box_of("raw.archive_index")
    assert box.heading == "key, when kind = &#x27;pages&#x27;"
    assert "raw.sightings" not in drawn.tables_in(box)
    assert "key holds an item_id." in page


def test_tables_with_no_shared_key_say_only_what_is_true():
    assert under_map.other_tables("raw") == ["raw.collection_gaps"]
    assert sorted(under_map.other_tables("nlp")) == [
        "nlp.health",
        "nlp.maintenance_runs",
    ]
    assert under_map.other_tables("ref") == []
    page = str(table_map())
    drawn = read_map(page)
    assert drawn.box_of("raw.collection_gaps").heading == "no shared key"
    assert "joined to raw.items by time range" in page
    assert drawn.box_of("nlp.health").heading == under_map.ALONE_HEADING


def test_collection_gaps_really_joins_items_by_time_range():
    assert "lake.raw.collection_gaps g" in FACTS_SELECT
    assert "i.first_seen_at BETWEEN g.gap_end" in FACTS_SELECT
    found = relation("raw.collection_gaps.gap_end", "raw.items")
    assert found.held_as == "time range"
    assert "first_seen_at BETWEEN" in found.condition


def test_the_tables_that_stand_alone_are_joined_by_nothing():
    readers = {}
    for path in (ROOT / "src/monitor").rglob("*"):
        if path.suffix not in {".py", ".sql"}:
            continue
        text = path.read_text(encoding="utf-8")
        for table in ("nlp.health", "nlp.maintenance_runs"):
            if f"lake.{table}" in text:
                readers[(path.name, table)] = text
    assert set(readers) == {
        ("under_maintenance.sql", "nlp.maintenance_runs"),
    }
    for text in readers.values():
        assert "JOIN" not in text.upper()


@pytest.mark.parametrize("schema", ["raw", "nlp", "ref", "new"])
@pytest.mark.parametrize(
    ("target", "column"),
    [
        ("ref.sources", "source_id"),
        ("ref.states", "state_usps"),
        ("raw.items", "item_id"),
        ("nlp.briefs", "brief_id"),
    ],
)
def test_a_box_label_stays_true_for_a_new_table(
    monkeypatch, fresh_map, schema, target, column
):
    table = f"{schema}.brand_new"
    monkeypatch.setitem(TABLES, table, f"{column} TEXT")
    monkeypatch.setattr(
        under_map,
        "RELATIONS",
        (
            *RELATIONS,
            under_map.Relation(
                table,
                column,
                target,
                {"ref.states": "usps"}.get(target, column),
                "many to one",
            ),
        ),
    )
    drawn = read_map(str(table_map()))
    box = drawn.box_of(table)
    if box.css == "tm-alone":
        assert "no shared key" not in box.heading
    assert line_problems(str(table_map())) == []


def test_a_new_table_with_no_relation_can_stand_alone(monkeypatch, fresh_map):
    monkeypatch.setitem(TABLES, "nlp.brand_new", "name TEXT")
    assert "nlp.brand_new" in under_map.other_tables("nlp")
    page = str(table_map())
    assert link("nlp.brand_new") in page
    box = read_map(page).box_of("nlp.brand_new")
    assert box.heading == under_map.ALONE_HEADING


def test_a_new_relation_moves_a_table_into_its_card(monkeypatch, fresh_map):
    monkeypatch.setitem(TABLES, "nlp.brand_new", "item_id TEXT")
    monkeypatch.setattr(
        under_map,
        "RELATIONS",
        (
            *RELATIONS,
            under_map.Relation(
                "nlp.brand_new",
                "item_id",
                "raw.items",
                "item_id",
                "many to one",
            ),
        ),
    )
    assert "nlp.brand_new" in card_members()["nlp_items"]
    assert "nlp.brand_new" not in under_map.other_tables("nlp")
    assert link("nlp.brand_new") in str(table_map())


def test_gold_tables_are_shown_as_derived():
    assert set(gold_tables()) == {t for t in TABLES if t.startswith("gold.")}
    page = str(table_map())
    assert DERIVED_HEADING in page
    assert "tm-derived" in page
    assert not any(r.target.startswith("gold.") for r in RELATIONS), (
        "A gold table is a result, so nothing keys to it."
    )


def test_one_trunk_carries_item_id():
    page = str(table_map())
    assert page.count('class="tm-trunk"') == 2  # the line and its legend entry
    members = card_members()
    assert len(members["nlp_items"]) == 12
    assert members["raw_items"][0] == "raw.items"
    assert all(
        "item_id" not in row_keys(t, ("item_id",)).split(", ")
        for t in members["nlp_items"]
    )


def test_a_list_column_on_the_map_is_many_to_many():
    assert "item_ids TEXT[]" in TABLES["nlp.llm_traces"]
    many = [r for r in RELATIONS if r.held_as == "list" and r.on_map]
    assert [r.source_text for r in many] == ["nlp.llm_traces.item_ids"]
    assert "tm-many" in str(table_map())


# Layout, measured from what the boxes hold.


def test_the_default_map_has_no_clipping_or_overlap():
    assert clip_problems(str(table_map())) == []


GROWTH = [
    ("raw", None),
    ("nlp", None),
    ("ref", None),
    ("new", None),
    ("gold", None),
    ("raw", ("raw.items", "item_id", "item_id")),
    ("raw", ("raw.fetches", "fetch_id", "fetch_id")),
    ("nlp", ("raw.items", "item_id", "item_id")),
    ("nlp", ("nlp.storylines", "storyline_id", "storyline_id")),
    ("nlp", ("nlp.llm_traces", "call_id", "call_id")),
    ("ref", ("ref.sources", "source_id", "source_id")),
    ("ref", ("ref.entities", "entity_id", "entity_id")),
    ("ref", ("ref.states", "state_usps", "usps")),
    ("nlp", ("ref.sources", "source_id", "source_id")),
]


@pytest.mark.parametrize("count", [1, 3, 6, 12])
@pytest.mark.parametrize(
    ("schema", "joined"),
    GROWTH,
    ids=lambda value: value[0] if isinstance(value, tuple) else value,
)
def test_new_tables_grow_the_view_box_and_nothing_is_cut_off(
    monkeypatch, fresh_map, schema, joined, count
):
    names = [f"{schema}.brand_new_{n}" for n in range(count)]
    added = []
    for name in names:
        monkeypatch.setitem(
            TABLES,
            name,
            f"{joined[1]} TEXT" if joined else "name TEXT",
        )
        if joined:
            added.append(
                under_map.Relation(
                    name, joined[1], joined[0], joined[2], "many to one"
                )
            )
    monkeypatch.setattr(under_map, "RELATIONS", (*RELATIONS, *added))
    page = str(table_map())
    for name in names:
        assert link(name) in page
    assert clip_problems(page) == []
    assert through_problems(page) == []
    assert line_problems(page) == []


def test_a_row_too_narrow_for_its_keys_wraps_and_its_box_grows(
    monkeypatch, fresh_map
):
    name = "nlp.a_table_with_quite_a_long_name"
    before = read_map(str(table_map())).boxes
    monkeypatch.setitem(TABLES, name, "item_id TEXT, " + "source_id TEXT")
    monkeypatch.setattr(
        under_map,
        "RELATIONS",
        (
            *RELATIONS,
            under_map.Relation(
                name, "item_id", "raw.items", "item_id", "many to one"
            ),
            *[
                under_map.Relation(
                    name,
                    f"very_long_key_column_{n}",
                    "ref.sources",
                    "source_id",
                    "many to one",
                )
                for n in range(2)
            ],
        ),
    )
    table_map.cache_clear()
    page = str(table_map())
    row = read_map(page).rows[name]
    assert row.bottom - row.y > 25
    assert clip_problems(page) == []
    assert len(read_map(page).boxes) == len(before)


def test_every_box_in_the_side_column_has_a_schema_heading():
    page = str(table_map())
    drawn = read_map(page)
    side = [box for box in drawn.boxes if box.x == under_map.SIDE_X]
    below_band = [box for box in side if box.y > drawn.box_of("ref.states").y]
    top = min(box.y for box in below_band)
    assert any(
        x == under_map.SIDE_X and "nlp" in text and top - 30 < y < top
        for text, x, y in drawn.schema_headings
    )


def test_the_item_id_label_clears_both_boxes():
    drawn = read_map(str(table_map()))
    raw = drawn.box_of("raw.items")
    nlp = drawn.box_of("nlp.mentions")
    text, x, y, anchor = next(
        label for label in drawn.labels if label[0] == "item_id"
    )
    assert anchor == "middle"
    half = len(text) * 7.2 / 2
    assert x - half - raw.right >= 8
    assert nlp.x - (x + half) >= 8
    trunk = next(runs for css, runs in drawn.lines if css == "tm-trunk")[0]
    arrow_half_height = 10.5
    assert y + 4 <= trunk[-1][1] - arrow_half_height


def test_the_list_note_does_not_sit_on_its_line():
    drawn = read_map(str(table_map()))
    run = next(runs for css, runs in drawn.lines if css == "tm-many")[0]
    line_y = max(y for _, y in run)
    _, _, y, _ = next(
        label for label in drawn.labels if "is a list" in label[0]
    )
    assert line_y - y >= 7
    assert y - 14 >= drawn.box_of("nlp.mentions").bottom


def test_the_key_text_meets_the_projector_floor_at_1280():
    sizes = {
        css: float(size)
        for css, size in re.findall(
            r"\.(tm-\w+) \{[^}]*?font: \d+ ([\d.]+)px", under_map.STYLE
        )
    }
    assert sizes["tm-keys"] >= 13.5
    assert sizes["tm-head"] >= 13.5


def test_every_line_style_is_used_and_the_legend_explains_it():
    page = str(table_map())
    legend = re.search(r'<g class="tm-legend">.*?</g>', page, flags=re.S)[0]
    drawn = {css for css, _ in read_map(page).lines}
    in_legend = set(re.findall(r'<path class="(tm-\w+)"', legend))
    assert (
        drawn
        == in_legend
        == {
            "tm-trunk",
            "tm-key",
            "tm-many",
            "tm-derived",
        }
    )


def test_the_map_uses_theme_tokens_with_fallbacks():
    page = str(table_map())
    tokens = re.findall(r"var\(([^)]*)\)", page)
    assert tokens
    assert all("," in token for token in tokens)
    assert not re.search(r"(fill|stroke): #", page)


# Rows the writers wrote, through their own code.


class FakeMessages:
    def __init__(self, texts):
        self.texts = list(texts)

    async def create(self, **params):
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            stop_reason="end_turn",
            model="fake-model",
        )


class FakeAnthropic:
    def __init__(self, texts):
        self.messages = FakeMessages(texts)
        self.beta = SimpleNamespace(messages=self.messages)


def unit_vector():
    vector = np.ones(384, dtype=np.float32)
    return vector / np.linalg.norm(vector)


async def write_through_real_code(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    body = (FIXTURES / "rss2.xml").read_bytes()
    archive = ArchiveWriter(settings)
    archive.start()
    poller = Poller(
        settings,
        writer,
        [Source("peach", "Peach Post", "https://example.com", [FEED_URL])],
        set(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=body)
            )
        ),
        archive=archive,
    )
    await poller.poll_once(poller.feeds[0])
    writer.flush()
    archive.stop()
    rows = conn.execute(
        "SELECT item_id, source_id, title, first_seen_at FROM lake.raw.items"
    ).fetchall()
    items = [
        Item(
            item_id,
            source_id=source_id,
            title=title,
            first_seen_at=seen,
            vec=unit_vector(),
        )
        for item_id, source_id, title, seen in rows
    ]
    database = Database(conn)
    dedupe = DedupeParser(settings, database, index=DedupeIndex(settings))
    output = await dedupe.run(items)
    for table, table_rows in output.rows.items():
        writer.add(table, table_rows)
    storylines = StorylineParser(settings, database, writer, None)
    storylines.loaded = True
    output = await storylines.run(items)
    for table, table_rows in output.rows.items():
        writer.add(table, table_rows)
    item_ids = [item_id for item_id, *_ in rows]
    answer = json.dumps(ANSWERS["briefs"])
    llm = LlmClient(
        settings, writer, Budget(settings), client=FakeAnthropic([answer])
    )
    briefs = BriefWriter(settings, database, writer, llm)

    async def gathered(start, end, focus):
        references = {
            str(n): item_id for n, item_id in enumerate(item_ids * 7, 1)
        }
        return BriefInput(list(references), [], references)

    briefs._gather = gathered
    await briefs.write(now=datetime.now(UTC))
    writer.flush()
    RollupEngine(settings, writer).run(conn, rebuild=True)
    writer.flush()
    conn.close()


@pytest.fixture(scope="module")
def written(lake_template, tmp_path_factory):
    from conftest import clone_lake

    settings = clone_lake(lake_template, tmp_path_factory.mktemp("written"))
    add_rows(settings, {"ref.sources": [source("peach", "Peach Post")]})
    asyncio.run(write_through_real_code(settings))
    return settings


WRITTEN = [
    ("raw.fetch_details.fetch_id", "raw.fetches"),
    ("raw.items.source_id", "ref.sources"),
    ("raw.fetches.source_id", "ref.sources"),
    ("raw.sightings.item_id", "raw.items"),
    ("raw.sightings.source_id", "ref.sources"),
    ("raw.items.feed_url", "raw.fetches"),
    ("raw.sightings.feed_url", "raw.fetches"),
    ("raw.archive_index.key", "raw.fetches"),
    ("raw.archive_index.source_id", "ref.sources"),
    ("nlp.dup_groups.item_id", "raw.items"),
    ("nlp.cluster_decisions.item_id", "raw.items"),
    ("nlp.cluster_decisions.nearest_id", "nlp.dup_groups"),
    ("nlp.cluster_decisions.nearest_id", "nlp.storylines"),
    ("nlp.storyline_assign.item_id", "raw.items"),
    ("nlp.storyline_assign.source_id", "ref.sources"),
    ("nlp.storyline_assign.storyline_id", "nlp.storylines"),
    ("nlp.brief_stats.brief_id", "nlp.briefs"),
    ("nlp.llm_traces.call_id", "nlp.llm_calls"),
    ("nlp.llm_traces.prompt_hash", "nlp.llm_prompts"),
    ("nlp.llm_traces.item_ids", "raw.items"),
    ("nlp.llm_traces.batch_id", "nlp.briefs"),
    ("gold.source_bins.source_id", "ref.sources"),
    ("gold.storyline_bins.storyline_id", "nlp.storylines"),
]


def join_counts(conn, found):
    """Rows whose value has a match in the target, and rows with none."""
    values = (
        f"(SELECT unnest({found.column}) AS value FROM lake.{found.table})"
        if found.held_as == "list"
        else f"(SELECT {found.column} AS value, * FROM lake.{found.table})"
    )
    where = f"WHERE {found.condition}" if found.condition else ""
    target = f"SELECT {found.target_column} FROM lake.{found.target}"
    return conn.execute(
        f"SELECT count(*) FILTER (WHERE value IN ({target})), "
        f"count(*) FILTER (WHERE value IS NOT NULL AND value "
        f"NOT IN ({target})) FROM {values} {where}"
    ).fetchone()


@pytest.mark.parametrize(("source", "target"), WRITTEN)
def test_a_relation_holds_on_rows_the_writers_wrote(written, source, target):
    conn = connect(written, read_only=True)
    try:
        matched, unmatched = join_counts(conn, relation(source, target))
    finally:
        conn.close()
    assert matched > 0
    assert unmatched == 0


def test_a_citation_names_an_item_the_writers_wrote(written):
    conn = connect(written, read_only=True)
    try:
        citations = conn.execute(
            "SELECT citations FROM lake.nlp.briefs"
        ).fetchone()[0]
        items = {
            row[0]
            for row in conn.execute(
                "SELECT item_id FROM lake.raw.items"
            ).fetchall()
        }
    finally:
        conn.close()
    assert set(json.loads(citations).values()) <= items


def test_a_feed_row_in_the_archive_holds_the_feed_url(written):
    conn = connect(written, read_only=True)
    try:
        kinds = conn.execute(
            "SELECT DISTINCT kind, key FROM lake.raw.archive_index"
        ).fetchall()
    finally:
        conn.close()
    assert kinds == [("feeds", FEED_URL)]


# A seeded sample: each key family joins on rows with matching values.


@pytest.fixture(scope="module")
def seeded(lake_template, tmp_path_factory):
    from conftest import clone_lake

    settings = clone_lake(lake_template, tmp_path_factory.mktemp("map"))
    seed(settings)
    add_rows(
        settings,
        {
            "raw.items": [item("joined", item_time())],
            "raw.fetches": [{"fetch_id": "f1", "source_id": "peach"}],
            "raw.fetch_details": [{"fetch_id": "f1", "n_bytes": 1}],
            "nlp.briefs": [{"brief_id": "b1", "focus": "all"}],
            "nlp.brief_stats": [{"brief_id": "b1", "sentences_kept": 1}],
            "nlp.llm_calls": [{"call_id": "c1", "purpose": "classify_a"}],
            "nlp.llm_prompts": [{"prompt_hash": "p1", "purpose": "x"}],
            "nlp.llm_traces": [
                {
                    "call_id": "c1",
                    "prompt_hash": "p1",
                    "batch_id": "batch1",
                    "item_ids": ["joined", "cap1"],
                }
            ],
            "nlp.classify_a": [
                {"item_id": "joined", "batch_id": "batch1", "method": "llm"}
            ],
            "nlp.mentions": [
                {"mention_id": "m1", "item_id": "joined", "entity_id": "e1"}
            ],
            "ref.entities": [{"entity_id": "e1", "race_key": "2026-GA-SEN"}],
            "nlp.race_calls": [
                {
                    "call_id": "rc1",
                    "item_id": "joined",
                    "race_key": "2026-GA-SEN",
                    "winner_entity_id": "e1",
                }
            ],
            "ref.races": [{"race_key": "2026-GA-SEN"}],
            "raw.archive_index": [
                {"kind": "pages", "key": "joined", "source_id": "peach"},
                {"kind": "feeds", "key": "https://example.com/feed.xml"},
            ],
            "ref.counties": [{"fips": "13121", "state_usps": "GA"}],
            "ref.places": [{"geoid": "1304000", "state_usps": "GA"}],
            "nlp.places": [
                {
                    "item_id": "joined",
                    "state_usps": "GA",
                    "county_fips": "13121",
                    "place_geoid": "1304000",
                    "method": "gazetteer",
                }
            ],
            "gold.source_bins": [{"source_id": "peach", "n_items": 1}],
            "gold.entity_bins": [{"entity_id": "e1", "n_mentions": 1}],
            "gold.county_bins": [{"county_fips": "13121", "n_items": 1}],
            "nlp.storylines": [{"storyline_id": "s1", "version": 2}],
            "gold.storyline_bins": [{"storyline_id": "s1", "n_items": 1}],
        },
    )
    return settings


def item_time():
    return datetime.now(UTC)


def join_sql(found):
    source = f"lake.{found.table}"
    if found.held_as == "list":
        source = (
            f"(SELECT unnest({found.column}) AS {found.column} "
            f"FROM lake.{found.table})"
        )
    where = f"WHERE {found.condition}" if found.condition else ""
    return (
        f"SELECT count(*) FROM (SELECT * FROM {source} {where}) a "
        f"JOIN lake.{found.target} b "
        f"ON a.{found.column} = b.{found.target_column}"
    )


SAMPLED = [
    (source, target)
    for source, target in [
        ("raw.archive_index.key", "raw.items"),
        ("raw.items.source_id", "ref.sources"),
        ("raw.fetch_details.fetch_id", "raw.fetches"),
        ("nlp.mentions.entity_id", "ref.entities"),
        ("nlp.mentions.item_id", "raw.items"),
        ("nlp.race_calls.winner_entity_id", "ref.entities"),
        ("nlp.race_calls.race_key", "ref.races"),
        ("nlp.brief_stats.brief_id", "nlp.briefs"),
        ("nlp.llm_traces.call_id", "nlp.llm_calls"),
        ("nlp.llm_traces.prompt_hash", "nlp.llm_prompts"),
        ("nlp.llm_traces.item_ids", "raw.items"),
        ("nlp.classify_a.batch_id", "nlp.llm_traces"),
        ("nlp.places.county_fips", "ref.counties"),
        ("nlp.places.place_geoid", "ref.places"),
        ("nlp.places.state_usps", "ref.states"),
        ("ref.entities.race_key", "ref.races"),
        ("ref.counties.state_usps", "ref.states"),
        ("gold.source_bins.source_id", "ref.sources"),
        ("gold.entity_bins.entity_id", "ref.entities"),
        ("gold.county_bins.county_fips", "ref.counties"),
        ("gold.storyline_bins.storyline_id", "nlp.storylines"),
    ]
]


def test_the_sample_covers_each_key_family():
    assert {target for _, target in SAMPLED} >= {
        "raw.items",
        "ref.sources",
        "ref.entities",
        "ref.races",
        "ref.states",
        "ref.counties",
        "ref.places",
        "nlp.storylines",
        "nlp.llm_calls",
        "nlp.llm_prompts",
        "nlp.llm_traces",
        "nlp.briefs",
        "raw.fetches",
    }


@pytest.mark.parametrize(("source", "target"), SAMPLED)
def test_a_sampled_relation_joins_on_a_seeded_lake(seeded, source, target):
    conn = connect(seeded, read_only=True)
    try:
        joined = conn.execute(join_sql(relation(source, target))).fetchone()[0]
    finally:
        conn.close()
    assert joined > 0


def test_the_condition_keeps_a_feed_url_out_of_the_item_join(seeded):
    found = relation("raw.archive_index.key", "raw.items")
    conn = connect(seeded, read_only=True)
    try:
        rows = conn.execute(
            f"SELECT count(*) FROM lake.{found.table} WHERE {found.condition}"
        ).fetchone()[0]
        total = conn.execute(
            f"SELECT count(*) FROM lake.{found.table}"
        ).fetchone()[0]
    finally:
        conn.close()
    assert 0 < rows < total


# The page around the map.


@pytest.fixture(scope="module")
def page(seeded):
    with app_client(seeded) as client:
        yield client.get("/under/lake").text


def test_the_page_holds_the_map_near_the_top(page):
    assert page.index('id="map"') < page.index('id="how"')
    assert page.index('id="map"') < page.index('id="tables"')


def test_the_note_says_what_the_lake_lacks(page):
    assert (
        "DuckLake has no foreign keys, primary keys, or unique constraints"
        in page
    )
    assert "a value the code keeps equal in both" in page


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("CREATE TABLE keyed (x INT PRIMARY KEY)", "not supported"),
        ("CREATE TABLE keyed (x INT UNIQUE)", "not supported"),
        (
            "CREATE TABLE keyed "
            "(x TEXT, FOREIGN KEY (x) REFERENCES items (item_id))",
            "no primary key or unique constraint",
        ),
    ],
)
def test_ducklake_rejects_the_constraints_the_note_names(
    settings, statement, reason
):
    conn = connect(settings)
    try:
        conn.execute("USE lake.raw")
        with pytest.raises(duckdb.Error, match=reason):
            conn.execute(statement)
    finally:
        conn.close()


def test_the_note_names_a_record_key_that_differs_from_the_line(page):
    assert "newest row per record key" in page
    assert "one key has many rows" not in page
    assert "<code>nlp.mentions</code>" in page
    assert "<code>mention_id</code>" in page
    assert record_key("nlp.mentions") == ["mention_id"]
    assert relation("nlp.mentions.item_id", "raw.items").column == "item_id"
    assert "PARTITION BY m.mention_id" in MENTIONS_SQL


def test_the_page_lists_every_relation_in_words(page):
    for declared_relation in RELATIONS:
        assert f"<code>{declared_relation.source_text}</code>" in page
        assert f"<code>{declared_relation.target_text}</code>" in page


def test_the_list_says_what_it_covers_and_leaves_out(page):
    assert "This list covers every key between lake tables" in page
    assert "The map draws the main ones" in page
    assert "<code>bin_start</code>" in page


def test_a_condition_is_in_the_same_row_as_its_relation(page):
    rows = re.findall(r"<tr><td><code>.*?</tr>", page, flags=re.S)
    conditional = [r for r in RELATIONS if r.condition]
    assert conditional
    for found in conditional:
        wanted = escape(found.condition)
        assert any(
            f"<code>{found.source_text}</code>" in row
            and f"<code>{found.target_text}</code>" in row
            and f"<code>{wanted}</code>" in row
            for row in rows
        ), found.source_text


def test_the_notes_show_no_markdown(page):
    assert all("`" not in r.note for r in RELATIONS)


def test_a_map_link_opens_the_table_browser_entry(page, seeded):
    with app_client(seeded) as client:
        html = client.get("/under/lake?table=nlp.mentions").text
    assert 'id="detail"' in html
    assert '<h2 class="u-section-title">nlp.mentions</h2>' in html
    assert link("nlp.mentions") in page
