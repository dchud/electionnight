"""Stage B: deep extraction from full text, or the summary if none exists."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from importlib import resources
from typing import Any

from monitor.ingest.sources import Source
from monitor.lake import hash_id
from monitor.llm.client import (
    LlmBudgetStop,
    LlmClient,
    LlmInvalid,
    LlmRefused,
    LlmTruncated,
    LlmUnavailable,
)
from monitor.llm.schemas import EntityB, ItemB, PlaceB, RaceCallB
from monitor.logs import get
from monitor.orchestrator import Database
from monitor.parsers.base import (
    RELEVANT_SQL,
    Item,
    ParserOutput,
    needs_parser_sql,
)
from monitor.parsers.classify_a import Vocabulary, load_vocabulary
from monitor.parsers.entities import EntityResolver, normalize_name
from monitor.parsers.fetch_text import SHORT_TEXT_CHARS
from monitor.parsers.geo import Gazetteer
from monitor.parsers.race_calls import (
    CJK_RE,
    appears_in,
    call_problem,
    call_row,
    fold,
    names_person,
)
from monitor.ref.races import (
    BALLOT_MEASURE_CODE,
    load_races_yaml,
    normalize_race_key,
    normalize_state,
)
from monitor.settings import Settings
from monitor.web.panels import load_poll_closes

log = get(__name__)

# Characters per token, approximately, for the 6,000-token input limit.
CHARS_PER_TOKEN = {"zh": 1.2, "ja": 1.2, "ko": 1.5, "ar": 2.5, "ru": 3.0}
DEFAULT_CHARS_PER_TOKEN = 4.0
MAX_INPUT_TOKENS = 6000
MAX_OUTPUT_TOKENS = 4000
EVIDENCE_MAX_WORDS = 25
EVIDENCE_MIN_WORDS = 3
# How far from a tone quote an entity's name may be, in characters.
NAME_WINDOW_CHARS = 200
WAIT_SECONDS = 10.0
BUDGET_WAIT_SECONDS = 60.0
TONE_LIMIT = 2.0


def truncate_at_paragraph(text: str, language: str) -> str:
    """Cut to the input limit at a paragraph boundary when one is close."""
    per_token = CHARS_PER_TOKEN.get(language, DEFAULT_CHARS_PER_TOKEN)
    limit = int(MAX_INPUT_TOKENS * per_token)
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit]


def clamp_tone(value: float | None) -> float | None:
    if value is None:
        return None
    return max(-TONE_LIMIT, min(TONE_LIMIT, float(value)))


def word_count(quote: str) -> int:
    """Words by spacing, with two Chinese or Japanese characters as a word."""
    return max(len(quote.split()), len(CJK_RE.findall(quote)) // 2)


def quote_problem(quote: str | None, text: str) -> str | None:
    """Why an evidence quote fails, or None if it is acceptable."""
    if not quote:
        return "no quote"
    words = word_count(quote)
    if words > EVIDENCE_MAX_WORDS:
        return "too long"
    if words < EVIDENCE_MIN_WORDS:
        return "too short"
    if not appears_in(quote, text):
        return "not verbatim"
    return None


def name_near_quote(entity: EntityB, quote: str, text: str) -> bool:
    """True when the entity's name is in the quote or close to it."""
    body = fold(text)
    needle = fold(quote)
    names = [entity.name, *entity.surface_forms]
    for match in re.finditer(re.escape(needle), body):
        start = max(0, match.start() - NAME_WINDOW_CHARS)
        window = body[start : match.end() + NAME_WINDOW_CHARS]
        if entity.ent_type == "PERSON":
            if names_person(names, window):
                return True
        elif any(fold(name) in window for name in names if name):
            return True
    return False


def tone_problem(entity: EntityB, text: str) -> str | None:
    """Why a tone's evidence fails, or None if it is acceptable."""
    reason = quote_problem(entity.tone_evidence, text)
    if reason:
        return reason
    assert entity.tone_evidence is not None
    if not name_near_quote(entity, entity.tone_evidence, text):
        return "name not near quote"
    return None


def checked_tone(
    entity: EntityB, text: str
) -> tuple[float | None, str | None]:
    """Keep a tone only with evidence that is about this entity."""
    tone = clamp_tone(entity.tone)
    if tone is None or tone_problem(entity, text):
        return None, None
    return tone, entity.tone_evidence


def locate(surfaces: list[str], text: str) -> tuple[str | None, int, int]:
    """The first surface found in the text, with its character offsets."""
    for surface in surfaces:
        if not surface:
            continue
        start = text.find(surface)
        if start >= 0:
            return surface, start, start + len(surface)
    return None, -1, -1


class ExtractParser:
    """One item per call; items wait out API outages rather than failing."""

    name = "extract_b"
    version = "1"
    batch_size = 1

    def __init__(
        self,
        settings: Settings,
        llm: LlmClient,
        db: Database,
        sources: dict[str, Source],
        resolver: EntityResolver,
        gazetteer: Gazetteer,
        vocabulary: Vocabulary | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.db = db
        self.sources = sources
        self.resolver = resolver
        self.gazetteer = gazetteer
        self.vocabulary = vocabulary or load_vocabulary(settings.paths.config)
        self.max_concurrency = settings.llm.concurrency_b
        template = (
            resources.files("monitor.llm")
            .joinpath("prompts/extract_b.md")
            .read_text(encoding="utf-8")
        )
        self.system = template.format(topics=", ".join(self.vocabulary.topics))
        threshold = settings.nlp.relevance_threshold
        self.input_query = needs_parser_sql(
            self.name,
            self.version,
            RELEVANT_SQL.format(threshold=threshold)
            + " AND i.item_id IN (SELECT item_id FROM lake.nlp.parser_runs "
            "WHERE parser = 'fetch_text' AND status IN ('ok', 'skipped'))",
        )
        closes = load_poll_closes(settings.paths.config)
        self.first_closes = {
            state: close.at for close in closes for state in close.states
        }
        self.earliest_close = closes[0].at if closes else None
        self.known_races = {
            race["race_key"]
            for race in load_races_yaml(settings).get("races") or []
        }

    async def _article_text(self, item: Item) -> tuple[str, str]:
        """The text to analyze and where it came from.

        A short or paywalled page goes to the model with the feed summary.
        """
        text = item.text
        paywalled = item.paywalled
        if text is None:
            rows = await self.db.fetch(
                """
                SELECT text, paywalled FROM lake.raw.article_text
                WHERE item_id = $item_id AND n_chars > 0
                ORDER BY fetched_at DESC LIMIT 1
                """,
                {"item_id": item.item_id},
            )
            text = rows[0][0] if rows else None
            paywalled = bool(rows[0][1]) if rows else False
        if not text:
            return f"{item.title}\n\n{item.summary}", "summary"
        partial = paywalled or len(text) < SHORT_TEXT_CHARS
        if partial and item.summary and item.summary not in text:
            return f"{item.summary}\n\n{text}", "summary+text"
        return text, "full_text"

    def _user_message(self, item: Item, text: str) -> str:
        source = self.sources.get(item.source_id)
        return json.dumps(
            {
                "outlet": source.name if source else item.source_id,
                "language": item.language,
                "title": item.title,
                "text": truncate_at_paragraph(text, item.language),
            },
            ensure_ascii=False,
        )

    async def _call(self, item: Item, text: str) -> ItemB:
        """Call the model, waiting through outages and budget stops."""
        user = self._user_message(item, text)
        max_tokens = MAX_OUTPUT_TOKENS
        retried_truncation = False
        while True:
            if not self.llm.should_attempt():
                await asyncio.sleep(WAIT_SECONDS)
                continue
            try:
                return await self.llm.structured(
                    "extract_b",
                    ItemB,
                    self.system,
                    user,
                    max_tokens=max_tokens,
                    batch_id=item.item_id,
                    item_ids=[item.item_id],
                )
            except LlmUnavailable:
                await asyncio.sleep(WAIT_SECONDS)
            except LlmBudgetStop:
                await asyncio.sleep(BUDGET_WAIT_SECONDS)
            except LlmTruncated:
                if retried_truncation:
                    raise
                retried_truncation = True
                max_tokens *= 2

    def _mention_rows(
        self, item: Item, result: ItemB, text: str, states: list[str]
    ) -> list[dict[str, Any]]:
        rows = []
        checked = self._checkable(item, text)
        for entity in result.entities:
            surface, start, end = locate(
                [*entity.surface_forms, entity.name], text
            )
            tone, evidence = checked_tone(entity, checked)
            state = normalize_state(entity.state) or entity.state
            entity_id, how = self.resolver.resolve(
                [entity.name, *entity.surface_forms],
                entity.ent_type,
                [state] if state else states,
            )
            if (
                entity_id is None
                and len(normalize_name(entity.name).split()) > 1
            ):
                # A later pass must see the name that ruled out a match.
                surface, start, end = locate([entity.name], text)
            rows.append(
                {
                    "mention_id": hash_id(
                        item.item_id, entity.name, entity.ent_type
                    ),
                    "item_id": item.item_id,
                    "surface": surface or entity.name,
                    "char_start": start if start >= 0 else None,
                    "char_end": end if start >= 0 else None,
                    "ent_type": entity.ent_type,
                    "role": entity.role,
                    "entity_id": entity_id,
                    "resolved_by": how,
                    "tone": tone,
                    "tone_evidence": evidence,
                    "party": entity.party,
                    "state": state,
                    "office": entity.office,
                    "district": entity.district,
                    "parser": self.name,
                    "parser_version": self.version,
                }
            )
        return rows

    def _place_rows(
        self, item: Item, places: list[PlaceB], states: list[str]
    ) -> list[dict[str, Any]]:
        source = self.sources.get(item.source_id)
        home_state = source.home_state if source else None
        rows = []

        def add(surface: str, place_type: str, method: str, **fields: Any):
            rows.append(
                {
                    "place_id": hash_id(item.item_id, surface, method),
                    "item_id": item.item_id,
                    "surface": surface,
                    "place_type": place_type,
                    "method": method,
                    "parser": self.name,
                    "parser_version": self.version,
                    **fields,
                }
            )

        for place in places:
            add(
                place.name,
                place.place_type,
                "llm",
                state_usps=place.state,
            )
            match = self.gazetteer.resolve(
                place.name,
                place.place_type,
                place.state,
                place.district,
                states,
                home_state,
            )
            if match is not None:
                add(
                    place.name,
                    place.place_type,
                    "gazetteer",
                    state_usps=match.state_usps,
                    county_fips=match.county_fips,
                    place_geoid=match.place_geoid,
                    cd_code=match.cd_code,
                    match_score=match.match_score,
                )
        if not places and home_state:
            add(
                home_state,
                "state",
                "source_home",
                state_usps=home_state,
                match_score=0.5,
            )
        return rows

    def _race_key(
        self,
        state: str | None,
        office: str | None,
        district: str | None,
        measure: str | None = None,
    ) -> str | None:
        """A race's key, if that race is on the ballot.

        A measure's key is built from its wording, so it is not looked up.
        """
        key = normalize_race_key(state, office, district, measure)
        if key is None or BALLOT_MEASURE_CODE in key:
            return key
        if self.known_races and key not in self.known_races:
            return None
        return key

    def _race_keys(
        self, result: ItemB, accepted: list[tuple[RaceCallB, str | None]]
    ) -> list[str]:
        keys = [
            self._race_key(
                race.state, race.office, race.district, race.measure
            )
            for race in result.races
        ]
        keys += [
            self._race_key(call.state, call.office, call.district)
            for call, _ in accepted
        ]
        return sorted({key for key in keys if key})

    @staticmethod
    def _checkable(item: Item, text: str) -> str:
        """Everything the model may quote from."""
        parts = (item.title, item.summary, text)
        return "\n\n".join(part for part in parts if part)

    def _before_polls_closed(self, item: Item, call: RaceCallB) -> bool:
        """True for an item from before polls closed in the call's state."""
        times: list[datetime] = [
            when for when in (item.published_at, item.first_seen_at) if when
        ]
        key = normalize_race_key(call.state, call.office, call.district)
        state = normalize_state(call.state) or (
            key.split("-")[1] if key else None
        )
        close = self.first_closes.get(state or "", self.earliest_close)
        return bool(times and close and min(times) < close)

    def _checked_calls(
        self,
        output: ParserOutput,
        item: Item,
        result: ItemB,
        checked: str,
        states: list[str],
    ) -> list[tuple[RaceCallB, str | None]]:
        """Calls that pass the evidence rules, with their winners' ids."""
        accepted = []
        for call in result.race_calls:
            winner_id = None
            if call.winner_name:
                state = normalize_state(call.state) or call.state
                winner_id, _ = self.resolver.resolve(
                    [call.winner_name],
                    "PERSON",
                    [state] if state else states,
                )
            names = self.resolver.names_of(winner_id)
            reason = call_problem(call, checked, names)
            if not reason and self._before_polls_closed(item, call):
                reason = "before polls closed"
            if reason:
                self._reject(output, item, "race_call", call.evidence, reason)
            else:
                accepted.append((call, winner_id))
        return accepted

    def _reject(
        self,
        output: ParserOutput,
        item: Item,
        kind: str,
        quote: str | None,
        reason: str,
    ) -> None:
        output.add(
            "nlp.rejected_quotes",
            {
                "item_id": item.item_id,
                "kind": kind,
                "quote": quote,
                "reason": reason,
                "model": self.settings.llm.model_bulk,
                "parser": self.name,
                "parser_version": self.version,
            },
        )

    def _emit(
        self,
        output: ParserOutput,
        item: Item,
        result: ItemB,
        text: str,
        basis: str,
    ) -> None:
        states = list(item.states)
        checked = self._checkable(item, text)
        accepted = self._checked_calls(output, item, result, checked, states)
        race_keys = self._race_keys(result, accepted)
        topics = [
            t for t in result.topics_refined if t in self.vocabulary.topic_set
        ]
        raw = {**result.model_dump(), "race_keys": race_keys}
        output.add(
            "nlp.extract_b",
            {
                "item_id": item.item_id,
                "summary_en": result.summary_en,
                "article_tone": clamp_tone(result.article_tone),
                "topics_refined": topics,
                "text_basis": basis,
                "raw": raw,
                "model": self.settings.llm.model_bulk,
                "parser": self.name,
                "parser_version": self.version,
            },
        )
        for row in self._mention_rows(item, result, text, states):
            output.add("nlp.mentions", row)
        for entity in result.entities:
            if entity.tone is not None:
                reason = tone_problem(entity, checked)
                if reason:
                    self._reject(
                        output, item, "tone", entity.tone_evidence, reason
                    )
        for row in self._place_rows(item, result.places, states):
            output.add("nlp.places", row)
        for call, winner_id in accepted:
            output.add(
                "nlp.race_calls",
                call_row(
                    item.item_id,
                    call,
                    self._race_key(call.state, call.office, call.district),
                    winner_id,
                    item.first_seen_at,
                    self.name,
                    self.version,
                    checked,
                ),
            )

    async def run(self, batch: list[Item]) -> ParserOutput:
        output = ParserOutput()
        for item in batch:
            text, basis = await self._article_text(item)
            try:
                result = await self._call(item, text)
            except (LlmRefused, LlmTruncated, LlmInvalid) as error:
                output.mark(item.item_id, "error", type(error).__name__)
                continue
            self._emit(output, item, result, text, basis)
        return output
