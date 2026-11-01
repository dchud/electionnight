from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient

from monitor.lake import BatchWriter, connect
from monitor.web.app import create_app, first_sentence, render_cited
from monitor.web.filters import Filters, FocusSets, race_state_pair, since_4pm
from monitor.web.panels import (
    bin_series,
    lean_glyph,
    lean_text,
    load_poll_closes,
    parse_clock,
    sparkline_points,
    whole_bin,
)

ET_OFFSET = timedelta(hours=-5)
NOW = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)


def seed(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    now = datetime.now(UTC) - timedelta(minutes=5)
    writer.add(
        "raw.items",
        [
            {
                "item_id": f"i{n}",
                "source_id": "wire",
                "title": f"Story {n} about Georgia",
                "summary": "Summary.",
                "url": f"https://example.com/{n}",
                "first_seen_at": now,
                "published_at": now,
                "language": "en",
                "feed_categories": [],
            }
            for n in range(4)
        ],
    )
    writer.add(
        "raw.items",
        [
            {
                "item_id": "old",
                "source_id": "wire",
                "title": "Backlog story",
                "summary": "",
                "url": "https://example.com/old",
                "first_seen_at": now,
                "published_at": now - timedelta(days=3),
                "language": "en",
                "feed_categories": [],
            }
        ],
    )
    writer.add(
        "nlp.classify_a",
        [
            {
                "item_id": item_id,
                "election_relevant": item_id != "i3",
                "relevance_conf": 0.9,
                "levels": ["state"],
                "primary_level": "state",
                "states": ["GA"],
                "race_types": ["senate"],
                "topics": ["turnout"],
                "free_tags": [],
                "is_opinion": False,
                "is_results_report": False,
            }
            for item_id in ("i0", "i1", "i2", "i3", "old")
        ],
    )
    writer.add(
        "nlp.dup_groups",
        [{"item_id": "i1", "dup_group_id": "g", "is_canonical": False}],
    )
    writer.add(
        "nlp.storyline_assign",
        [
            {
                "item_id": "i0",
                "storyline_id": "s1",
                "similarity": 0.95,
                "assigned_at": now,
            },
            {
                "item_id": "i1",
                "storyline_id": "s2",
                "similarity": 0.95,
                "assigned_at": now,
            },
        ],
    )
    writer.add(
        "nlp.storylines",
        [
            {
                "storyline_id": "s1",
                "version": 1,
                "label": "Georgia polling delays",
                "summary": "Delays were reported.[^1]",
                "citations": '{"1": "i0"}',
                "first_seen_at": now,
                "last_item_at": now,
                "status": "active",
                "states": ["GA"],
            },
            {
                "storyline_id": "s2",
                "version": 1,
                "label": "Merged away",
                "first_seen_at": now,
                "last_item_at": now,
                "status": "merged",
                "merged_into": "s1",
            },
        ],
    )
    writer.add(
        "ref.sources",
        [
            {
                "source_id": "wire",
                "name": "Wire Service",
                "lean_label": "C",
                "country": "US",
            }
        ],
    )
    writer.add(
        "ref.races",
        [
            {
                "race_key": "2026-GA-SEN",
                "state": "GA",
                "office": "SEN",
                "close_race": True,
                "watch": False,
                "candidates": [],
            }
        ],
    )
    writer.add(
        "ref.states",
        [
            {
                "usps": "GA",
                "fips": "13",
                "name": "Georgia",
                "battleground": True,
            }
        ],
    )
    writer.flush()
    conn.close()


@pytest.fixture(scope="module")
def client(lake_template, tmp_path_factory):
    """One app for this module's tests, which only read."""
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("web"))
    seed(settings)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_home_and_fragments_render(client):
    assert "Live Wire" in client.get("/").text
    for path in (
        "kpis",
        "wire",
        "rising",
        "brief",
        "states-rising",
        "race-calls",
        "translated",
        "status-strip",
    ):
        assert client.get(f"/frag/{path}?window=24h").status_code == 200


def test_wire_lists_relevant_stories_with_outlet_and_lean(client):
    text = client.get("/frag/wire?window=24h").text
    assert "Story 0 about Georgia" in text
    assert "Wire Service" in text
    assert "○○●○○" in text
    assert "Story 3" not in text


def test_unique_control_hides_duplicates(client):
    assert "Story 1 " not in client.get("/frag/wire").text
    assert "Story 1 " in client.get("/frag/wire?copies=all").text


def test_focus_battleground_and_close_race_match_the_state(client):
    for focus in ("battleground", "close", "state:GA"):
        assert "Story 0" in client.get(f"/frag/wire?focus={focus}").text
    assert "Story 0" not in client.get("/frag/wire?focus=state:OH").text


def test_as_of_excludes_later_items(client):
    earlier = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    response = client.get("/frag/wire", params={"as_of": earlier})
    assert "Story 0" not in response.text


def test_backlog_item_sits_at_its_published_time(client):
    wire = client.get("/frag/wire").text
    assert wire.index("Story 0") < wire.index("Backlog story")
    recent = client.get("/frag/evidence?kind=state&key=GA&window=1h").text
    assert "Backlog story" not in recent


def test_merged_storyline_counts_under_the_survivor(client):
    rows = client.get("/api/data/stream?window=1h&copies=all").json()["table"]
    assert {row["label"] for row in rows} == {"Georgia polling delays"}
    assert sum(row["n"] for row in rows) == 2


def test_stream_has_a_row_for_every_bin(client):
    rows = client.get("/api/data/stream?window=1h").json()["table"]
    assert len(rows) >= 12
    assert 0 in {row["n"] for row in rows}


@pytest.mark.parametrize(
    "chart",
    [
        "stream",
        "topic_heat",
        "bump",
        "tone_by_lean",
        "coverage_matrix",
        "global_press",
        "map",
    ],
)
def test_chart_data_endpoints_respond(client, chart):
    assert client.get(f"/api/data/{chart}?window=24h").status_code == 200


@pytest.mark.parametrize(
    "chart",
    ["stream", "topic_heat", "bump", "tone_by_lean", "coverage_matrix"],
)
def test_specs_are_vega_lite_with_named_data(client, chart):
    spec = client.get(f"/api/spec/{chart}").json()
    assert "vega-lite" in spec["$schema"]
    assert "table" in str(spec)


def test_unknown_chart_is_404(client):
    assert client.get("/api/spec/nope").status_code == 404
    assert client.get("/api/data/nope").status_code == 404


def test_evidence_shows_items_and_the_exact_filter(client):
    text = client.get(
        "/frag/evidence?kind=state&key=GA&window=24h&focus=battleground"
    ).text
    assert "Story 0" in text
    assert "GA · 24 h · unique · focus=Battlegrounds" in text


def test_article_and_citation_fragments(client):
    assert "Open original" in client.get("/frag/article/i0").text
    assert "Wire Service" in client.get("/frag/cite/i0").text
    assert client.get("/frag/article/none").status_code == 404


def test_map_data_marks_battlegrounds_and_focus(client):
    data = client.get("/api/data/map?window=24h&focus=battleground").json()
    assert data["battleground"] == ["GA"]
    assert data["focus_states"] == ["GA"]
    assert data["states"][0]["usps"] == "GA"


def test_web_process_never_writes_to_the_lake(settings):
    seed(settings)
    conn = connect(settings, read_only=True)
    before = conn.execute(
        "SELECT max(snapshot_id) FROM lake.snapshots()"
    ).fetchall()
    with TestClient(create_app(settings)) as test_client:
        test_client.get("/frag/wire")
    assert (
        conn.execute(
            "SELECT max(snapshot_id) FROM lake.snapshots()"
        ).fetchall()
        == before
    )
    conn.close()


def test_filters_parse_rejects_unknown_values():
    filters = Filters.parse(
        {
            "window": "9y",
            "focus": "x",
            "level": "x",
            "lang": "x",
            "copies": "x",
        }
    )
    assert (filters.window, filters.focus, filters.level) == (
        "1h",
        "all",
        "all",
    )
    assert Filters.parse({"focus": "state:GA"}).focused_state == "GA"
    assert Filters.parse({"focus": "state:GA; DROP"}).focus == "all"


def test_query_string_omits_defaults_and_round_trips():
    assert Filters().query_string() == ""
    filters = Filters(window="3h", focus="state:GA", copies="all")
    assert (
        Filters.parse(
            dict(
                p.split("=")
                for p in filters.query_string().replace("%3A", ":").split("&")
            )
        )
        == filters
    )


def test_since_4pm_picks_the_latest_at_or_before():
    evening = datetime(2026, 11, 3, 23, 0, tzinfo=UTC)  # 6 pm ET
    assert since_4pm(evening) == datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    morning = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)  # 10 am ET
    assert since_4pm(morning) == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)


def test_focus_sql_is_true_for_all():
    assert Filters().focus_sql(FocusSets())[0] == "TRUE"


@pytest.mark.parametrize(
    ("race", "pair"),
    [
        ({"state": "GA", "office": "SEN"}, "GA|senate"),
        ({"state": "AZ", "office": "GOV"}, "AZ|governor"),
        ({"state": "PA", "office": "H07"}, "PA|house"),
        ({"state": "CA", "office": "M-prop-1"}, "CA|ballot_measure"),
        ({"state": None, "office": "SEN"}, None),
    ],
)
def test_race_state_pair(race, pair):
    assert race_state_pair(race) == pair


def test_lean_glyph_and_text():
    assert lean_glyph("L") == "●○○○○"
    assert lean_glyph("R") == "○○○○●"
    assert lean_glyph("Unrated") == ""
    assert lean_text("NA") == "Non-US outlet"
    assert lean_text("LL", "GB") == "Non-US outlet"
    assert lean_text(None) == "Unrated"


def test_sparkline_and_bins():
    assert sparkline_points([]) == ""
    points = sparkline_points([0, 5, 10], width=100, height=20)
    assert points.split()[0] == "0.0,19.0"
    end = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    series = bin_series(
        [{"bin_start": end - timedelta(minutes=5), "n": 3}], end, bins=3
    )
    assert series == [0, 3, 0]


def test_poll_times_fall_on_election_night():
    assert parse_clock("19:00").day == 3
    assert parse_clock("00:00").day == 4
    assert parse_clock(None) is None
    closes = load_poll_closes(Path("config"))
    assert closes
    assert closes == sorted(closes, key=lambda close: close.at)
    assert "GA" in next(c for c in closes if c.at.hour == 19).states


def test_render_cited_links_known_citations_and_escapes():
    html = str(render_cited("A <b>claim</b>.[^1] Bad.[^9]", {"1": "item"}))
    assert "&lt;b&gt;" in html
    assert 'data-item="item"' in html
    assert "[9]" not in html


def test_first_sentence_keeps_citations():
    assert first_sentence("One.[^1] Two.[^2]") == "One.[^1]"
    assert first_sentence(None) == ""


def test_live_time_repeats_within_ten_seconds_and_never_runs_behind():
    before = datetime.now(UTC)
    first, second = Filters().now, Filters().now
    assert first >= before
    assert first - before <= timedelta(seconds=10)
    assert first.second % 10 == 0
    assert second - first <= timedelta(seconds=10)


def test_oldest_topic_bin_is_counted_whole():
    moment = datetime(2026, 11, 3, 21, 37, 12, 5, tzinfo=UTC)
    assert whole_bin(moment) == datetime(2026, 11, 3, 21, 30, tzinfo=UTC)
    on_edge = datetime(2026, 11, 3, 21, 45, tzinfo=UTC)
    assert whole_bin(on_edge) == on_edge
