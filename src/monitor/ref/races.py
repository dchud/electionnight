"""Race keys, published race ratings, and the `config/races.yaml` watchlist.

Ratings come from the rating tables on Wikipedia, which list each rater with
the date of its last update. A race's `rating` is the first available of Cook
Political Report, Inside Elections, and Sabato's Crystal Ball. Every rater's
value stays in `ratings`. `close_race` applies to `rating` only; `watch` is
true when any rater puts the race in the close set.

House seats absent from Wikipedia's competitive-seat table are rated safe by
every rater that the table covers. They carry a null `rating`.

An entry in the `overrides` list with `state` sets that state's battleground
flag. An entry with `race_key` replaces the named fields on that race, and
creates the race if it is absent. Regeneration keeps the list.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from monitor.ingest.sources import slugify
from monitor.logs import get
from monitor.ref.wiki import (
    Cell,
    Item,
    Link,
    Table,
    WikiClient,
    article_url,
    parse_tables,
)
from monitor.settings import Settings

log = get(__name__)

YEAR = 2026
YEAR_TEXT = str(YEAR)

SENATE = "senate"
GOVERNOR = "governor"
HOUSE = "house"
ATTORNEY_GENERAL = "attorney_general"
SECRETARY_OF_STATE = "secretary_of_state"
LT_GOVERNOR = "lt_governor"
BALLOT_MEASURE = "ballot_measure"

OFFICE_CODES = {
    SENATE: "SEN",
    GOVERNOR: "GOV",
    ATTORNEY_GENERAL: "AG",
    SECRETARY_OF_STATE: "SOS",
    LT_GOVERNOR: "LG",
}

STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "american samoa": "AS",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "guam": "GU",
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
    "northern mariana islands": "MP",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "puerto rico": "PR",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "us virgin islands": "VI",
    "virgin islands": "VI",
    "washington": "WA",
    "washington dc": "DC",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
AP_STATE_ABBREVIATIONS = {
    "ala": "AL",
    "ariz": "AZ",
    "ark": "AR",
    "calif": "CA",
    "colo": "CO",
    "conn": "CT",
    "del": "DE",
    "dc": "DC",
    "fla": "FL",
    "ga": "GA",
    "ill": "IL",
    "ind": "IN",
    "kan": "KS",
    "ky": "KY",
    "la": "LA",
    "md": "MD",
    "mass": "MA",
    "mich": "MI",
    "minn": "MN",
    "miss": "MS",
    "mo": "MO",
    "mont": "MT",
    "neb": "NE",
    "nev": "NV",
    "nh": "NH",
    "nj": "NJ",
    "nm": "NM",
    "ny": "NY",
    "nc": "NC",
    "nd": "ND",
    "okla": "OK",
    "ore": "OR",
    "pa": "PA",
    "ri": "RI",
    "sc": "SC",
    "sd": "SD",
    "tenn": "TN",
    "vt": "VT",
    "va": "VA",
    "wash": "WA",
    "wva": "WV",
    "wis": "WI",
    "wyo": "WY",
}
USPS_CODES = set(STATE_NAMES.values())
# One seat, whose district code is 00. Delegates count here too.
AT_LARGE_STATES = {
    "AK",
    "AS",
    "DC",
    "DE",
    "GU",
    "MP",
    "ND",
    "PR",
    "SD",
    "VI",
    "VT",
    "WY",
}
MAX_DISTRICT = 60
# What a measure's key has that no other race's key has.
BALLOT_MEASURE_CODE = "-M-"

TOSS_UP = "Toss-up"
CLOSE_PREFIXES = ("Tilt ", "Lean ")
RATING_WORDS = {
    "safe": "Safe",
    "solid": "Safe",
    "likely": "Likely",
    "lean": "Lean",
    "tilt": "Tilt",
}
PRIMARY_RATERS = ("Cook", "IE", "Sabato")
RATER_NAMES = {
    "Cook": "Cook Political Report",
    "IE": "Inside Elections",
    "Sabato": "Sabato's Crystal Ball",
}

SENATE_PAGE = "2026 United States Senate elections"
GOVERNOR_PAGE = "2026 United States gubernatorial elections"
HOUSE_PAGE = "2026 United States House of Representatives elections"
HOUSE_RATINGS_PAGE = (
    "2026 United States House of Representatives election ratings"
)
ATTORNEY_GENERAL_PAGE = "2026 United States attorney general elections"
SECRETARY_OF_STATE_PAGE = "2026 United States secretary of state elections"
LT_GOVERNOR_PAGE = "2026 United States lieutenant gubernatorial elections"
MEASURES_PAGE = "2026 United States ballot measures"

COMPETITIVE_HEADING = "Latest published ratings for competitive seats"
RATING_TABLES = (
    (SENATE, SENATE_PAGE, "Predictions"),
    (GOVERNOR, GOVERNOR_PAGE, "Predictions"),
    (HOUSE, HOUSE_RATINGS_PAGE, COMPETITIVE_HEADING),
    (ATTORNEY_GENERAL, ATTORNEY_GENERAL_PAGE, "Predictions"),
    (SECRETARY_OF_STATE, SECRETARY_OF_STATE_PAGE, "Predictions"),
)
TERRITORIES_HEADING = "Territories and federal district"
ROSTER_TABLES = (
    (
        SENATE,
        SENATE_PAGE,
        (
            "Elections leading to the next Congress",
            "Special elections during the preceding Congress",
        ),
    ),
    (GOVERNOR, GOVERNOR_PAGE, ("States", TERRITORIES_HEADING)),
    (ATTORNEY_GENERAL, ATTORNEY_GENERAL_PAGE, ("States", TERRITORIES_HEADING)),
    (SECRETARY_OF_STATE, SECRETARY_OF_STATE_PAGE, ("Race summary",)),
    (LT_GOVERNOR, LT_GOVERNOR_PAGE, ("Race summary",)),
)
HOUSE_DELEGATES_HEADING = "Non-voting delegates"
INCUMBENT_HEADERS = {
    "senator",
    "governor",
    "member",
    "delegate",
    "attorney general",
    "secretary of state",
    "lieutenant governor",
}
GENERIC_MEASURE_WORDS = {
    "amendment",
    "ballot measure",
    "initiative",
    "issue",
    "measure",
    "prop",
    "proposition",
    "question",
    "referendum",
}
WATCHED_MEASURE_TOPICS = {"Abortion", "Elections", "Voter identification"}

# A build that finds fewer races than this leaves the existing file in place,
# because the likely cause is a changed page layout.
MIN_RATED = {SENATE: 30, GOVERNOR: 30, HOUSE: 100}
MIN_RACES = {SENATE: 33, GOVERNOR: 36, HOUSE: 435}

HEADER = """\
# Race watchlist. Written by `monitor ref build-races`; only `overrides` is
# kept when the file is regenerated.
#
# overrides: each entry is one of
#   {state: NC, battleground: true}             force a state in or out
#   {race_key: 2026-NC-SEN, watch: true}        replace fields on a race, or
#                                               add a race that is not listed
#
# rating: first available of Cook, Inside Elections, Sabato; see `ratings`.
# close_race: rating is Toss-up, Tilt *, or Lean *.
# watch: any rater puts the race in that set, or an override says so.
"""


def _plain(text: str) -> str:
    cleaned = re.sub(r"[.'’]", "", text.lower())
    return re.sub(r"[\s_]+", " ", cleaned).strip()


STATE_NAME_RE = re.compile(
    r"\b(?:"
    + "|".join(sorted(map(re.escape, STATE_NAMES), key=len, reverse=True))
    + r")s?\b"
)
FEDERAL_RE = re.compile(r"\b(?:us|u s|united states|federal|congress\w*)\b")
DISTRICT_NUMBER_RE = re.compile(r"\d{1,2}(?:st|nd|rd|th)?")
HOUSE_PREFIX_RE = re.compile(
    r"\b(?:us|u s|united states|house|of|representatives?|rep|congressman|"
    r"congresswoman|member)\b"
)
RACE_WORDS = {
    "the",
    "race",
    "seat",
    "contest",
    "election",
    "elections",
    "general",
    "special",
    "for",
    "office",
    YEAR_TEXT,
}
OFFICE_WORDS = {
    SENATE: {
        "sen",
        "senate",
        "senator",
        "senators",
        "us",
        "u",
        "s",
        "united",
        "states",
        "federal",
        "of",
    },
    GOVERNOR: {"gov", "governor", "governors", "gubernatorial", "state", "of"},
    LT_GOVERNOR: {
        "lieutenant",
        "lt",
        "ltgov",
        "lg",
        "gov",
        "governor",
        "governors",
        "state",
        "of",
    },
    ATTORNEY_GENERAL: {
        "attorney",
        "attorneys",
        "general",
        "generals",
        "atty",
        "gen",
        "ag",
        "state",
        "of",
    },
    SECRETARY_OF_STATE: {"secretary", "sec", "sos", "of", "state", "states"},
    HOUSE: {
        "house",
        "representatives",
        "representative",
        "rep",
        "reps",
        "us",
        "u",
        "s",
        "united",
        "states",
        "of",
        "congress",
        "congressional",
        "congressman",
        "congresswoman",
        "member",
        "cd",
        "h",
        "district",
        "delegate",
        "resident",
        "commissioner",
        "at",
        "large",
        "federal",
    },
}


def _is_bare_district(text: str) -> bool:
    """True for office text that is only a code such as `TX-35`."""
    return re.fullmatch(
        r"(?:[A-Za-z]{2}[-\s]?\d{1,2}|[A-Za-z .']+\s\d{1,2}(?:st|nd|rd|th)?)",
        text.strip(),
    ) is not None and not re.search(r"district", text, re.I)


def normalize_state(value: object) -> str | None:
    """Return the USPS code for a state name, abbreviation, or code."""
    if value is None:
        return None
    raw = str(value).strip()
    if raw.upper() in USPS_CODES:
        return raw.upper()
    plain = re.sub(r"\s+", " ", _plain(raw).replace(",", " ")).strip()
    plain = plain.removeprefix("the ")
    return STATE_NAMES.get(plain) or AP_STATE_ABBREVIATIONS.get(plain)


def _office_is_clear(kind: str, text: str) -> bool:
    """True when the text holds only words that belong to this office.

    A state's name before "Senate" or "House" means that state's chamber,
    unless the text also says the office is federal.
    """
    named_state = STATE_NAME_RE.search(text) is not None
    if kind in {SENATE, HOUSE} and named_state:
        if not FEDERAL_RE.search(text):
            return False
    words = re.sub(r"[^\w\s]", " ", STATE_NAME_RE.sub(" ", text)).split()
    allowed = OFFICE_WORDS[kind] | RACE_WORDS
    return all(
        word in allowed
        or (kind == HOUSE and DISTRICT_NUMBER_RE.fullmatch(word))
        for word in words
    )


def _classify_office(office: str) -> str | None:
    """The office, or None unless the text is clearly that office."""
    text = _plain(office).replace("-", " ")
    if not text:
        return None
    if re.search(r"\bstate (senat|house|assembl|rep|legislat)", text):
        return None
    if "state supreme" in text:
        return None
    rules = (
        (LT_GOVERNOR, r"\blieutenant\b|\blt gov|\bltgov|^lg$|^lt$"),
        (ATTORNEY_GENERAL, r"attorney general|atty gen|^ag$"),
        (SECRETARY_OF_STATE, r"secretary of state|sec of state|^sos$"),
        (GOVERNOR, r"^gov$|\bgov\b|governor|gubernatorial"),
        (SENATE, r"^sen$|\bsen\b|senate|senator"),
        (
            HOUSE,
            r"^house$|\bus house\b|\bu s house\b|house of rep|^rep$|\brep\b"
            r"|representative|congress|^cd$|^h$|delegate"
            r"|resident commissioner",
        ),
        (
            BALLOT_MEASURE,
            r"measure|proposition|^prop\b|\bquestion\b|\bamendment\b"
            r"|initiative|referendum|\bissue\b",
        ),
    )
    for office_kind, pattern in rules:
        if re.search(pattern, text):
            if office_kind == BALLOT_MEASURE:
                return office_kind
            return office_kind if _office_is_clear(office_kind, text) else None
    return None


def parse_district(value: object) -> tuple[str | None, str | None]:
    """Split a district form into (state, two-digit number or "AL").

    Accepts forms such as `TX-35`, `35`, `35th`, `CD-35`, `District 35`,
    `Texas 35`, `Texas's 35th congressional district`, `AK-AL`, and `at-large`.
    Text naming anything else, such as `City Council District 5`, is not a
    congressional district and gives (None, None).
    """
    if value is None or isinstance(value, bool):
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    code = re.fullmatch(
        r"([A-Za-z]{2})[-\s]?(\d{1,2}|AL|al|at[-\s]?large)", text
    )
    if code:
        prefix = code.group(1).upper()
        token = _district_token(code.group(2))
        if prefix == "CD":
            return None, token
        return (prefix, token) if prefix in USPS_CODES else (None, None)
    lowered = _plain(text).replace(",", " ").replace("-", " ")
    if re.search(r"\bat ?large\b", lowered) or lowered in {"al", "a l"}:
        token = "AL"
    else:
        number = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", lowered)
        if number is None:
            return None, None
        token = _district_token(number.group(1))
    prefix = re.sub(
        r"\b(at ?large|congressional|district|cd|\d{1,2}(st|nd|rd|th)?)\b",
        " ",
        lowered,
    )
    prefix = HOUSE_PREFIX_RE.sub(" ", prefix)
    prefix = re.sub(r"s\s*$", "", prefix.strip()) if "'" in text else prefix
    prefix = prefix.strip()
    state = normalize_state(prefix)
    return (None, None) if prefix and state is None else (state, token)


def _district_token(value: str) -> str:
    if re.fullmatch(r"\d+", value):
        return f"{int(value):02d}"
    return "AL"


def _district_code(usps: str, token: str | None) -> str | None:
    """Apply the at-large rule. Returns a two-digit district or None."""
    if usps in AT_LARGE_STATES:
        if token in {None, "AL", "00", "01"}:
            return "00"
        return None
    if token in {None, "AL", "00"}:
        return None
    assert token is not None
    return token if 1 <= int(token) <= MAX_DISTRICT else None


def measure_slug(name: str, usps: str | None = None) -> str | None:
    """Slug a measure's identifier, the part before any colon or comma."""
    text = re.split(r"[:,]", name.strip(), maxsplit=1)[0]
    text = re.sub(r"^\d{4}\s+", "", text)
    for state_name, code in STATE_NAMES.items():
        if code == usps and text.lower().startswith(state_name + " "):
            text = text[len(state_name) + 1 :]
            break
    text = re.sub(r"\bProp\.?(?=\s|$)", "Proposition", text, flags=re.I)
    slug = slugify(re.sub(r"['’]", "", text))
    if len(slug) > 48:
        slug = slug[:48].rsplit("-", 1)[0]
    return slug or None


def normalize_race_key(
    state: object,
    office: object,
    district: object = None,
    measure: object = None,
) -> str | None:
    """Return the race key for loosely written inputs, or None.

    The key is `2026-GA-SEN`, `2026-AZ-GOV`, `2026-PA-H07`, or
    `2026-CA-M-<slug>`, plus `AG`, `SOS`, and `LG` for those offices. An
    at-large seat is `H00`. State legislative and local offices return None.
    """
    office_text = "" if office is None else str(office).strip()
    measure_text = "" if measure is None else str(measure).strip()
    kind = _classify_office(office_text)
    district_state, token = parse_district(district)
    if measure_text:
        if kind not in {None, BALLOT_MEASURE}:
            return None
        usps = normalize_state(state)
        slug = measure_slug(measure_text, usps) if usps else None
        return f"{YEAR}-{usps}-M-{slug}" if slug else None
    if kind is None and office_text:
        office_state, office_token = parse_district(office_text)
        if office_token is None or not _is_bare_district(office_text):
            return None
        kind = HOUSE
        district_state = district_state or office_state
        token = token or office_token
    if kind is None and token is not None:
        kind = HOUSE
    if kind == HOUSE and token is None and office_text:
        office_state, office_token = parse_district(office_text)
        district_state = district_state or office_state
        token = office_token
    usps = district_state or normalize_state(state)
    if usps is None or kind is None:
        return None
    if kind != HOUSE and kind != BALLOT_MEASURE and token is not None:
        return None
    if kind == HOUSE:
        code = _district_code(usps, token)
        return f"{YEAR}-{usps}-H{code}" if code else None
    if kind == BALLOT_MEASURE:
        if _plain(office_text).replace("-", " ") in GENERIC_MEASURE_WORDS:
            return None
        slug = measure_slug(office_text, usps)
        return f"{YEAR}-{usps}-M-{slug}" if slug else None
    return f"{YEAR}-{usps}-{OFFICE_CODES[kind]}"


def normalize_rating(text: str) -> str | None:
    """Map a table cell to `Toss-up`, `Tilt R`, `Lean D`, `Safe R`, ..."""
    cleaned = text.strip()
    if re.match(r"toss[- ]?up", cleaned, flags=re.I):
        return TOSS_UP
    match = re.match(r"(safe|solid|likely|lean|tilt)\s+(\w+)", cleaned, re.I)
    if not match:
        return None
    return f"{RATING_WORDS[match.group(1).lower()]} {match.group(2).upper()}"


def is_close(rating: str | None) -> bool:
    if rating is None:
        return False
    return rating == TOSS_UP or rating.startswith(CLOSE_PREFIXES)


def rater_short_name(header: str) -> str:
    """Strip the date from a rater column header such as `IE Oct. 29, 2026`."""
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    name = re.split(rf"\s+(?:{months})[a-z]*\.?\s+\d", header, maxsplit=1)[0]
    return name.strip().rstrip(".")


def rater_date(header: str) -> str:
    return header[len(rater_short_name(header)) :].strip(" .")


def parse_location(text: str) -> tuple[str | None, str | None, bool]:
    """Split a first-column label into (usps, district token, is_special)."""
    special = "(special)" in text.lower()
    name = re.sub(r"\s*\(.*?\)\s*", " ", text).strip()
    match = re.fullmatch(r"(.+?)\s+(at-large|\d+)", name)
    if match:
        token = _district_token(
            "AL" if match.group(2) == "at-large" else match.group(2)
        )
        return normalize_state(match.group(1)), token, special
    return normalize_state(name), None, special


def race_identity(
    office: str, location: str
) -> tuple[str | None, str | None, str | None, bool]:
    """Return (race_key, usps, district, is_special) for a table label."""
    usps, token, special = parse_location(location)
    if usps is None:
        return None, None, None, special
    if office == HOUSE:
        code = _district_code(usps, token)
        key = f"{YEAR}-{usps}-H{code}" if code else None
        return key, usps, code, special
    return f"{YEAR}-{usps}-{OFFICE_CODES[office]}", usps, None, special


@dataclass
class RosterRow:
    race_key: str
    state: str
    office: str
    district: str | None
    special: bool
    incumbents: list[Link] = field(default_factory=list)
    incumbent_text: str = ""
    candidates: list[Item] = field(default_factory=list)


def _header_index(rows: list[list[Cell]], names: set[str]) -> int | None:
    for row in rows:
        for index, cell in enumerate(row):
            if cell.text.strip().lower() in names:
                return index
    return None


def roster_rows(office: str, table: Table) -> list[RosterRow]:
    """Read one candidate table: office, incumbent, and candidate cells."""
    head = table.rows[:2]
    candidates_at = _header_index(head, {"candidates"})
    incumbent_at = _header_index(head, INCUMBENT_HEADERS)
    if candidates_at is None:
        return []
    rows = []
    for row in table.rows:
        if len(row) <= candidates_at:
            continue
        cell = row[candidates_at]
        if cell.header or cell.text.strip().lower() == "candidates":
            continue
        key, usps, district, special = race_identity(office, row[0].text)
        if key is None or usps is None:
            continue
        incumbent = row[incumbent_at] if incumbent_at is not None else None
        rows.append(
            RosterRow(
                race_key=key,
                state=usps,
                office=office,
                district=district,
                special=special,
                incumbents=incumbent.links if incumbent else [],
                incumbent_text=incumbent.text if incumbent else "",
                candidates=cell.items,
            )
        )
    return rows


def parse_roster(pages: dict[str, str]) -> list[RosterRow]:
    """Every race with its candidates, from the given page HTML by title."""
    rows: list[RosterRow] = []
    for office, page, headings in ROSTER_TABLES:
        if page not in pages:
            continue
        for table in parse_tables(pages[page]):
            if table.heading in headings:
                found = roster_rows(office, table)
                if "special" in table.heading.lower():
                    for row in found:
                        row.special = True
                rows.extend(found)
    if HOUSE_PAGE in pages:
        for table in parse_tables(pages[HOUSE_PAGE]):
            is_state = normalize_state(table.heading) is not None
            if is_state or table.heading == HOUSE_DELEGATES_HEADING:
                rows.extend(roster_rows(HOUSE, table))
    seen: set[str] = set()
    unique = []
    for row in rows:
        if row.race_key not in seen:
            seen.add(row.race_key)
            unique.append(row)
    return unique


@dataclass
class RatedRace:
    race_key: str
    state: str
    office: str
    district: str | None
    special: bool
    ratings: dict[str, str]
    rating_url: str


def parse_rating_table(
    office: str, table: Table, url: str
) -> tuple[list[RatedRace], dict[str, str]]:
    """Read one rating table. Returns races and each rater's update date."""
    tops = [cell.text for cell in table.rows[0]]
    if "Ratings" not in tops:
        return [], {}
    first = tops.index("Ratings")
    headers = [cell.text for cell in table.rows[1]]
    raters = [rater_short_name(header) for header in headers[first:]]
    dates = {
        rater_short_name(header): rater_date(header)
        for header in headers[first:]
    }
    races = []
    for row in table.rows[2:]:
        key, usps, district, special = race_identity(office, row[0].text)
        if key is None or usps is None:
            continue
        ratings: dict[str, str] = {}
        for rater, cell in zip(raters, row[first:], strict=False):
            value = normalize_rating(cell.text)
            if value is not None:
                ratings[rater] = value
        races.append(
            RatedRace(key, usps, office, district, special, ratings, url)
        )
    return races, dates


def choose_rating(ratings: dict[str, str]) -> tuple[str | None, str | None]:
    """Return the first available primary rating and its rater's name."""
    for rater in PRIMARY_RATERS:
        if rater in ratings:
            return ratings[rater], RATER_NAMES[rater]
    return None, None


def race_row(
    key: str,
    state: str,
    office: str,
    district: str | None,
    special: bool,
    rated: RatedRace | None,
    candidates: list[str],
) -> dict[str, Any]:
    ratings = rated.ratings if rated else {}
    rating, rater = choose_rating(ratings)
    row: dict[str, Any] = {
        "race_key": key,
        "state": state,
        "office": office,
        "district": district,
        "rating": rating,
        "rating_source": f"{rater} via Wikipedia" if rater else None,
        "rating_url": rated.rating_url if rated and rater else None,
        "close_race": is_close(rating),
        "candidates": candidates,
        "watch": any(is_close(value) for value in ratings.values()),
    }
    if special:
        row["special"] = True
    if ratings:
        row["ratings"] = dict(sorted(ratings.items()))
    return row


def parse_measures(html: str) -> list[dict[str, Any]]:
    """Measures on the Nov 3 ballot, with `watch` set from topic tables."""
    tables = [t for t in parse_tables(html) if len(t.rows) > 1]
    watched: set[tuple[str, str]] = set()
    for table in tables:
        if table.heading in WATCHED_MEASURE_TOPICS:
            header = [cell.text for cell in table.rows[0]]
            if "State" not in header or "Measure" not in header:
                continue
            state_at = header.index("State")
            measure_at = header.index("Measure")
            for row in table.rows[1:]:
                usps = normalize_state(row[state_at].text)
                if usps:
                    watched.add((usps, row[measure_at].text))
    rows: dict[str, dict[str, Any]] = {}
    for table in tables:
        usps = normalize_state(table.heading)
        header = [cell.text for cell in table.rows[0]]
        if usps is None or "Measure" not in header or "Date" not in header:
            continue
        measure_at = header.index("Measure")
        date_at = header.index("Date")
        for row in table.rows[1:]:
            if row[date_at].text.strip() != "Nov 3":
                continue
            name = row[measure_at].text
            key = normalize_race_key(usps, "measure", measure=name)
            if key is None or key in rows:
                continue
            rows[key] = {
                "race_key": key,
                "state": usps,
                "office": BALLOT_MEASURE,
                "district": None,
                "rating": None,
                "rating_source": None,
                "rating_url": None,
                "close_race": False,
                "candidates": [],
                "watch": (usps, name) in watched,
                "measure_name": name,
            }
    return list(rows.values())


def battleground_states(
    races: Iterable[dict[str, Any]], overrides: Iterable[dict[str, Any]]
) -> list[str]:
    """States with a close Senate or governor race, then state overrides."""
    states = {
        race["state"]
        for race in races
        if race.get("close_race") and race["office"] in {SENATE, GOVERNOR}
    }
    for entry in overrides:
        usps = normalize_state(entry.get("state"))
        if usps is None or "race_key" in entry:
            continue
        if entry.get("battleground", True):
            states.add(usps)
        else:
            states.discard(usps)
    return sorted(states)


def apply_race_overrides(
    races: list[dict[str, Any]], overrides: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_key = {race["race_key"]: race for race in races}
    for entry in overrides:
        key = entry.get("race_key")
        if not key:
            continue
        race = by_key.get(key)
        if race is None:
            parts = str(key).split("-")
            race = {
                "race_key": key,
                "state": parts[1] if len(parts) > 1 else None,
                "office": None,
                "district": None,
                "rating": None,
                "rating_source": None,
                "rating_url": None,
                "close_race": False,
                "candidates": [],
                "watch": False,
            }
            by_key[key] = race
        race.update({k: v for k, v in entry.items() if k != "note"})
    return [by_key[key] for key in sorted(by_key)]


def races_path(settings: Settings) -> Path:
    return settings.paths.config / "races.yaml"


def load_races_yaml(settings: Settings) -> dict[str, Any]:
    path = races_path(settings)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def candidates_by_race(settings: Settings) -> dict[str, list[str]]:
    """Entity ids per race, from the roster file when it exists."""
    path = settings.paths.ref / "entities.jsonl"
    if not path.exists():
        return {}
    found: dict[str, list[str]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                entity = json.loads(line)
                if entity.get("race_key"):
                    found.setdefault(entity["race_key"], []).append(
                        entity["entity_id"]
                    )
    return {key: sorted(ids) for key, ids in found.items()}


def assemble_races(
    pages: dict[str, str],
    measures_html: str | None,
    candidates: dict[str, list[str]],
    overrides: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Combine ratings, the race roster, and measures into race rows."""
    rated: dict[str, RatedRace] = {}
    dates: dict[str, dict[str, str]] = {}
    for office, page, heading in RATING_TABLES:
        if page not in pages:
            continue
        anchor = heading.replace(" ", "_")
        for table in parse_tables(pages[page]):
            if table.heading != heading or len(table.rows) < 3:
                continue
            races, found = parse_rating_table(
                office, table, article_url(page, anchor)
            )
            if races:
                dates[office] = found
                for race in races:
                    rated.setdefault(race.race_key, race)
                break
    rows: dict[str, dict[str, Any]] = {}
    for roster in parse_roster(pages):
        rows[roster.race_key] = race_row(
            roster.race_key,
            roster.state,
            roster.office,
            roster.district,
            roster.special,
            rated.get(roster.race_key),
            candidates.get(roster.race_key, []),
        )
    for key, race in rated.items():
        if key not in rows:
            rows[key] = race_row(
                key,
                race.state,
                race.office,
                race.district,
                race.special,
                race,
                candidates.get(key, []),
            )
    if measures_html:
        for measure in parse_measures(measures_html):
            rows.setdefault(measure["race_key"], measure)
    return apply_race_overrides(list(rows.values()), overrides), dates


def check_counts(races: list[dict[str, Any]]) -> None:
    """Raise when a table looks wrong, leaving the existing file in place."""
    total = Counter(race["office"] for race in races)
    rated = Counter(race["office"] for race in races if race["rating"])
    problems = [
        f"{office}: {total[office]} races, expected {minimum}"
        for office, minimum in MIN_RACES.items()
        if total[office] < minimum
    ]
    problems += [
        f"{office}: {rated[office]} rated races, expected {minimum}"
        for office, minimum in MIN_RATED.items()
        if rated[office] < minimum
    ]
    if problems:
        raise RuntimeError("race tables look wrong: " + "; ".join(problems))


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True, width=200
    )
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(HEADER + body, encoding="utf-8")
    os.replace(temporary, path)


def summary_lines(
    races: list[dict[str, Any]], battleground: list[str]
) -> list[str]:
    close = [race for race in races if race.get("close_race")]
    lines = [
        f"races {len(races)}, close {len(close)}, "
        f"watch {sum(1 for race in races if race.get('watch'))}",
        "battleground states: " + " ".join(battleground),
    ]
    for office in (
        SENATE,
        GOVERNOR,
        HOUSE,
        ATTORNEY_GENERAL,
        SECRETARY_OF_STATE,
    ):
        keys = [
            f"{race['race_key'].removeprefix(f'{YEAR}-')} ({race['rating']})"
            for race in close
            if race["office"] == office
        ]
        if keys:
            lines.append(f"close {office} ({len(keys)}): " + ", ".join(keys))
    other = [
        race["race_key"].removeprefix(f"{YEAR}-")
        for race in races
        if race.get("watch")
        and not race.get("close_race")
        and race["office"] in {SENATE, GOVERNOR}
    ]
    if other:
        lines.append(
            "watch only, close per another rater: " + ", ".join(other)
        )
    return lines


def build_races(settings: Settings, dry_run: bool = False) -> None:
    """Fetch current ratings and write `config/races.yaml`.

    With `dry_run` it prints the result and writes nothing.
    """
    existing = load_races_yaml(settings)
    overrides = list(existing.get("overrides") or [])
    wiki = WikiClient()
    pages: dict[str, str] = {}
    revisions: dict[str, int] = {}
    titles = {page for _, page, _ in RATING_TABLES}
    titles |= {page for _, page, _ in ROSTER_TABLES}
    titles |= {HOUSE_PAGE, MEASURES_PAGE}
    for title in sorted(titles):
        pages[title], revisions[title] = wiki.page(title)
    races, dates = assemble_races(
        pages,
        pages[MEASURES_PAGE],
        candidates_by_race(settings),
        overrides,
    )
    check_counts(races)
    battleground = battleground_states(races, overrides)
    for line in summary_lines(races, battleground):
        print(line)
    if dry_run:
        print("dry run: nothing written")
        return
    document = {
        "overrides": overrides,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "battleground_states": battleground,
        "rater_dates": dates,
        "source_revisions": dict(sorted(revisions.items())),
        "races": races,
    }
    _write_yaml(races_path(settings), document)
    print(f"wrote {races_path(settings)}")


def refresh_race_candidates(settings: Settings) -> int:
    """Copy entity ids from the roster file into `config/races.yaml`."""
    document = load_races_yaml(settings)
    if not document.get("races"):
        return 0
    by_race = candidates_by_race(settings)
    changed = 0
    for race in document["races"]:
        ids = by_race.get(race["race_key"], [])
        if race.get("candidates") != ids:
            race["candidates"] = ids
            changed += 1
    if changed:
        _write_yaml(races_path(settings), document)
    return changed
