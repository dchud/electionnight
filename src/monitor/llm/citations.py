"""Validation for model-written text that must cite its sources.

A sentence with no citation is removed. A citation to anything that was not
supplied is removed, and a sentence left with none is removed with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CITATION_RE = re.compile(r"\[\^?(\w+)\]")
BOUNDARY_RE = re.compile(
    r"(?P<end>[.!?。！？][\"'”’)]*)(?P<refs>(?:\[\^?\w+\])*)(?P<gap>\s+|$)"
)
LAST_WORD_RE = re.compile(r"([\w.]+)$")
NEXT_STARTS_SENTENCE_RE = re.compile(r"[\"'“‘(]*[^\W\d_]", re.UNICODE)
# A period after one of these usually does not end a sentence.
ABBREVIATIONS = {
    "sen",
    "rep",
    "reps",
    "gov",
    "lt",
    "pres",
    "sec",
    "gen",
    "col",
    "maj",
    "capt",
    "sgt",
    "rev",
    "hon",
    "dr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "st",
    "jr",
    "sr",
    "vs",
    "no",
    "inc",
    "corp",
    "co",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
    "ala",
    "ariz",
    "ark",
    "calif",
    "colo",
    "conn",
    "del",
    "fla",
    "ill",
    "ind",
    "kan",
    "mass",
    "mich",
    "minn",
    "miss",
    "neb",
    "nev",
    "okla",
    "ore",
    "pa",
    "tenn",
    "tex",
    "va",
    "wash",
    "wis",
    "wyo",
}


@dataclass
class Validated:
    markdown: str
    citations: dict[str, str] = field(default_factory=dict)
    sentences_kept: int = 0
    sentences_dropped: int = 0
    citations_dropped: int = 0


def _ends_sentence(text: str, match: re.Match[str]) -> bool:
    """Whether punctuation at this match really closes a sentence."""
    if match.group("refs") or match.end() >= len(text):
        return True
    if match.group("end")[0] != ".":
        return True
    word = LAST_WORD_RE.search(text[: match.start("end")])
    if word:
        last = word.group(1).split(".")[-1].lower()
        # An initial such as "J." or "U.S." usually continues the sentence.
        if last in ABBREVIATIONS or len(last) == 1:
            return False
    # A lowercase word or a number next means the sentence continues.
    following = text[match.end() :]
    first = following.lstrip("\"'“‘(")[:1]
    if first.isdigit() or first.islower():
        return False
    return NEXT_STARTS_SENTENCE_RE.match(following) is not None


def split_sentences(text: str) -> list[str]:
    """Split on sentence ends, keeping trailing citation markers attached.

    A citation marker always ends a sentence. Without one, a period after a
    common abbreviation, an initial, or before a lowercase word does not.
    """
    pieces: list[str] = []
    start = 0
    for match in BOUNDARY_RE.finditer(text):
        if _ends_sentence(text, match):
            pieces.append(text[start : match.end()].strip())
            start = match.end()
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces


def validate_cited_text(text: str, allowed: dict[str, str]) -> Validated:
    """Keep cited sentences; `allowed` maps citation refs to item ids.

    Citation numbers in the result are assigned in order of first use.
    """
    numbers: dict[str, str] = {}
    citations: dict[str, str] = {}
    result = Validated("")
    paragraphs = []
    for paragraph in text.split("\n\n"):
        kept = []
        for sentence in split_sentences(paragraph):
            refs = CITATION_RE.findall(sentence)
            body = CITATION_RE.sub("", sentence).strip()
            body = re.sub(r"\s+([.,;:!?])", r"\1", body)
            valid = [ref for ref in refs if ref in allowed]
            result.citations_dropped += len(refs) - len(valid)
            if not valid or not body:
                result.sentences_dropped += 1
                continue
            markers = ""
            for ref in dict.fromkeys(valid):
                if ref not in numbers:
                    number = str(len(numbers) + 1)
                    numbers[ref] = number
                    citations[number] = allowed[ref]
                markers += f"[^{numbers[ref]}]"
            kept.append(f"{body}{markers}")
            result.sentences_kept += 1
        if kept:
            paragraphs.append(" ".join(kept))
    result.markdown = "\n\n".join(paragraphs)
    result.citations = citations
    return result


def validate_structured(
    paragraphs: list[list[tuple[str, list[str]]]],
    allowed: dict[str, str],
) -> Validated:
    """Validate text the model already split into sentences with citations.

    Each sentence stays whole. Only its citations decide whether it is kept.
    """
    numbers: dict[str, str] = {}
    result = Validated("")
    rendered = []
    for paragraph in paragraphs:
        kept = []
        for sentence, refs in paragraph:
            body = CITATION_RE.sub("", sentence).strip()
            body = re.sub(r"\s+([.,;:!?])", r"\1", body)
            valid = [ref for ref in refs if ref in allowed]
            result.citations_dropped += len(refs) - len(valid)
            if not valid or not body:
                result.sentences_dropped += 1
                continue
            markers = ""
            for ref in dict.fromkeys(valid):
                if ref not in numbers:
                    number = str(len(numbers) + 1)
                    numbers[ref] = number
                    result.citations[number] = allowed[ref]
                markers += f"[^{numbers[ref]}]"
            kept.append(f"{body}{markers}")
            result.sentences_kept += 1
        if kept:
            rendered.append(" ".join(kept))
    result.markdown = "\n\n".join(rendered)
    return result
