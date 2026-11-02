"""Resolve place names to FIPS codes with the Census gazetteer."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from monitor.parsers.classify_a import STATE_NAMES, USPS
from monitor.ref.races import MAX_DISTRICT, parse_district

COUNTY_SUFFIX = re.compile(
    r"\s+(county|parish|borough|census area|municipality)$", re.I
)
CITY_PREFIX = re.compile(r"^(city of|town of|village of)\s+", re.I)
FUZZY_SCORE = 88
AT_LARGE_TOKEN = "AL"


def normalize_place(name: str) -> str:
    text = COUNTY_SUFFIX.sub("", name.strip().lower().replace(".", ""))
    text = CITY_PREFIX.sub("", text)
    return text.replace("saint ", "st ")


def normalize_cd(state: str | None, district: Any) -> str | None:
    """A congressional district as `XX-NN`, or None.

    A state written in the district text wins over the state given.
    """
    named, token = parse_district(district)
    code = named or state
    if not code or token is None:
        return None
    if token == AT_LARGE_TOKEN:
        return f"{code}-00"
    return f"{code}-{token}" if int(token) <= MAX_DISTRICT else None


def state_code(name: str | None) -> str | None:
    if not name:
        return None
    text = name.strip()
    if text.upper() in USPS:
        return text.upper()
    lowered = text.lower()
    if lowered == "washington":
        return "WA"
    if lowered in {"district of columbia", "washington, dc", "dc"}:
        return "DC"
    return STATE_NAMES.get(lowered)


@dataclass(frozen=True)
class GeoEntry:
    geoid: str
    name: str
    state: str
    aland: int


@dataclass
class GeoMatch:
    state_usps: str | None = None
    county_fips: str | None = None
    place_geoid: str | None = None
    cd_code: str | None = None
    match_score: float = 0.0


class Gazetteer:
    def __init__(
        self, counties: list[GeoEntry], places: list[GeoEntry]
    ) -> None:
        self.counties: dict[str, list[GeoEntry]] = defaultdict(list)
        self.places: dict[str, list[GeoEntry]] = defaultdict(list)
        self.by_state: dict[str, list[tuple[str, GeoEntry, str]]] = (
            defaultdict(list)
        )
        for entry in counties:
            key = normalize_place(entry.name)
            self.counties[key].append(entry)
            self.by_state[entry.state].append((key, entry, "county"))
        for entry in places:
            key = normalize_place(entry.name)
            self.places[key].append(entry)
            self.by_state[entry.state].append((key, entry, "city"))

    @classmethod
    def load(cls, ref_dir: Path) -> Gazetteer:
        def read(name: str, id_column: str) -> list[GeoEntry]:
            path = ref_dir / name
            if not path.exists():
                return []
            with path.open(newline="", encoding="utf-8") as handle:
                return [
                    GeoEntry(
                        row[id_column],
                        row["name"],
                        row["state_usps"],
                        int(float(row.get("aland") or 0)),
                    )
                    for row in csv.DictReader(handle)
                ]

        return cls(read("counties.csv", "fips"), read("places.csv", "geoid"))

    @staticmethod
    def _choose(
        candidates: list[GeoEntry],
        states: list[str],
        home_state: str | None,
    ) -> tuple[GeoEntry | None, float]:
        """Disambiguate by a state in the item, then the outlet's state.

        Several candidates with no state to choose among give no match.
        """
        if not candidates:
            return None, 0.0
        if len(candidates) == 1:
            return candidates[0], 1.0
        for state in states:
            matching = [c for c in candidates if c.state == state]
            if matching:
                return max(matching, key=lambda c: c.aland), 0.95
        if home_state:
            matching = [c for c in candidates if c.state == home_state]
            if matching:
                return max(matching, key=lambda c: c.aland), 0.85
        return None, 0.0

    def _fuzzy(
        self, name: str, state: str, kind: str
    ) -> tuple[GeoEntry | None, float]:
        key = normalize_place(name)
        best: GeoEntry | None = None
        best_score = 0.0
        for candidate, entry, entry_kind in self.by_state.get(state, []):
            if entry_kind != kind:
                continue
            score = fuzz.ratio(key, candidate)
            if score > best_score:
                best, best_score = entry, score
        if best is not None and best_score >= FUZZY_SCORE:
            return best, best_score / 100
        return None, 0.0

    def resolve(
        self,
        name: str,
        place_type: str,
        state: str | None,
        district: Any,
        item_states: list[str],
        home_state: str | None,
    ) -> GeoMatch | None:
        """Match one place. Returns None when nothing matches."""
        given = state_code(state)
        if place_type == "state":
            code = state_code(name) or given
            return GeoMatch(state_usps=code, match_score=1.0) if code else None
        if place_type == "cd":
            named, _ = parse_district(name)
            code = normalize_cd(
                named
                or given
                or (
                    home_state
                    if not item_states
                    else (item_states[0] if len(item_states) == 1 else None)
                ),
                district if district is not None else name,
            )
            if code is None:
                return None
            return GeoMatch(state_usps=code[:2], cd_code=code, match_score=1.0)
        kinds = {"county": "county", "city": "city"}
        kind = kinds.get(place_type)
        if kind is None:
            return None
        table = self.counties if kind == "county" else self.places
        candidates = table.get(normalize_place(name), [])
        states = [given] if given else item_states
        if given:
            candidates = [c for c in candidates if c.state == given]
        entry, score = self._choose(candidates, states, home_state)
        if entry is None:
            for fallback in (given, *item_states, home_state):
                if fallback:
                    entry, score = self._fuzzy(name, fallback, kind)
                    if entry is not None:
                        break
        if entry is None:
            return None
        if kind == "county":
            return GeoMatch(
                state_usps=entry.state,
                county_fips=entry.geoid,
                match_score=score,
            )
        return GeoMatch(
            state_usps=entry.state,
            place_geoid=entry.geoid,
            match_score=score,
        )
