"""Global dashboard controls: parsed from the URL, applied to every query."""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LIVE_NOW_SECONDS = 10

WINDOWS = {
    "15m": ("15 m", timedelta(minutes=15)),
    "1h": ("1 h", timedelta(hours=1)),
    "3h": ("3 h", timedelta(hours=3)),
    "since4pm": ("Since 4 pm", None),
    "24h": ("24 h", timedelta(hours=24)),
}
LEVELS = {
    "all": "All levels",
    "national": "National",
    "state": "State",
    "local": "Local",
}
FOCUSES = {
    "all": "All",
    "battleground": "Battlegrounds",
    "close": "Close races",
    "watchlist": "Watchlist",
}
STATE_RE = re.compile(r"^state:[A-Z]{2}$")
UNWATCHED = "race_unwatched"


def live_now() -> datetime:
    """The current time, rounded up to 10 seconds.

    Query results are cached by their parameters, and an exact timestamp would
    never repeat. Rounding up keeps every item seen so far inside the window.
    """
    moment = datetime.now(UTC).timestamp()
    return datetime.fromtimestamp(
        math.ceil(moment / LIVE_NOW_SECONDS) * LIVE_NOW_SECONDS, UTC
    )


def since_4pm(as_of: datetime) -> datetime:
    """The most recent 4 pm Eastern at or before a time."""
    local = as_of.astimezone(ET)
    start = local.replace(hour=16, minute=0, second=0, microsecond=0)
    if start > local:
        start -= timedelta(days=1)
    return start.astimezone(UTC)


@dataclass(frozen=True)
class FocusSets:
    """Current membership of each Focus choice."""

    battleground_states: tuple[str, ...] = ()
    close_race_keys: tuple[str, ...] = ()
    close_state_races: tuple[str, ...] = ()
    watch_states: tuple[str, ...] = ()
    watch_race_keys: tuple[str, ...] = ()
    watch_state_races: tuple[str, ...] = ()


@dataclass(frozen=True)
class Filters:
    window: str = "1h"
    focus: str = "all"
    level: str = "all"
    lang: str = "all"
    copies: str = "unique"
    as_of: datetime | None = None
    default_window: str = "1h"

    @classmethod
    def parse(cls, params: Any, default_window: str = "1h") -> Filters:
        window = params.get("window") or default_window
        focus = params.get("focus") or "all"
        level = params.get("level") or "all"
        lang = params.get("lang") or "all"
        copies = params.get("copies") or "unique"
        as_of_text = params.get("as_of")
        as_of = None
        if as_of_text:
            try:
                as_of = datetime.fromisoformat(as_of_text.replace(" ", "+"))
                if as_of.tzinfo is None:
                    as_of = as_of.replace(tzinfo=UTC)
            except ValueError:
                as_of = None
        return cls(
            window=window if window in WINDOWS else default_window,
            focus=focus
            if focus in FOCUSES or STATE_RE.match(focus)
            else "all",
            level=level if level in LEVELS else "all",
            lang=lang if lang in {"all", "en"} else "all",
            copies=copies if copies in {"unique", "all"} else "unique",
            as_of=as_of,
            default_window=default_window,
        )

    @property
    def now(self) -> datetime:
        return self.as_of or live_now()

    @property
    def window_start(self) -> datetime:
        span = WINDOWS[self.window][1]
        if span is None:
            return since_4pm(self.now)
        return self.now - span

    @property
    def is_replay(self) -> bool:
        return self.as_of is not None

    @property
    def focused_state(self) -> str | None:
        return (
            self.focus.split(":")[1]
            if self.focus.startswith("state:")
            else None
        )

    def with_(self, **changes: Any) -> Filters:
        return replace(self, **changes)

    def query_string(self, **changes: Any) -> str:
        """URL params for these filters, omitting defaults."""
        values = replace(self, **changes) if changes else self
        pairs = {}
        if values.window != values.default_window:
            pairs["window"] = values.window
        if values.focus != "all":
            pairs["focus"] = values.focus
        if values.level != "all":
            pairs["level"] = values.level
        if values.lang != "all":
            pairs["lang"] = values.lang
        if values.copies != "unique":
            pairs["copies"] = values.copies
        if values.as_of:
            pairs["as_of"] = values.as_of.isoformat()
        return urlencode(pairs)

    def focus_sql(
        self, sets: FocusSets, alias: str = "f"
    ) -> tuple[str, dict[str, Any]]:
        """SQL restricting items to the Focus choice."""
        if self.focus == "battleground":
            return (
                f"(list_has_any({alias}.states, $bg_states) "
                f"OR list_has_any({alias}.race_keys, $close_keys) "
                f"OR list_has_any({alias}.state_races, $close_pairs))",
                {
                    "bg_states": list(sets.battleground_states),
                    "close_keys": list(sets.close_race_keys),
                    "close_pairs": list(sets.close_state_races),
                },
            )
        if self.focus == "close":
            return (
                f"(list_has_any({alias}.race_keys, $close_keys) "
                f"OR list_has_any({alias}.state_races, $close_pairs))",
                {
                    "close_keys": list(sets.close_race_keys),
                    "close_pairs": list(sets.close_state_races),
                },
            )
        if self.focus == "watchlist":
            return (
                f"(list_has_any({alias}.states, $watch_states) "
                f"OR list_has_any({alias}.race_keys, $watch_keys) "
                f"OR list_has_any({alias}.state_races, $watch_pairs))",
                {
                    "watch_states": list(sets.watch_states),
                    "watch_keys": list(sets.watch_race_keys),
                    "watch_pairs": list(sets.watch_state_races),
                },
            )
        if self.focused_state:
            return (
                f"list_contains({alias}.states, $focus_state)",
                {"focus_state": self.focused_state},
            )
        return "TRUE", {}

    def where(
        self,
        sets: FocusSets,
        alias: str = "f",
        windowed: bool = True,
        unique: bool | None = None,
        relevant: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        """SQL and params applying every global control to `facts`.

        `unique` overrides the Unique control: outlet-level metrics count all
        copies, because republishing is that outlet's coverage choice. Replay
        keeps out anything first seen after its time, wherever the item sits on
        the time axis.
        """
        clauses = []
        params: dict[str, Any] = {"as_of": self.now}
        clauses.append(f"{alias}.seen_raw_at <= $as_of")
        if windowed:
            clauses.append(f"{alias}.first_seen_at >= $window_start")
            params["window_start"] = self.window_start
        if relevant:
            clauses.append(f"{alias}.relevant")
        if self.level != "all":
            clauses.append(f"list_contains({alias}.levels, $level)")
            params["level"] = self.level
        if self.lang == "en":
            clauses.append(f"{alias}.language = 'en'")
        use_unique = self.copies == "unique" if unique is None else unique
        if use_unique:
            clauses.append(f"{alias}.canonical")
        focus_sql, focus_params = self.focus_sql(sets, alias)
        if focus_sql != "TRUE":
            clauses.append(focus_sql)
            params.update(focus_params)
        return " AND ".join(clauses), params

    def describe(self, windowed: bool = True, span: str | None = None) -> str:
        """The filter in words, shown at the top of evidence lists.

        `span` names a period other than the window; empty leaves it out, for
        a list whose time bin is named already.
        """
        if span is None:
            span = WINDOWS[self.window][0] if windowed else "all time"
        parts = [span] if span else []
        parts.append("unique" if self.copies == "unique" else "all copies")
        focus = (
            f"state {self.focused_state}"
            if self.focused_state
            else FOCUSES.get(self.focus, self.focus)
        )
        parts.append(f"focus={focus}")
        if self.level != "all":
            parts.append(LEVELS[self.level])
        if self.lang == "en":
            parts.append("EN only")
        return " · ".join(parts)


def init_ui_db(path: Path) -> None:
    """Create the web process's own preferences database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS watch "
            "(kind TEXT, key TEXT, PRIMARY KEY (kind, key))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS battleground_override "
            "(state TEXT PRIMARY KEY, included INTEGER)"
        )


def read_watch(path: Path) -> tuple[list[str], list[str]]:
    """Watched (states, race keys)."""
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT kind, key FROM watch").fetchall()
    return (
        [key for kind, key in rows if kind == "state"],
        [key for kind, key in rows if kind == "race"],
    )


def set_watch(path: Path, kind: str, key: str, watched: bool) -> None:
    with sqlite3.connect(path) as db:
        if watched:
            db.execute(
                "INSERT OR IGNORE INTO watch VALUES (?, ?)", (kind, key)
            )
        else:
            db.execute(
                "DELETE FROM watch WHERE kind = ? AND key = ?", (kind, key)
            )


def read_unwatched(path: Path) -> set[str]:
    """Races the reference data watches and the owner removed."""
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT key FROM watch WHERE kind = ?", (UNWATCHED,)
        ).fetchall()
    return {key for (key,) in rows}


def set_race_watch(
    path: Path, key: str, watched: bool, listed: bool = False
) -> None:
    """Record a choice about one race.

    `listed` means `ref.races` marks the race as watched. Removing such a race
    needs its own row, because nothing in this database can change the lake.
    """
    set_watch(path, "race", key, watched)
    set_watch(path, UNWATCHED, key, listed and not watched)


def read_overrides(path: Path) -> dict[str, bool]:
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT state, included FROM battleground_override"
        ).fetchall()
    return {state: bool(included) for state, included in rows}


def set_override(path: Path, state: str, included: bool | None) -> None:
    with sqlite3.connect(path) as db:
        if included is None:
            db.execute(
                "DELETE FROM battleground_override WHERE state = ?", (state,)
            )
        else:
            db.execute(
                "INSERT OR REPLACE INTO battleground_override VALUES (?, ?)",
                (state, int(included)),
            )


RACE_TYPE_BY_OFFICE = {
    "SEN": "senate",
    "SENATE": "senate",
    "GOV": "governor",
    "GOVERNOR": "governor",
    "AG": "attorney_general",
    "ATTORNEY_GENERAL": "attorney_general",
    "SOS": "secretary_of_state",
    "SECRETARY_OF_STATE": "secretary_of_state",
    "LG": "lt_governor",
    "LT_GOVERNOR": "lt_governor",
    "BALLOT_MEASURE": "ballot_measure",
}


def race_state_pair(race: dict[str, Any]) -> str | None:
    """The `state|race_type` form of a race, for matching classified items."""
    state = race.get("state")
    office = (race.get("office") or "").upper()
    if not state:
        return None
    if office.startswith("H"):
        return f"{state}|house"
    if office in RACE_TYPE_BY_OFFICE:
        return f"{state}|{RACE_TYPE_BY_OFFICE[office]}"
    if office.startswith("M"):
        return f"{state}|ballot_measure"
    return None
