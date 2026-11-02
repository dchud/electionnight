"""Dashboard figures against their definitions, and against their drawers.

Every figure here comes from a scratch lake through the same route or panel a
browser uses. The clock is fixed by passing `as_of`, so seeds can sit on
election night.
"""

import asyncio
import html
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

import duckdb
import pytest
from fastapi.testclient import TestClient

from monitor import analytics
from monitor.lake import BatchWriter, connect
from monitor.web.app import create_app
from monitor.web.charts import specs
from monitor.web.drilldown import placement_reason
from monitor.web.filters import Filters
from monitor.web.hub import Hub
from monitor.web.routes import sse

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 20, 0, tzinfo=ET)
AS_OF = "as_of=" + quote(NOW.isoformat())
BASELINE = datetime(2026, 11, 2, 12, 0, tzinfo=ET)
WINDOW = NOW - timedelta(minutes=30)


class Lake:
    """Writes rows the way the pipeline does."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.conn = connect(settings)
        self.writer = BatchWriter(self.conn, settings)
        self.count = 0

    def source(self, source_id, lean="C", country="US", **fields):
        self.writer.add(
            "ref.sources",
            [
                {
                    "source_id": source_id,
                    "name": source_id,
                    "lean_label": lean,
                    "country": country,
                    **fields,
                }
            ],
        )

    def story(self, storyline_id, label="Story", at=NOW, **fields):
        self.writer.add(
            "nlp.storylines",
            [
                {
                    "storyline_id": storyline_id,
                    "version": fields.pop("version", 1),
                    "label": label,
                    "summary": "Summary.[^1]",
                    "citations": "{}",
                    "first_seen_at": at,
                    "last_item_at": at,
                    "status": fields.pop("status", "active"),
                    "states": [],
                    **fields,
                }
            ],
        )

    def item(self, at=WINDOW, **fields):
        self.count += 1
        item_id = fields.pop("item_id", f"item{self.count}")
        self.writer.add(
            "raw.items",
            [
                {
                    "item_id": item_id,
                    "source_id": fields.pop("source", "wire"),
                    "title": f"Headline {item_id} end",
                    "summary": "Summary.",
                    "url": f"https://example.com/{item_id}",
                    "first_seen_at": at,
                    "published_at": fields.pop("published", at),
                    "language": fields.pop("language", "en"),
                    "feed_categories": [],
                }
            ],
        )
        self.classify(item_id, **fields)
        return item_id

    def classify(self, item_id, **fields):
        levels = fields.pop("levels", ["state"])
        canonical = fields.pop("canonical", True)
        group = fields.pop("group", None)
        storyline = fields.pop("storyline", None)
        tone = fields.pop("tone", None)
        if fields.pop("classified", True):
            self.writer.add(
                "nlp.classify_a",
                [
                    {
                        "item_id": item_id,
                        "election_relevant": fields.pop("relevant", True),
                        "relevance_conf": 0.9,
                        "levels": levels,
                        "primary_level": levels[0],
                        "states": fields.pop("states", []),
                        "race_types": [],
                        "topics": fields.pop("topics", ["turnout"]),
                        "free_tags": [],
                        "is_opinion": fields.pop("opinion", False),
                        "is_results_report": False,
                    }
                ],
            )
        if group or not canonical:
            self.writer.add(
                "nlp.dup_groups",
                [
                    {
                        "item_id": item_id,
                        "dup_group_id": group or "group",
                        "is_canonical": canonical,
                    }
                ],
            )
        if storyline:
            self.writer.add(
                "nlp.storyline_assign",
                [
                    {
                        "item_id": item_id,
                        "storyline_id": storyline,
                        "similarity": 0.95,
                        "assigned_at": NOW,
                    }
                ],
            )
        if tone is not None:
            self.writer.add(
                "nlp.extract_b",
                [
                    {
                        "item_id": item_id,
                        "summary_en": "Summary.",
                        "article_tone": tone,
                        "topics_refined": [],
                        "raw": json.dumps({"race_keys": []}),
                    }
                ],
            )

    def mention(self, item_id, entity_id, tone=None):
        self.count += 1
        self.writer.add(
            "nlp.mentions",
            [
                {
                    "mention_id": f"m{self.count}",
                    "item_id": item_id,
                    "surface": entity_id,
                    "ent_type": "PERSON",
                    "entity_id": entity_id,
                    "tone": tone,
                }
            ],
        )

    def call(self, item_id, **fields):
        self.writer.add(
            "nlp.race_calls",
            [
                {
                    "call_id": f"call-{item_id}",
                    "item_id": item_id,
                    "race_key": "2026-GA-SEN",
                    "winner_name": "Jon Doe",
                    "party": "Democratic",
                    "called_by": "AP",
                    "status": "projected",
                    "state": "GA",
                    **fields,
                }
            ],
        )

    def commit(self):
        self.writer.flush()


@pytest.fixture
def lake(settings):
    store = Lake(settings)
    store.source("wire")
    yield store
    store.conn.close()


def app_for(lake):
    lake.commit()
    return create_app(lake.settings)


def client_for(lake):
    return TestClient(app_for(lake))


def drawer(client, query, extra=AS_OF):
    """The evidence drawer's counts for a query the page would send."""
    text = client.get(f"/frag/evidence?{query}&{extra}").text
    heading = re.search(
        r"(\d+) stories · (\d+) outlets?(?: · (\d+) (?:toned )?mentions)?",
        text,
    )
    assert heading, text
    return SimpleNamespace(
        stories=int(heading[1]),
        outlets=int(heading[2]),
        mentions=None if heading[3] is None else int(heading[3]),
        filter=re.search(r'filter-line">([^<]+)', text)[1],
        text=text,
    )


def evidence_attrs(text):
    return [
        html.unescape(x) for x in re.findall(r'data-evidence="([^"]+)"', text)
    ]


def seed_shares(lake, scale=4, at=WINDOW):
    """The same shares in the baseline and in the window."""
    for count, states in ((50, []), (10, ["GA"]), (40, ["PA"])):
        for n in range(count):
            lake.item(BASELINE + timedelta(minutes=n), states=states)
        for _ in range(count * scale):
            lake.item(at, states=states)


def map_states(web, **params):
    filters = Filters.parse({"as_of": NOW.isoformat(), **params})
    data = web.panels.map_data(filters)
    return {row["usps"]: row for row in data["states"]}, data


# Attention index


def test_attention_is_one_when_shares_hold_and_some_stories_name_no_state(
    lake,
):
    seed_shares(lake)
    with client_for(lake) as client:
        states, _ = map_states(client.app.state.web)
        assert states["GA"]["attention"] == pytest.approx(1.0)
        assert states["PA"]["attention"] == pytest.approx(1.0)


def test_state_page_gives_the_attention_the_map_gives(lake):
    lake.writer.add(
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
    seed_shares(lake)
    for _ in range(40):
        lake.item(WINDOW, states=["GA"])
    with client_for(lake) as client:
        states, _ = map_states(client.app.state.web)
        page = client.get(f"/state/GA?{AS_OF}").text
        shown = re.search(
            r'Attention index</span><span class="kpi-value">([\d.]+)', page
        )
        assert float(shown[1]) == pytest.approx(
            states["GA"]["attention"], abs=0.006
        )
        assert states["GA"]["attention"] > 1


@pytest.mark.parametrize("focus", ["state:GA", "battleground", "watchlist"])
def test_focus_chooses_states_and_never_changes_a_value(lake, focus):
    lake.writer.add(
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
    seed_shares(lake, scale=1)
    with client_for(lake) as client:
        web = client.app.state.web
        everything, _ = map_states(web)
        focused, data = map_states(web, focus=focus)
        assert focused == everything
        assert data["focus_states"] is not None
        assert "PA" not in data["focus_states"]


def test_state_chart_does_not_move_under_focus(lake):
    lake.writer.add(
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
    seed_shares(lake, scale=1)
    with client_for(lake) as client:
        path = f"/api/data/state_attention?key=GA&{AS_OF}"
        plain = client.get(path).json()["table"]
        focused = client.get(path + "&focus=state:PA").json()["table"]
        assert plain == focused
        busy = [row for row in plain if row["n"]]
        assert busy
        for row in busy:
            assert row["attention"] == pytest.approx(
                analytics.attention_index(row["n"], row["total"], 10, 100),
                abs=0.001,
            )


def test_level_applies_to_both_sides_of_the_index(lake):
    for at, count in ((BASELINE, 1), (WINDOW, 4)):
        for _ in range(10 * count):
            lake.item(at, states=["GA"], levels=["local"])
        for _ in range(10 * count):
            lake.item(at, states=["PA"], levels=["local"])
    for _ in range(80):
        lake.item(BASELINE, states=["PA"], levels=["national"])
    with client_for(lake) as client:
        states, _ = map_states(client.app.state.web, level="local")
        assert states["GA"]["attention"] == pytest.approx(1.0)


def test_language_applies_to_both_sides_of_the_index(lake):
    for at, count in ((BASELINE, 1), (WINDOW, 4)):
        for _ in range(10 * count):
            lake.item(at, states=["GA"])
        for _ in range(10 * count):
            lake.item(at, states=["PA"])
    for _ in range(80):
        lake.item(BASELINE, states=["PA"], language="es")
    with client_for(lake) as client:
        states, _ = map_states(client.app.state.web, lang="en")
        assert states["GA"]["attention"] == pytest.approx(1.0)


def test_unique_control_applies_to_both_sides_of_the_index(lake):
    for at, count in ((BASELINE, 1), (WINDOW, 4)):
        for _ in range(10 * count):
            lake.item(at, states=["GA"])
        for _ in range(10 * count):
            lake.item(at, states=["PA"])
    for _ in range(80):
        lake.item(BASELINE, states=["PA"], canonical=False)
    with client_for(lake) as client:
        web = client.app.state.web
        unique, _ = map_states(web)
        assert unique["GA"]["attention"] == pytest.approx(1.0)
        copies, _ = map_states(web, copies="all")
        # All copies: GA held 10 of 100 in the baseline and 40 of 80 now.
        assert copies["GA"]["attention"] == pytest.approx(45 / 13, abs=0.001)


def test_replay_baseline_ignores_what_came_later(lake):
    late = BASELINE + timedelta(hours=12)
    for _ in range(10):
        lake.item(BASELINE, states=["GA"])
    for _ in range(10):
        lake.item(BASELINE, states=["PA"])
    for _ in range(300):
        lake.item(late, states=["PA"])
    with client_for(lake) as client:
        filters = Filters.parse(
            {
                "as_of": (BASELINE + timedelta(hours=1)).isoformat(),
                "window": "24h",
            }
        )
        states = client.app.state.web.panels.map_data(filters)["states"]
        ga = next(row for row in states if row["usps"] == "GA")
        assert ga["attention"] == pytest.approx(1.0)


# Mean tone on the map


def test_map_tone_leaves_out_opinion(lake):
    for _ in range(10):
        lake.item(states=["GA"], tone=1.0)
    for _ in range(10):
        lake.item(states=["GA"], tone=-2.0, opinion=True)
    with client_for(lake) as client:
        states, _ = map_states(client.app.state.web)
        assert states["GA"]["tone"] == pytest.approx(1.0)
        assert states["GA"]["tone_n"] == 10
        assert states["GA"]["n"] == 20


def test_map_flags_low_n_and_hides_under_five(lake):
    for _ in range(6):
        lake.item(states=["GA"], tone=1.0)
    for _ in range(4):
        lake.item(states=["OH"], tone=1.0)
    for _ in range(15):
        lake.item(states=["PA"], tone=1.0)
    with client_for(lake) as client:
        states, _ = map_states(client.app.state.web)
        assert states["GA"]["tone_low_n"] is True
        assert states["GA"]["tone_n"] == 6
        assert states["OH"]["tone"] is None
        assert states["PA"]["tone_low_n"] is False


def test_charts_draw_the_mean_inside_its_interval(lake):
    lake.source("left", lean="L")
    tones = [1.5, 1.0, 0.5, 1.2, 0.8, 1.1, 0.9]
    for tone in tones:
        item_id = lake.item(source="left")
        lake.mention(item_id, "e1", tone=tone)
    with client_for(lake) as client:
        rows = client.get(f"/api/data/tone_by_lean?{AS_OF}").json()["table"]
        for row in rows:
            assert row["mean"] == pytest.approx(sum(tones) / len(tones))
            assert row["ci_low"] < row["mean"] < row["ci_high"]


# Counts on screen against the drawers they open


def test_race_call_row_opens_every_claim_it_counts(lake):
    lake.source("other")
    old = lake.item(NOW - timedelta(hours=2), states=["GA"])
    copy = lake.item(source="other", states=["GA"], canonical=False)
    aside = lake.item(source="other", states=["GA"], relevant=False)
    for item_id in (old, copy, aside):
        lake.call(item_id)
    with client_for(lake) as client:
        panel = client.get(f"/frag/race-calls?{AS_OF}").text
        queries = evidence_attrs(panel)
        assert len(queries) == 1
        opened = drawer(client, queries[0])
        assert opened.stories == 3
        assert opened.outlets == 2
        assert re.search(r'class="num">2</td>', panel)
        assert "all time" in opened.filter


def test_race_calls_group_one_winner_under_any_spelling(lake):
    lake.source("other")
    first = lake.item(states=["GA"])
    second = lake.item(source="other", states=["GA"])
    lake.call(first, winner_name="Jon Doe", winner_entity_id="e1")
    lake.call(second, winner_name="Sen. Jon Doe", winner_entity_id="e1")
    with client_for(lake) as client:
        rows = client.app.state.web.panels.race_calls(
            Filters.parse({"as_of": NOW.isoformat()})
        )
        assert len(rows) == 1
        assert rows[0]["outlets"] == 2


def race_rows(client, **filters):
    panels = client.app.state.web.panels
    return panels.race_calls(
        Filters.parse({"as_of": NOW.isoformat(), **filters})
    )


def test_a_settled_race_shows_only_its_final_claims(lake):
    for name in ("a", "b", "c", "d"):
        lake.source(name)
    leads = [
        lake.item(NOW - timedelta(minutes=90), source=name, states=["GA"])
        for name in ("a", "b", "c")
    ]
    called = NOW - timedelta(minutes=30)
    finals = [
        lake.item(called, source=name, states=["GA"]) for name in ("c", "d")
    ]
    for item in leads:
        lake.call(item, status="leading", called_by="unspecified")
    for item in finals:
        lake.call(item)
    closer = lake.item(NOW - timedelta(minutes=60), source="a", states=["GA"])
    lake.call(closer, status="too_close", winner_name=None)
    with client_for(lake) as client:
        rows = race_rows(client)
    assert len(rows) == 1
    assert rows[0]["statuses"] == ["projected"]
    assert rows[0]["outlets"] == 2
    assert rows[0]["called_by"] == ["AP"]
    assert sorted(rows[0]["item_ids"]) == sorted(finals)
    assert rows[0]["first_reported"].timestamp() == called.timestamp()


def test_a_concession_does_not_read_as_the_winner_conceding(lake):
    lake.source("other")
    lake.call(lake.item(states=["GA"]), winner_name="Ann Roe")
    lake.call(
        lake.item(source="other", states=["GA"]),
        winner_name="Ann Roe",
        status="conceded",
        called_by="unspecified",
    )
    with client_for(lake) as client:
        rows = race_rows(client)
        page = client.get(
            "/frag/race-calls", params={"as_of": NOW.isoformat()}
        ).text
    assert rows[0]["statuses"] == ["conceded", "projected"]
    assert rows[0]["outlets"] == 2
    assert "opponent conceded, projected" in page
    assert "<td>conceded" not in page


def seeded_lead_and_hidden_final(lake):
    """A lead visible to filters, and a final claim they would hide."""
    lake.source("abroad")
    lead_at = NOW - timedelta(minutes=90)
    lead = lake.item(lead_at, states=["GA"], levels=["state"], language="en")
    lake.call(
        lead,
        status="leading",
        winner_name="Bob Loser",
        winner_entity_id="loser",
        called_by="unspecified",
    )
    final = lake.item(
        NOW - timedelta(minutes=30),
        source="abroad",
        states=[],
        levels=["national"],
        language="es",
    )
    lake.call(final, winner_name="Ann Winner", winner_entity_id="winner")


@pytest.mark.parametrize(
    "filters",
    [{"focus": "state:GA"}, {"level": "state"}, {"lang": "en"}],
)
def test_a_filter_hiding_the_final_claim_brings_back_no_lead(lake, filters):
    seeded_lead_and_hidden_final(lake)
    with client_for(lake) as client:
        rows = race_rows(client, **filters)
    assert "leading" not in {s for row in rows for s in row["statuses"]}


def test_a_candidate_page_does_not_show_a_lead_the_race_has_ended(lake):
    seeded_lead_and_hidden_final(lake)
    with client_for(lake) as client:
        panels = client.app.state.web.panels
        rows = panels.race_calls(
            Filters.parse({"as_of": NOW.isoformat()}),
            clause="rc.winner_entity_id = $entity_id",
            extra={"entity_id": "loser"},
        )
    assert rows == []


def test_a_replay_before_the_call_still_shows_the_lead(lake):
    seeded_lead_and_hidden_final(lake)
    earlier = NOW - timedelta(minutes=60)
    with client_for(lake) as client:
        panels = client.app.state.web.panels
        rows = panels.race_calls(Filters.parse({"as_of": earlier.isoformat()}))
    assert [row["statuses"] for row in rows] == [["leading"]]


def test_one_unsupported_final_claim_does_not_end_the_other_reports(lake):
    lake.source("other")
    lake.call(
        lake.item(NOW - timedelta(minutes=60), states=["GA"]),
        status="too_close",
        winner_name=None,
    )
    lake.call(
        lake.item(NOW - timedelta(minutes=30), source="other", states=["GA"]),
        called_by="unspecified",
    )
    with client_for(lake) as client:
        rows = race_rows(client)
    assert {tuple(row["statuses"]) for row in rows} == {
        ("projected",),
        ("too_close",),
    }


def test_two_outlets_settle_a_race_with_no_named_caller(lake):
    lake.source("other")
    lake.call(
        lake.item(NOW - timedelta(minutes=60), states=["GA"]),
        status="too_close",
        winner_name=None,
    )
    for source in ("wire", "other"):
        lake.call(
            lake.item(
                NOW - timedelta(minutes=30), source=source, states=["GA"]
            ),
            called_by="unspecified",
        )
    with client_for(lake) as client:
        rows = race_rows(client)
    assert [row["statuses"] for row in rows] == [["projected"]]


def test_a_too_close_report_does_not_join_a_candidates_lead(lake):
    lake.source("other")
    lake.call(
        lake.item(states=["GA"]),
        status="leading",
        winner_name="Jon Doe",
        winner_entity_id="e1",
    )
    lake.call(
        lake.item(source="other", states=["GA"]),
        status="too_close",
        winner_name="Jon Doe",
        winner_entity_id="e1",
    )
    with client_for(lake) as client:
        rows = race_rows(client)
    by_status = {row["statuses"][0]: row for row in rows}
    assert by_status["leading"]["outlets"] == 1
    assert by_status["too_close"]["outlets"] == 1
    assert by_status["too_close"]["winner_name"] is None


def test_conflicting_final_claims_both_show(lake):
    lake.source("other")
    first = lake.item(states=["GA"])
    second = lake.item(source="other", states=["GA"])
    lake.call(first, winner_name="Jon Doe")
    lake.call(second, winner_name="Ann Roe")
    with client_for(lake) as client:
        rows = race_rows(client)
    assert {row["winner_name"] for row in rows} == {"Jon Doe", "Ann Roe"}


def test_race_calls_list_newest_first_so_leads_are_not_pushed_out(lake):
    for number in range(61):
        item = lake.item(NOW - timedelta(minutes=30), states=["GA"])
        lake.call(item, race_key=f"2026-GA-{number:02d}")
    newer = lake.item(NOW - timedelta(minutes=5), states=["OH"])
    lake.call(
        newer, race_key="2026-OH-SEN", status="leading", winner_name="Ann Roe"
    )
    with client_for(lake) as client:
        rows = race_rows(client)
    assert rows[0]["statuses"] == ["leading"]
    assert len(rows) == 60
    assert rows[0]["total"] == 62


def test_the_race_card_says_when_rows_are_left_out(lake):
    for number in range(62):
        item = lake.item(NOW - timedelta(minutes=number), states=["GA"])
        lake.call(item, race_key=f"2026-GA-{number:02d}")
    with client_for(lake) as client:
        page = client.get(
            "/frag/race-calls", params={"as_of": NOW.isoformat()}
        ).text
    assert "60 of 62 shown, newest first" in page


def test_the_race_card_says_nothing_when_all_rows_fit(lake):
    lake.call(lake.item(states=["GA"]))
    with client_for(lake) as client:
        page = client.get(
            "/frag/race-calls", params={"as_of": NOW.isoformat()}
        ).text
    assert " shown," not in page


def test_a_storys_articles_agree_with_its_unique_count(lake):
    lake.story("s1")
    lake.item(NOW - timedelta(hours=2), group="g", storyline="s1")
    for minutes in (20, 10):
        lake.item(NOW - timedelta(minutes=minutes), canonical=False, group="g")
    query = {"as_of": NOW.isoformat(), "window": "1h"}
    with client_for(lake) as client:
        drill = client.app.state.web.drill
        unique = drill.story_articles(Filters.parse(query), "s1", {})
        every = drill.story_articles(
            Filters.parse({**query, "copies": "all"}), "s1", {}
        )
        page = client.get("/story/s1", params=query).text
    assert unique["articles_total"] == 0
    assert every["articles_total"] == 2
    assert "later copies fall inside it" in page
    assert "No stories from this storyline fall" not in page


def test_first_seen_on_a_story_counts_a_copy_seen_first(lake):
    lake.source("early")
    lake.story("s1")
    late = NOW - timedelta(minutes=10)
    early = NOW - timedelta(minutes=25)
    lake.item(late, group="g", storyline="s1")
    copy = lake.item(early, source="early", canonical=False, group="g")
    with client_for(lake) as client:
        web = client.app.state.web
        head = web.drill.story_head(
            Filters.parse({"as_of": NOW.isoformat()}), "s1", {}
        )
    assert head["first_report"]["item_id"] == copy


def test_every_topic_heat_cell_opens_its_count(lake):
    for _ in range(5):
        lake.item(NOW - timedelta(hours=2, minutes=20))
    for _ in range(3):
        lake.item(NOW - timedelta(minutes=10))
    with client_for(lake) as client:
        rows = client.get(f"/api/data/topic_heat?{AS_OF}").json()["table"]
        assert sorted(row["n"] for row in rows) == [3, 5]
        for row in rows:
            opened = drawer(client, row["evidence"])
            assert opened.stories == row["n"]
            assert "all time" not in opened.filter
            assert " 1 h " not in opened.filter


def test_every_bump_point_opens_its_mentions(lake):
    for at in (WINDOW, WINDOW - timedelta(minutes=20)):
        for _ in range(3):
            item_id = lake.item(at)
            lake.mention(item_id, "e1")
            lake.mention(item_id, "e1")
    with client_for(lake) as client:
        rows = client.get(f"/api/data/bump?{AS_OF}").json()["table"]
        assert len(rows) >= 2
        for row in rows:
            opened = drawer(client, row["evidence"])
            assert opened.mentions == row["n"]


def test_every_tone_dot_opens_the_mentions_behind_it(lake):
    lake.source("left", lean="L")
    lake.source("right", lean="R")
    for index in range(8):
        item_id = lake.item(
            source="left", canonical=index == 0, group="copies"
        )
        lake.mention(item_id, "e1", tone=1.0)
    for _ in range(6):
        item_id = lake.item(source="right", opinion=True)
        lake.mention(item_id, "e1", tone=-1.0)
    untoned = lake.item(source="left")
    lake.mention(untoned, "e1", tone=None)
    with client_for(lake) as client:
        rows = client.get(f"/api/data/tone_by_lean?{AS_OF}").json()["table"]
        assert {row["bucket"] for row in rows} == {"L", "All"}
        for row in rows:
            assert row["n"] == 8
            opened = drawer(client, row["evidence"])
            assert opened.mentions == 8
            assert opened.stories == 8
            assert "opinion excluded" in opened.filter


def test_global_press_point_opens_its_count(lake):
    for _ in range(4):
        lake.item(language="es")
    lake.item(WINDOW - timedelta(minutes=40), language="es")
    lake.item()
    with client_for(lake) as client:
        rows = client.get(f"/api/data/global_press?window=3h&{AS_OF}").json()[
            "table"
        ]
        assert rows
        for row in rows:
            opened = drawer(client, row["evidence"], f"window=3h&{AS_OF}")
            assert opened.stories == row["n"]


def test_stories_per_minute_opens_the_last_15_minutes(lake):
    for _ in range(30):
        lake.item(NOW - timedelta(minutes=5))
    for _ in range(70):
        lake.item(NOW - timedelta(minutes=40))
    with client_for(lake) as client:
        panel = client.get(f"/frag/kpis?{AS_OF}").text
        assert '<span class="kpi-value">2.0</span>' in panel
        opened = drawer(client, evidence_attrs(panel)[0])
        assert opened.stories == 30
        assert "last 15 minutes" in opened.filter


def test_rising_card_counts_open_their_stories_and_outlets(lake):
    lake.story("s1")
    for number in range(6):
        lake.source(f"o{number}")
        lake.item(
            NOW - timedelta(minutes=5),
            source=f"o{number}",
            storyline="s1",
            canonical=number == 0,
            group="copies",
        )
    for _ in range(3):
        lake.item(NOW - timedelta(minutes=5), storyline="s1")
    with client_for(lake) as client:
        panel = client.get(f"/frag/rising?{AS_OF}").text
        assert "4 in 15 min" in panel
        assert "7 outlets" in panel
        observed, outlets = evidence_attrs(panel)
        assert drawer(client, observed).stories == 4
        assert drawer(client, outlets).outlets == 7


def test_entity_mention_column_leaves_out_opinion_like_its_drawer(lake):
    lake.writer.add(
        "ref.entities",
        [{"entity_id": "e1", "canonical_name": "Jon Doe", "party": "Other"}],
    )
    for _ in range(6):
        lake.mention(lake.item(), "e1", tone=1.0)
    for _ in range(2):
        lake.mention(lake.item(opinion=True), "e1", tone=1.0)
    with client_for(lake) as client:
        rows = client.get(f"/frag/section/entity/e1/lean?{AS_OF}").text
        query = next(q for q in evidence_attrs(rows))
        assert drawer(client, query).mentions == 6


def test_story_copy_badge_opens_every_copy_it_counts(lake):
    lake.story("s1")
    first = lake.item(storyline="s1", group="wire-copy", states=["GA"])
    lake.item(
        storyline="s1", group="wire-copy", canonical=False, states=["GA"]
    )
    lake.item(
        storyline="s1",
        group="wire-copy",
        canonical=False,
        relevant=False,
        states=["GA"],
    )
    with client_for(lake) as client:
        web = client.app.state.web
        rows = web.drill.story_articles(
            Filters.parse({"as_of": NOW.isoformat()}), "s1", {}
        )["articles"]
        shown = next(row for row in rows if row["item_id"] == first)
        assert shown["copies"] == 2
        opened = drawer(client, "kind=dup&key=wire-copy&copies=all&span=all")
        assert opened.stories == shown["copies"]


def test_sources_counts_follow_the_filters_their_drawers_apply(lake):
    lake.item(states=["GA"])
    lake.item(states=["PA"])
    lake.item(states=["GA"], relevant=False)
    with client_for(lake) as client:
        web = client.app.state.web
        filters = Filters.parse(
            {"as_of": NOW.isoformat(), "focus": "state:GA"}
        )
        row = next(
            row
            for row in web.reference.sources(filters)["sources"]
            if row["source_id"] == "wire"
        )
        assert (row["items"], row["relevant"]) == (2, 1)
        extra = f"focus=state:GA&{AS_OF}"
        every = drawer(
            client,
            "kind=source&key=wire&span=all&rel=any&copies=all",
            extra,
        )
        assert every.stories == row["items"]
        relevant = drawer(
            client, "kind=source&key=wire&span=all&copies=all", extra
        )
        assert relevant.stories == row["relevant"]


# Rising


def test_storyline_card_counts_outlets_across_every_copy(lake):
    lake.story("s1")
    for number in range(6):
        lake.source(f"o{number}")
        lake.item(
            NOW - timedelta(minutes=5),
            source=f"o{number}",
            storyline="s1",
            canonical=number == 0,
            group="copies",
        )
    for _ in range(3):
        lake.item(NOW - timedelta(minutes=5), storyline="s1")
    with client_for(lake) as client:
        web = client.app.state.web
        for query in ({}, {"copies": "all"}):
            cards = web.panels.rising(
                Filters.parse({"as_of": NOW.isoformat(), **query})
            )
            assert cards[0]["outlets"] == 7


def test_rising_lists_only_storylines_that_are_rising(lake):
    lake.story("up", "Going up")
    lake.story("flat", "Too few")
    lake.story("down", "Going down")
    for _ in range(5):
        lake.item(NOW - timedelta(minutes=5), storyline="up")
    for _ in range(2):
        lake.item(NOW - timedelta(minutes=5), storyline="flat")
    for _ in range(40):
        lake.item(NOW - timedelta(minutes=60), storyline="down")
    with client_for(lake) as client:
        text = client.get(f"/frag/rising?{AS_OF}").text
        assert "Going up" in text
        assert "Too few" not in text
        assert "Going down" not in text


def test_rising_says_so_when_nothing_is(lake):
    lake.story("down", "Going down")
    for _ in range(40):
        lake.item(NOW - timedelta(minutes=60), storyline="down")
    with client_for(lake) as client:
        text = client.get(f"/frag/rising?{AS_OF}").text
        assert "No storyline is rising" in text


def test_states_rising_ignores_a_rise_every_state_shares(lake):
    for state, count in (("GA", 40), ("PA", 160)):
        for _ in range(count):
            lake.item(NOW - timedelta(minutes=60), states=[state])
        for _ in range(count):
            lake.item(NOW - timedelta(minutes=5), states=[state])
    with client_for(lake) as client:
        text = client.get(f"/frag/states-rising?{AS_OF}").text
        assert "No state's share of coverage is rising" in text


def test_states_rising_ranks_by_change_in_share_and_opens_its_count(lake):
    for state, before, after in (
        ("PA", 160, 100),
        ("OH", 30, 20),
        ("GA", 10, 40),
    ):
        for _ in range(before):
            lake.item(NOW - timedelta(minutes=60), states=[state])
        for _ in range(after):
            lake.item(NOW - timedelta(minutes=5), states=[state])
    with client_for(lake) as client:
        web = client.app.state.web
        rows = web.panels.states_rising(
            Filters.parse({"as_of": NOW.isoformat()})
        )
        assert rows[0]["usps"] == "GA"
        assert rows[0]["change"] == pytest.approx(
            analytics.attention_index(40, 160, 10, 200)
        )
        assert drawer(client, rows[0]["query"]).stories == 40
        focused = web.panels.states_rising(
            Filters.parse({"as_of": NOW.isoformat(), "focus": "state:PA"})
        )
        assert focused == []


# Coverage by lean


def test_a_copy_counts_in_its_canonicals_storyline_share(lake):
    lake.source("left", lean="LL")
    lake.story("s1")
    lake.item(source="left", group="g", storyline="s1")
    for _ in range(4):
        lake.item(source="left", canonical=False, group="g")
    with client_for(lake) as client:
        rows = client.get(f"/api/data/coverage_matrix?{AS_OF}").json()["table"]
    cells = {(row["storyline_id"], row["bucket"]): row for row in rows}
    assert cells["s1", "LL"]["share"] == 1.0


def test_rising_outlets_include_a_copys_outlet(lake):
    lake.source("left", lean="L")
    lake.story("s1")
    for number in range(3):
        at = NOW - timedelta(minutes=2)
        lake.item(at, group=f"g{number}", storyline="s1")
        lake.item(at, source="left", canonical=False, group=f"g{number}")
    with client_for(lake) as client:
        rows = client.app.state.web.panels.rising(
            Filters.parse({"as_of": NOW.isoformat()})
        )
    assert rows[0]["observed"] == 3
    assert rows[0]["outlets"] == 2


def test_the_copies_control_changes_a_storylines_count(lake):
    lake.story("s1")
    lake.item(group="g", storyline="s1")
    for _ in range(4):
        lake.item(canonical=False, group="g")
    with client_for(lake) as client:
        unique = drawer(client, "kind=storyline&key=s1&span=all")
        every = drawer(client, "kind=storyline&key=s1&span=all&copies=all")
    assert unique.stories == 1
    assert every.stories == 5


def test_a_copy_follows_when_its_canonical_is_assigned_later(lake):
    lake.story("s1")
    canonical = lake.item(group="g")
    copy = lake.item(canonical=False, group="g")
    with client_for(lake) as client:
        model = client.app.state.web.model
        storyline = (
            "SELECT item_id, storyline_id FROM facts WHERE item_id IN "
            "($canonical, $copy)"
        )
        ids = {"canonical": canonical, "copy": copy}
        before = model.query("story", storyline, ids)
        assert {row["storyline_id"] for row in before} == {None}
        lake.writer.add(
            "nlp.storyline_assign",
            [
                {
                    "item_id": canonical,
                    "storyline_id": "s1",
                    "similarity": 0.95,
                    "assigned_at": NOW,
                }
            ],
        )
        lake.commit()
        model.refresh()
        after = model.query("story", storyline + " AND TRUE", ids)
    assert {row["storyline_id"] for row in after} == {"s1"}


def test_bucket_with_no_coverage_reads_as_no_data(lake):
    lake.source("center", lean="C")
    lake.source("left", lean="L")
    lake.story("s1")
    lake.story("s2", "Other story")
    for _ in range(3):
        lake.item(source="center", storyline="s1")
    lake.item(source="left", storyline="s1")
    lake.item(source="left", storyline="s2")
    with client_for(lake) as client:
        rows = client.get(f"/api/data/coverage_matrix?{AS_OF}").json()["table"]
        cells = {(row["storyline_id"], row["bucket"]): row for row in rows}
        assert cells["s1", "R"]["share"] is None
        assert cells["s1", "Non-US"]["share"] is None
        assert cells["s2", "C"]["share"] == 0
        assert cells["s1", "C"]["divergence"] == pytest.approx(0.5)


# Live Wire


class Stream:
    """One `/events` connection, read the way the browser reads it."""

    def __init__(self, app, **params) -> None:
        self.request = SimpleNamespace(
            app=app, query_params=params, is_disconnected=self._connected
        )
        self.response = None
        self.iterator = None

    @staticmethod
    async def _connected() -> bool:
        return False

    async def __aenter__(self):
        self.response = await sse.events(self.request)
        self.iterator = self.response.body_iterator
        return self

    async def __aexit__(self, *_):
        await self.iterator.aclose()

    async def next(self):
        return await asyncio.wait_for(anext(self.iterator), 5)

    async def after_snapshot(self, hub):
        """Publish a snapshot and return the wire rows sent for it."""
        hub.publish({"type": "snapshot"})
        assert (await self.next())["event"] == "snapshot"
        pending = asyncio.ensure_future(anext(self.iterator))
        await asyncio.sleep(0.5)
        hub.publish({"type": "status"})
        event = await asyncio.wait_for(pending, 5)
        if event["event"] == "wire":
            assert (await self.next())["event"] == "status"
            return event["data"]
        return ""


def started(lake):
    app = app_for(lake)
    web = app.state.web
    web.model.refresh(True)
    return app, web


def commit_and_refresh(lake, web):
    lake.commit()
    web.model.refresh()


async def test_a_new_stream_tells_the_browser_to_reload_the_wire(lake):
    lake.item(datetime.now(UTC))
    app, _ = started(lake)
    async with Stream(app) as stream:
        assert (await stream.next())["event"] == "wire-ready"


async def test_reconnecting_loses_no_story(lake):
    now = datetime.now(UTC)
    lake.item(now, item_id="before")
    app, web = started(lake)
    async with Stream(app) as stream:
        await stream.next()
        lake.item(now, item_id="pushed")
        commit_and_refresh(lake, web)
        assert "Headline pushed end" in await stream.after_snapshot(web.hub)
    # Nothing is connected while this one arrives.
    lake.item(now, item_id="while-away")
    commit_and_refresh(lake, web)
    async with Stream(app) as stream:
        assert (await stream.next())["event"] == "wire-ready"
        reloaded = web.panels.wire(Filters())
        assert "while-away" in {row["item_id"] for row in reloaded}
        lake.item(now, item_id="after")
        commit_and_refresh(lake, web)
        pushed = await stream.after_snapshot(web.hub)
        assert "Headline after end" in pushed
        assert "while-away" not in pushed


async def test_story_classified_late_still_reaches_the_wire(lake):
    seen = datetime.now(UTC) - timedelta(minutes=40)
    lake.item(seen, item_id="late", classified=False)
    app, web = started(lake)
    async with Stream(app) as stream:
        await stream.next()
        lake.classify("late", states=["GA"])
        commit_and_refresh(lake, web)
        assert "Headline late end" in await stream.after_snapshot(web.hub)


async def test_story_placed_back_after_a_gap_still_reaches_the_wire(lake):
    now = datetime.now(UTC)
    lake.writer.add(
        "raw.collection_gaps",
        [
            {
                "gap_start": now - timedelta(minutes=50),
                "gap_end": now - timedelta(minutes=1),
                "reason": "sleep",
            }
        ],
    )
    app, web = started(lake)
    async with Stream(app) as stream:
        await stream.next()
        item_id = lake.item(
            now, item_id="in-gap", published=now - timedelta(minutes=45)
        )
        commit_and_refresh(lake, web)
        row = web.panels.article(item_id)
        assert row["first_seen_at"] < row["seen_raw_at"]
        assert "Headline in-gap end" in await stream.after_snapshot(web.hub)


async def test_a_later_update_does_not_push_a_story_twice(lake):
    now = datetime.now(UTC)
    app, web = started(lake)
    async with Stream(app) as stream:
        await stream.next()
        lake.item(now, item_id="once")
        commit_and_refresh(lake, web)
        assert "Headline once end" in await stream.after_snapshot(web.hub)
        lake.classify("once", tone=1.5, states=["GA"])
        commit_and_refresh(lake, web)
        assert await stream.after_snapshot(web.hub) == ""


async def test_stream_honors_the_filters_in_its_url(lake):
    now = datetime.now(UTC)
    app, web = started(lake)
    async with Stream(app, focus="state:GA") as stream:
        await stream.next()
        lake.item(now, item_id="georgia", states=["GA"])
        lake.item(now, item_id="ohio", states=["OH"])
        commit_and_refresh(lake, web)
        pushed = await stream.after_snapshot(web.hub)
        assert "Headline georgia end" in pushed
        assert "Headline ohio end" not in pushed


def test_wire_recent_takes_items_by_when_they_became_visible(lake):
    now = datetime.now(UTC)
    lake.item(now - timedelta(hours=3), item_id="old", classified=False)
    _, web = started(lake)
    through = web.model.refresh_seq
    assert web.panels.wire_recent(Filters(), 0, through) == []
    lake.classify("old", states=["GA"])
    commit_and_refresh(lake, web)
    newer = web.model.refresh_seq
    assert newer > through
    rows = web.panels.wire_recent(Filters(), through, newer)
    assert [row["item_id"] for row in rows] == ["old"]
    assert web.panels.wire_recent(Filters(), newer, newer) == []


def test_item_turning_irrelevant_and_back_is_new_again(lake):
    lake.item(datetime.now(UTC), item_id="flip")
    _, web = started(lake)
    first = web.model.refresh_seq
    lake.classify("flip", relevant=False)
    commit_and_refresh(lake, web)
    lake.classify("flip", relevant=True, states=["GA"])
    commit_and_refresh(lake, web)
    rows = web.panels.wire_recent(Filters(), first, web.model.refresh_seq)
    assert [row["item_id"] for row in rows] == ["flip"]


# A failed refresh


async def test_status_events_keep_flowing_when_refresh_fails(settings):
    def refresh():
        raise duckdb.IOException("catalog read failed")

    model = SimpleNamespace(
        refresh=refresh,
        recent_p95_ms=lambda: 0.0,
        snapshot=SimpleNamespace(snapshot_id=1, committed_at="x", changed=()),
    )
    settings.web.snapshot_poll_seconds = 0.01
    hub = Hub(model, settings)
    queue = hub.subscribe()
    task = asyncio.ensure_future(hub.run())
    event = await asyncio.wait_for(queue.get(), 5)
    task.cancel()
    assert event == {"type": "status"}


def test_strip_names_a_failed_refresh_until_one_succeeds(lake, monkeypatch):
    lake.item(datetime.now(UTC))
    app = app_for(lake)
    web = app.state.web
    web.model.refresh(True)
    assert not any(
        "Cannot read new data" in text
        for text in web.panels.status_strip()["warnings"]
    )
    original = web.model.latest_snapshot

    def failing():
        raise duckdb.IOException("catalog read failed")

    monkeypatch.setattr(web.model, "latest_snapshot", failing)
    with pytest.raises(duckdb.IOException):
        web.model.refresh()
    assert any(
        "Cannot read new data" in text
        for text in web.panels.status_strip()["warnings"]
    )
    monkeypatch.setattr(web.model, "latest_snapshot", original)
    web.model.refresh()
    assert not any(
        "Cannot read new data" in text
        for text in web.panels.status_strip()["warnings"]
    )


# Replay


def test_replay_leaves_out_what_was_first_seen_later(lake):
    lake.item(
        NOW,
        item_id="late",
        published=NOW - timedelta(hours=3),
        states=["GA"],
    )
    lake.item(NOW - timedelta(hours=2), item_id="ontime", states=["GA"])
    earlier = "as_of=" + quote((NOW - timedelta(hours=1)).isoformat())
    with client_for(lake) as client:
        wire = client.get(f"/frag/wire?{earlier}").text
        assert "Headline ontime end" in wire
        assert "Headline late end" not in wire
        assert (
            "Headline late end"
            not in client.get(
                f"/frag/evidence?kind=state&key=GA&window=3h&{earlier}"
            ).text
        )
        web = client.app.state.web
        data = web.panels.map_data(
            Filters.parse(
                {
                    "as_of": (NOW - timedelta(hours=1)).isoformat(),
                    "window": "3h",
                }
            )
        )
        assert data["states"][0]["n"] == 1
        # The rule still places it once it has been seen.
        later = client.get(f"/frag/wire?{AS_OF}").text
        assert later.index("Headline ontime end") < later.index(
            "Headline late end"
        )


def test_article_says_when_it_was_first_seen_after_the_replay_time(lake):
    lake.item(NOW, item_id="late", published=NOW - timedelta(hours=3))
    earlier = "as_of=" + quote((NOW - timedelta(hours=1)).isoformat())
    with client_for(lake) as client:
        assert (
            "First seen after the replay time"
            in client.get(f"/frag/article/late?{earlier}").text
        )
        assert (
            "First seen after the replay time"
            not in client.get(f"/frag/article/late?{AS_OF}").text
        )


def test_article_names_the_reason_it_was_moved(lake):
    lake.writer.add(
        "raw.collection_gaps",
        [
            {
                "gap_start": NOW - timedelta(minutes=50),
                "gap_end": NOW,
                "reason": "sleep",
            }
        ],
    )
    lake.item(NOW, item_id="backlog", published=NOW - timedelta(hours=3))
    lake.item(
        NOW + timedelta(minutes=1),
        item_id="gap",
        published=NOW - timedelta(minutes=20),
    )
    lake.item(NOW, item_id="plain")
    with client_for(lake) as client:
        backlog = client.get("/frag/article/backlog").text
        assert "more than 2 hours" in backlog
        assert "collection gap" not in backlog
        gap = client.get("/frag/article/gap").text
        assert "collection gap" in gap
        assert "more than 2 hours" not in gap
        plain = client.get("/frag/article/plain").text
        assert "time charts place" not in plain


def test_placement_reason_follows_the_two_hour_rule():
    seen = NOW
    base = {"seen_raw_at": seen}
    assert placement_reason({**base, "first_seen_at": seen}) is None
    assert (
        placement_reason({**base, "first_seen_at": seen - timedelta(hours=3)})
        == "backlog"
    )
    assert (
        placement_reason(
            {**base, "first_seen_at": seen - timedelta(minutes=30)}
        )
        == "gap"
    )


def test_active_storylines_kpi_is_as_of_the_replay_time(lake):
    then = NOW - timedelta(hours=5)
    lake.story("s1", created_at=then)
    lake.story(
        "s1", version=2, status="dormant", created_at=NOW - timedelta(hours=1)
    )
    with client_for(lake) as client:
        web = client.app.state.web
        for moment, active in (
            (then + timedelta(minutes=1), 1),
            (NOW, 0),
            (then - timedelta(minutes=1), 0),
        ):
            filters = Filters.parse({"as_of": moment.isoformat()})
            assert web.panels.kpis(filters)["active_storylines"] == active


def test_rising_uses_the_status_at_the_replay_time(lake):
    then = NOW - timedelta(hours=5)
    lake.story("s1", "Busy then", created_at=then - timedelta(hours=1))
    lake.story(
        "s1",
        "Busy then",
        version=2,
        status="dormant",
        created_at=NOW - timedelta(hours=1),
    )
    for _ in range(6):
        lake.item(then - timedelta(minutes=3), storyline="s1")
    earlier = "as_of=" + quote(then.isoformat())
    with client_for(lake) as client:
        assert "Busy then" in client.get(f"/frag/rising?{earlier}").text
        assert "Busy then" not in client.get(f"/frag/rising?{AS_OF}").text


def test_replay_says_labels_and_assignments_are_current(lake):
    with client_for(lake) as client:
        page = client.get(f"/?{AS_OF}").text
        assert re.search(
            r'id="replay-note"\s*>.*labels and assignments are current', page
        )
        assert re.search(r'id="replay-note" hidden', client.get("/").text)


def test_feed_health_is_not_shown_for_a_past_moment(lake):
    with client_for(lake) as client:
        text = client.get(f"/frag/kpis?{AS_OF}").text
        assert "Feed health is known only for the present moment" in text
        live = client.get("/frag/kpis").text
        assert "Feed health is known only" not in live


# The bump chart


def test_every_party_on_the_bump_chart_has_a_color(lake):
    lake.writer.add(
        "ref.entities",
        [
            {"entity_id": "d", "canonical_name": "D", "party": "Democratic"},
            {"entity_id": "r", "canonical_name": "R", "party": "Republican"},
            {"entity_id": "i", "canonical_name": "I", "party": "Independent"},
            {"entity_id": "g", "canonical_name": "G", "party": "Green"},
            {"entity_id": "n", "canonical_name": "N", "party": None},
        ],
    )
    for entity_id in "drign":
        lake.mention(lake.item(), entity_id)
    with client_for(lake) as client:
        rows = client.get(f"/api/data/bump?{AS_OF}").json()["table"]
        assert {row["entity_id"] for row in rows} == set("drign")
        assert {row["party"] for row in rows} <= set(specs.PARTY_DOMAIN)
        parties = {row["entity_id"]: row["party"] for row in rows}
        assert parties["d"] == "Democratic"
        assert parties["r"] == "Republican"
        assert parties["i"] == "Other"


# Merged storylines


def test_chain_of_merges_lands_on_the_last_storyline(lake):
    lake.story("a", "Alpha", version=2, status="merged", merged_into="b")
    lake.story("b", "Beta", version=2, status="merged", merged_into="c")
    lake.story("c", "Gamma")
    for storyline in "ab":
        for _ in range(3):
            lake.item(NOW - timedelta(minutes=5), storyline=storyline)
    with client_for(lake) as client:
        web = client.app.state.web
        rows = web.model.rows(
            "SELECT DISTINCT storyline_id FROM facts "
            "WHERE storyline_id IS NOT NULL"
        )
        assert rows == [{"storyline_id": "c"}]
        cards = client.get(f"/frag/rising?{AS_OF}").text
        assert "Gamma" in cards


def test_open_page_of_a_merged_storyline_shows_the_survivor(lake):
    lake.story("a", "Alpha", version=2, status="merged", merged_into="c")
    lake.story("c", "Gamma")
    for _ in range(3):
        lake.item(NOW - timedelta(minutes=5), storyline="a")
    with client_for(lake) as client:
        head = client.get(f"/frag/section/story/a/head?{AS_OF}").text
        assert "Gamma" in head
        assert '<span class="kpi-value">3</span>' in head
        chart = client.get(f"/api/data/story_volume?key=a&{AS_OF}").json()
        assert sum(row["n"] for row in chart["table"]) == 3


# The default window


def test_every_window_can_be_chosen_when_the_default_is_not_one_hour():
    filters = Filters.parse({}, default_window="3h")
    assert filters.window == "3h"
    assert filters.query_string() == ""
    query = filters.query_string(window="1h")
    assert query == "window=1h"
    assert Filters.parse({"window": "1h"}, default_window="3h").window == "1h"
