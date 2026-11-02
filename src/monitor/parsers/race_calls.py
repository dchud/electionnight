"""Rules for race-call claims reported in the news.

These are claims in articles, never official results. The model decides what a
sentence means, including whether it reports a settled result of this election.
`tests/fixtures/race_claims.jsonl` measures how well it does. The code checks
only what code can check: that the evidence appears verbatim in the article,
that the article names the candidate and any named caller, and that the model
did not itself label the contest a primary.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import Any

from monitor.lake import hash_id
from monitor.llm.schemas import RaceCallB

FINAL_STATUSES = {"projected", "declared", "conceded"}
UNSPECIFIED = {"unspecified", "unknown", "none"}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Each pattern reads article text as written, where an acronym is in capitals,
# and reads a model's caller value with case ignored.
CALLERS = (
    (
        "AP",
        r"\bAP\b|(?i:associated press|ассошиэйтед|أسوشيتد برس|美联社|美聯社)",
    ),
    ("NBC", r"\bNBC\b"),
    ("CNN", r"\bCNN\b"),
    ("Fox", r"\bFox\b|\bFOX\b"),
    ("DDHQ", r"\bDDHQ\b|(?i:decision ?desk)"),
    ("ABC", r"\bABC\b"),
    ("CBS", r"\bCBS\b"),
    ("NYT", r"\bNYT\b|(?i:new york times)"),
)

# Reads the model's own label for a contest, not a sentence from an article.
PRIMARY_LABEL_RE = re.compile(
    r"\bprimar(?:y|ies)\b|\bprimarias?\b|\bprimaires?\b|\bvorwahl", re.I
)
SPACE_RE = re.compile(r"\s+")
WORD_RE = re.compile(r"\w+", re.UNICODE)
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
# Typographic marks a model may copy as their plain forms.
PLAIN_MARKS = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


def normalize_text(text: str | None) -> str:
    plain = (text or "").translate(PLAIN_MARKS)
    return SPACE_RE.sub(" ", plain).strip().casefold()


def appears_in(quote: str | None, text: str) -> bool:
    """True when a quote is verbatim, ignoring spacing, quotes, and case."""
    needle = normalize_text(quote)
    return bool(needle) and needle in normalize_text(text)


def fold(text: str | None) -> str:
    """Normalized text with accents removed, for comparing names."""
    decomposed = unicodedata.normalize("NFKD", normalize_text(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _surname(name: str) -> str | None:
    words = [w for w in WORD_RE.findall(fold(name)) if w not in SUFFIXES]
    return words[-1] if words else None


def _has_word(text: str, word: str) -> bool:
    if CJK_RE.search(word):
        return word in text
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def names_person(names: Iterable[str | None], text: str) -> bool:
    """True when the text has the surname from any of these names."""
    folded = fold(text)
    surnames = (_surname(name) for name in names if name)
    return any(_has_word(folded, word) for word in surnames if word)


def normalize_caller(value: str | None) -> str:
    if not value or value.strip().casefold() in UNSPECIFIED:
        return "unspecified"
    for caller, pattern in CALLERS:
        if re.search(pattern, value, re.I):
            return caller
    return "other"


def checked_caller(value: str | None, text: str) -> str:
    """The caller, if the article names that caller."""
    caller = normalize_caller(value)
    if caller == "unspecified":
        return caller
    if caller == "other":
        return caller if appears_in(value, text) else "unspecified"
    pattern = dict(CALLERS)[caller]
    return caller if re.search(pattern, text) else "unspecified"


def _script(text: str) -> str | None:
    """The writing system most of the letters in a text belong to."""
    scripts = Counter(
        unicodedata.name(char, "").split(" ")[0]
        for char in text
        if char.isalpha()
    )
    return scripts.most_common(1)[0][0] if scripts else None


def _names_candidate(names: Iterable[str | None], text: str) -> bool:
    """True unless a name that could appear in the text does not.

    A name written in one script cannot be compared with text in another, so
    that comparison is not made.
    """
    known = [name for name in names if name]
    script = _script(text)
    comparable = [name for name in known if _script(name) == script]
    if not comparable:
        return True
    return names_person(comparable, text)


def call_problem(
    call: RaceCallB, text: str, names: Iterable[str | None] = ()
) -> str | None:
    """Why a claim fails the evidence rules, or None if it passes.

    `names` adds other known names for the winner, such as renderings in
    other scripts, to the name the model gave.
    """
    if not appears_in(call.evidence, text):
        return "not verbatim"
    if PRIMARY_LABEL_RE.search(call.race_desc or ""):
        return "labeled a primary"
    if call.status not in FINAL_STATUSES and call.status != "leading":
        return None
    if not call.winner_name:
        return "no winner named"
    if not _names_candidate([call.winner_name, *names], text):
        return "winner not in article"
    return None


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
    text: str,
) -> dict[str, Any]:
    caller = checked_caller(call.called_by, text)
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
