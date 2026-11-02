from datetime import UTC, datetime, timedelta

import httpx
import pytest

from monitor.disk import DiskGuard
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect
from monitor.llm.client import LlmRefused
from monitor.llm.schemas import (
    Brief,
    BriefParagraph,
    BriefSentence,
    EntityB,
    EntityResolution,
    EntityResolutionBatch,
    ItemB,
    PlaceB,
    RaceCallB,
    RaceRefB,
)
from monitor.orchestrator import Database
from monitor.parsers.base import Item
from monitor.parsers.briefs import BriefSchedule, BriefWriter
from monitor.parsers.entities import (
    EntityPass,
    EntityResolver,
    normalize_name,
)
from monitor.parsers.extract_b import (
    ExtractParser,
    checked_tone,
    locate,
    truncate_at_paragraph,
)
from monitor.parsers.fetch_text import (
    FetchTextParser,
    canonical_from_html,
    is_paywalled,
    parse_robots,
)
from monitor.parsers.geo import Gazetteer, GeoEntry, normalize_cd
from monitor.parsers.race_calls import (
    appears_in,
    call_confidence,
    call_problem,
    normalize_caller,
)
from monitor.web.panels import PollClose

NOW = datetime(2026, 11, 3, 23, 0, tzinfo=UTC)
AFTER_POLLS = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
ARTICLE = (
    "The Associated Press projected that Maria Rivera won Ohio's Senate "
    "race.\nRivera, a tireless campaigner, thanked volunteers. Tom Cole "
    "leads in early returns in Hamilton County."
)
PAGE = (
    "<html><head><title>T</title><link rel='canonical' "
    "href='https://example.com/story?utm_source=x'></head><body><article>"
    + "<p>Election officials reported record turnout across the county on "
    "Tuesday, with long lines at many precincts.</p>"
    * 12
    + "</article></body></html>"
)


class FakeLlm:
    """Returns queued results or raises queued errors, for any schema."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.attempt = True

    def should_attempt(self):
        return self.attempt

    async def structured(self, purpose, schema, system, user, **options):
        self.calls.append((purpose, user, options))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def entity(**fields):
    defaults = dict(
        name="Maria Rivera",
        surface_forms=["Maria Rivera", "Rivera"],
        ent_type="PERSON",
        role="candidate",
        party="Democratic",
        state="OH",
        office="US Senate",
        district=None,
        tone=1.0,
        tone_evidence="Rivera, a tireless campaigner, thanked volunteers.",
    )
    return EntityB(**{**defaults, **fields})


def race_call(**fields):
    defaults = dict(
        race_desc="Ohio Senate",
        state="OH",
        office="Senate",
        district=None,
        winner_name="Maria Rivera",
        party="Democratic",
        called_by="AP",
        status="projected",
        claimed_at_text=None,
        evidence="The Associated Press projected that Maria Rivera won "
        "Ohio's Senate race.",
    )
    return RaceCallB(**{**defaults, **fields})


def item_b(**fields):
    defaults = dict(
        summary_en="AP projected Rivera won.",
        article_tone=0.2,
        entities=[entity()],
        places=[
            PlaceB(
                name="Hamilton County",
                place_type="county",
                state="OH",
                district=None,
            )
        ],
        races=[
            RaceRefB(state="OH", office="Senate", district=None, measure=None)
        ],
        race_calls=[race_call()],
        topics_refined=["senate_race", "not_a_topic"],
    )
    return ItemB(**{**defaults, **fields})


@pytest.fixture
def gazetteer():
    return Gazetteer(
        counties=[
            GeoEntry("39061", "Hamilton County", "OH", 1000),
            GeoEntry("18057", "Hamilton County", "IN", 2000),
            GeoEntry("13121", "Fulton County", "GA", 500),
        ],
        places=[
            GeoEntry("3915000", "Cincinnati", "OH", 200),
            GeoEntry("1304000", "Atlanta", "GA", 300),
            GeoEntry("4835000", "Springfield", "IL", 10),
            GeoEntry("2970000", "Springfield", "MO", 20),
        ],
    )


@pytest.fixture
def resolver(settings):
    built = EntityResolver(settings)
    built.load(
        [
            {
                "entity_id": "rivera",
                "canonical_name": "Maria Rivera",
                "state": "OH",
                "ent_type": "PERSON",
                "aliases": ["Sen. Rivera", "Марія Рівера"],
            },
            {
                "entity_id": "smith-ga",
                "canonical_name": "John Smith",
                "state": "GA",
                "ent_type": "PERSON",
                "aliases": ["Smith"],
            },
            {
                "entity_id": "smith-mi",
                "canonical_name": "Jane Smith",
                "state": "MI",
                "ent_type": "PERSON",
                "aliases": ["Smith"],
            },
        ]
    )
    return built


# Fetching


def test_robots_rules_by_status():
    agent = "ElectionNightMonitor"
    url = "https://x.org/a"
    assert parse_robots(404, "").can_fetch(agent, url)
    assert not parse_robots(403, "").can_fetch(agent, url)
    assert not parse_robots(503, "").can_fetch(agent, url)
    assert not parse_robots(200, "User-agent: *\nDisallow: /").can_fetch(
        agent, url
    )
    assert parse_robots(200, "User-agent: other\nDisallow: /").can_fetch(
        agent, url
    )


def test_canonical_link_and_paywall_rules():
    assert canonical_from_html(PAGE) == "https://example.com/story"
    metered = Source("s", "S", "https://s.org", paywall="metered")
    free = Source("f", "F", "https://f.org", paywall="none")
    assert is_paywalled(402, "", free)
    assert is_paywalled(200, "short", metered)
    assert not is_paywalled(200, "short", free)
    assert not is_paywalled(200, "x" * 500, metered)


def fetch_parser(settings, handler, source=None, free=100.0):
    settings.nlp.fetch_domain_interval_seconds = 0.0
    source = source or Source("s", "S", "https://example.com")
    return FetchTextParser(
        settings,
        {"s": source},
        DiskGuard(settings, free_space=lambda path: free),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def story():
    return Item("a", source_id="s", url="https://example.com/story", title="T")


async def test_fetch_extracts_text_and_identifies_the_project(settings):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=PAGE)

    item = story()
    output = await fetch_parser(settings, handler).run([item])
    row = output.rows["raw.article_text"][0]
    assert "record turnout" in row["text"]
    assert row["n_chars"] == len(row["text"])
    assert row["canonical_url"] == "https://example.com/story"
    assert item.text == row["text"]
    assert seen == ["/robots.txt", "/story"]


async def test_fetch_honors_robots_and_never_requests_the_page(settings):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /")

    output = await fetch_parser(settings, handler).run([story()])
    assert seen == ["/robots.txt"]
    assert output.statuses["a"][0] == "skipped"
    assert "raw.article_text" not in output.rows


async def test_fetch_skips_hard_paywall_without_a_request(settings):
    def handler(request):
        raise AssertionError("no request expected")

    source = Source("s", "S", "https://example.com", paywall="hard")
    output = await fetch_parser(settings, handler, source).run([story()])
    assert output.statuses["a"] == ("skipped", "hard paywall")


async def test_disk_guard_stops_fetching(settings):
    def handler(request):
        raise AssertionError("no request expected")

    output = await fetch_parser(settings, handler, free=4.0).run([story()])
    assert output.statuses["a"] == ("skipped", "disk guard")


async def test_repeated_refusals_back_off_the_domain(settings):
    pages = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        pages.append(request.url.path)
        return httpx.Response(403)

    parser = fetch_parser(settings, handler)
    for _ in range(5):
        await parser.run([story()])
    assert len(pages) == 3


# Extraction


def test_truncate_prefers_a_paragraph_boundary():
    text = "a" * 20000 + "\n" + "b" * 10000
    assert truncate_at_paragraph(text, "en") == "a" * 20000
    assert truncate_at_paragraph("short", "en") == "short"
    assert len(truncate_at_paragraph("字" * 20000, "zh")) == 7200


def test_tone_needs_a_short_verbatim_quote():
    assert checked_tone(entity(), ARTICLE)[0] == 1.0
    assert checked_tone(entity(tone=9.0), ARTICLE)[0] == 2.0
    invented = entity(tone_evidence="Rivera is a disgrace to the office.")
    assert checked_tone(invented, ARTICLE) == (None, None)
    long_quote = entity(tone_evidence=" ".join(["word"] * 26))
    assert checked_tone(long_quote, ARTICLE) == (None, None)
    assert checked_tone(entity(tone=None), ARTICLE) == (None, None)


def test_locate_finds_character_offsets():
    surface, start, end = locate(["Nobody", "Tom Cole"], ARTICLE)
    assert surface == "Tom Cole"
    assert ARTICLE[start:end] == "Tom Cole"
    assert locate(["Nobody"], ARTICLE) == (None, -1, -1)


def test_a_leading_claim_passes_when_its_evidence_is_in_the_article():
    leading = race_call(
        status="leading",
        winner_name="Tom Cole",
        evidence="Tom Cole leads in early returns in Hamilton County.",
    )
    assert call_problem(leading, ARTICLE) is None


def test_a_claim_whose_evidence_is_not_in_the_article_is_rejected():
    assert call_problem(race_call(), ARTICLE) is None
    invented = race_call(evidence="Rivera won the race, officials said.")
    assert call_problem(invented, ARTICLE) == "not verbatim"


def test_appears_in_ignores_spacing_quotes_and_case():
    assert appears_in("the ASSOCIATED  press", ARTICLE)
    assert appears_in("Ohio’s Senate", ARTICLE)
    assert not appears_in("", ARTICLE)


def test_caller_and_confidence_normalization():
    assert normalize_caller("Associated Press") == "AP"
    assert normalize_caller("Fox News") == "Fox"
    assert normalize_caller("The Daily Planet") == "other"
    assert normalize_caller(None) == "unspecified"
    assert call_confidence("projected", "AP") == 0.9
    assert call_confidence("projected", "unspecified") == 0.7
    assert call_confidence("leading", "AP") == 0.5


def extract_parser(settings, conn, llm, resolver, gazetteer):
    return ExtractParser(
        settings,
        llm,
        Database(conn),
        {"s": Source("s", "S", "https://s.org", home_state="OH")},
        resolver,
        gazetteer,
    )


async def test_extraction_writes_every_table(settings, resolver, gazetteer):
    conn = connect(settings)
    parser = extract_parser(
        settings, conn, FakeLlm(item_b()), resolver, gazetteer
    )
    item = Item(
        "a",
        source_id="s",
        title="T",
        text=ARTICLE,
        states=["OH"],
        first_seen_at=AFTER_POLLS,
    )
    output = await parser.run([item])
    extract = output.rows["nlp.extract_b"][0]
    assert extract["text_basis"] == "full_text"
    assert extract["topics_refined"] == ["senate_race"]
    assert extract["raw"]["race_keys"] == ["2026-OH-SEN"]
    mention = output.rows["nlp.mentions"][0]
    assert mention["entity_id"] == "rivera"
    assert mention["resolved_by"] == "exact"
    assert (
        ARTICLE[mention["char_start"] : mention["char_end"]]
        == (mention["surface"])
    )
    assert mention["tone"] == 1.0
    places = output.rows["nlp.places"]
    assert {row["method"] for row in places} == {"llm", "gazetteer"}
    resolved = next(r for r in places if r["method"] == "gazetteer")
    assert resolved["county_fips"] == "39061"
    call = output.rows["nlp.race_calls"][0]
    assert call["race_key"] == "2026-OH-SEN"
    assert call["called_by"] == "AP"
    assert call["winner_entity_id"] == "rivera"
    conn.close()


async def test_rejected_call_is_not_written(settings, resolver, gazetteer):
    conn = connect(settings)
    result = item_b(race_calls=[race_call(evidence="Not in the article.")])
    parser = extract_parser(
        settings, conn, FakeLlm(result), resolver, gazetteer
    )
    item = Item("a", source_id="s", text=ARTICLE, first_seen_at=NOW)
    output = await parser.run([item])
    assert "nlp.race_calls" not in output.rows
    conn.close()


async def test_extraction_uses_stored_text_or_falls_back_to_summary(
    settings, resolver, gazetteer
):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "raw.article_text",
        [{"item_id": "a", "text": ARTICLE, "n_chars": len(ARTICLE)}],
    )
    writer.flush()
    llm = FakeLlm(item_b(race_calls=[]), item_b(race_calls=[]))
    parser = extract_parser(settings, conn, llm, resolver, gazetteer)
    stored = await parser.run([Item("a", source_id="s")])
    assert stored.rows["nlp.extract_b"][0]["text_basis"] == "full_text"
    summary = await parser.run(
        [Item("b", source_id="s", title="Title", summary="Sum")]
    )
    assert summary.rows["nlp.extract_b"][0]["text_basis"] == "summary"
    assert "Title" in llm.calls[1][1]
    conn.close()


async def test_refusal_marks_only_that_item(settings, resolver, gazetteer):
    conn = connect(settings)
    parser = extract_parser(
        settings, conn, FakeLlm(LlmRefused("x")), resolver, gazetteer
    )
    output = await parser.run([Item("a", source_id="s", text=ARTICLE)])
    assert output.statuses["a"] == ("error", "LlmRefused")
    conn.close()


async def test_source_home_state_used_when_no_places(
    settings, resolver, gazetteer
):
    conn = connect(settings)
    parser = extract_parser(
        settings,
        conn,
        FakeLlm(item_b(places=[], race_calls=[])),
        resolver,
        gazetteer,
    )
    output = await parser.run([Item("a", source_id="s", text=ARTICLE)])
    assert output.rows["nlp.places"][0]["method"] == "source_home"
    assert output.rows["nlp.places"][0]["state_usps"] == "OH"
    conn.close()


# Entity resolution


def test_normalize_name_removes_titles_and_accents():
    assert normalize_name("Sen. José Núñez Jr.") == "jose nunez"
    assert normalize_name("Gov. Maria Rivera") == "maria rivera"


def test_resolver_exact_normalized_and_fuzzy(resolver):
    assert resolver.resolve(["Maria Rivera"], "PERSON", []) == (
        "rivera",
        "exact",
    )
    assert resolver.resolve(["Марія Рівера"], "PERSON", []) == (
        "rivera",
        "exact",
    )
    assert resolver.resolve(["Gov. Maria Rivera"], "PERSON", []) == (
        "rivera",
        "normalized",
    )
    assert resolver.resolve(["Maria Riviera"], "PERSON", ["OH"]) == (
        "rivera",
        "fuzzy",
    )


def test_resolver_uses_state_for_shared_aliases_and_abstains_without_it(
    resolver,
):
    assert resolver.resolve(["Smith"], "PERSON", ["MI"])[0] == "smith-mi"
    assert resolver.resolve(["Smith"], "PERSON", [])[0] is None
    assert resolver.resolve(["Maria Rivera"], "ORG", [])[0] is None
    assert resolver.resolve(["Maria Riviera"], "PERSON", ["GA"])[0] is None


async def test_entity_pass_resolves_new_names_and_appends_copies(
    settings, resolver
):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "nlp.mentions",
        [
            {
                "mention_id": f"m{n}",
                "item_id": f"i{n}",
                "surface": "Sen. Warnock",
                "ent_type": "PERSON",
                "state": "GA",
                "parser": "extract_b",
                "parser_version": "1",
            }
            for n in range(2)
        ],
    )
    writer.flush()
    llm = FakeLlm(
        EntityResolutionBatch(
            resolutions=[
                EntityResolution(
                    surface="Sen. Warnock",
                    canonical_name="Raphael Warnock",
                    is_new_entity=True,
                    is_person=True,
                    party="Democratic",
                    state="GA",
                    office="US Senator",
                )
            ]
        )
    )
    entity_pass = EntityPass(settings, Database(conn), writer, llm, resolver)
    entity_pass.reload = _noop
    counts = await entity_pass.run()
    assert counts == {"sent": 1, "new_entities": 1, "mentions": 2}
    writer.flush()
    rows = conn.execute(
        """
        SELECT mention_id, entity_id, resolved_by FROM lake.nlp.mentions
        QUALIFY row_number() OVER (
            PARTITION BY mention_id ORDER BY created_at DESC) = 1
        """
    ).fetchall()
    assert len(rows) == 2
    assert all(row[1] and row[2] == "llm" for row in rows)
    names = conn.execute(
        "SELECT canonical_name, origin FROM lake.ref.entities"
    ).fetchall()
    assert names == [("Raphael Warnock", "llm")]
    assert await entity_pass.run() == {
        "sent": 0,
        "new_entities": 0,
        "mentions": 0,
    }
    conn.close()


async def _noop():
    return None


# Geography


def test_gazetteer_disambiguates_by_item_state_then_home_then_size(gazetteer):
    def resolve(name, kind, state=None, states=(), home=None):
        return gazetteer.resolve(name, kind, state, None, list(states), home)

    assert resolve("Hamilton County", "county", "OH").county_fips == "39061"
    assert (
        resolve("Hamilton County", "county", states=["IN"]).county_fips
        == "18057"
    )
    assert (
        resolve("Hamilton County", "county", home="OH").county_fips == "39061"
    )
    assert resolve("Hamilton", "county") is None
    assert resolve("Springfield", "city", "MO").place_geoid == "2970000"
    assert resolve("Atlanta", "city").place_geoid == "1304000"
    assert resolve("Atlanta", "city", "OH") is None
    assert resolve("Nowhere", "city", "OH") is None
    assert resolve("Georgia", "state").state_usps == "GA"
    assert resolve("the region", "other") is None


def test_gazetteer_fuzzy_match_within_state(gazetteer):
    match = gazetteer.resolve("Cincinatti", "city", "OH", None, [], None)
    assert match.place_geoid == "3915000"
    assert match.match_score < 1.0


def test_congressional_district_codes(gazetteer):
    assert normalize_cd("TX", 35) == "TX-35"
    assert normalize_cd("PA", "7th") == "PA-07"
    assert normalize_cd("AK", "at-large") == "AK-00"
    assert normalize_cd(None, 3) is None
    match = gazetteer.resolve("district", "cd", "TX", "35", [], None)
    assert match.cd_code == "TX-35"


# Briefs


def schedule(settings, closes=()):
    return BriefSchedule(settings, list(closes))


def test_brief_cadence_follows_live_start(settings):
    plan = schedule(settings)
    live_from = datetime.fromisoformat(settings.llm.brief_live_from)
    assert plan.due(live_from) is True
    plan.last_at = live_from
    assert plan.due(live_from + timedelta(minutes=19)) is False
    assert plan.due(live_from + timedelta(minutes=20)) is True
    earlier = live_from - timedelta(hours=5)
    plan.last_at = earlier
    assert plan.due(earlier + timedelta(minutes=59)) is False
    assert plan.due(earlier + timedelta(minutes=60)) is True


def test_extra_brief_after_a_major_close(settings):
    close_at = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
    major = PollClose(close_at, ("PA", "MI", "NJ"))
    minor = PollClose(close_at + timedelta(hours=1), ("MT",))
    plan = schedule(settings, [major, minor])
    plan.last_at = close_at + timedelta(minutes=9)
    moment = close_at + timedelta(minutes=10)
    assert plan.due(moment) is True
    assert plan.due(moment) is False
    plan.last_at = close_at + timedelta(minutes=69)
    assert plan.due(close_at + timedelta(minutes=70)) is False


def seed_brief_items(writer, count=6, start=NOW):
    writer.add(
        "raw.items",
        [
            {
                "item_id": f"i{n}",
                "source_id": f"s{n % 3}",
                "title": f"Headline {n}",
                "summary": "Summary",
                "first_seen_at": start - timedelta(minutes=n + 1),
                "feed_categories": [],
            }
            for n in range(count)
        ],
    )
    writer.add(
        "nlp.classify_a",
        [
            {
                "item_id": f"i{n}",
                "election_relevant": True,
                "relevance_conf": 0.9,
                "states": ["GA"],
                "levels": [],
                "race_types": [],
                "topics": [],
                "free_tags": [],
                "is_opinion": False,
                "is_results_report": False,
            }
            for n in range(count)
        ],
    )
    writer.flush()


async def test_brief_keeps_cited_sentences_and_records_the_model(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_brief_items(writer)
    result = Brief(
        paragraphs=[
            BriefParagraph(
                sentences=[
                    BriefSentence(text="Polls closed.", citations=["1"]),
                    BriefSentence(text="No source here.", citations=[]),
                    BriefSentence(text="Invented id.", citations=["99"]),
                ]
            )
        ]
    )
    llm = FakeLlm(result)
    brief = BriefWriter(settings, Database(conn), writer, llm)
    written = await brief.write(now=NOW)
    assert written.sentences_kept == 1
    assert written.sentences_dropped == 2
    options = llm.calls[0][2]
    assert options["model"] == "claude-sonnet-5-5"
    assert options["effort"] == "medium"
    assert options["beta_fallback"] is True
    writer.flush()
    rows = conn.execute(
        "SELECT markdown, citations, focus, model FROM lake.nlp.briefs"
    ).fetchall()
    assert rows[0][0] == "Polls closed.[^1]"
    assert "i0" in rows[0][1]
    assert rows[0][2:] == ("all", "claude-sonnet-5-5")
    conn.close()


async def test_brief_needs_enough_items(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_brief_items(writer, count=2)
    llm = FakeLlm()
    brief = BriefWriter(settings, Database(conn), writer, llm)
    assert await brief.write(now=NOW) is None
    assert llm.calls == []
    conn.close()
