import json
from pathlib import Path

import pytest
import yaml
from conftest import clone_lake

from monitor.lake import BatchWriter, connect, latest_sql
from monitor.ref.loader import poll_close_timestamp, sync_ref_tables
from monitor.settings import Settings

SOURCES_HEADER = (
    "source_id,name,homepage,feed_urls,scope,home_state,country,language,"
    "lean_label,lean_source,lean_source_url,lean_retrieved_on,"
    "reliability_note,ownership,state_affiliated,paywall,poll_tier,enabled,"
    "notes\n"
)
LEAN_HEADER = (
    "source_id,name,lean_label,lean_source,lean_source_url,lean_retrieved_on,"
    "adfontes_bias,adfontes_reliability,adfontes_url,confirmed,notes\n"
)


def write_inputs(settings: Settings) -> None:
    ref = settings.paths.ref
    config = settings.paths.config
    ref.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    (ref / "states.csv").write_text(
        "usps,fips,name\nAK,02,Alaska\nGA,13,Georgia\nPR,72,Puerto Rico\n"
    )
    (ref / "counties.csv").write_text(
        "fips,name,state_usps,lat,lon,aland\n"
        "13121,Fulton County,GA,33.79,-84.46,1364558845\n"
        "35013,Doña Ana County,NM,32.35,-106.83,9863000000\n"
    )
    (ref / "places.csv").write_text(
        "geoid,name,state_usps,lat,lon,aland\n"
        "1304000,Atlanta,GA,33.76,-84.42,350000000\n"
    )
    entities = [
        {
            "entity_id": "e1",
            "canonical_name": "Jon Ossoff",
            "ent_type": "PERSON",
            "party": "Democratic",
            "state": "GA",
            "office": "senate",
            "race_key": "2026-GA-SEN",
            "incumbent": True,
            "wikidata_qid": "Q1",
            "aliases": ["Ossoff"],
            "origin": "fec+wikipedia",
        },
        {
            "entity_id": "e2",
            "canonical_name": "Pat Roe",
            "ent_type": "PERSON",
            "party": None,
            "state": "GA",
            "office": "house",
            "race_key": None,
            "incumbent": False,
            "wikidata_qid": None,
            "aliases": [],
            "origin": "wikipedia",
        },
    ]
    (ref / "entities.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in entities)
    )
    (config / "races.yaml").write_text(
        yaml.safe_dump(
            {
                "overrides": [],
                "battleground_states": ["GA"],
                "races": [
                    {
                        "race_key": "2026-GA-SEN",
                        "state": "GA",
                        "office": "senate",
                        "district": None,
                        "rating": "Toss-up",
                        "rating_source": "Cook Political Report via Wikipedia",
                        "rating_url": "https://example.org/r",
                        "close_race": True,
                        "candidates": ["e1"],
                        "watch": True,
                        "ratings": {"Cook": "Toss-up"},
                    },
                    {
                        "race_key": "2026-AK-H00",
                        "state": "AK",
                        "office": "house",
                        "district": "00",
                        "rating": None,
                        "rating_source": None,
                        "rating_url": None,
                        "close_race": False,
                        "candidates": [],
                        "watch": False,
                    },
                ],
            }
        )
    )
    (config / "poll_closings.yaml").write_text(
        yaml.safe_dump(
            {
                "election_date": "2026-11-03",
                "states": {
                    "AK": {"first_close": "00:00", "all_close": "01:00"},
                    "GA": {"first_close": "19:00", "all_close": "19:00"},
                },
            }
        )
    )
    (config / "sources.csv").write_text(
        SOURCES_HEADER
        + "npr,NPR,https://npr.org,https://npr.org/rss|https://npr.org/two,"
        + "national,,US,en,Unrated,,,,,,false,none,1,true,seed note\n"
        + "wire,Wire,https://wire.example,,wire,,US,en,Unrated,,,,,,"
        + "false,none,2,false,\n"
    )
    (config / "lean_ratings.csv").write_text(
        LEAN_HEADER
        + "npr,NPR,LL,AllSides,https://a.example/npr,2026-11-01,-3.5,44.2,"
        + "https://b.example/npr,true,lean note\n"
        + "wire,Wire,R,AllSides,https://a.example/wire,2026-11-01,9.0,30.0,"
        + "https://b.example/wire,false,proposal\n"
    )


@pytest.fixture
def settings(lake_template, tmp_path):
    configured = clone_lake(lake_template, tmp_path)
    configured.paths.config = tmp_path / "config"
    write_inputs(configured)
    return configured


@pytest.fixture
def conn(settings):
    connection = connect(settings)
    yield connection
    connection.close()


@pytest.fixture
def writer(conn, settings):
    return BatchWriter(conn, settings)


def latest(conn, table, key):
    cursor = conn.execute(latest_sql(table, key))
    names = [column[0] for column in cursor.description]
    return {
        row[names.index(key)]: dict(zip(names, row, strict=True))
        for row in cursor.fetchall()
    }


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM lake.{table}").fetchone()[0]


def test_first_sync_loads_every_table(conn, writer, settings):
    state = {}
    counts = sync_ref_tables(conn, writer, settings, state)
    assert counts == {
        "ref.counties": 2,
        "ref.places": 1,
        "ref.states": 3,
        "ref.entities": 2,
        "ref.races": 2,
        "ref.sources": 2,
    }
    writer.flush()
    assert count(conn, "ref.states") == 3
    assert count(conn, "ref.places") == 1


def test_rows_land_with_the_right_types(conn, writer, settings):
    sync_ref_tables(conn, writer, settings, {})
    writer.flush()
    states = latest(conn, "ref.states", "usps")
    assert states["GA"]["battleground"] is True
    assert states["GA"]["fips"] == "13"
    assert states["AK"]["battleground"] is False
    assert states["GA"]["poll_close_et"] == "2026-11-03T19:00:00-05:00"
    assert states["AK"]["poll_close_et"] == "2026-11-04T00:00:00-05:00"
    assert states["AK"]["poll_close_all_et"] == "2026-11-04T01:00:00-05:00"
    assert states["PR"]["poll_close_et"] is None
    counties = latest(conn, "ref.counties", "fips")
    assert counties["35013"]["name"] == "Doña Ana County"
    assert counties["13121"]["aland"] == 1364558845
    assert counties["13121"]["lat"] == pytest.approx(33.79)
    entities = latest(conn, "ref.entities", "entity_id")
    assert entities["e1"]["aliases"] == ["Ossoff"]
    assert entities["e2"]["aliases"] == []
    assert entities["e1"]["incumbent"] is True
    races = latest(conn, "ref.races", "race_key")
    assert races["2026-GA-SEN"]["candidates"] == ["e1"]
    assert races["2026-AK-H00"]["candidates"] == []
    assert races["2026-AK-H00"]["rating"] is None
    assert races["2026-GA-SEN"]["close_race"] is True


def test_unchanged_inputs_do_nothing(conn, writer, settings):
    state = {}
    sync_ref_tables(conn, writer, settings, state)
    writer.flush()
    assert sync_ref_tables(conn, writer, settings, state) == {}
    assert writer.pending() == 0


def test_restart_appends_nothing(conn, writer, settings):
    sync_ref_tables(conn, writer, settings, {})
    writer.flush()
    before = {t: count(conn, t) for t in ("ref.places", "ref.entities")}
    state = {}
    assert sync_ref_tables(conn, writer, settings, state) == {}
    assert writer.pending() == 0
    assert {t: count(conn, t) for t in before} == before
    assert set(state) == {
        "ref.counties",
        "ref.places",
        "ref.states",
        "ref.entities",
        "ref.races",
        "ref.sources",
    }


def test_cold_start_reloads_large_table_when_counts_differ(
    conn, writer, settings
):
    sync_ref_tables(conn, writer, settings, {})
    writer.flush()
    path = settings.paths.ref / "places.csv"
    path.write_text(
        path.read_text() + "1349000,Macon,GA,32.80,-83.69,645000000\n"
    )
    assert sync_ref_tables(conn, writer, settings, {}) == {"ref.places": 2}


def test_changed_large_file_is_reappended_whole(conn, writer, settings):
    state = {}
    sync_ref_tables(conn, writer, settings, state)
    writer.flush()
    path = settings.paths.ref / "counties.csv"
    path.write_text(path.read_text().replace("Fulton County", "Fulton Cty"))
    assert sync_ref_tables(conn, writer, settings, state) == {
        "ref.counties": 2
    }
    writer.flush()
    assert count(conn, "ref.counties") == 4
    newest = latest(conn, "ref.counties", "fips")
    assert newest["13121"]["name"] == "Fulton Cty"


def test_changed_small_file_appends_only_changed_rows(conn, writer, settings):
    state = {}
    sync_ref_tables(conn, writer, settings, state)
    writer.flush()
    path = settings.paths.config / "races.yaml"
    document = yaml.safe_load(path.read_text())
    document["races"][0]["rating"] = "Lean D"
    path.write_text(yaml.safe_dump(document))
    assert sync_ref_tables(conn, writer, settings, state) == {"ref.races": 1}
    writer.flush()
    assert count(conn, "ref.races") == 3
    newest = latest(conn, "ref.races", "race_key")
    assert newest["2026-GA-SEN"]["rating"] == "Lean D"


def test_battleground_change_updates_states(conn, writer, settings):
    state = {}
    sync_ref_tables(conn, writer, settings, state)
    writer.flush()
    path = settings.paths.config / "races.yaml"
    document = yaml.safe_load(path.read_text())
    document["battleground_states"] = ["AK"]
    path.write_text(yaml.safe_dump(document))
    counts = sync_ref_tables(conn, writer, settings, state)
    assert counts["ref.states"] == 2
    writer.flush()
    newest = latest(conn, "ref.states", "usps")
    assert newest["AK"]["battleground"] is True
    assert newest["GA"]["battleground"] is False


def test_poll_closing_change_updates_states(conn, writer, settings):
    state = {}
    sync_ref_tables(conn, writer, settings, state)
    writer.flush()
    path = settings.paths.config / "poll_closings.yaml"
    document = yaml.safe_load(path.read_text())
    document["states"]["GA"]["all_close"] = "20:00"
    path.write_text(yaml.safe_dump(document))
    assert sync_ref_tables(conn, writer, settings, state) == {"ref.states": 1}


def test_confirmed_lean_overrides_and_unconfirmed_does_not(
    conn, writer, settings
):
    sync_ref_tables(conn, writer, settings, {})
    writer.flush()
    sources = latest(conn, "ref.sources", "source_id")
    npr = sources["npr"]
    assert npr["lean_label"] == "LL"
    assert npr["lean_source"] == "AllSides"
    assert npr["lean_source_url"] == "https://a.example/npr"
    assert npr["lean_retrieved_on"] == "2026-11-01"
    assert npr["adfontes_bias"] == pytest.approx(-3.5)
    assert npr["adfontes_reliability"] == pytest.approx(44.2)
    assert npr["adfontes_url"] == "https://b.example/npr"
    assert npr["notes"] == "seed note"
    assert npr["feed_urls"] == "https://npr.org/rss|https://npr.org/two"
    assert npr["enabled"] is True
    assert npr["poll_tier"] == 1
    wire = sources["wire"]
    assert wire["lean_label"] == "Unrated"
    assert wire["lean_source"] is None
    assert wire["adfontes_bias"] is None
    assert wire["state_affiliated"] is False


def test_float_precision_does_not_trigger_a_reload(conn, writer, settings):
    sync_ref_tables(conn, writer, settings, {})
    writer.flush()
    assert sync_ref_tables(conn, writer, settings, {}) == {}


def test_missing_lean_file_still_loads_sources(conn, writer, settings):
    (settings.paths.config / "lean_ratings.csv").unlink()
    counts = sync_ref_tables(conn, writer, settings, {})
    assert counts["ref.sources"] == 2
    writer.flush()
    assert latest(conn, "ref.sources", "source_id")["npr"]["lean_label"] == (
        "Unrated"
    )


def test_ownership_and_note_columns_come_from_sources(conn, writer, settings):
    path = settings.paths.config / "sources.csv"
    path.write_text(
        SOURCES_HEADER
        + "npr,NPR,https://npr.org,,national,,US,en,Unrated,,,,solid,Nonprofit"
        + ",false,none,1,true,\n"
    )
    sync_ref_tables(conn, writer, settings, {})
    writer.flush()
    npr = latest(conn, "ref.sources", "source_id")["npr"]
    assert npr["reliability_note"] == "solid"
    assert npr["ownership"] == "Nonprofit"


def test_absent_files_are_skipped_and_picked_up_later(conn, writer, settings):
    (settings.paths.ref / "entities.jsonl").rename(
        settings.paths.ref / "entities.hidden"
    )
    state = {}
    counts = sync_ref_tables(conn, writer, settings, state)
    assert "ref.entities" not in counts
    (settings.paths.ref / "entities.hidden").rename(
        settings.paths.ref / "entities.jsonl"
    )
    assert sync_ref_tables(conn, writer, settings, state) == {
        "ref.entities": 2
    }


def test_bad_file_is_retried_and_does_not_block_others(conn, writer, settings):
    good = (settings.paths.ref / "entities.jsonl").read_text()
    (settings.paths.ref / "entities.jsonl").write_text("{not json\n")
    (settings.paths.config / "races.yaml").write_text("a: [unclosed\n")
    state = {}
    counts = sync_ref_tables(conn, writer, settings, state)
    assert "ref.entities" not in counts
    assert "ref.races" not in counts
    assert counts["ref.places"] == 1
    assert "ref.entities" not in state
    (settings.paths.ref / "entities.jsonl").write_text(good)
    assert sync_ref_tables(conn, writer, settings, state)["ref.entities"] == 2


def test_loader_appends_through_the_writer_buffer_only(conn, writer, settings):
    sync_ref_tables(conn, writer, settings, {})
    assert writer.pending() > 0
    assert count(conn, "ref.states") == 0


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("20:00", "2026-11-03T20:00:00-05:00"),
        ("19:30", "2026-11-03T19:30:00-05:00"),
        ("00:00", "2026-11-04T00:00:00-05:00"),
        ("01:00", "2026-11-04T01:00:00-05:00"),
        (None, None),
    ],
)
def test_poll_close_timestamp(clock, expected):
    assert poll_close_timestamp(clock, "2026-11-03") == expected


def test_published_poll_closings_cover_every_state_in_order():
    path = Path(__file__).parents[1] / "config" / "poll_closings.yaml"
    document = yaml.safe_load(path.read_text())
    assert document["as_published_url"].startswith("https://")
    states = document["states"]
    assert len(states) == 51
    assert "DC" in states
    for usps, hours in states.items():
        first = poll_close_timestamp(hours["first_close"], "2026-11-03")
        last = poll_close_timestamp(hours["all_close"], "2026-11-03")
        assert first <= last, usps
