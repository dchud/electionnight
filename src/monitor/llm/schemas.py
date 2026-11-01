"""Response schemas for every structured Claude call.

All schemas are closed, because structured outputs require it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


Level = Literal["national", "state", "local"]
PrimaryLevel = Literal["national", "state", "local", "none"]
EntityType = Literal[
    "PERSON", "ORG", "PARTY", "OFFICE", "MEASURE", "GPE", "EVENT"
]
EntityRole = Literal[
    "candidate",
    "official",
    "election_official",
    "party",
    "org",
    "measure",
    "other",
]
CallStatus = Literal[
    "projected", "declared", "conceded", "leading", "too_close", "uncalled"
]
PlaceType = Literal["state", "county", "city", "cd", "other"]


class ItemA(Closed):
    item_ref: str
    election_relevant: bool
    relevance_conf: float
    us_politics: bool
    levels: list[Level]
    primary_level: PrimaryLevel
    states: list[str]
    race_types: list[str]
    topics: list[str]
    free_tags: list[str]
    is_results_report: bool
    is_opinion: bool
    headline_en: str | None
    summary_en: str | None


class BatchA(Closed):
    items: list[ItemA]


class EntityB(Closed):
    name: str
    surface_forms: list[str]
    ent_type: EntityType
    role: EntityRole
    party: str | None
    state: str | None
    office: str | None
    district: str | None
    tone: float | None
    tone_evidence: str | None


class RaceCallB(Closed):
    race_desc: str
    state: str | None
    office: str | None
    district: str | None
    winner_name: str | None
    party: str | None
    called_by: str | None
    status: CallStatus
    claimed_at_text: str | None
    evidence: str


class PlaceB(Closed):
    name: str
    place_type: PlaceType
    state: str | None
    district: str | None


class RaceRefB(Closed):
    state: str | None
    office: str | None
    district: str | None
    measure: str | None


class ItemB(Closed):
    summary_en: str
    article_tone: float
    entities: list[EntityB]
    places: list[PlaceB]
    races: list[RaceRefB]
    race_calls: list[RaceCallB]
    topics_refined: list[str]


class StorylineLabel(Closed):
    label: str
    summary: str
    citations: list[str]


class EntityResolution(Closed):
    surface: str
    canonical_name: str | None
    is_new_entity: bool
    is_person: bool
    party: str | None
    state: str | None
    office: str | None


class EntityResolutionBatch(Closed):
    resolutions: list[EntityResolution]


class EntityAliases(Closed):
    entity_ref: str
    aliases: list[str]


class AliasBatch(Closed):
    entities: list[EntityAliases]


class BriefSentence(Closed):
    text: str
    citations: list[str]


class BriefParagraph(Closed):
    sentences: list[BriefSentence]


class Brief(Closed):
    paragraphs: list[BriefParagraph]
