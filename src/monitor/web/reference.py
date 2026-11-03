"""Data for the sources, methods, status, and watchlist pages."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from markupsafe import Markup

from monitor.settings import Settings
from monitor.web.drilldown import OFFICE_ORDER, parse_iso, race_label
from monitor.web.filters import (
    Filters,
    read_overrides,
    read_unwatched,
    read_watch,
    set_override,
    set_race_watch,
    set_watch,
)
from monitor.web.methods_doc import drifted
from monitor.web.methods_doc import render as render_methods
from monitor.web.panels import (
    ET,
    Panels,
    lean_glyph,
    lean_text,
    load_sql,
    safe_url,
)
from monitor.web.readmodel import ReadModel

METHODS_PATH = Path(__file__).resolve().parents[3] / "docs" / "methods.md"
FETCH_RATE_WINDOW = timedelta(hours=24)
DISK_TTL_SECONDS = 30.0
STALE_STATUS_SECONDS = 180
LEAN_ORDER = ("L", "LL", "C", "LR", "R", "NA", "Unrated")
LEAN_SORT = {label: index for index, label in enumerate(LEAN_ORDER)}
CHART_BUCKETS = {"NA": "Non-US", "Unrated": "Unrated"}
DISK_ROWS = (
    ("lake_live", "lake live data"),
    ("awaiting_cleanup", "compacted, awaiting cleanup"),
    ("catalog", "catalog"),
    ("archive", "archive"),
    ("backups", "backups"),
    ("models", "models"),
    ("reference", "reference data"),
    ("logs", "logs"),
)
FEED_STATES = {"down": 0, "failing": 1, "waiting": 2, "ok": 3}
CRITERIA_INCLUDE = (
    "Established newsrooms with editorial standards and corrections "
    "policies, across a modest spectrum: Left through Right per AllSides, "
    "centered on Lean Left, Center, and Lean Right.",
    "Nonprofit state newsrooms.",
    "Public broadcasters with editorial independence.",
)
CRITERIA_EXCLUDE = (
    "Conspiracy or fabrication-prone sites.",
    "State-controlled media of authoritarian governments, such as RT and "
    "Sputnik.",
    "Pure opinion aggregators.",
    "Partisan campaign or party sites.",
)


def censorship_note(notes: str | None) -> str:
    """An outlet's censorship note from the seed, without the directive."""
    text = notes or ""
    if "censor" not in text.lower():
        return ""
    return text.split(";")[0].strip()


def lean_display(row: dict[str, Any]) -> dict[str, Any]:
    """What a lean cell shows. A rating without a citation reads `Unrated`."""
    label = row.get("lean_label") or "Unrated"
    url = safe_url(row.get("lean_source_url"))
    text = lean_text(label, row.get("country"))
    if text != "Non-US outlet" and label in LEAN_ORDER[:5] and not url:
        label, text = "Unrated", "Unrated"
    if text in {"Non-US outlet", "Unrated"}:
        return {
            "lean_shown": text,
            "lean_glyph": "",
            "lean_url": "",
            "lean_sort": LEAN_SORT["NA" if text == "Non-US outlet" else label],
        }
    return {
        "lean_shown": text,
        "lean_glyph": lean_glyph(label),
        "lean_url": url,
        "lean_sort": LEAN_SORT.get(label, len(LEAN_ORDER)),
    }


def feed_state(feed: dict[str, Any]) -> str:
    """Whether a feed is working, from the poller's per-feed state."""
    if feed.get("open"):
        return "down"
    if feed.get("failures"):
        return "failing"
    if not feed.get("last_success_at"):
        return "waiting"
    return "ok"


GAP_CAUSES = {
    "sleep": "Laptop asleep",
    "network": "Network down",
    "pipeline": "Pipeline stopped",
}


def clock_text(moment: datetime) -> str:
    return moment.astimezone(ET).strftime("%b %-d, %-I:%M %p")


class Reference:
    def __init__(
        self, settings: Settings, model: ReadModel, panels: Panels
    ) -> None:
        self.settings = settings
        self.model = model
        self.panels = panels
        self._methods: tuple[float, dict[str, Any]] | None = None
        self._disk: tuple[float, dict[str, float]] | None = None
        self._disk_lock = threading.Lock()

    # Sources

    def sources(self, filters: Filters) -> dict[str, Any]:
        now = filters.now
        rows = self.model.query(
            "sources_table", "SELECT * FROM sources ORDER BY name"
        )
        # The same filters as the drawers these counts open.
        where, params = filters.where(
            self.panels.focus_sets(),
            windowed=False,
            unique=False,
            relevant=False,
        )
        counts = {
            row["source_id"]: row
            for row in self.model.query(
                "source_counts",
                """
                SELECT f.source_id, count(*) AS items,
                       count(*) FILTER (WHERE f.relevant) AS relevant
                FROM facts f WHERE {where} GROUP BY 1
                """.replace("{where}", where),
                params,
            )
        }
        fetches = {
            row["source_id"]: row
            for row in self.model.query(
                "source_fetches",
                """
                SELECT source_id, count(*) AS n,
                       count(*) FILTER (WHERE failed) AS failed
                FROM fetch_log
                WHERE started_at <= $as_of AND started_at > $since
                GROUP BY 1
                """,
                {"as_of": now, "since": now - FETCH_RATE_WINDOW},
            )
        }
        feeds: dict[str, list[dict[str, Any]]] = {}
        for feed in self.model.read_status().get("feed_details") or []:
            feeds.setdefault(feed.get("source_id"), []).append(feed)
        table = [self._source_row(row, counts, fetches, feeds) for row in rows]
        flagged = [
            row
            for row in table
            if row["state_affiliated"] or row["censorship"]
        ]
        return {
            "sources": table,
            "flagged": flagged,
            "criteria_include": CRITERIA_INCLUDE,
            "criteria_exclude": CRITERIA_EXCLUDE,
        }

    def _source_row(
        self,
        row: dict[str, Any],
        counts: dict[str, dict[str, Any]],
        fetches: dict[str, dict[str, Any]],
        feeds: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        source_id = row["source_id"]
        count = counts.get(source_id, {})
        items = count.get("items", 0)
        fetch = fetches.get(source_id, {})
        attempts = fetch.get("n", 0)
        own_feeds = feeds.get(source_id, [])
        successes = [
            parsed
            for parsed in (
                parse_iso(feed.get("last_success_at")) for feed in own_feeds
            )
            if parsed
        ]
        states = [feed_state(feed) for feed in own_feeds]
        return {
            **row,
            **lean_display(row),
            "homepage": safe_url(row.get("homepage")),
            "feed_list": [
                url for url in (row.get("feed_urls") or "").split("|") if url
            ],
            "censorship": censorship_note(row.get("notes")),
            "items": items,
            "relevant": count.get("relevant", 0),
            "relevant_share": count.get("relevant", 0) / items
            if items
            else None,
            "last_success": max(successes) if successes else None,
            "fetch_attempts": attempts,
            "fetch_failed": fetch.get("failed", 0),
            "error_rate": fetch.get("failed", 0) / attempts
            if attempts
            else None,
            "feed_state": min(states, key=FEED_STATES.__getitem__)
            if states
            else "",
            "feed_errors": [
                feed["last_error"]
                for feed in own_feeds
                if feed.get("last_error")
            ],
        }

    def lean_distribution(self) -> dict[str, Any]:
        rows = self.model.query(
            "sources_lean",
            "SELECT lean_label, lean_source_url, country FROM sources",
        )
        counts: dict[str, int] = {}
        for row in rows:
            shown = lean_display(row)["lean_shown"]
            counts[shown] = counts.get(shown, 0) + 1
        keys = {"Non-US outlet": "NA", "Unrated": "Unrated"}
        order = ("L", "LL", "C", "LR", "R", "Non-US outlet", "Unrated")
        return {
            "table": [
                {
                    "bucket": CHART_BUCKETS.get(keys.get(label, label), label),
                    "key": keys.get(label, label),
                    "n": counts[label],
                }
                for label in order
                if label in counts
            ]
        }

    # Methods

    def methods_document(self) -> dict[str, Any]:
        """The Methods document, its headings, and any drift from settings."""
        try:
            changed = METHODS_PATH.stat().st_mtime
        except OSError:
            return {
                "html": Markup(""),
                "headings": [],
                "has_math": False,
                "drifted": [],
            }
        if self._methods is None or self._methods[0] != changed:
            text = METHODS_PATH.read_text(encoding="utf-8")
            document = render_methods(text)
            self._methods = (
                changed,
                {
                    "html": document.html,
                    "headings": document.headings,
                    "has_math": document.has_math,
                    "drifted": drifted(text, self.settings),
                },
            )
        return self._methods[1]

    def ledger(self, filters: Filters) -> dict[str, Any]:
        rows = self.model.query(
            "ledger", load_sql("ledger"), {"as_of": filters.now}
        )
        return {
            "ledger": rows,
            "ledger_usd": sum(row["usd"] for row in rows),
            "ledger_calls": sum(row["calls"] for row in rows),
        }

    # Status

    def disk(self) -> dict[str, float]:
        """Disk figures, read at most once per interval.

        The walk stats every lake file, and maintenance can remove one while
        it runs. A failed walk keeps the last figures.
        """
        with self._disk_lock:
            now = time.monotonic()
            if self._disk and now - self._disk[0] < DISK_TTL_SECONDS:
                return self._disk[1]
            try:
                figures = self.model.disk_figures()
            except OSError:
                return self._disk[1] if self._disk else {}
            self._disk = (now, figures)
            return figures

    def collection_gaps(self) -> list[dict[str, Any]]:
        """Collection gaps as the status page shows them."""
        return [
            {
                "start": clock_text(gap["gap_start"]),
                "end": clock_text(gap["gap_end"]),
                "minutes": round(
                    (gap["gap_end"] - gap["gap_start"]).total_seconds() / 60
                ),
                "cause": GAP_CAUSES.get(gap["reason"], gap["reason"]),
            }
            for gap in self.panels.collection_gaps()
        ]

    def status(self, filters: Filters) -> dict[str, Any]:
        status = self.model.read_status()
        now = datetime.now(UTC)
        updated = parse_iso(status.get("updated_at"))
        age = (now - updated).total_seconds() if updated else None
        details = status.get("feed_details") or []
        feeds = sorted(
            (
                {
                    **feed,
                    "state": feed_state(feed),
                    "last_success": parse_iso(feed.get("last_success_at")),
                }
                for feed in details
            ),
            key=lambda f: (FEED_STATES[f["state"]], f.get("source_id") or ""),
        )
        states = {name: 0 for name in FEED_STATES}
        for feed in feeds:
            states[feed["state"]] += 1
        figures = self.disk()
        labels = dict(DISK_ROWS)
        # A category added to the disk report still shows, under its key.
        labels.update(
            (key, key.replace("_", " "))
            for key in figures
            if key not in labels and key != "free"
        )
        peak = max((figures.get(key, 0) for key in labels), default=0)
        disk_rows = [
            {
                "label": label,
                "gb": figures[key],
                "width": figures[key] / peak * 100 if peak else 0,
            }
            for key, label in labels.items()
            if key in figures
        ]
        llm = status.get("llm") or {}
        return {
            "has_status": bool(status),
            "status_age": age,
            "status_stale": age is None or age > STALE_STATUS_SECONDS,
            "started_at": parse_iso(status.get("started_at")),
            "last_commit_at": parse_iso(status.get("last_commit_at")),
            "last_rollup_at": parse_iso(status.get("last_rollup_at")),
            "last_maintenance_at": parse_iso(
                status.get("last_maintenance_at")
            ),
            "last_backup_at": parse_iso(status.get("last_backup_at")),
            "shedding": status.get("shedding"),
            "network": status.get("network") or {},
            "gaps": self.collection_gaps(),
            "catch_up": status.get("catch_up"),
            "stages": status.get("stages") or [],
            "feeds": feeds,
            "feed_counts": states,
            "feeds_summary": status.get("feeds") or {},
            "problem_feeds": [f for f in feeds if f["state"] != "ok"],
            "llm": llm,
            "budget": llm.get("budget") or {},
            "disk_rows": disk_rows,
            "disk_free": figures.get("free"),
            "disk_level": (status.get("disk") or {}).get("level"),
            **self.ledger(filters),
        }

    # Watchlist

    def watch_state(self) -> dict[str, Any]:
        path = self.settings.paths.ui_db
        states, races = read_watch(path)
        return {
            "states": set(states),
            "races": set(races),
            "unwatched": read_unwatched(path),
            "overrides": read_overrides(path),
        }

    def race_row(self, race_key: str) -> dict[str, Any] | None:
        rows = self.model.query(
            "race_row",
            "SELECT * FROM races WHERE race_key = $key",
            {"key": race_key},
        )
        return rows[0] if rows else None

    def state_row(self, usps: str) -> dict[str, Any] | None:
        rows = self.model.query(
            "watch_state_row",
            "SELECT usps, name, battleground FROM states WHERE usps = $usps",
            {"usps": usps},
        )
        return rows[0] if rows else None

    def set_race(self, race: dict[str, Any], watched: bool) -> None:
        set_race_watch(
            self.settings.paths.ui_db,
            race["race_key"],
            watched,
            listed=bool(race.get("watch")),
        )

    def set_state(self, usps: str, watched: bool) -> None:
        set_watch(self.settings.paths.ui_db, "state", usps, watched)

    def set_battleground(self, usps: str, included: bool | None) -> None:
        set_override(self.settings.paths.ui_db, usps, included)

    def race_watched(self, race: dict[str, Any]) -> bool:
        watch = self.watch_state()
        key = race["race_key"]
        return key in watch["races"] or (
            bool(race.get("watch")) and key not in watch["unwatched"]
        )

    def override_context(self, state: dict[str, Any]) -> dict[str, Any]:
        overrides = self.watch_state()["overrides"]
        usps = state["usps"]
        override = overrides.get(usps)
        listed = bool(state.get("battleground"))
        return {
            "usps": usps,
            "override": override,
            "listed": listed,
            "effective": listed if override is None else override,
        }

    def watchlist(self, filters: Filters) -> dict[str, Any]:
        races = self.model.query(
            "watch_races",
            "SELECT race_key, state, office, district, rating, rating_source, "
            "rating_url, close_race, watch FROM races",
        )
        states = self.model.query(
            "watch_states",
            "SELECT usps, name, battleground FROM states ORDER BY usps",
        )
        watch = self.watch_state()
        race_rows = [
            {
                **row,
                "label": race_label(row),
                "rating_url": safe_url(row.get("rating_url")),
                "watched": row["race_key"] in watch["races"]
                or (
                    bool(row.get("watch"))
                    and row["race_key"] not in watch["unwatched"]
                ),
            }
            for row in races
        ]
        race_rows.sort(
            key=lambda r: (
                r.get("state") or "",
                OFFICE_ORDER.get(r["office"], 9),
                r.get("district") or "",
                r["race_key"],
            )
        )
        return {
            "watch_races": race_rows,
            "watched_count": sum(1 for row in race_rows if row["watched"]),
            "state_rows": [
                {
                    **self.override_context(row),
                    "name": row["name"],
                    "watched": row["usps"] in watch["states"],
                }
                for row in states
            ],
            **self.focus_counts(filters),
        }

    def focus_counts(self, filters: Filters) -> dict[str, Any]:
        """How many relevant items each editable Focus selects right now."""
        sets = self.panels.focus_sets()
        counts = {}
        for focus in ("watchlist", "battleground", "close"):
            where, params = filters.with_(focus=focus).where(sets)
            counts[focus] = self.model.query(
                "focus_count",
                f"SELECT count(*) AS n FROM facts f WHERE {where}",
                params,
            )[0]["n"]
        return {
            "focus_counts": counts,
            "battleground_size": len(sets.battleground_states),
            "watch_state_count": len(sets.watch_states),
            "watch_race_count": len(sets.watch_race_keys),
        }
