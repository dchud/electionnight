"""Language identification over title and summary."""

from __future__ import annotations

import asyncio
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


def build_detector() -> Any:
    return (
        LanguageDetectorBuilder.from_languages(*LANGUAGES)
        .with_low_accuracy_mode()
        .build()
    )


def resolve_language(
    declared: str, detected: str | None, confidence: float
) -> tuple[str, float, str]:
    """Pick a language. Returns (language, confidence, method)."""
    declared = (declared or "").lower()[:2]
    if detected is None:
        return declared or "en", 0.0, "feed"
    if detected == declared:
        return declared, confidence, "feed+detected"
    if confidence >= TRUST_DETECTION or not declared:
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
                item.language, detected, confidence
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
