"""The table map: its declared relations, and the diagram drawn from them."""

import re
from datetime import UTC, datetime

import numpy as np
import pytest
from test_under import add_rows, app_client, item, seed

from monitor.lake import TABLES, connect
from monitor.parsers.dedupe import DedupeIndex
from monitor.parsers.storylines import StorylineEngine
from monitor.settings import Settings
from monitor.web import under_map
from monitor.web.under_map import (
    ALONE_HEADING,
    DERIVED_HEADING,
    RELATIONS,
    alone_tables,
    card_members,
    columns_of,
    gold_tables,
    row_keys,
    table_map,
)


@pytest.fixture
def fresh_map():
    table_map.cache_clear()
    yield
    table_map.cache_clear()


def link(table):
    return f'href="?table={table}#detail"'


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
    declared = {
        r.table for r in RELATIONS if r.target == hub and r.column == key
    }
    assert holders == declared
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
    for relation in RELATIONS:
        pair = {relation.table, relation.target}
        assert not (
            "nlp.race_calls" in pair
            and pair & {"nlp.llm_calls", "nlp.llm_traces"}
        )
    assert "call_id" in columns_of("nlp.race_calls")
    assert "call_id" in columns_of("nlp.llm_calls")


def test_a_list_column_is_declared_many_to_many():
    assert "item_ids TEXT[]" in TABLES["nlp.llm_traces"]
    many = [r for r in RELATIONS if r.many_to_many]
    assert [r.source_text for r in many] == ["nlp.llm_traces.item_ids"]
    assert "tm-many" in str(table_map())


def test_nearest_id_holds_a_storyline_at_the_storyline_stage():
    engine = StorylineEngine(Settings())
    vector = np.ones(384, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    now = datetime.now(UTC)
    first, _ = engine.assign("a", vector, "T", "s", now, [], None)
    engine.assign("b", vector, "T", "s", now, [], None)
    assert engine.last_decision["nearest_id"] == first.storyline_id
    relation = next(
        r
        for r in RELATIONS
        if r.column == "nearest_id" and r.target == "nlp.storylines"
    )
    assert relation.condition == "stage = 'storylines'"


def test_nearest_id_holds_a_group_at_the_dedupe_stage():
    index = DedupeIndex(Settings())
    vector = np.ones(384, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    now = datetime.now(UTC)
    index.add(None, vector, now, "2026-11-03", "group-1", "item-1")
    index.find(None, vector, now, "2026-11-03", "item-2")
    assert index.last_nearest[0] == "group-1"
    relation = next(
        r
        for r in RELATIONS
        if r.column == "nearest_id" and r.target == "nlp.dup_groups"
    )
    assert relation.condition == "stage = 'dedupe'"


def test_tables_with_no_shared_key_stand_alone():
    assert alone_tables("raw") == ["raw.collection_gaps"]
    assert sorted(alone_tables("nlp")) == [
        "nlp.health",
        "nlp.maintenance_runs",
    ]
    assert alone_tables("ref") == []
    assert str(table_map()).count(ALONE_HEADING) == 2


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


def test_a_new_table_appears_with_no_change_here(monkeypatch, fresh_map):
    monkeypatch.setitem(TABLES, "nlp.brand_new", "name TEXT")
    assert "nlp.brand_new" in alone_tables("nlp")
    assert link("nlp.brand_new") in str(table_map())


def test_a_new_relation_moves_a_table_into_its_card(monkeypatch, fresh_map):
    monkeypatch.setitem(TABLES, "nlp.brand_new", "item_id TEXT")
    monkeypatch.setattr(
        under_map,
        "RELATIONS",
        (
            *RELATIONS,
            under_map.Relation(
                "nlp.brand_new", "item_id", "raw.items", "item_id"
            ),
        ),
    )
    assert "nlp.brand_new" in card_members()["nlp_items"]
    assert "nlp.brand_new" not in alone_tables("nlp")
    assert link("nlp.brand_new") in str(table_map())


def card_rects(page):
    """Each card's rectangle in the SVG, as (x, y, right, bottom)."""
    return [
        (int(x), int(y), int(x) + int(w), int(y) + int(h))
        for x, y, w, h in re.findall(
            r'<rect class="tm-(?:card|alone|gold)" x="(\d+)" y="(\d+)" '
            r'width="(\d+)" height="(\d+)"',
            page,
        )
    ]


def assert_apart(rects):
    for number, first in enumerate(rects):
        for second in rects[number + 1 :]:
            assert (
                first[2] <= second[0]
                or second[2] <= first[0]
                or first[3] <= second[1]
                or second[3] <= first[1]
            ), (first, second)


def test_cards_do_not_overlap():
    rects = card_rects(str(table_map()))
    assert len(rects) == len(under_map.CARDS) + 3
    assert_apart(rects)


@pytest.mark.parametrize("schema", ["raw", "nlp", "ref", "gold", "new"])
def test_many_new_tables_still_fit_with_no_overlap(
    monkeypatch, fresh_map, schema
):
    names = [f"{schema}.brand_new_{n}" for n in range(6)]
    for name in names:
        monkeypatch.setitem(TABLES, name, "name TEXT")
    page = str(table_map())
    for name in names:
        assert link(name) in page
    assert_apart(card_rects(page))


def test_no_line_style_means_two_things():
    page = str(table_map())
    for css in ("tm-trunk", "tm-key", "tm-many", "tm-derived"):
        assert f'class="{css}"' in page


def test_the_map_uses_theme_tokens_with_fallbacks():
    page = str(table_map())
    tokens = re.findall(r"var\(([^)]*)\)", page)
    assert tokens
    assert all("," in token for token in tokens)
    assert not re.search(r"(fill|stroke): #", page)


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


def join_sql(relation):
    source = f"lake.{relation.table}"
    if relation.many_to_many:
        source = (
            f"(SELECT unnest({relation.column}) AS {relation.column} "
            f"FROM lake.{relation.table})"
        )
    where = f"WHERE {relation.condition}" if relation.condition else ""
    return (
        f"SELECT count(*) FROM (SELECT * FROM {source} {where}) a "
        f"JOIN lake.{relation.target} b "
        f"ON a.{relation.column} = b.{relation.target_column}"
    )


SAMPLED = [
    relation
    for relation in RELATIONS
    if relation.source_text
    in {
        "raw.archive_index.key",
        "raw.items.source_id",
        "raw.fetch_details.fetch_id",
        "nlp.mentions.entity_id",
        "nlp.mentions.item_id",
        "nlp.race_calls.winner_entity_id",
        "nlp.race_calls.race_key",
        "nlp.storyline_assign.storyline_id",
        "nlp.brief_stats.brief_id",
        "nlp.llm_traces.call_id",
        "nlp.llm_traces.prompt_hash",
        "nlp.llm_traces.item_ids",
        "nlp.classify_a.batch_id",
        "nlp.places.county_fips",
        "nlp.places.place_geoid",
        "nlp.places.state_usps",
        "ref.entities.race_key",
        "ref.counties.state_usps",
        "gold.source_bins.source_id",
        "gold.entity_bins.entity_id",
        "gold.county_bins.county_fips",
        "gold.storyline_bins.storyline_id",
    }
]


def test_the_sample_covers_each_key_family():
    targets = {relation.target for relation in SAMPLED}
    assert targets >= {
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


@pytest.mark.parametrize("relation", SAMPLED, ids=lambda r: r.source_text)
def test_a_sampled_relation_joins_on_a_seeded_lake(seeded, relation):
    conn = connect(seeded, read_only=True)
    try:
        joined = conn.execute(join_sql(relation)).fetchone()[0]
    finally:
        conn.close()
    assert joined > 0


def test_the_condition_keeps_a_feed_url_out_of_the_item_join(seeded):
    relation = next(r for r in RELATIONS if r.column == "key")
    conn = connect(seeded, read_only=True)
    try:
        rows = conn.execute(
            f"SELECT count(*) FROM lake.{relation.table} "
            f"WHERE {relation.condition}"
        ).fetchone()[0]
        total = conn.execute(
            f"SELECT count(*) FROM lake.{relation.table}"
        ).fetchone()[0]
    finally:
        conn.close()
    assert 0 < rows < total


@pytest.fixture(scope="module")
def page(seeded):
    with app_client(seeded) as client:
        yield client.get("/under/lake").text


def test_the_page_holds_the_map_near_the_top(page):
    assert page.index('id="map"') < page.index('id="how"')
    assert page.index('id="map"') < page.index('id="tables"')


def test_the_page_says_nothing_enforces_the_relations(page):
    assert "DuckLake has no foreign keys" in page
    assert "one key has many rows" in page


def test_the_page_lists_every_relation_in_words(page):
    for relation in RELATIONS:
        assert f"<code>{relation.source_text}</code>" in page
        assert f"<code>{relation.target_text}</code>" in page


def test_a_map_link_opens_the_table_browser_entry(page, seeded):
    with app_client(seeded) as client:
        html = client.get("/under/lake?table=nlp.mentions").text
    assert 'id="detail"' in html
    assert '<h2 class="u-section-title">nlp.mentions</h2>' in html
    assert link("nlp.mentions") in page
