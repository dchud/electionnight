"""Stage A: classify every item and translate non-English text."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from monitor.ingest.sources import Source
from monitor.lake import hash_id
from monitor.llm.budget import BudgetState
from monitor.llm.client import (
    LlmBudgetStop,
    LlmClient,
    LlmInvalid,
    LlmRefused,
    LlmTruncated,
    LlmUnavailable,
)
from monitor.llm.schemas import BatchA, ItemA
from monitor.logs import get
from monitor.parsers.base import Item, ParserOutput, needs_parser_sql
from monitor.settings import Settings

log = get(__name__)

UNAVAILABLE_WAIT_SECONDS = 5.0
MAX_FREE_TAGS = 3
LOW_SIGNAL_SCOPES = {"international"}
POLITICS_FEED_RE = re.compile(r"politic|election|us-news|usa|america", re.I)

STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington state": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
USPS = set(STATE_NAMES.values()) | {"DC", "WA"}


@dataclass
class Vocabulary:
    topics: list[str]
    race_types: list[str]
    keywords: dict[str, Any]

    @property
    def topic_set(self) -> set[str]:
        return set(self.topics)

    @property
    def race_type_set(self) -> set[str]:
        return set(self.race_types)


def load_vocabulary(config_dir: Path) -> Vocabulary:
    taxonomy = yaml.safe_load(
        (config_dir / "taxonomy.yaml").read_text(encoding="utf-8")
    )
    keywords = yaml.safe_load(
        (config_dir / "election_keywords.yaml").read_text(encoding="utf-8")
    )
    return Vocabulary(taxonomy["topics"], taxonomy["race_types"], keywords)


def condensed_watchlist(config_dir: Path) -> str:
    """One line per close or watched race, in a stable order."""
    path = config_dir / "races.yaml"
    if not path.exists():
        return "(none listed)"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    lines = []
    for race in data.get("races", []):
        if race.get("close_race") or race.get("watch"):
            names = ", ".join(race.get("candidate_names") or [])
            lines.append(f"{race.get('race_key')}: {names}")
    return "\n".join(sorted(lines)) or "(none listed)"


def build_system_prompt(vocabulary: Vocabulary, config_dir: Path) -> str:
    template = (
        resources.files("monitor.llm")
        .joinpath("prompts/classify_a.md")
        .read_text(encoding="utf-8")
    )
    return template.format(
        race_types=", ".join(vocabulary.race_types),
        topics=", ".join(vocabulary.topics),
        watchlist=condensed_watchlist(config_dir),
    )


def _contains(text: str, phrase: str) -> bool:
    """Word-bounded match for Latin text; plain substring otherwise."""
    if phrase.isascii():
        return re.search(rf"\b{re.escape(phrase)}\b", text) is not None
    return phrase in text


def states_in_text(text: str) -> list[str]:
    return sorted(
        {code for name, code in STATE_NAMES.items() if _contains(text, name)}
    )


def classify_by_keywords(
    item: Item, source: Source | None, vocabulary: Vocabulary
) -> dict[str, Any]:
    """Keyword classification. Conservative: no translation and no tone."""
    keywords = vocabulary.keywords
    text = f"{item.title} {item.summary}".lower()
    relevance = keywords["relevance"]
    lists = [relevance["en"]]
    if item.language != "en" and item.language in relevance:
        lists.append(relevance[item.language])
    strong = sum(
        _contains(text, phrase)
        for group in lists
        for phrase in group["strong"]
    )
    weak = sum(
        _contains(text, phrase) for group in lists for phrase in group["weak"]
    )
    states = states_in_text(text)
    relevant = strong >= 1 or weak >= 2 or (weak >= 1 and bool(states))
    confidence = (
        min(0.9, 0.5 + 0.15 * strong + 0.05 * weak) if relevant else 0.7
    )
    topics = [
        topic
        for topic, phrases in keywords["topics"].items()
        if topic in vocabulary.topic_set
        and any(_contains(text, phrase) for phrase in phrases)
    ]
    race_types = [
        race
        for race, phrases in keywords["race_types"].items()
        if any(_contains(text, phrase) for phrase in phrases)
    ]
    levels: list[str] = []
    if relevant:
        if any(_contains(text, t) for t in keywords["local_terms"]):
            levels.append("local")
        if states or (source and source.home_state):
            levels.append("state")
        if any(_contains(text, t) for t in keywords["national_terms"]):
            levels.insert(0, "national")
        if not levels:
            levels.append("national")
    return {
        "election_relevant": relevant,
        "relevance_conf": confidence,
        "us_politics": relevant or weak >= 1,
        "levels": levels,
        "primary_level": levels[0] if levels else "none",
        "states": states if relevant else [],
        "race_types": race_types if relevant else [],
        "topics": topics if relevant else [],
        "free_tags": [],
        "is_results_report": "results_projection" in topics
        or "concession_victory" in topics,
        "is_opinion": is_opinion_url(item.url, item.feed_categories),
        "headline_en": None,
        "summary_en": None,
        "method": "keywords",
    }


def is_opinion_url(url: str, categories: list[str]) -> bool:
    pattern = r"/(opinion|opinions|editorial|editorials|column|columns)/"
    if re.search(pattern, url.lower()):
        return True
    return any(
        re.search(r"opinion|editorial|commentary", c, re.I) for c in categories
    )


def normalize_llm_result(
    result: ItemA, item: Item, vocabulary: Vocabulary
) -> dict[str, Any]:
    """Restrict a model result to the closed vocabularies."""
    topics = [t for t in result.topics if t in vocabulary.topic_set]
    unknown = [t for t in result.topics if t not in vocabulary.topic_set]
    free_tags = [*result.free_tags, *unknown][:MAX_FREE_TAGS]
    is_english = item.language == "en"
    return {
        "election_relevant": result.election_relevant,
        "relevance_conf": max(0.0, min(1.0, result.relevance_conf)),
        "us_politics": result.us_politics,
        "levels": list(result.levels),
        "primary_level": result.primary_level,
        "states": sorted(
            {s.upper() for s in result.states if s.upper() in USPS}
        ),
        "race_types": [
            r for r in result.race_types if r in vocabulary.race_type_set
        ],
        "topics": topics,
        "free_tags": free_tags,
        "is_results_report": result.is_results_report,
        "is_opinion": result.is_opinion
        or is_opinion_url(item.url, item.feed_categories),
        "headline_en": None if is_english else result.headline_en,
        "summary_en": None if is_english else result.summary_en,
        "method": "llm",
    }


def user_message(batch: list[Item], sources: dict[str, Source]) -> str:
    payload = []
    for index, item in enumerate(batch):
        source = sources.get(item.source_id)
        payload.append(
            {
                "item_ref": str(index),
                "language": item.language,
                "scope": source.scope if source else "",
                "home_state": source.home_state if source else "",
                "categories": item.feed_categories[:6],
                "title": item.title,
                "summary": item.summary[:800],
            }
        )
    return json.dumps(payload, ensure_ascii=False)


class ClassifyParser:
    name = "classify_a"
    version = "1"
    input_query = needs_parser_sql(name, version)

    def __init__(
        self,
        settings: Settings,
        llm: LlmClient | None,
        sources: dict[str, Source],
        vocabulary: Vocabulary | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.sources = sources
        self.vocabulary = vocabulary or load_vocabulary(settings.paths.config)
        self.batch_size = settings.llm.batch_a
        self.max_concurrency = settings.llm.concurrency_a
        self.system = build_system_prompt(
            self.vocabulary, settings.paths.config
        )

    def _low_signal(self, item: Item) -> bool:
        source = self.sources.get(item.source_id)
        if source is None or source.scope not in LOW_SIGNAL_SCOPES:
            return False
        return not POLITICS_FEED_RE.search(item.url)

    def _emit(
        self,
        output: ParserOutput,
        item: Item,
        fields: dict[str, Any],
        batch_id: str | None = None,
    ) -> None:
        item.relevant = fields["election_relevant"]
        item.relevance_conf = fields["relevance_conf"]
        item.states = fields["states"]
        item.primary_level = fields["primary_level"]
        item.headline_en = fields["headline_en"]
        item.summary_en = fields["summary_en"]
        output.add(
            "nlp.classify_a",
            {
                "item_id": item.item_id,
                "parser": self.name,
                "parser_version": self.version,
                "batch_id": batch_id,
                **fields,
            },
        )

    def _keywords(self, output: ParserOutput, items: list[Item]) -> None:
        for item in items:
            source = self.sources.get(item.source_id)
            self._emit(
                output,
                item,
                classify_by_keywords(item, source, self.vocabulary),
            )

    async def _call_model(
        self, output: ParserOutput, batch: list[Item]
    ) -> None:
        assert self.llm is not None
        item_ids = [item.item_id for item in batch]
        batch_id = hash_id("classify_a", *item_ids)
        try:
            response = await self.llm.structured(
                "classify_a",
                BatchA,
                self.system,
                user_message(batch, self.sources),
                max_tokens=600 * len(batch) + 500,
                batch_id=batch_id,
                item_ids=item_ids,
            )
        except (LlmRefused, LlmTruncated, LlmInvalid) as error:
            if len(batch) == 1:
                log.warning(
                    "classify_fallback",
                    item_id=batch[0].item_id,
                    error=type(error).__name__,
                )
                self._keywords(output, batch)
                return
            middle = len(batch) // 2
            await self._call_model(output, batch[:middle])
            await self._call_model(output, batch[middle:])
            return
        results = {result.item_ref: result for result in response.items}
        missing = []
        for index, item in enumerate(batch):
            result = results.get(str(index))
            if result is None:
                missing.append(item)
            elif (
                item.language != "en"
                and not result.headline_en
                and len(batch) > 1
            ):
                await self._call_model(output, [item])
            else:
                self._emit(
                    output,
                    item,
                    normalize_llm_result(result, item, self.vocabulary),
                    batch_id,
                )
        if missing:
            self._keywords(output, missing)

    async def run(self, batch: list[Item]) -> ParserOutput:
        output = ParserOutput()
        if self.llm is None:
            self._keywords(output, batch)
            return output
        to_model = batch
        if self.llm.budget.state() is BudgetState.SOFT:
            low = [i for i in batch if self._low_signal(i)]
            self._keywords(output, low)
            to_model = [i for i in batch if not self._low_signal(i)]
        if not to_model:
            return output
        while True:
            if not self.llm.should_attempt():
                self._keywords(output, to_model)
                return output
            try:
                await self._call_model(output, to_model)
                return output
            except LlmBudgetStop:
                self._keywords(output, to_model)
                return output
            except LlmUnavailable:
                if self.llm.degraded:
                    self._keywords(output, to_model)
                    return output
                await asyncio.sleep(UNAVAILABLE_WAIT_SECONDS)
