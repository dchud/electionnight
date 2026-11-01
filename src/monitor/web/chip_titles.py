"""Words for the short codes on story chips, shown on hover."""

from __future__ import annotations

from typing import Any

from monitor.ref.races import STATE_NAMES

LANGUAGE_NAMES = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "uk": "Ukrainian",
    "zh": "Chinese",
}
LEAN_NAMES = {
    "L": "Left",
    "LL": "Lean left",
    "C": "Center",
    "LR": "Lean right",
    "R": "Right",
}
# The table also holds aliases, such as "washington dc". Each state's full name
# comes first, so the first name wins.
USPS_NAMES: dict[str, str] = {}
for _name, _usps in STATE_NAMES.items():
    _title = _name.title().replace(" Of ", " of ").replace("Us ", "U.S. ")
    USPS_NAMES.setdefault(_usps, _title)


def state_name(usps: Any) -> str:
    """A state's full name, or the code if it is not a state."""
    return USPS_NAMES.get(str(usps), str(usps))


def language_name(code: Any) -> str:
    """A language's name, or the code in capitals if it is not listed."""
    text = str(code or "")
    return LANGUAGE_NAMES.get(text.lower(), text.upper())


def level_title(level: Any) -> str:
    """What a story's N, S, or L badge stands for."""
    return f"{str(level or '').capitalize()} story"


def lean_title(label: Any) -> str:
    """An outlet's lean in words. Other labels already are words."""
    text = str(label or "Unrated")
    name = LEAN_NAMES.get(text)
    return f"Outlet lean: {name}" if name else text


def tone_title(tone: Any) -> str:
    """A tone value with its direction and scale."""
    if tone is None:
        return ""
    value = float(tone)
    if value > 0:
        word = "positive"
    elif value < 0:
        word = "negative"
    else:
        word = "neutral"
    return f"Tone {value:+.1f}, {word}, on a scale of -2 to +2"
