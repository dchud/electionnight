"""Validation for model-written text that must cite its sources.

A sentence with no citation is removed. A citation to anything that was not
supplied is removed, and a sentence left with none is removed with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CITATION_RE = re.compile(r"\[\^?(\w+)\]")
SENTENCE_RE = re.compile(r"(?<=[.!?。！？])(?:\[\^?\w+\])*\s+")


@dataclass
class Validated:
    markdown: str
    citations: dict[str, str] = field(default_factory=dict)
    sentences_kept: int = 0
    sentences_dropped: int = 0
    citations_dropped: int = 0


def split_sentences(text: str) -> list[str]:
    """Split on sentence ends, keeping trailing citation markers attached."""
    pieces: list[str] = []
    start = 0
    for match in SENTENCE_RE.finditer(text):
        end = match.end()
        pieces.append(text[start:end].strip())
        start = end
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
    """Validate text already split into (sentence, citation refs) pairs."""
    joined = []
    for paragraph in paragraphs:
        parts = []
        for sentence, refs in paragraph:
            cleaned = CITATION_RE.sub("", sentence).strip()
            parts.append(cleaned + "".join(f"[{ref}]" for ref in refs))
        joined.append(" ".join(parts))
    return validate_cited_text("\n\n".join(joined), allowed)
