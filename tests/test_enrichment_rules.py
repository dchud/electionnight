"""Rules that keep enrichment from stating more than its sources do."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import numpy as np
import pytest
import yaml
from pydantic import BaseModel

from monitor.disk import DiskGuard
from monitor.ingest.sources import Source
from monitor.lake import BatchWriter, connect
from monitor.llm import client as client_module
from monitor.llm.budget import Budget, BudgetState
from monitor.llm.client import (
    LlmClient,
    LlmInvalid,
    LlmTruncated,
    LlmUnavailable,
)
from monitor.llm.schemas import (
    BatchA,
    EntityB,
    EntityResolution,
    EntityResolutionBatch,
    ItemA,
    ItemB,
    PlaceB,
    RaceCallB,
    RaceRefB,
)
from monitor.orchestrator import Database, Stage
from monitor.parsers import classify_a as classify_module
from monitor.parsers.base import RELEVANT_SQL, Item, ParserOutput
from monitor.parsers.briefs import BriefSchedule, BriefWriter
from monitor.parsers.classify_a import (
    ClassifyParser,
    build_system_prompt,
    classify_by_keywords,
    condensed_watchlist,
    load_vocabulary,
    normalize_llm_result,
    states_in_text,
)
from monitor.parsers.dedupe import (
    DedupeIndex,
    DedupeParser,
    hamming,
    normalize_title,
    simhash64,
    title_hash_of,
)
from monitor.parsers.entities import EntityPass, EntityResolver
from monitor.parsers.extract_b import (
    ExtractParser,
    checked_tone,
    quote_problem,
)
from monitor.parsers.fetch_text import FetchTextParser
from monitor.parsers.geo import Gazetteer, GeoEntry, normalize_cd
from monitor.parsers.lang import LangParser, resolve_language
from monitor.parsers.race_calls import (
    call_problem,
    call_row,
    normalize_caller,
)
from monitor.pipeline import Pipeline

CONFIG = Path("config")
AFTER_CLOSE = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
LONG_BODY = (
    "Election officials in Ohio reported steady turnout across the state "
    "on Tuesday, and county boards said counting continued overnight. " * 8
)


class FakeLlm:
    """Returns queued results or raises queued errors, for any schema."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def should_attempt(self):
        return True

    async def structured(self, purpose, schema, system, user, **options):
        self.calls.append((purpose, user, options))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def race_call(**fields):
    defaults = dict(
        race_desc="Ohio Senate",
        state="OH",
        office="Senate",
        district=None,
        winner_name="Maria Rivera",
        party="Democratic",
        called_by=None,
        status="projected",
        claimed_at_text=None,
        evidence="The AP projected that Maria Rivera won.",
    )
    return RaceCallB(**{**defaults, **fields})


def entity(**fields):
    defaults = dict(
        name="Maria Rivera",
        surface_forms=["Maria Rivera", "Rivera"],
        ent_type="PERSON",
        role="candidate",
        party=None,
        state=None,
        office=None,
        district=None,
        tone=None,
        tone_evidence=None,
    )
    return EntityB(**{**defaults, **fields})


def item_b(**fields):
    defaults = dict(
        summary_en="Summary.",
        article_tone=0.0,
        entities=[],
        places=[],
        races=[],
        race_calls=[],
        topics_refined=[],
    )
    return ItemB(**{**defaults, **fields})


def extract_parser(settings, conn, llm, resolver=None, sources=None):
    return ExtractParser(
        settings,
        llm,
        Database(conn),
        sources if sources is not None else {},
        resolver or EntityResolver(settings),
        Gazetteer([], []),
    )


def known(entity_id, name, state, aliases=(), origin="wikipedia"):
    return {
        "entity_id": entity_id,
        "canonical_name": name,
        "state": state,
        "ent_type": "PERSON",
        "aliases": list(aliases),
        "origin": origin,
    }


def unit(*values):
    vector = np.zeros(384, dtype=np.float32)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


def noise(seed):
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=384).astype(np.float32)
    return vector / np.linalg.norm(vector)


async def _noop():
    return None


# Race-call evidence


@pytest.mark.parametrize(
    ("status", "winner", "evidence", "reason"),
    [
        ("projected", "Smith", "The AP called it for Jones.", "winner"),
        ("conceded", "Jones", "Smith conceded on Tuesday.", "winner"),
        ("leading", "Jones", "Smith leads with 40% counted.", "winner"),
        ("projected", None, "The AP projected a winner.", "no winner named"),
        ("leading", None, "One candidate leads.", "no winner named"),
    ],
)
def test_a_claim_must_name_its_candidate(status, winner, evidence, reason):
    call = race_call(status=status, winner_name=winner, evidence=evidence)
    assert reason in call_problem(call, evidence)


@pytest.mark.parametrize(
    ("evidence", "article"),
    [
        ("Cole conceded defeat.", "Cole conceded defeat. Rivera will serve."),
        ("The race was called.", "Rivera holds seat\n\nThe race was called."),
        ("Die AP rief sie aus.", "Rivera gewinnt. Die AP rief sie aus."),
    ],
)
def test_the_candidate_may_be_named_outside_the_evidence(evidence, article):
    call = race_call(winner_name="Rivera", evidence=evidence)
    assert call_problem(call, article) is None


def test_a_contest_the_model_labels_a_primary_is_rejected():
    evidence = "The AP called the race for Rivera."
    labeled = race_call(
        race_desc="Ohio Republican Senate primary", evidence=evidence
    )
    plain = race_call(race_desc="Ohio Senate", evidence=evidence)
    article = f"Rivera won. {evidence}"
    assert call_problem(labeled, article) == "labeled a primary"
    assert call_problem(plain, article) is None


def test_evidence_must_appear_in_the_article():
    call = race_call(status="projected", winner_name="Smith", evidence="x")
    call = call.model_copy(update={"evidence": "Smith won, officials said."})
    assert call_problem(call, "Jones won, officials said.") == "not verbatim"


@pytest.mark.parametrize(
    ("quote", "article"),
    [
        ("Rivera’s win - a first", "Rivera's win \u2014 a first."),
        ("Rivera won  the\nrace", "RIVERA WON THE RACE."),
        ('"Rivera won"', "\u201cRivera won\u201d"),
    ],
)
def test_verbatim_ignores_spacing_case_and_typographic_marks(quote, article):
    call = race_call(winner_name="Rivera", evidence=quote)
    assert call_problem(call, article) is None


@pytest.mark.parametrize(
    "evidence",
    [
        "Former Gov. Ann Cole won the Senate race, AP projected.",
        "Ann Cole defeated former Rep. Bo Hart, AP projected.",
        "Ann Cole won the state's open Senate seat, AP projected.",
        "Ann Cole is projected to win re-election, according to NBC News.",
        "Ann Cole, who was seeking a second term, won re-election Tuesday.",
        "Ann Cole won, a result that could help Democrats.",
        "AP called it for Ann Cole less than an hour after polls closed.",
        "Ann Cole won outright, avoiding a Dec. 1 runoff, AP projected.",
        "Ann Cole (I-NE) defeated Sen. Bo Hart.",
        "Ann Cole won the seat, the first Democrat to do so since 1994.",
        "Bo Hart called Ann Cole to concede the race.",
        "Bo Hart conceded to Ann Cole.",
    ],
)
def test_the_rules_leave_the_meaning_of_a_sentence_to_the_model(evidence):
    call = race_call(winner_name="Ann Cole", evidence=evidence)
    assert call_problem(call, f"Before. {evidence} After.") is None


@pytest.mark.parametrize(
    ("winner", "names", "text"),
    [
        ("Maria Rivera", [], "Según la AP, María Rivera derrotó a Cole."),
        ("Maria Rivera", [], "Rivera est élue sénatrice de l'Ohio."),
        ("Maria Rivera", [], "Riveras Sieg in Ohio ist amtlich."),
        ("Maria Rivera", [], "Коул признал поражение от Риверы."),
        ("Maria Rivera", ["Мария Ривера"], "Победа Марии Риверы признана."),
        ("Maria Rivera", [], "أعلنت وكالة أسوشيتد برس فوز لريفيرا."),
        ("Maria Rivera", [], "据AP预测,里维拉赢得参议院席位。"),
        ("Ривера", [], "Ривера избрана сенатором."),
        ("里维拉", [], "里维拉赢得参议院席位"),
    ],
)
def test_a_name_is_compared_only_in_latin_text(winner, names, text):
    call = race_call(winner_name=winner, evidence=text)
    assert call_problem(call, text, names=names) is None


def test_a_latin_alias_can_name_the_candidate():
    text = "Kowalski won the seat."
    call = race_call(winner_name="Коваль", evidence=text)
    assert call_problem(call, text) is None
    assert call_problem(call, text, names=["Jan Kowalski"]) is None
    assert call_problem(call, text, names=["Jan Nowak"]) is not None


def test_a_state_is_stored_as_its_usps_code():
    call = race_call(state="Georgia")
    row = call_row("i", call, None, None, AFTER_CLOSE, "p", "1", "text")
    assert row["state"] == "GA"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("AP", "AP"),
        ("ap", "AP"),
        ("The Associated Press", "AP"),
        ("the AP", "AP"),
        ("Associated Press (AP)", "AP"),
        ("Fox News Decision Desk", "Fox"),
        ("DecisionDeskHQ", "DDHQ"),
        ("Decision Desk HQ", "DDHQ"),
        ("NBC News", "NBC"),
        ("the New York Times", "NYT"),
        ("Secretary of State", "other"),
        (None, "unspecified"),
        ("unknown", "unspecified"),
    ],
)
def test_caller_names_normalize(value, expected):
    assert normalize_caller(value) == expected


@pytest.mark.parametrize(
    ("called_by", "text", "caller", "confidence"),
    [
        ("AP", "The AP projected that Rivera won.", "AP", 0.9),
        ("AP", "Rivera won, officials said.", "unspecified", 0.7),
        ("CNN", "Rivera won, the AP said.", "unspecified", 0.7),
        ("Secretary of State", "The Secretary of State spoke.", "other", 0.7),
        ("Secretary of State", "Rivera won.", "unspecified", 0.7),
    ],
)
def test_a_caller_stays_only_if_the_article_names_it(
    called_by, text, caller, confidence
):
    call = race_call(called_by=called_by)
    row = call_row("i", call, "2026-OH-SEN", None, AFTER_CLOSE, "p", "1", text)
    assert row["called_by"] == caller
    assert row["confidence"] == confidence


@pytest.mark.parametrize(
    ("first_seen", "published", "state", "kept"),
    [
        # Ohio's polls close at 19:30 Eastern, 00:30 UTC.
        (datetime(2026, 11, 4, 0, 29, tzinfo=UTC), None, "OH", False),
        (datetime(2026, 11, 4, 0, 31, tzinfo=UTC), None, "OH", True),
        (
            datetime(2026, 11, 4, 3, 0, tzinfo=UTC),
            datetime(2026, 11, 2, 15, 0, tzinfo=UTC),
            "OH",
            False,
        ),
        # With no state, the earliest close is 18:00 Eastern, 23:00 UTC.
        (datetime(2026, 11, 3, 22, 59, tzinfo=UTC), None, None, False),
        (datetime(2026, 11, 3, 23, 1, tzinfo=UTC), None, None, True),
    ],
)
async def test_a_call_from_before_the_poll_close_is_rejected(
    settings, first_seen, published, state, kept
):
    conn = connect(settings)
    result = item_b(races=[], race_calls=[race_call(state=state)])
    parser = extract_parser(settings, conn, FakeLlm(result))
    item = Item(
        "a",
        title="T",
        text="The AP projected that Maria Rivera won.",
        first_seen_at=first_seen,
        published_at=published,
    )
    output = await parser.run([item])
    assert bool(output.rows.get("nlp.race_calls")) is kept
    if not kept:
        reasons = [r["reason"] for r in output.rows["nlp.rejected_quotes"]]
        assert reasons == ["before polls closed"]
    conn.close()


async def test_a_rejected_call_adds_no_race_key_to_the_item(settings):
    conn = connect(settings)
    result = item_b(
        race_calls=[race_call(evidence="Not in the article.")],
        races=[
            RaceRefB(state="GA", office="Senate", district=None, measure=None)
        ],
    )
    parser = extract_parser(settings, conn, FakeLlm(result))
    item = Item("a", title="T", text="Body.", first_seen_at=AFTER_CLOSE)
    output = await parser.run([item])
    assert output.rows["nlp.extract_b"][0]["raw"]["race_keys"] == [
        "2026-GA-SEN"
    ]
    conn.close()


async def test_a_race_not_on_the_ballot_has_no_key(settings):
    conn = connect(settings)
    result = item_b(
        race_calls=[race_call(state="NY", race_desc="New York Senate")]
    )
    parser = extract_parser(settings, conn, FakeLlm(result))
    item = Item(
        "a",
        title="T",
        text="The AP projected that Maria Rivera won.",
        first_seen_at=AFTER_CLOSE,
    )
    output = await parser.run([item])
    assert output.rows["nlp.race_calls"][0]["race_key"] is None
    conn.close()


async def test_a_winner_alias_lets_a_translated_call_through(settings):
    conn = connect(settings)
    evidence = "Ривера избрана сенатором."
    resolver = EntityResolver(settings)
    resolver.load(
        [known("rivera", "Maria Rivera", "OH", ["Мария Ривера", "Rivera"])]
    )
    result = item_b(
        race_calls=[race_call(status="declared", evidence=evidence)]
    )
    parser = extract_parser(settings, conn, FakeLlm(result), resolver)
    item = Item("a", title="T", text=evidence, first_seen_at=AFTER_CLOSE)
    output = await parser.run([item])
    assert output.rows["nlp.race_calls"][0]["winner_entity_id"] == "rivera"
    conn.close()


# Tone evidence


def test_a_tone_quote_needs_three_words():
    assert quote_problem("the", "the text") == "too short"
    assert quote_problem("the text", "the text") == "too short"
    assert quote_problem("the whole text", "the whole text") is None
    chinese = "里维拉赢得了参议院席位"
    assert quote_problem("里维拉", chinese) == "too short"
    assert quote_problem(chinese, chinese) is None


def test_a_tone_quote_needs_the_entity_close_by():
    far = "x " * 300
    text = f"Tom Cole spoke. {far} Rivera is a disgrace to the office."
    cole = entity(name="Tom Cole", surface_forms=["Tom Cole"], tone=-2.0)
    about_rivera = cole.model_copy(
        update={"tone_evidence": "Rivera is a disgrace"}
    )
    assert checked_tone(about_rivera, text) == (None, None)
    rivera = entity(tone=-2.0, tone_evidence="is a disgrace to")
    assert checked_tone(rivera, text) == (-2.0, "is a disgrace to")
    nearby = about_rivera.model_copy(
        update={"tone_evidence": "Cole spoke. x x"}
    )
    assert checked_tone(nearby, f"Tom Cole spoke. x x {far}")[0] == -2.0


async def test_a_short_quote_is_recorded_as_rejected(settings):
    conn = connect(settings)
    result = item_b(entities=[entity(tone=-2.0, tone_evidence="the")])
    parser = extract_parser(settings, conn, FakeLlm(result))
    item = Item("a", title="T", text="Rivera said the words.")
    output = await parser.run([item])
    assert output.rows["nlp.mentions"][0]["tone"] is None
    assert output.rows["nlp.rejected_quotes"][0]["reason"] == "too short"
    conn.close()


# Text sent to the model and checked against


async def test_quotes_check_against_title_and_summary_too(settings):
    conn = connect(settings)
    evidence = "AP: Rivera wins Ohio Senate race"
    result = item_b(
        entities=[entity(tone=1.0, tone_evidence=evidence)],
        race_calls=[race_call(evidence=evidence, called_by="AP")],
    )
    parser = extract_parser(settings, conn, FakeLlm(result))
    item = Item(
        "a",
        title=evidence,
        summary="The AP projected Rivera the winner.",
        text="Subscribe to continue reading. Already a subscriber? Log in.",
        first_seen_at=AFTER_CLOSE,
    )
    output = await parser.run([item])
    assert output.rows["nlp.race_calls"][0]["called_by"] == "AP"
    assert output.rows["nlp.mentions"][0]["tone"] == 1.0
    conn.close()


async def test_short_or_paywalled_text_is_sent_with_the_summary(settings):
    conn = connect(settings)
    llm = FakeLlm(item_b(), item_b(), item_b())
    parser = extract_parser(settings, conn, llm)
    summary = "The AP projected Rivera the winner."
    short = Item("a", title="T", summary=summary, text="Subscribe to read.")
    paywalled = Item(
        "b", title="T", summary=summary, text=LONG_BODY, paywalled=True
    )
    full = Item("c", title="T", summary=summary, text=LONG_BODY)
    output = await parser.run([short, paywalled, full])
    sent = [json.loads(call[1])["text"] for call in llm.calls]
    assert summary in sent[0]
    assert summary in sent[1]
    assert summary not in sent[2]
    bases = [row["text_basis"] for row in output.rows["nlp.extract_b"]]
    assert bases == ["summary+text", "summary+text", "full_text"]
    conn.close()


async def test_a_stored_paywall_flag_is_read_back(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "raw.article_text",
        [
            {
                "item_id": "a",
                "text": LONG_BODY,
                "n_chars": len(LONG_BODY),
                "paywalled": True,
            }
        ],
    )
    writer.flush()
    llm = FakeLlm(item_b())
    parser = extract_parser(settings, conn, llm)
    await parser.run([Item("a", title="T", summary="Feed summary here.")])
    assert "Feed summary here." in json.loads(llm.calls[0][1])["text"]
    conn.close()


# Entity attribution


def test_a_surname_from_another_state_is_rejected(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("smith-tx", "John Smith", "TX", ["Smith"]),
            known(
                "schumer",
                "Chuck Schumer",
                "NY",
                ["Schumer"],
                origin="hand",
            ),
            known("trump", "Donald Trump", None, ["Trump"], origin="hand"),
        ]
    )
    assert resolver.resolve(["Smith"], "PERSON", ["OH"]) == (None, None)
    assert resolver.resolve(["Sen. Smith"], "PERSON", ["OH"]) == (None, None)
    assert resolver.resolve(["Smith"], "PERSON", ["TX"])[0] == "smith-tx"
    assert resolver.resolve(["Smith"], "PERSON", [])[0] == "smith-tx"
    assert resolver.resolve(["Schumer"], "PERSON", ["OH"])[0] == "schumer"
    assert resolver.resolve(["Trump"], "PERSON", ["OH"])[0] == "trump"


def test_a_full_name_resolves_whatever_the_item_is_classified_under(settings):
    resolver = EntityResolver(settings)
    resolver.load([known("ossoff", "Jon Ossoff", "GA", ["Ossoff"])])
    for names in (
        ["Jon Ossoff"],
        ["jon ossoff"],
        ["Sen. Jon Ossoff"],
        ["Jon Ossoff", "Ossoff"],
    ):
        assert resolver.resolve(names, "PERSON", ["NC"])[0] == "ossoff"
        assert resolver.resolve(names, "PERSON", ["NC", "ME"])[0] == "ossoff"
    assert resolver.resolve(["Ossoff"], "PERSON", ["NC"]) == (None, None)


def test_a_state_the_text_gives_binds_a_full_name(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("smith-tx", "John Smith", "TX"),
            known("trump", "Donald Trump", None, origin="hand"),
        ]
    )
    assert resolver.resolve(["John Smith"], "PERSON", ["TX"], "OH") == (
        None,
        None,
    )
    assert resolver.resolve(["John Smith"], "PERSON", ["OH"], "TX")[0] == (
        "smith-tx"
    )
    assert resolver.resolve(["Donald Trump"], "PERSON", [], "FL")[0] == "trump"


def test_a_full_name_two_people_hold_still_needs_a_state(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("rogers-mi", "Mike Rogers", "MI"),
            known("rogers-al", "Mike Rogers", "AL"),
        ]
    )
    assert resolver.resolve(["Mike Rogers"], "PERSON", ["MI"])[0] == (
        "rogers-mi"
    )
    for states in ([], ["OH"], ["MI", "AL"]):
        assert resolver.resolve(["Mike Rogers"], "PERSON", states) == (
            None,
            None,
        )


def test_a_full_name_exception_is_for_people_and_suffixes_still_bind(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            {
                **known("gop-ga", "Georgia Republicans", "GA"),
                "ent_type": "ORG",
            },
            known("cooper", "Roy Cooper", "NC"),
        ]
    )
    assert resolver.resolve(["Georgia Republicans"], "ORG", ["OH"]) == (
        None,
        None,
    )
    assert resolver.resolve(["Roy Cooper Jr."], "PERSON", ["OH"]) == (
        None,
        None,
    )


async def test_extraction_reads_a_full_name_from_outside_the_item_states(
    settings,
):
    conn = connect(settings)
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("ossoff", "Jon Ossoff", "GA", ["Ossoff"]),
            known("smith-tx", "John Smith", "TX"),
        ]
    )
    people = [
        entity(name="Jon Ossoff", surface_forms=["Ossoff"]),
        entity(name="John Smith", surface_forms=["Smith"], state="OH"),
    ]
    parser = extract_parser(
        settings, conn, FakeLlm(item_b(entities=people)), resolver
    )
    item = Item(
        "a", title="T", text="Ossoff met Smith.", states=["NC"], summary=""
    )
    output = await parser.run([item])
    rows = {r["surface"]: r["entity_id"] for r in output.rows["nlp.mentions"]}
    assert rows == {"Ossoff": "ossoff", "John Smith": None}
    conn.close()


def test_an_ambiguous_surname_is_not_given_to_either_holder(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("susan", "Susan Collins", "ME", ["Collins"]),
            known("mike", "Mike Collins", "GA", ["Collins"]),
        ]
    )
    for states in (["ME", "GA"], ["GA", "ME"]):
        assert resolver.resolve(["Collins"], "PERSON", states) == (None, None)
        assert resolver.resolve(["Sen. Collins"], "PERSON", states) == (
            None,
            None,
        )
    assert resolver.resolve(["Collins"], "PERSON", ["ME"])[0] == "susan"
    assert resolver.resolve(["Collins"], "PERSON", ["GA", "OH"])[0] == "mike"
    assert resolver.resolve(["Collins"], "PERSON", ["GA"], "ME")[0] == "susan"


def test_an_ambiguous_surname_is_not_given_to_a_similar_name(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("mike", "Mike Johnson", "LA", ["Johnson"]),
            known("ron", "Ron Johnson", "WI", ["Johnson"]),
            known("johnston", "Ann Johnston", "GA", ["Johnston"]),
        ]
    )
    assert resolver.resolve(["Johnson"], "PERSON", ["GA"]) == (None, None)


def test_a_misspelled_surname_two_people_tie_on_is_no_match(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("susan", "Susan Collins", "ME", ["Collins"]),
            known("mike", "Mike Collins", "GA", ["Collins"]),
        ]
    )
    assert resolver.resolve(["Colins"], "PERSON", ["ME", "GA"]) == (None, None)
    assert resolver.resolve(["Colins"], "PERSON", ["ME"])[0] == "susan"


def test_a_surname_alias_does_not_beat_a_disagreeing_full_name(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("trump", "Donald Trump", None, ["Trump"], origin="hand"),
            known("rob", "Rob Menendez", "NJ", ["Menendez"]),
        ]
    )
    for names, states in (
        (["Melania Trump", "Trump"], []),
        (["Fred Trump", "Mr. Trump"], []),
        (["Bob Menendez", "Menendez"], ["NJ"]),
    ):
        assert resolver.resolve(names, "PERSON", states) == (None, None)
    assert resolver.resolve(["Trump"], "PERSON", [])[0] == "trump"
    assert resolver.resolve(["Sen. Trump"], "PERSON", [])[0] == "trump"
    assert resolver.resolve(["Donald J. Trump", "Trump"], "PERSON", [])[0] == (
        "trump"
    )
    assert resolver.resolve(["D. Trump", "Trump"], "PERSON", [])[0] == "trump"


def test_a_generational_suffix_must_match(settings):
    resolver = EntityResolver(settings)
    resolver.load(
        [
            known("trump", "Donald Trump", None, ["Trump"], origin="hand"),
            known(
                "rfk",
                "Robert F. Kennedy Jr.",
                None,
                ["Kennedy"],
                origin="hand",
            ),
        ]
    )
    assert resolver.resolve(["Donald Trump Jr."], "PERSON", []) == (None, None)
    assert resolver.resolve(["Donald Trump Jr.", "Trump"], "PERSON", []) == (
        None,
        None,
    )
    assert resolver.resolve(["Robert F. Kennedy Jr."], "PERSON", [])[0] == (
        "rfk"
    )
    assert resolver.resolve(["Robert F. Kennedy"], "PERSON", [])[0] == "rfk"


async def test_an_unresolved_mention_keeps_the_full_name(settings):
    conn = connect(settings)
    resolver = EntityResolver(settings)
    resolver.load([known("trump", "Donald Trump", None, ["Trump"])])
    person = entity(name="Melania Trump", surface_forms=["Trump"])
    parser = extract_parser(
        settings, conn, FakeLlm(item_b(entities=[person])), resolver
    )
    output = await parser.run([Item("a", title="T", text="Trump spoke.")])
    row = output.rows["nlp.mentions"][0]
    assert row["surface"] == "Melania Trump"
    assert row["entity_id"] is None
    assert row["char_start"] is None
    conn.close()


async def test_entity_state_names_become_codes(settings):
    conn = connect(settings)
    resolver = EntityResolver(settings)
    resolver.load([known("rivera", "Maria Rivera", "OH")])
    person = entity(state="Ohio")
    parser = extract_parser(
        settings, conn, FakeLlm(item_b(entities=[person])), resolver
    )
    output = await parser.run([Item("a", title="T", text="Maria Rivera won")])
    row = output.rows["nlp.mentions"][0]
    assert row["entity_id"] == "rivera"
    assert row["state"] == "OH"
    conn.close()


def mention(mention_id, surface, state=None, item_id="i"):
    return {
        "mention_id": mention_id,
        "item_id": item_id,
        "surface": surface,
        "ent_type": "PERSON",
        "state": state,
        "parser": "extract_b",
        "parser_version": "1",
    }


async def test_entity_pass_applies_only_within_a_state(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "nlp.mentions",
        [
            mention("me1", "Collins", "ME"),
            mention("me2", "Collins", "ME"),
            mention("ga1", "Collins", "GA"),
        ],
    )
    writer.flush()
    resolver = EntityResolver(settings)
    resolver.load([known("collins-ga", "Mike Collins", "GA", ["Collins"])])
    llm = FakeLlm(EntityResolutionBatch(resolutions=[]))
    entity_pass = EntityPass(settings, Database(conn), writer, llm, resolver)
    entity_pass.reload = _noop
    await entity_pass.run()
    writer.flush()
    rows = dict(
        conn.execute(
            """
            SELECT mention_id, entity_id FROM lake.nlp.mentions
            QUALIFY row_number() OVER (
                PARTITION BY mention_id ORDER BY created_at DESC) = 1
            """
        ).fetchall()
    )
    assert rows == {"me1": None, "me2": None, "ga1": "collins-ga"}
    conn.close()


async def test_entity_pass_maps_answers_back_to_their_state(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "nlp.mentions",
        [
            mention("fl1", "Scott", "FL"),
            mention("sc1", "Scott", "SC"),
            mention("none1", "Scott"),
        ],
    )
    writer.flush()
    answer = EntityResolution(
        surface="Scott",
        canonical_name="Rick Scott",
        is_new_entity=True,
        is_person=True,
        party="Republican",
        state="FL",
        office=None,
    )
    llm = FakeLlm(EntityResolutionBatch(resolutions=[answer]))
    entity_pass = EntityPass(
        settings, Database(conn), writer, llm, EntityResolver(settings)
    )
    entity_pass.reload = _noop
    await entity_pass.run()
    writer.flush()
    rows = dict(
        conn.execute(
            """
            SELECT mention_id, entity_id IS NOT NULL
            FROM lake.nlp.mentions
            QUALIFY row_number() OVER (
                PARTITION BY mention_id ORDER BY created_at DESC) = 1
            """
        ).fetchall()
    )
    assert rows == {"fl1": True, "sc1": False, "none1": False}
    conn.close()


async def test_unresolvable_names_do_not_starve_the_rest(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    rows = [
        mention(f"a{n}-{k}", f"Ambiguous{n}")
        for n in range(120)
        for k in range(3)
    ]
    rows += [mention(f"new{n}", f"Newperson{n}") for n in range(5)]
    writer.add("nlp.mentions", rows)
    writer.flush()

    class Echo(FakeLlm):
        async def structured(self, purpose, schema, system, user, **options):
            batch = json.loads(user)
            self.calls.append([entry["surface"] for entry in batch])
            return EntityResolutionBatch(resolutions=[])

    llm = Echo()
    entity_pass = EntityPass(
        settings, Database(conn), writer, llm, EntityResolver(settings)
    )
    entity_pass.reload = _noop
    for _ in range(6):
        await entity_pass.run()
    sent = {surface for batch in llm.calls for surface in batch}
    assert {f"Newperson{n}" for n in range(5)} <= sent
    conn.close()


async def test_a_model_answer_in_another_state_is_a_new_person(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add("nlp.mentions", [mention("m1", "J. Smith")])
    writer.flush()
    resolver = EntityResolver(settings)
    resolver.load([known("smith-tx", "John Smith", "TX")])
    answer = EntityResolution(
        surface="J. Smith",
        canonical_name="John Smith",
        is_new_entity=True,
        is_person=True,
        party=None,
        state="OH",
        office=None,
    )
    llm = FakeLlm(EntityResolutionBatch(resolutions=[answer]))
    entity_pass = EntityPass(settings, Database(conn), writer, llm, resolver)
    entity_pass.reload = _noop
    await entity_pass.run()
    writer.flush()
    entity_id = conn.execute(
        """
        SELECT entity_id FROM lake.nlp.mentions
        QUALIFY row_number() OVER (
            PARTITION BY mention_id ORDER BY created_at DESC) = 1
        """
    ).fetchone()[0]
    assert entity_id not in {None, "smith-tx"}
    conn.close()


async def test_the_pass_resolves_every_full_name_on_the_roster(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    full_names = [f"Person{n} Lastname{n}" for n in range(60)]
    unknown_names = [f"Unknown{n}" for n in range(60)]
    writer.add(
        "nlp.mentions",
        [mention(f"f{n}", name) for n, name in enumerate(full_names)]
        + [mention(f"u{n}", name) for n, name in enumerate(unknown_names)]
        + [mention("elsewhere", "Person0 Lastname0", "OH")],
    )
    writer.flush()
    resolver = EntityResolver(settings)
    resolver.load(
        [known(name, name, "GA", [name.split()[1]]) for name in full_names]
    )
    sent = []

    class Echo(FakeLlm):
        async def structured(self, purpose, schema, system, user, **options):
            sent.extend(entry["surface"] for entry in json.loads(user))
            return EntityResolutionBatch(resolutions=[])

    entity_pass = EntityPass(
        settings, Database(conn), writer, Echo(), resolver
    )
    entity_pass.reload = _noop
    counts = await entity_pass.run()
    writer.flush()
    assert counts["mentions"] == 60
    assert len(sent) == 30
    assert set(sent) <= {*unknown_names, "Person0 Lastname0"}
    rows = dict(
        conn.execute(
            """
            SELECT mention_id, entity_id FROM lake.nlp.mentions
            QUALIFY row_number() OVER (
                PARTITION BY mention_id ORDER BY created_at DESC) = 1
            """
        ).fetchall()
    )
    for n, name in enumerate(full_names):
        assert rows[f"f{n}"] == name
    # The text gave this mention another state, so it stays open.
    assert rows["elsewhere"] is None
    conn.close()


# Dedupe


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "Élections américaines : les démocrates gardent le Sénat",
            "Élections américaines : Trump perd la Géorgie",
        ),
        ("Midterms : Ossoff réélu en Géorgie", "Midterms : Collins battue"),
        ("Election results - Georgia", "Election results - Ohio"),
        ("Live results | Arizona Senate", "Live results | Nevada Governor"),
        ("Live updates - Ossoff wins", "Live updates - Collins wins"),
        (
            "Elecciones EEUU - Trump gana en Florida",
            "Elecciones EEUU - Demócratas pierden Texas",
        ),
        (
            "Georgia Senate race results - Georgia",
            "Georgia Senate race results - Florida",
        ),
    ],
)
async def test_a_trailing_clause_is_not_an_outlet_name(
    settings, first, second
):
    assert normalize_title(first) != normalize_title(second)
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    items = [
        Item("a", title=first, first_seen_at=AFTER_CLOSE, vec=noise(1)),
        Item("b", title=second, first_seen_at=AFTER_CLOSE, vec=noise(2)),
    ]
    await parser.run(items)
    assert items[1].is_canonical is True
    conn.close()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senate race tightens - AP News", "senate race tightens"),
        ("Senate race tightens | The Hill", "senate race tightens"),
        ("Senate race tightens — CNN", "senate race tightens"),
        ("Senate race tightens - The New York Times", "senate race tightens"),
        ("Senate race tightens | Texas Tribune", "senate race tightens"),
    ],
)
def test_an_outlet_suffix_is_still_removed(title, expected):
    assert normalize_title(title) == expected


@pytest.mark.parametrize(
    "title", ["", "!!!", "Live updates", "Election results"]
)
async def test_a_short_title_never_matches_by_hash(settings, title):
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    items = [
        Item("a", title=title, first_seen_at=AFTER_CLOSE, vec=noise(1)),
        Item("b", title=title, first_seen_at=AFTER_CLOSE, vec=noise(2)),
    ]
    await parser.run(items)
    assert items[1].is_canonical is True
    conn.close()


async def test_titles_from_disjoint_states_are_not_duplicates(settings):
    template = (
        "Polls close in {} as voters decide who will represent the state "
        "in the U.S. Senate next year"
    )
    pair = (template.format("Georgia"), template.format("Ohio"))
    assert (
        hamming(
            simhash64(normalize_title(pair[0])),
            simhash64(normalize_title(pair[1])),
        )
        <= 3
    )
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    items = [
        Item(
            "a",
            title=pair[0],
            states=["GA"],
            first_seen_at=AFTER_CLOSE,
            vec=noise(1),
        ),
        Item(
            "b",
            title=pair[1],
            states=["OH"],
            first_seen_at=AFTER_CLOSE,
            vec=noise(2),
        ),
        Item(
            "c",
            title=pair[1],
            states=[],
            first_seen_at=AFTER_CLOSE,
            vec=noise(3),
        ),
    ]
    await parser.run(items)
    assert items[1].is_canonical is True
    assert items[2].is_canonical is False
    conn.close()


def close_to(vector, seed, amount=0.1):
    moved = vector + amount * noise(seed)
    return moved / np.linalg.norm(moved)


async def test_a_wire_copy_tagged_with_other_states_is_one_story(settings):
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    text = noise(1)
    wire = "Voters to decide fate of state taxes this fall"
    first = Item(
        "a",
        source_id="ap",
        title=f"{wire} - AP News",
        states=["MD"],
        first_seen_at=AFTER_CLOSE,
        vec=text,
    )
    copy = Item(
        "b",
        source_id="wtop",
        title=wire,
        states=["VA"],
        first_seen_at=AFTER_CLOSE + timedelta(minutes=1),
        vec=close_to(text, 2),
    )
    output = await parser.run([first, copy])
    assert first.is_canonical is True
    assert copy.is_canonical is False
    groups = {r["item_id"]: r for r in output.rows["nlp.dup_groups"]}
    assert groups["b"]["dup_group_id"] == groups["a"]["dup_group_id"]
    assert groups["b"]["method"] == "title_hash"
    conn.close()


async def test_a_generic_title_in_two_states_is_two_stories(settings):
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    title = "Election results: live updates and what to know"
    items = [
        Item(
            "a",
            source_id="wsb",
            title=title,
            states=["GA"],
            first_seen_at=AFTER_CLOSE,
            vec=noise(1),
        ),
        Item(
            "b",
            source_id="kcra",
            title=title,
            states=["CA"],
            first_seen_at=AFTER_CLOSE,
            vec=noise(2),
        ),
    ]
    await parser.run(items)
    assert [item.is_canonical for item in items] == [True, True]
    conn.close()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (["GA"], []),
        ([], ["GA"]),
        ([], []),
        (["GA", "OH"], ["OH"]),
    ],
)
def test_one_title_merges_when_no_state_conflicts_whatever_the_text(
    settings, first, second
):
    # One copy may have only a title to embed, or a summary the other lacks,
    # so their vectors say nothing. Only two state tags can show a conflict.
    index = DedupeIndex(settings)
    title = title_hash_of("Voters to decide fate of state taxes this fall")
    index.add(title, noise(1), AFTER_CLOSE, "d", "first", "a", states=first)
    found = index.find(title, noise(2), AFTER_CLOSE, "d", "b", states=second)
    assert found is not None
    assert found.group_id == "first"
    assert found.method == "title_hash"


def test_one_title_in_two_states_needs_the_full_text_threshold(settings):
    index = DedupeIndex(settings)
    title = title_hash_of("Voters to decide fate of state taxes this fall")
    text = noise(1)
    index.add(title, text, AFTER_CLOSE, "d", "first", "a", states=["MD"])
    near = close_to(text, 2)
    assert float(near @ text) >= index.min_cosine
    assert index.find(title, near, AFTER_CLOSE, "d", "b", states=["VA"])
    far = close_to(text, 2, amount=1.0)
    assert float(far @ text) < index.min_cosine
    assert index.find(title, far, AFTER_CLOSE, "d", "c", states=["VA"]) is None


def test_a_near_title_in_two_states_stays_apart_even_with_the_same_text(
    settings,
):
    index = DedupeIndex(settings)
    template = (
        "Polls close in {} as voters decide who will represent the state "
        "in the U.S. Senate next year"
    )
    first = title_hash_of(template.format("Georgia"))
    second = title_hash_of(template.format("Ohio"))
    assert first != second
    text = noise(1)
    index.add(first, text, AFTER_CLOSE, "d", "first", "a", states=["GA"])
    assert index.find(second, text, AFTER_CLOSE, "d", "b", states=["OH"]) is (
        None
    )


def test_a_near_match_on_text_needs_a_shared_state(settings):
    index = DedupeIndex(settings)
    vector = noise(1)
    first = title_hash_of("Polls close in Georgia tonight across the state")
    second = title_hash_of("A completely different headline about turnout now")
    index.add(first, vector, AFTER_CLOSE, "d", "first", "a", states=["GA"])
    assert (
        index.find(second, vector, AFTER_CLOSE, "d", "b", states=["OH"])
        is None
    )
    found = index.find(second, vector, AFTER_CLOSE, "d", "c", states=["GA"])
    assert found is not None
    assert found.method == "embedding"


async def test_the_canonical_copy_is_chosen_among_relevant_items(settings):
    conn = connect(settings)
    parser = DedupeParser(
        settings, Database(conn), index=DedupeIndex(settings)
    )
    title = "Georgia Senate race tightens as voters head to polls"
    first = Item(
        "a",
        title=title,
        relevance_conf=0.3,
        first_seen_at=AFTER_CLOSE,
        vec=noise(1),
    )
    second = Item(
        "b",
        title=f"{title} - WSB",
        relevant=True,
        relevance_conf=0.95,
        first_seen_at=AFTER_CLOSE + timedelta(minutes=1),
        vec=noise(2),
    )
    third = Item(
        "c",
        title=title,
        first_seen_at=AFTER_CLOSE + timedelta(minutes=2),
        vec=noise(3),
    )
    await parser.run([first, second, third])
    assert second.is_canonical is True
    assert third.is_canonical is False
    conn.close()


async def test_a_restart_restores_states_and_relevance(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    now = datetime.now(UTC)
    template = (
        "Polls close in {} as voters decide who will represent the state "
        "in the U.S. Senate next year"
    )
    title = template.format("Georgia")
    writer.add(
        "raw.items",
        [
            {
                "item_id": "a",
                "source_id": "s",
                "title": title,
                "first_seen_at": now,
                "feed_categories": [],
            },
            {
                "item_id": "b",
                "source_id": "s",
                "title": title,
                "first_seen_at": now,
                "feed_categories": [],
            },
        ],
    )
    writer.add(
        "nlp.classify_a",
        [
            {
                "item_id": "a",
                "election_relevant": False,
                "relevance_conf": 0.2,
                "states": [],
                "levels": [],
                "race_types": [],
                "topics": [],
                "free_tags": [],
            },
            {
                "item_id": "b",
                "election_relevant": True,
                "relevance_conf": 0.9,
                "states": ["GA"],
                "levels": [],
                "race_types": [],
                "topics": [],
                "free_tags": [],
            },
        ],
    )
    writer.add(
        "nlp.embeddings",
        [
            {"item_id": "a", "vec": noise(1).tolist()},
            {"item_id": "b", "vec": noise(2).tolist()},
        ],
    )
    writer.add(
        "nlp.dup_groups",
        [
            {"item_id": "a", "dup_group_id": "ga", "is_canonical": True},
            {"item_id": "b", "dup_group_id": "gb", "is_canonical": True},
        ],
    )
    writer.flush()
    parser = DedupeParser(settings, Database(conn))
    other_state = Item(
        "c",
        title=template.format("Ohio"),
        states=["OH"],
        relevant=True,
        relevance_conf=0.9,
        first_seen_at=now,
        vec=noise(3),
    )
    same_state = Item(
        "d",
        title=title,
        states=["GA"],
        relevant=True,
        relevance_conf=0.9,
        first_seen_at=now,
        vec=noise(4),
    )
    output = await parser.run([other_state, same_state])
    rows = {row["item_id"]: row for row in output.rows["nlp.dup_groups"]}
    assert rows["c"]["is_canonical"] is True
    assert rows["d"]["dup_group_id"] == "gb"
    conn.close()


# Classification


def test_states_in_text_ignores_parts_of_longer_names():
    assert states_in_text("west virginia lawmakers meet") == ["WV"]
    assert states_in_text("virginia and west virginia vote") == ["VA", "WV"]
    assert states_in_text("the new york times reports") == []
    assert states_in_text("the new york post and new york voters") == ["NY"]


def test_keywords_accept_the_plural_midterms():
    vocabulary = load_vocabulary(CONFIG)
    item = Item("a", title="Midterms: Democrats eye the House")
    assert classify_by_keywords(item, None, vocabulary)["election_relevant"]


def model_result(ref, **fields):
    defaults = dict(
        item_ref=ref,
        election_relevant=True,
        relevance_conf=0.9,
        us_politics=True,
        levels=["national"],
        primary_level="national",
        states=["GA"],
        race_types=[],
        topics=[],
        free_tags=[],
        is_results_report=False,
        is_opinion=False,
        headline_en="Headline",
        summary_en="Summary",
    )
    return ItemA(**{**defaults, **fields})


def response(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def status_error(code):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "failed",
        response=httpx2.Response(code, request=request),
        body=None,
    )


class ScriptedClient:
    """Plays replies in order, or calls a handler for each request."""

    def __init__(self, *replies, handler=None):
        self.replies = list(replies)
        self.handler = handler
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.beta))

    async def beta(self, **params):
        return await self.create(**params)

    async def create(self, **params):
        self.calls.append(params)
        if self.handler is not None:
            return await self.handler(params)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def scripted_llm(settings, *replies, handler=None, budget=None):
    return LlmClient(
        settings,
        None,
        budget or Budget(settings),
        client=ScriptedClient(*replies, handler=handler),
    )


async def test_an_outage_does_not_overwrite_model_rows(settings):
    settings.llm.degrade_after_seconds = 0
    good = BatchA(items=[model_result("0")]).model_dump_json()
    llm = scripted_llm(
        settings,
        response("bad"),
        response("bad"),
        response(good),
        status_error(500),
    )
    parser = ClassifyParser(settings, llm, {})
    first = Item("a", title="Les démocrates", summary="x", language="fr")
    second = Item("b", title="Élections", summary="y", language="fr")
    output = await parser.run([first, second])
    rows = output.rows["nlp.classify_a"]
    assert [(r["item_id"], r["method"]) for r in rows] == [
        ("a", "llm"),
        ("b", "keywords"),
    ]
    assert first.headline_en == "Headline"
    assert first.states == ["GA"]


def test_below_the_threshold_is_stored_as_not_relevant(settings):
    threshold = settings.nlp.relevance_threshold
    parser = ClassifyParser(settings, None, {})
    output = ParserOutput()
    low = Item("a", title="Possibly about the election", summary="x")
    high = Item("b", title="About the election", summary="y")
    for item, confidence in ((low, threshold - 0.1), (high, threshold)):
        result = model_result(
            item.item_id, election_relevant=True, relevance_conf=confidence
        )
        parser._emit(
            output, item, normalize_llm_result(result, item, parser.vocabulary)
        )
    rows = {r["item_id"]: r for r in output.rows["nlp.classify_a"]}
    assert rows["a"]["election_relevant"] is False
    assert low.relevant is False
    assert rows["b"]["election_relevant"] is True
    assert high.relevant is True


async def test_outage_items_return_to_the_model_on_recovery(
    settings, monkeypatch
):
    monkeypatch.setattr(classify_module, "UNAVAILABLE_WAIT_SECONDS", 0.0)
    settings.llm.degrade_after_seconds = 0
    good = BatchA(items=[model_result("0")]).model_dump_json()
    llm = scripted_llm(settings, status_error(500), response(good))
    parser = ClassifyParser(settings, llm, {})
    backlog = []
    parser.requeue = backlog.append
    now = datetime.now(UTC)
    during = Item("a", title="T", first_seen_at=now)
    old = Item("old", title="T", first_seen_at=now - timedelta(hours=7))
    output = await parser.run([during, old])
    assert {r["method"] for r in output.rows["nlp.classify_a"]} == {"keywords"}
    assert backlog == []
    llm.last_probe = -client_module.PROBE_SECONDS
    later = Item("b", title="T", first_seen_at=now)
    output = await parser.run([later])
    assert output.rows["nlp.classify_a"][0]["method"] == "llm"
    assert backlog == [during]
    assert during.backfill is True


async def test_budget_keywords_do_not_return_on_recovery(settings):
    settings.llm.hard_usd_cap = 0
    llm = scripted_llm(settings)
    parser = ClassifyParser(settings, llm, {})
    backlog = []
    parser.requeue = backlog.append
    item = Item("a", title="T", first_seen_at=datetime.now(UTC))
    await parser.run([item])
    assert parser.outage_items == []
    assert backlog == []


async def test_a_budget_cap_holds_the_requeue(settings, monkeypatch):
    monkeypatch.setattr(classify_module, "UNAVAILABLE_WAIT_SECONDS", 0.0)
    settings.llm.degrade_after_seconds = 0
    good = BatchA(items=[model_result("0")]).model_dump_json()
    budget = Budget(settings)
    llm = scripted_llm(
        settings, status_error(500), response(good), budget=budget
    )
    parser = ClassifyParser(settings, llm, {})
    backlog = []
    parser.requeue = backlog.append
    held = Item("a", title="T", first_seen_at=datetime.now(UTC))
    await parser.run([held])
    budget.record(settings.llm.daily_usd_cap)
    assert budget.state() is BudgetState.SOFT
    llm.last_probe = -client_module.PROBE_SECONDS
    await parser.run([Item("b", title="T", first_seen_at=datetime.now(UTC))])
    assert backlog == []
    assert parser.outage_items == [held]


async def test_relevance_reads_only_the_latest_classification(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    base = {"levels": [], "race_types": [], "topics": [], "free_tags": []}
    writer.add(
        "nlp.classify_a",
        [
            {
                **base,
                "item_id": "a",
                "election_relevant": True,
                "relevance_conf": 0.6,
                "states": [],
                "created_at": AFTER_CLOSE,
            },
            {
                **base,
                "item_id": "a",
                "election_relevant": False,
                "relevance_conf": 0.9,
                "states": [],
                "created_at": AFTER_CLOSE + timedelta(minutes=1),
            },
        ],
    )
    writer.add(
        "raw.items",
        [
            {
                "item_id": "a",
                "first_seen_at": AFTER_CLOSE,
                "feed_categories": [],
            }
        ],
    )
    writer.flush()
    where = RELEVANT_SQL.format(threshold=0.5)
    rows = conn.execute(
        f"SELECT item_id FROM lake.raw.items i WHERE {where}"
    ).fetchall()
    assert rows == []
    conn.close()


# Language


@pytest.mark.parametrize(
    "title",
    [
        "Polls close in Maine",
        "Mamdani wins",
        "Abbott vs. Hinojosa",
        "Pelosi retires",
        "Rubio in Caracas",
        "Prop 50 passes",
        "Hochul Stefanik",
    ],
)
async def test_a_short_title_keeps_the_declared_language(title):
    output = await LangParser().run([Item("a", title=title, language="en")])
    assert output.rows["nlp.lang"][0]["language"] == "en"


async def test_long_text_can_override_the_declared_language():
    title = (
        "Les électeurs américains se rendent aux urnes pour choisir les "
        "membres du Congrès mardi"
    )
    output = await LangParser().run([Item("a", title=title, language="en")])
    assert output.rows["nlp.lang"][0]["language"] == "fr"


async def test_short_chinese_counts_as_enough_text():
    title = "美国中期选举投票今天开始，民主党争夺参议院"
    output = await LangParser().run([Item("a", title=title, language="en")])
    assert output.rows["nlp.lang"][0]["language"] == "zh"


@pytest.mark.parametrize("title", ["2026", "!!!", ""])
async def test_text_with_no_letters_is_not_given_a_language(title):
    item = Item("a", title=title, language="")
    output = await LangParser().run([item])
    assert output.rows["nlp.lang"][0]["language"] == "en"


def test_resolve_language_uses_length():
    assert resolve_language("en", "de", 0.97, 16)[0] == "en"
    assert resolve_language("en", "de", 0.97, 40)[0] == "de"
    assert resolve_language("", "de", 0.97, 16)[0] == "de"
    assert resolve_language("", "ja", 0.0, 0)[0] == "en"


# Watchlist


def test_watchlist_names_candidates_and_has_no_empty_lines(settings):
    ref = settings.paths.ref
    ref.mkdir(parents=True, exist_ok=True)
    races = yaml.safe_load((CONFIG / "races.yaml").read_text("utf-8"))["races"]
    race = next(
        r
        for r in races
        if r["race_key"] == "2026-GA-SEN" and r.get("candidates")
    )
    rows = [
        {
            "entity_id": race["candidates"][0],
            "canonical_name": "Jon Ossoff",
            "party": "Democratic",
        },
        {
            "entity_id": race["candidates"][1],
            "canonical_name": "Minor Candidate",
            "party": "Libertarian",
        },
    ]
    (ref / "entities.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    named = condensed_watchlist(CONFIG, ref).splitlines()
    assert "2026-GA-SEN: Jon Ossoff" in named
    assert all(not line.endswith(": ") for line in named)
    assert not any("Minor Candidate" in line for line in named)
    assert any(line.startswith("2026-AR-M-") for line in named)


def test_watchlist_without_a_roster_has_no_empty_lines():
    lines = condensed_watchlist(CONFIG).splitlines()
    assert lines
    assert all(":" not in line for line in lines)
    assert all("-M-" in line for line in lines)


def test_the_prompt_stays_stable_with_names(settings):
    vocabulary = load_vocabulary(CONFIG)
    first = build_system_prompt(vocabulary, CONFIG, settings.paths.ref)
    assert first == build_system_prompt(vocabulary, CONFIG, settings.paths.ref)
    assert "{watchlist}" not in first


# Briefs


def seed_items(writer, per_source):
    rows = []
    classes = []
    for source, count in per_source.items():
        for n in range(count):
            item_id = f"{source}-{n}"
            rows.append(
                {
                    "item_id": item_id,
                    "source_id": source,
                    "title": f"Headline {item_id}",
                    "summary": "Summary",
                    "first_seen_at": AFTER_CLOSE - timedelta(minutes=n + 1),
                    "feed_categories": [],
                }
            )
            classes.append(
                {
                    "item_id": item_id,
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
            )
    writer.add("raw.items", rows)
    writer.add("nlp.classify_a", classes)
    writer.flush()


async def test_one_outlet_cannot_fill_a_brief(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_items(writer, {"hostile": 60, "a": 4, "b": 4, "c": 4})
    brief = BriefWriter(settings, Database(conn), writer, FakeLlm())
    data = await brief._gather(
        AFTER_CLOSE - timedelta(hours=2), AFTER_CLOSE, "all"
    )
    counts: dict[str, int] = {}
    for item_id in data.references.values():
        source = item_id.split("-")[0]
        counts[source] = counts.get(source, 0) + 1
    assert counts == {"hostile": 3, "a": 3, "b": 3, "c": 3}
    conn.close()


async def test_a_failed_brief_waits_for_the_next_interval(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_items(writer, {"a": 3, "b": 3})
    llm = FakeLlm(LlmTruncated("briefs"), LlmTruncated("briefs"))
    brief = BriefWriter(settings, Database(conn), writer, llm)
    brief.schedule.closes = []
    assert await brief.maybe_write(AFTER_CLOSE) is False
    assert len(llm.calls) == 1
    assert await brief.maybe_write(AFTER_CLOSE + timedelta(minutes=1)) is False
    assert len(llm.calls) == 1
    interval = timedelta(minutes=settings.llm.brief_minutes)
    assert await brief.maybe_write(AFTER_CLOSE + interval) is False
    assert len(llm.calls) == 2
    conn.close()


async def test_requeued_items_go_behind_live_items(settings):
    pipeline = Pipeline(settings, no_llm=True)
    stages = pipeline.build_stages()
    classify = next(s for s in stages if s.parser.name == "classify_a")
    classify.parser.requeue(Item("a"))
    assert len(classify.backlog) == 1
    assert classify.live.qsize() == 0
    pipeline.conn.close()


def test_a_failed_attempt_does_not_move_the_window(settings):
    plan = BriefSchedule(settings, [])
    plan.last_at = AFTER_CLOSE
    plan.last_attempt_at = AFTER_CLOSE + timedelta(minutes=20)
    minute = AFTER_CLOSE + timedelta(minutes=21)
    assert plan.due(minute) is False
    assert plan.due(AFTER_CLOSE + timedelta(minutes=40)) is True


async def test_a_brief_has_room_for_adaptive_thinking(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    seed_items(writer, {"a": 3, "b": 3})
    llm = FakeLlm(LlmTruncated("briefs"))
    brief = BriefWriter(settings, Database(conn), writer, llm)
    with pytest.raises(LlmTruncated):
        await brief.write(now=AFTER_CLOSE)
    options = llm.calls[0][2]
    assert options["max_tokens"] >= 16000
    assert options["request_timeout"] >= 300
    conn.close()


async def test_a_brief_passes_its_timeout_to_the_sdk(settings):
    from monitor.llm.schemas import Brief

    reply = response(Brief(paragraphs=[]).model_dump_json())
    llm = scripted_llm(settings, reply)
    await llm.structured(
        "briefs",
        Brief,
        "s",
        "u",
        max_tokens=16000,
        request_timeout=300.0,
        beta_fallback=True,
    )
    assert llm.client.calls[0]["timeout"] == 300.0


# Client failure handling


class Answer(BaseModel):
    word: str


@pytest.mark.parametrize("code", [400, 413])
async def test_a_rejected_request_is_invalid_not_an_outage(settings, code):
    llm = scripted_llm(settings, status_error(code))
    with pytest.raises(LlmInvalid):
        await llm.structured("classify_a", Answer, "s", "u")
    assert len(llm.client.calls) == 1


async def test_a_rejected_batch_splits_and_falls_back(settings):
    good = BatchA(items=[model_result("0")]).model_dump_json()
    llm = scripted_llm(
        settings, status_error(413), response(good), status_error(413)
    )
    parser = ClassifyParser(settings, llm, {})
    items = [Item("a", title="T"), Item("b", title="T")]
    output = await asyncio.wait_for(parser.run(items), 5)
    methods = {
        r["item_id"]: r["method"] for r in output.rows["nlp.classify_a"]
    }
    assert methods == {"a": "llm", "b": "keywords"}


async def test_a_rejected_extraction_marks_the_item(settings):
    conn = connect(settings)
    llm = scripted_llm(settings, status_error(400))
    parser = extract_parser(settings, conn, llm)
    output = await asyncio.wait_for(
        parser.run([Item("a", title="T", text="Body.")]), 5
    )
    assert output.statuses["a"] == ("error", "LlmInvalid")
    conn.close()


async def test_a_credit_refusal_waits_and_does_not_mark_the_item(
    settings, monkeypatch
):
    monkeypatch.setattr("monitor.parsers.extract_b.WAIT_SECONDS", 0.0)
    conn = connect(settings)
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    refusal = anthropic.BadRequestError(
        "Error code: 400 - Your credit balance is too low to access the "
        "Anthropic API.",
        response=httpx2.Response(400, request=request),
        body=None,
    )
    llm = scripted_llm(settings, refusal, response(item_b().model_dump_json()))
    parser = extract_parser(settings, conn, llm)
    output = await asyncio.wait_for(
        parser.run([Item("a", title="T", text="Body.")]), 5
    )
    assert output.statuses == {}
    assert len(output.rows["nlp.extract_b"]) == 1
    conn.close()


async def hang(_params):
    await asyncio.sleep(3600)


async def test_a_hung_api_is_noticed_within_the_window(settings, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(client_module, "COLD_WAIT_SECONDS", 0.01)
    settings.llm.degrade_after_seconds = 0.05
    llm = scripted_llm(settings, handler=hang)
    task = asyncio.ensure_future(
        llm.structured("classify_a", Answer, "s", "u")
    )
    await asyncio.sleep(0.15)
    assert llm.degraded is True
    assert llm.should_attempt() is True
    assert llm.should_attempt() is False
    task.cancel()


async def test_a_hung_call_ends_at_its_deadline(settings, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(client_module, "COLD_WAIT_SECONDS", 0.01)
    settings.llm.degrade_after_seconds = 0.05
    llm = scripted_llm(settings, handler=hang)
    with pytest.raises(LlmUnavailable):
        await asyncio.wait_for(
            llm.structured("classify_a", Answer, "s", "u"), 2
        )
    assert llm.degraded is True


async def test_hung_calls_do_not_queue_behind_a_lock(settings, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(client_module, "COLD_WAIT_SECONDS", 0.01)
    settings.llm.degrade_after_seconds = 0.1
    llm = scripted_llm(settings, handler=hang)
    started = time.monotonic()

    async def one():
        with pytest.raises(LlmUnavailable):
            await llm.structured("classify_a", Answer, "s", "u")

    await asyncio.wait_for(asyncio.gather(*(one() for _ in range(6))), 2)
    assert time.monotonic() - started < 0.4


async def test_a_slow_answer_still_succeeds(settings, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 2.0)
    settings.llm.degrade_after_seconds = 0.05

    async def slow(_params):
        await asyncio.sleep(0.2)
        return response('{"word": "ok"}')

    llm = scripted_llm(settings, handler=slow)
    task = asyncio.ensure_future(
        llm.structured("classify_a", Answer, "s", "u")
    )
    await asyncio.sleep(0.1)
    assert llm.degraded is True
    assert (await task).word == "ok"
    assert llm.degraded is False


async def test_a_long_brief_does_not_read_as_a_hung_api(settings, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 2.0)
    settings.llm.degrade_after_seconds = 0.05

    async def slow(_params):
        await asyncio.sleep(0.2)
        return response('{"word": "ok"}')

    llm = scripted_llm(settings, handler=slow)
    task = asyncio.ensure_future(
        llm.structured("briefs", Answer, "s", "u", request_timeout=30.0)
    )
    await asyncio.sleep(0.1)
    assert llm.degraded is False
    assert (await task).word == "ok"


async def test_answers_to_other_calls_show_the_api_is_alive(
    settings, monkeypatch
):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(client_module, "COLD_WAIT_SECONDS", 0.0)
    settings.llm.degrade_after_seconds = 0.15

    async def mixed(params):
        if params["messages"][-1]["content"] == "slow":
            await asyncio.sleep(0.5)
        return response('{"word": "ok"}')

    llm = scripted_llm(settings, handler=mixed)
    llm.gates["classify_a"] = client_module._CacheGate(
        last_success=time.monotonic()
    )
    slow = asyncio.ensure_future(
        llm.structured("classify_a", Answer, "s", "slow")
    )
    for _ in range(4):
        await asyncio.sleep(0.05)
        await llm.structured("classify_a", Answer, "s", "fast")
        assert llm.degraded is False
    await slow


async def test_keywords_run_soon_after_the_api_hangs(settings, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(client_module, "COLD_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(client_module, "PROBE_SECONDS", 0.05)
    monkeypatch.setattr(classify_module, "UNAVAILABLE_WAIT_SECONDS", 0.01)
    settings.llm.degrade_after_seconds = 0.2
    llm = scripted_llm(settings, handler=hang)
    parser = ClassifyParser(settings, llm, {})
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    stage = Stage(
        parser, Database(conn), writer, linger_seconds=0.0, backfill=False
    )
    for n in range(60):
        stage.enqueue(Item(f"i{n}", title="Midterm results"))
    tasks = [asyncio.ensure_future(stage.worker()) for _ in range(6)]
    await asyncio.sleep(1.0)
    for task in tasks:
        task.cancel()
    assert stage.finished == 60
    conn.close()


# Geography


def gazetteer():
    return Gazetteer(
        counties=[
            GeoEntry("13121", "Fulton County", "GA", 1000),
            GeoEntry("17057", "Fulton County", "IL", 2000),
        ],
        places=[
            GeoEntry("1", "Paris", "TX", 50),
            GeoEntry("2", "Paris", "TN", 40),
        ],
    )


def test_no_state_context_means_no_county():
    places = gazetteer()
    assert places.resolve("Fulton County", "county", None, None, [], None) is (
        None
    )
    assert places.resolve("Paris", "city", None, None, [], None) is None
    assert places.resolve("Paris", "city", None, None, ["GA"], None) is None
    match = places.resolve("Fulton County", "county", None, None, ["GA"], None)
    assert match.county_fips == "13121"
    match = places.resolve("Fulton County", "county", None, None, [], "IL")
    assert match.county_fips == "17057"


@pytest.mark.parametrize(
    ("name", "state", "district", "item_states", "home", "expected"),
    [
        ("TX-35", None, None, ["CA"], None, "TX-35"),
        ("TX-35", None, None, [], "CA", "TX-35"),
        ("Georgia's 13th", None, None, ["FL"], None, "GA-13"),
        ("TX-35", None, "35", ["CA"], None, "TX-35"),
        ("district", "TX", "35", [], None, "TX-35"),
    ],
)
def test_a_district_code_keeps_its_own_state(
    name, state, district, item_states, home, expected
):
    match = gazetteer().resolve(name, "cd", state, district, item_states, home)
    assert match.cd_code == expected
    assert match.state_usps == expected[:2]


@pytest.mark.parametrize(
    "name", ["2026 midterm district", "99", "City Council District 5"]
)
def test_text_that_is_not_a_district_has_no_code(name):
    assert gazetteer().resolve(name, "cd", "GA", None, [], None) is None


def test_normalize_cd_reads_only_a_real_district():
    assert normalize_cd("GA", "2026") is None
    assert normalize_cd("GA", "99") is None
    assert normalize_cd("GA", "TX-35") == "TX-35"


async def test_places_without_context_write_no_gazetteer_row(settings):
    conn = connect(settings)
    place = PlaceB(
        name="Fulton County", place_type="county", state=None, district=None
    )
    llm = FakeLlm(item_b(places=[place]))
    parser = ExtractParser(
        settings,
        llm,
        Database(conn),
        {},
        EntityResolver(settings),
        gazetteer(),
    )
    output = await parser.run([Item("a", title="T", text="Fulton County.")])
    rows = output.rows["nlp.places"]
    assert [row["method"] for row in rows] == ["llm"]
    conn.close()


# Fetching


def fetch_parser(settings, handler, gate=None):
    settings.nlp.fetch_domain_interval_seconds = 0.0
    return FetchTextParser(
        settings,
        {"s": Source("s", "S", "https://example.com")},
        DiskGuard(settings, free_space=lambda path: 100.0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gate=gate,
    )


class OnlineGate:
    async def wait_online(self):
        return None

    async def reachable(self):
        return True


@pytest.mark.parametrize(
    ("encoding", "sentence"),
    [
        ("windows-1251", "Выборы в Соединённых Штатах проходят сегодня."),
        ("gb2312", "美国中期选举今天开始投票，民主党和共和党争夺参议院。"),
        ("iso-8859-1", "Les électeurs américains ont voté pour le Sénat."),
    ],
)
async def test_a_charset_declared_in_the_page_is_honored(
    settings, encoding, sentence
):
    body = " ".join([sentence] * 12)
    page = (
        f"<html><head><meta charset='{encoding}'><title>T</title></head>"
        f"<body><article><p>{body}</p><p>{body}</p></article></body></html>"
    ).encode(encoding)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, content=page, headers={"content-type": "text/html"}
        )

    item = Item("a", source_id="s", url="https://example.com/a", title="T")
    output = await fetch_parser(settings, handler).run([item])
    text = output.rows["raw.article_text"][0]["text"]
    assert sentence in text
    assert "�" not in text


async def test_a_charset_in_the_header_is_honored(settings):
    sentence = "Les électeurs américains ont voté pour le Sénat."
    body = " ".join([sentence] * 12)
    page = (
        f"<html><head><title>T</title></head><body><article><p>{body}</p>"
        f"<p>{body}</p></article></body></html>"
    ).encode("latin-1")

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=page,
            headers={"content-type": "text/html; charset=ISO-8859-1"},
        )

    item = Item("a", source_id="s", url="https://example.com/a", title="T")
    output = await fetch_parser(settings, handler).run([item])
    assert sentence in output.rows["raw.article_text"][0]["text"]


async def test_a_robots_timeout_blocks_the_domain_only_briefly(settings):
    calls = {"robots": 0, "pages": 0}

    def handler(request):
        if request.url.path == "/robots.txt":
            calls["robots"] += 1
            # The first request, and its one retry for plain bytes.
            if calls["robots"] <= 2:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(404)
        calls["pages"] += 1
        return httpx.Response(200, text="<html><body><p>x</p></body></html>")

    parser = fetch_parser(settings, handler, gate=OnlineGate())
    story = Item("a", source_id="s", url="https://example.com/a", title="T")
    first = await parser.run([story])
    assert first.statuses["a"][0] == "skipped"
    assert calls["pages"] == 0
    # Still inside the retry delay, nothing is requested again.
    await parser.run([story])
    assert calls["robots"] == 2
    entry = parser.robots["example.com"]
    entry.fetched_at -= entry.ttl + 1
    second = await parser.run([story])
    assert "a" not in second.statuses
    assert calls["pages"] == 1


async def test_a_server_error_on_robots_blocks_only_briefly(settings):
    def handler(request):
        return httpx.Response(503)

    parser = fetch_parser(settings, handler, gate=OnlineGate())
    story = Item("a", source_id="s", url="https://example.com/a", title="T")
    await parser.run([story])
    assert parser.robots["example.com"].ttl < 3600


async def test_a_paywalled_fetch_marks_the_item(settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(403)

    item = Item("a", source_id="s", url="https://example.com/a", title="T")
    await fetch_parser(settings, handler).run([item])
    assert item.paywalled is True


# Stages


async def test_a_failed_hydrate_leaves_items_owed(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "raw.items",
        [{"item_id": "x", "title": "T", "first_seen_at": AFTER_CLOSE}],
    )
    writer.flush()

    class Recording:
        name = "p"
        version = "1"
        batch_size = 1
        max_concurrency = 1
        input_query = "SELECT item_id FROM lake.raw.items LIMIT $limit"

        async def run(self, batch):
            return ParserOutput()

    database = Database(conn)
    stage = Stage(Recording(), database, writer)
    real_fetch = database.fetch
    calls = {"n": 0}

    async def flaky(sql, params=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database is locked")
        return await real_fetch(sql, params)

    database.fetch = flaky
    with pytest.raises(RuntimeError):
        await stage._load_backlog()
    assert stage.tracked == set()
    await stage._load_backlog()
    assert stage.tracked == {"x"}
    assert [item.item_id for item in stage.backlog] == ["x"]
    conn.close()


async def test_a_live_item_arriving_during_hydrate_is_not_doubled(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "raw.items",
        [{"item_id": "x", "title": "T", "first_seen_at": AFTER_CLOSE}],
    )
    writer.flush()

    class Recording:
        name = "p"
        version = "1"
        batch_size = 1
        max_concurrency = 1
        input_query = "SELECT item_id FROM lake.raw.items LIMIT $limit"

        async def run(self, batch):
            return ParserOutput()

    database = Database(conn)
    stage = Stage(Recording(), database, writer)
    real_fetch = database.fetch
    calls = {"n": 0}

    async def racing(sql, params=None):
        calls["n"] += 1
        if calls["n"] == 2:
            stage.enqueue(Item("x", title="T"))
        return await real_fetch(sql, params)

    database.fetch = racing
    await stage._load_backlog()
    assert stage.live.qsize() == 1
    assert len(stage.backlog) == 0
    conn.close()


async def test_the_storyline_pass_runs_on_the_event_loop(settings):
    seen = []

    class Recording:
        def periodic(self):
            seen.append(threading.current_thread())

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.storylines = Recording()
    await pipeline.storyline_pass_job()
    assert seen == [threading.current_thread()]


def test_merge_pass_uses_the_merged_centroid(settings):
    from monitor.parsers.storylines import StorylineEngine

    engine = StorylineEngine(settings)
    engine.join = 0.999
    engine.merge_threshold = 0.95
    now = AFTER_CLOSE
    ids = []
    for n, vector in enumerate(
        (unit(1, 0), unit(1, 0.05), unit(1, 0.1), unit(0, 1))
    ):
        storyline, _ = engine.assign(
            f"i{n}",
            vector,
            "t",
            "s",
            now + timedelta(minutes=n),
            [],
            None,
        )
        storyline.status = "active"
        ids.append(storyline.storyline_id)
    merged = engine.merge_pass()
    assert {newer for newer, _ in merged} == set(ids[1:3])
    assert engine.storylines[ids[3]].status == "active"
