from pathlib import Path

import pytest
import yaml

from monitor.ref import races as races_module
from monitor.ref.races import (
    ATTORNEY_GENERAL_PAGE,
    GOVERNOR_PAGE,
    HOUSE_PAGE,
    HOUSE_RATINGS_PAGE,
    MEASURES_PAGE,
    SENATE_PAGE,
    apply_race_overrides,
    assemble_races,
    battleground_states,
    build_races,
    check_counts,
    is_close,
    measure_slug,
    normalize_race_key,
    normalize_rating,
    normalize_state,
    parse_location,
    parse_roster,
    rater_date,
    rater_short_name,
    refresh_race_candidates,
)
from monitor.settings import Paths, Settings

FIXTURES = Path(__file__).parent / "fixtures" / "ref"


def fixture_pages() -> dict[str, str]:
    return {
        SENATE_PAGE: (FIXTURES / "senate.html").read_text(),
        GOVERNOR_PAGE: (FIXTURES / "governor.html").read_text(),
        HOUSE_PAGE: (FIXTURES / "house.html").read_text(),
        HOUSE_RATINGS_PAGE: (FIXTURES / "house_ratings.html").read_text(),
        ATTORNEY_GENERAL_PAGE: (
            FIXTURES / "attorney_general.html"
        ).read_text(),
        MEASURES_PAGE: (FIXTURES / "measures.html").read_text(),
    }


def assembled(overrides=None, candidates=None):
    pages = fixture_pages()
    races, dates = assemble_races(
        pages, pages[MEASURES_PAGE], candidates or {}, overrides or []
    )
    return {race["race_key"]: race for race in races}, dates


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("GA", "Senate"), "2026-GA-SEN"),
        (("GA", "U.S. Senate"), "2026-GA-SEN"),
        (("GA", "US Senate"), "2026-GA-SEN"),
        (("Georgia", "Sen."), "2026-GA-SEN"),
        (("ga", "senator"), "2026-GA-SEN"),
        (("FL", "senate_special"), "2026-FL-SEN"),
        (("AZ", "Governor"), "2026-AZ-GOV"),
        (("AZ", "Gov"), "2026-AZ-GOV"),
        (("Arizona", "Gov."), "2026-AZ-GOV"),
        (("AZ", "gubernatorial"), "2026-AZ-GOV"),
        (("TX", "House", "TX-35"), "2026-TX-H35"),
        (("TX", "House", "35"), "2026-TX-H35"),
        (("TX", "House", 35), "2026-TX-H35"),
        (("TX", "U.S. House", "35th"), "2026-TX-H35"),
        (("TX", "Rep.", "District 35"), "2026-TX-H35"),
        (("TX", "TX-35"), "2026-TX-H35"),
        ((None, "TX-35"), "2026-TX-H35"),
        ((None, None, "TX-35"), "2026-TX-H35"),
        (("TX", None, "35"), "2026-TX-H35"),
        (
            ("TX", "House of Representatives", "35th Congressional District"),
            "2026-TX-H35",
        ),
        (
            ("Texas", "House", "Texas's 35th congressional district"),
            "2026-TX-H35",
        ),
        (("PA", "House", "7"), "2026-PA-H07"),
        (("PA", "Representative", "PA-07"), "2026-PA-H07"),
        (("NE", "congressional district", "NE-02"), "2026-NE-H02"),
        (("AK", "House", "at-large"), "2026-AK-H00"),
        (("AK", "House", "AK-AL"), "2026-AK-H00"),
        (("AK", "House", "1"), "2026-AK-H00"),
        (("AK", "House"), "2026-AK-H00"),
        (("Alaska", "House", "at large"), "2026-AK-H00"),
        (("WY", "U.S. Rep", "00"), "2026-WY-H00"),
        (("DC", "Delegate"), "2026-DC-H00"),
        (("D.C.", "House"), "2026-DC-H00"),
        (("Washington, D.C.", "Delegate"), "2026-DC-H00"),
        (("Washington", "Governor"), "2026-WA-GOV"),
        (("N.C.", "Senate"), "2026-NC-SEN"),
        (("GA", "Lt. Governor"), "2026-GA-LG"),
        (("GA", "lieutenant governor"), "2026-GA-LG"),
        (("GA", "lt_governor"), "2026-GA-LG"),
        (("GA", "Attorney General"), "2026-GA-AG"),
        (("GA", "attorney_general"), "2026-GA-AG"),
        (("GA", "AG"), "2026-GA-AG"),
        (("GA", "Secretary of State"), "2026-GA-SOS"),
        (("GA", "SOS"), "2026-GA-SOS"),
        (("CA", "measure", None, "Prop 50"), "2026-CA-M-proposition-50"),
        (("CA", "Prop 50"), "2026-CA-M-proposition-50"),
        (("CA", None, None, "Prop. 36"), "2026-CA-M-proposition-36"),
        (
            ("CA", "Proposition", None, "Proposition 1: Veterans"),
            "2026-CA-M-proposition-1",
        ),
        (
            (
                "Massachusetts",
                "ballot_measure",
                None,
                "Massachusetts Question 1, Records",
            ),
            "2026-MA-M-question-1",
        ),
    ],
)
def test_normalize_race_key(args, expected):
    assert normalize_race_key(*args) == expected


@pytest.mark.parametrize(
    "args",
    [
        ("TX", "State House", "35"),
        ("TX", "State Senate"),
        ("TX", "state senator"),
        ("TX", "Mayor"),
        ("TX", "state_legislature"),
        ("TX", "state_supreme_court"),
        ("XX", "Senate"),
        (None, "Senate"),
        ("TX", None),
        ("TX", ""),
        ("PA", "House"),
        ("PA", "House", "at-large"),
        ("TX", "House", "not a district"),
        ("TX", "House", "99"),
        ("AK", "House", "2"),
        ("CA", "measure"),
    ],
)
def test_normalize_race_key_rejects(args):
    assert normalize_race_key(*args) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("TX", "TX"),
        ("tx", "TX"),
        ("Texas", "TX"),
        ("Mich.", "MI"),
        ("District of Columbia", "DC"),
        ("U.S. Virgin Islands", "VI"),
        ("Atlantis", None),
        (None, None),
    ],
)
def test_normalize_state(value, expected):
    assert normalize_state(value) == expected


@pytest.mark.parametrize(
    ("text", "expected", "close"),
    [
        ("Tossup", "Toss-up", True),
        ("Toss-up", "Toss-up", True),
        ("Tilt R", "Tilt R", True),
        ("Tilt D (flip)", "Tilt D", True),
        ("Lean D (flip)", "Lean D", True),
        ("Lean I", "Lean I", True),
        ("Likely R", "Likely R", False),
        ("Solid R", "Safe R", False),
        ("Safe D", "Safe D", False),
        ("", None, False),
        ("N/A", None, False),
    ],
)
def test_normalize_rating(text, expected, close):
    assert normalize_rating(text) == expected
    assert is_close(expected) is close


def test_rater_header_parts():
    assert rater_short_name("IE Oct. 29, 2026") == "IE"
    assert rater_short_name("MS NOW Oct 29, 2026") == "MS NOW"
    assert rater_short_name("Econ. Oct. 31, 2026") == "Econ"
    assert rater_date("Cook Oct 30, 2026") == "Oct 30, 2026"


def test_parse_location():
    assert parse_location("Texas 35") == ("TX", "35", False)
    assert parse_location("Alaska at-large") == ("AK", "AL", False)
    assert parse_location("Florida (special)") == ("FL", None, True)
    assert parse_location("Florida (Class 3)") == ("FL", None, False)
    assert parse_location("Overall") == (None, None, False)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Nevada Question 6, Right to Abortion", "question-6"),
        ("Proposition 1: California Veteran Bond", "proposition-1"),
        ("Amendment 81, Colorado Notice", "amendment-81"),
        ("2026 Virginia Voting Rights Amendment", "voting-rights-amendment"),
        (
            "Repeal Alaska's electoral system",
            "repeal-alaskas-electoral-system",
        ),
    ],
)
def test_measure_slug(name, expected):
    usps = "NV" if name.startswith("Nevada") else None
    if "Virginia" in name:
        usps = "VA"
    assert measure_slug(name, usps) == expected


def test_measure_slug_is_bounded():
    slug = measure_slug("word " * 40)
    assert slug is not None
    assert len(slug) <= 48
    assert not slug.endswith("-")


def test_ratings_prefer_cook_then_keep_all_raters():
    races, dates = assembled()
    michigan = races["2026-MI-SEN"]
    assert michigan["rating"] == "Toss-up"
    assert michigan["rating_source"] == "Cook Political Report via Wikipedia"
    assert michigan["rating_url"].endswith(
        "2026_United_States_Senate_elections#Predictions"
    )
    assert michigan["close_race"] is True
    assert michigan["ratings"]["IE"] == "Tilt D"
    assert michigan["ratings"]["Sabato"] == "Lean D"
    assert dates["senate"]["Cook"] == "Oct 30, 2026"


def test_summary_row_is_not_a_race():
    races, _ = assembled()
    assert not any("Overall" in key for key in races)


def test_race_with_only_one_rater_uses_it():
    races, _ = assembled()
    attorney = races["2026-AZ-AG"]
    assert attorney["rating"] == "Lean D"
    assert attorney["rating_source"] == "Sabato's Crystal Ball via Wikipedia"
    assert attorney["close_race"] is True


def test_watch_is_broader_than_close_race():
    races, _ = assembled()
    arizona = races["2026-AZ-GOV"]
    assert arizona["rating"] == "Likely D"
    assert arizona["close_race"] is False
    assert arizona["watch"] is True
    alabama = races["2026-AL-SEN"]
    assert alabama["watch"] is False


def test_house_keys_districts_and_unrated_seats():
    races, _ = assembled()
    assert races["2026-TX-H35"]["district"] == "35"
    assert races["2026-TX-H35"]["rating"] == "Toss-up"
    assert races["2026-AK-H00"]["district"] == "00"
    delegate = races["2026-DC-H00"]
    assert delegate["rating"] is None
    assert delegate["rating_source"] is None
    assert delegate["rating_url"] is None
    assert delegate["close_race"] is False


def test_special_election_is_flagged():
    races, _ = assembled()
    assert races["2026-FL-SEN"]["special"] is True
    assert races["2026-FL-SEN"]["rating"] == "Likely R"
    assert "special" not in races["2026-GA-SEN"]


def test_measures_on_the_november_ballot_only():
    races, _ = assembled()
    assert races["2026-CA-M-proposition-1"]["office"] == "ballot_measure"
    assert races["2026-NV-M-question-6"]["watch"] is True
    assert races["2026-NV-M-question-7"]["watch"] is False
    kansas = [key for key in races if key.startswith("2026-KS-M-")]
    assert kansas == ["2026-KS-M-citizenship-voting-requirement-amendment"]


def test_roster_parses_candidates_and_incumbents():
    rows = {row.race_key: row for row in parse_roster(fixture_pages())}
    alaska = rows["2026-AK-SEN"]
    assert [link.title for link in alaska.incumbents] == [
        "Dan Sullivan (U.S. senator)"
    ]
    assert "▌Dan J. Sullivan (Republican)" in [
        item.text for item in alaska.candidates
    ]
    assert rows["2026-TX-H35"].incumbents == []
    assert "2026-TX-H18" not in rows


def test_candidates_come_from_the_roster_file():
    races, _ = assembled(candidates={"2026-GA-SEN": ["a", "b"]})
    assert races["2026-GA-SEN"]["candidates"] == ["a", "b"]
    assert races["2026-MI-SEN"]["candidates"] == []


def test_battleground_uses_close_senate_and_governor_only():
    races, _ = assembled()
    states = battleground_states(races.values(), [])
    assert states == ["AK", "GA", "KS", "MI", "NE"]


def test_battleground_overrides_add_and_remove():
    races, _ = assembled()
    overrides = [
        {"state": "PA", "battleground": True},
        {"state": "ne", "battleground": False},
        {"state": "OH"},
    ]
    states = battleground_states(races.values(), overrides)
    assert states == ["AK", "GA", "KS", "MI", "OH", "PA"]


def test_race_overrides_replace_fields_and_create_races():
    races, _ = assembled(
        overrides=[
            {"race_key": "2026-AL-SEN", "watch": True, "note": "owner call"},
            {
                "race_key": "2026-CA-M-prop-99",
                "office": "ballot_measure",
                "watch": True,
            },
        ]
    )
    assert races["2026-AL-SEN"]["watch"] is True
    assert "note" not in races["2026-AL-SEN"]
    assert races["2026-AL-SEN"]["rating"] == "Safe R"
    created = races["2026-CA-M-prop-99"]
    assert created["state"] == "CA"
    assert created["candidates"] == []


def test_race_overrides_keep_sorted_order():
    rows = apply_race_overrides(
        [{"race_key": "2026-TX-SEN"}, {"race_key": "2026-AK-SEN"}], []
    )
    assert [row["race_key"] for row in rows] == [
        "2026-AK-SEN",
        "2026-TX-SEN",
    ]


def test_check_counts_rejects_a_thin_result():
    races, _ = assembled()
    with pytest.raises(RuntimeError, match="look wrong"):
        check_counts(list(races.values()))


class FakeWiki:
    def page(self, title):
        return fixture_pages().get(title, "<p>empty</p>"), 123


@pytest.fixture
def settings(tmp_path, monkeypatch):
    configured = Settings(
        paths=Paths.under(tmp_path, config=tmp_path / "config")
    )
    configured.paths.ref.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(races_module, "WikiClient", FakeWiki)
    monkeypatch.setattr(races_module, "MIN_RACES", {})
    monkeypatch.setattr(races_module, "MIN_RATED", {})
    return configured


def test_dry_run_prints_and_writes_nothing(settings, capsys):
    build_races(settings, dry_run=True)
    output = capsys.readouterr().out
    assert "battleground states: AK GA KS MI NE" in output
    assert "dry run" in output
    assert not (settings.paths.config / "races.yaml").exists()
    assert not settings.paths.catalog.exists()


def test_build_writes_yaml_with_overrides_first(settings):
    build_races(settings)
    path = settings.paths.config / "races.yaml"
    text = path.read_text()
    assert text.startswith("# Race watchlist")
    assert yaml.safe_load(text).keys() >= {
        "overrides",
        "battleground_states",
        "rater_dates",
        "source_revisions",
        "races",
    }
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert body[0].startswith("overrides:")
    assert not settings.paths.catalog.exists()


def test_regeneration_keeps_overrides(settings):
    build_races(settings)
    path = settings.paths.config / "races.yaml"
    document = yaml.safe_load(path.read_text())
    document["overrides"] = [
        {"state": "PA", "battleground": True},
        {"race_key": "2026-AL-SEN", "watch": True},
    ]
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    build_races(settings)
    rebuilt = yaml.safe_load(path.read_text())
    assert rebuilt["overrides"] == document["overrides"]
    assert "PA" in rebuilt["battleground_states"]
    by_key = {race["race_key"]: race for race in rebuilt["races"]}
    assert by_key["2026-AL-SEN"]["watch"] is True


def test_failed_count_check_leaves_existing_file(settings, monkeypatch):
    build_races(settings)
    path = settings.paths.config / "races.yaml"
    before = path.read_text()
    monkeypatch.setattr(races_module, "MIN_RACES", {"house": 435})
    with pytest.raises(RuntimeError):
        build_races(settings)
    assert path.read_text() == before


def test_refresh_copies_entity_ids_from_roster(settings):
    build_races(settings)
    (settings.paths.ref / "entities.jsonl").write_text(
        '{"entity_id": "abc", "race_key": "2026-GA-SEN"}\n'
        '{"entity_id": "def", "race_key": null}\n'
    )
    assert refresh_race_candidates(settings) == 1
    path = settings.paths.config / "races.yaml"
    document = yaml.safe_load(path.read_text())
    by_key = {race["race_key"]: race for race in document["races"]}
    assert by_key["2026-GA-SEN"]["candidates"] == ["abc"]
    assert path.read_text().startswith("# Race watchlist")
    assert refresh_race_candidates(settings) == 0
