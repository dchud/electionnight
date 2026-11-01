"""Rules for race-call claims reported in the news.

These are claims in articles, never official results. A claim counts only if
its evidence appears verbatim in the article, and a win or concession claim
must use explicit language.
"""

from __future__ import annotations

import re
from typing import Any

from monitor.lake import hash_id
from monitor.llm.schemas import RaceCallB

CALLERS = {
    "ap": "AP",
    "associated press": "AP",
    "nbc": "NBC",
    "nbc news": "NBC",
    "cnn": "CNN",
    "fox": "Fox",
    "fox news": "Fox",
    "ddhq": "DDHQ",
    "decision desk": "DDHQ",
    "decision desk hq": "DDHQ",
    "abc": "ABC",
    "abc news": "ABC",
    "cbs": "CBS",
    "cbs news": "CBS",
    "nyt": "NYT",
    "new york times": "NYT",
    "the new york times": "NYT",
}
FINAL_STATUSES = {"projected", "declared", "conceded"}
EXPLICIT_RE = re.compile(
    r"project|declar|conced|called|call(s|ing) the race|winner|wins?\b|won\b|"
    r"re-?elected|elected|defeat|victor|gana|ganó|proyect|remporte|победи|"
    r"胜|當選|当选|فاز",
    re.I,
)
SPACE_RE = re.compile(r"\s+")
QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def normalize_text(text: str | None) -> str:
    return SPACE_RE.sub(" ", (text or "").translate(QUOTES)).strip().casefold()


def appears_in(quote: str | None, text: str) -> bool:
    """True when a quote is verbatim, ignoring spacing, quotes, and case."""
    needle = normalize_text(quote)
    return bool(needle) and needle in normalize_text(text)


def normalize_caller(value: str | None) -> str:
    if not value:
        return "unspecified"
    key = value.strip().casefold()
    if key in {"unspecified", "unknown", "none"}:
        return "unspecified"
    return CALLERS.get(key, "other")


def accept_call(call: RaceCallB, text: str) -> bool:
    """Apply the evidence rules."""
    if not appears_in(call.evidence, text):
        return False
    if call.status in FINAL_STATUSES:
        return bool(EXPLICIT_RE.search(call.evidence))
    return True


def call_confidence(status: str, caller: str) -> float:
    if status in FINAL_STATUSES:
        return 0.9 if caller not in {"unspecified", "other"} else 0.7
    return 0.5


def call_row(
    item_id: str,
    call: RaceCallB,
    race_key: str | None,
    winner_entity_id: str | None,
    first_seen_at: Any,
    parser: str,
    version: str,
) -> dict[str, Any]:
    caller = normalize_caller(call.called_by)
    return {
        "call_id": hash_id(
            item_id, race_key or call.race_desc, call.winner_name, call.status
        ),
        "item_id": item_id,
        "race_key": race_key,
        "race_desc": call.race_desc,
        "winner_name": call.winner_name,
        "winner_entity_id": winner_entity_id,
        "party": call.party,
        "called_by": caller,
        "status": call.status,
        "claimed_at_text": call.claimed_at_text,
        "confidence": call_confidence(call.status, caller),
        "evidence": call.evidence,
        "state": call.state,
        "office": call.office,
        "district": call.district,
        "first_seen_at": first_seen_at,
        "parser": parser,
        "parser_version": version,
    }
