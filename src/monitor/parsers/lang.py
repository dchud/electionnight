"""Language identification over title and summary."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from lingua import Language, LanguageDetectorBuilder

from monitor.parsers.base import Item, ParserOutput, needs_parser_sql

LANGUAGES = {
    Language.ENGLISH: "en",
    Language.SPANISH: "es",
    Language.FRENCH: "fr",
    Language.ARABIC: "ar",
    Language.CHINESE: "zh",
    Language.RUSSIAN: "ru",
    Language.GERMAN: "de",
    Language.PORTUGUESE: "pt",
    Language.ITALIAN: "it",
    Language.JAPANESE: "ja",
    Language.KOREAN: "ko",
    Language.UKRAINIAN: "uk",
    Language.HEBREW: "he",
}

# Below this confidence the feed's declared language is kept.
TRUST_DETECTION = 0.75
# With fewer letters than this the declared language is kept. On sampled
# headlines the detector gave a wrong language at high confidence on text of
# under 40 letters ("Polls close in Maine" as German, at 0.97), and none at 40
# or more. A Chinese, Japanese, or Korean character counts as three letters.
MIN_LETTERS_TO_OVERRIDE = 40
WIDE_LETTER_WEIGHT = 3
WIDE_LETTER_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def build_detector() -> Any:
    return (
        LanguageDetectorBuilder.from_languages(*LANGUAGES)
        .with_low_accuracy_mode()
        .build()
    )


def letter_count(text: str) -> int:
    """Letters in a text, with wide characters counted as several."""
    letters = sum(character.isalpha() for character in text)
    wide = len(WIDE_LETTER_RE.findall(text))
    return letters + wide * (WIDE_LETTER_WEIGHT - 1)


def resolve_language(
    declared: str,
    detected: str | None,
    confidence: float,
    letters: int | None = None,
) -> tuple[str, float, str]:
    """Pick a language. Returns (language, confidence, method).

    The declared language stands unless the detector is confident and the
    text is long enough for that confidence to mean something.
    """
    declared = (declared or "").lower()[:2]
    if detected is None or confidence <= 0:
        return declared or "en", 0.0, "feed"
    if detected == declared:
        return declared, confidence, "feed+detected"
    long_enough = letters is None or letters >= MIN_LETTERS_TO_OVERRIDE
    if not declared or (confidence >= TRUST_DETECTION and long_enough):
        return detected, confidence, "detected"
    return declared, confidence, "feed"


class LangParser:
    name = "lang"
    version = "1"
    batch_size = 64
    max_concurrency = 1
    input_query = needs_parser_sql(name, version)

    def __init__(self) -> None:
        self._detector = build_detector()

    def _detect(self, text: str) -> tuple[str | None, float]:
        values = self._detector.compute_language_confidence_values(text)
        if not values:
            return None, 0.0
        top = values[0]
        return LANGUAGES.get(top.language), float(top.value)

    def _run(self, batch: list[Item]) -> ParserOutput:
        output = ParserOutput()
        for item in batch:
            text = f"{item.title}. {item.summary}"[:1000]
            detected, confidence = self._detect(text)
            language, score, method = resolve_language(
                item.language, detected, confidence, letter_count(text)
            )
            item.language = language
            output.add(
                "nlp.lang",
                {
                    "item_id": item.item_id,
                    "language": language,
                    "confidence": score,
                    "method": method,
                    "parser": self.name,
                    "parser_version": self.version,
                },
            )
        return output

    async def run(self, batch: list[Item]) -> ParserOutput:
        return await asyncio.to_thread(self._run, batch)
