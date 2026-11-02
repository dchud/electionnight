"""Data for every dashboard panel. All queries run on the read model."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import yaml

from monitor import analytics
from monitor.settings import Settings
from monitor.web.filters import (
    Filters,
    FocusSets,
    race_state_pair,
    read_overrides,
    read_unwatched,
    read_watch,
)
from monitor.web.readmodel import ReadModel

ET = ZoneInfo("America/New_York")
GAP_NOTICE = timedelta(minutes=30)
TOPIC_HEAT_SPAN = timedelta(hours=3)
# The attention index compares each window with this period.
BASELINE_START = datetime(2026, 11, 1, 12, 0, tzinfo=ET)
BASELINE_END = datetime(2026, 11, 3, 12, 0, tzinfo=ET)
LEAN_BUCKETS = ("L", "LL", "C", "LR", "R")
LEAN_POSITION = {label: index for index, label in enumerate(LEAN_BUCKETS)}
SPARK_BINS = 24
# Rank bins widen with the window, so a day of data stays readable.
BUMP_BIN_SHORT = 15
BUMP_BIN_LONG = 60
BUMP_LONG_SPAN = timedelta(hours=6)
BIN_MINUTES = 5
HEAT_BIN_MINUTES = 15
PRESS_BIN_MINUTES = 30
# As many rows as the page keeps in its live list.
WIRE_PUSH_LIMIT = 100
KPI_RECENT_MINUTES = 15
RISING_SHORT = timedelta(minutes=analytics.SHORT_WINDOW_MINUTES)
RISING_SPAN = timedelta(
    minutes=analytics.SHORT_WINDOW_MINUTES + analytics.TRAILING_WINDOW_MINUTES
)


def load_sql(name: str) -> str:
    return (
        resources.files("monitor.web")
        .joinpath(f"queries/{name}.sql")
        .read_text(encoding="utf-8")
    )


def lean_glyph(label: str | None) -> str:
    """A monochrome 5-position glyph; `●○○○○` for Left."""
    position = LEAN_POSITION.get(label or "")
    if position is None:
        return ""
    return "".join("●" if i == position else "○" for i in range(5))


def lean_text(label: str | None, country: str | None = None) -> str:
    if label == "NA" or (country and country != "US"):
        return "Non-US outlet"
    return label or "Unrated"


def evidence_query(**params: Any) -> str:
    """URL params that open the evidence drawer for one figure."""
    return urlencode(
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in params.items()
        }
    )


def safe_url(url: str | None) -> str:
    """The URL when it is http or https. Feeds supply these, so check them."""
    text = (url or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else ""


def decorate_item(row: dict[str, Any]) -> dict[str, Any]:
    """Add the lean glyph and text, and clear an unsafe link."""
    return {
        **row,
        "url": safe_url(row.get("url")),
        "glyph": lean_glyph(row.get("lean_label")),
        "lean": lean_text(row.get("lean_label"), row.get("country")),
    }


def sparkline_points(
    values: Sequence[float], width: int = 120, height: int = 24
) -> str:
    """SVG polyline points for a series, scaled to its own maximum."""
    if not values:
        return ""
    peak = max(values) or 1
    step = width / max(len(values) - 1, 1)
    return " ".join(
        f"{index * step:.1f},{height - (value / peak) * (height - 2) - 1:.1f}"
        for index, value in enumerate(values)
    )


def bin_series(
    rows: list[dict[str, Any]], end: datetime, bins: int = SPARK_BINS
) -> list[int]:
    """Counts per 5-minute bin, oldest first, ending at `end`."""
    series = [0] * bins
    last = end.timestamp() // (BIN_MINUTES * 60)
    for row in rows:
        position = row["bin_start"].timestamp() // (BIN_MINUTES * 60)
        index = int(bins - 1 - (last - position))
        if 0 <= index < bins:
            series[index] = int(row["n"])
    return series


def parse_clock(text: str | None) -> datetime | None:
    """A time such as `8:00 PM` on election night, in Eastern time."""
    if not text:
        return None
    text = str(text).strip().upper().replace(".", "")
    for layout in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            parsed = datetime.strptime(text, layout)
        except ValueError:
            continue
        day = 3 if parsed.hour >= 6 else 4
        return datetime(2026, 11, day, parsed.hour, parsed.minute, tzinfo=ET)
    return None


def whole_bin(moment: datetime, minutes: int = 15) -> datetime:
    """The start of the bin holding a moment, so no bin is counted in part."""
    return moment.replace(
        minute=moment.minute - moment.minute % minutes,
        second=0,
        microsecond=0,
    )


def _clock(moment: datetime) -> str:
    return moment.astimezone(ET).strftime("%-I:%M %p").lower()


@dataclass(frozen=True)
class PollClose:
    at: datetime
    states: tuple[str, ...]

    @property
    def clock(self) -> str:
        return self.at.astimezone(ET).strftime("%-I:%M")

    @property
    def label(self) -> str:
        hour = self.at.astimezone(ET).strftime("%-I:%M %p").lower()
        return f"{hour} {' '.join(self.states)}"


def load_poll_closes(config_dir: Path) -> list[PollClose]:
    """First poll-closing time per state, grouped by time."""
    path = config_dir / "poll_closings.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    states = data.get("states") or {}
    groups: dict[datetime, list[str]] = {}
    for state, entry in states.items():
        if not isinstance(entry, dict):
            continue
        at = parse_clock(entry.get("first_close"))
        if at is not None:
            groups.setdefault(at, []).append(str(state))
    return [
        PollClose(at, tuple(sorted(names)))
        for at, names in sorted(groups.items())
    ]


@dataclass(frozen=True)
class AttentionBaseline:
    """Relevant items in the baseline period, per state and in all."""

    by_state: dict[str, int]
    total: int


class Panels:
    """Named queries, shaped for templates and charts."""

    def __init__(self, settings: Settings, model: ReadModel) -> None:
        self.settings = settings
        self.model = model
        self.poll_closes = load_poll_closes(settings.paths.config)
        # No race has a result before polls first close.
        self.calls_open = min(
            (close.at for close in self.poll_closes), default=None
        )

    def focus_sets(self) -> FocusSets:
        """Current members of each Focus choice."""
        races = self.model.query(
            "focus_races",
            "SELECT race_key, state, office, close_race, watch FROM races",
        )
        states = self.model.query(
            "focus_states", "SELECT usps, battleground FROM states"
        )
        ui_db = self.settings.paths.ui_db
        overrides = read_overrides(ui_db)
        watch_states, watch_keys = read_watch(ui_db)
        unwatched = read_unwatched(ui_db)
        battleground = {
            row["usps"]
            for row in states
            if overrides.get(row["usps"], bool(row["battleground"]))
        } | {state for state, included in overrides.items() if included}
        close = [row for row in races if row["close_race"]]
        watched = [
            row
            for row in races
            if (row["watch"] and row["race_key"] not in unwatched)
            or row["race_key"] in watch_keys
        ]
        return FocusSets(
            battleground_states=tuple(sorted(battleground)),
            close_race_keys=tuple(r["race_key"] for r in close),
            close_state_races=tuple(
                pair for pair in map(race_state_pair, close) if pair
            ),
            watch_states=tuple(sorted(watch_states)),
            watch_race_keys=tuple(r["race_key"] for r in watched),
            watch_state_races=tuple(
                pair for pair in map(race_state_pair, watched) if pair
            ),
        )

    def _where(
        self, filters: Filters, **options: Any
    ) -> tuple[str, dict[str, Any]]:
        return filters.where(self.focus_sets(), **options)

    # Header

    def kpis(self, filters: Filters) -> dict[str, Any]:
        where, params = self._where(filters, windowed=False)
        now = filters.now
        since = now - timedelta(minutes=BIN_MINUTES * SPARK_BINS)
        rows = self.model.query(
            "kpi_bins",
            f"""
            SELECT time_bucket(INTERVAL '5 minutes', f.first_seen_at)
                       AS bin_start, count(*) AS n
            FROM facts f WHERE {where} AND f.first_seen_at >= $since
            GROUP BY 1
            """,
            {**params, "since": since},
        )
        series = bin_series(rows, now)
        recent = self.model.query(
            "kpi_recent",
            f"""
            SELECT count(*) AS n FROM facts f
            WHERE {where} AND f.first_seen_at >= $since
            """,
            {**params, "since": now - timedelta(minutes=KPI_RECENT_MINUTES)},
        )
        active = self.model.query(
            "kpi_active",
            """
            SELECT count(*) AS n FROM (
                SELECT status FROM storyline_versions
                WHERE created_at <= $as_of
                QUALIFY row_number() OVER (
                    PARTITION BY storyline_id
                    ORDER BY version DESC, created_at DESC) = 1
            ) WHERE status = 'active'
            """,
            {"as_of": now},
        )
        status = self.model.read_status()
        feeds = status.get("feeds") or {}
        upcoming = next(
            (close for close in self.poll_closes if close.at > now), None
        )
        return {
            "per_minute": recent[0]["n"] / KPI_RECENT_MINUTES,
            "recent_minutes": KPI_RECENT_MINUTES,
            "spark": sparkline_points(series),
            "active_storylines": active[0]["n"],
            # Feed health is known only for the present moment.
            "feeds_healthy": None
            if filters.is_replay
            else feeds.get("healthy", 0),
            "feeds_total": None
            if filters.is_replay
            else feeds.get("total", 0),
            "next_close": upcoming.label if upcoming else "All polls closed",
        }

    def status_strip(self) -> dict[str, Any]:
        status = self.model.read_status()
        now = datetime.now(UTC)
        last_commit = status.get("last_commit_at")
        lag = None
        if last_commit:
            lag = (now - datetime.fromisoformat(last_commit)).total_seconds()
        llm = status.get("llm") or {}
        budget = llm.get("budget") or {}
        disk = status.get("disk") or {}
        warnings = []
        if lag is None or lag > 180:
            warnings.append("Pipeline is not committing")
        network = status.get("network") or {}
        if network.get("online") is False and network.get("offline_since"):
            since = datetime.fromisoformat(network["offline_since"])
            warnings.append(
                f"Offline since {_clock(since)}: not collecting. "
                "Collection resumes when the network returns"
            )
        failed_since = self.model.refresh_failed_since
        if failed_since is not None:
            warnings.append(
                f"Cannot read new data since {_clock(failed_since)}: "
                "panels show older data"
            )
        gaps = self.collection_gaps()
        if gaps and now - gaps[0]["gap_end"] < GAP_NOTICE:
            warnings.append(
                f"No collection {_clock(gaps[0]['gap_start'])}"
                f"–{_clock(gaps[0]['gap_end'])}; stories published then "
                "were read on return"
            )
        writer = status.get("writer") or {}
        if writer.get("held_rows"):
            warnings.append(
                f"{writer['held_rows']} rows held back: commits are failing"
            )
        if writer.get("rejected_rows"):
            warnings.append(
                f"{writer['rejected_rows']} rows rejected: not in the lake"
            )
        if (status.get("archive") or {}).get("alive") is False:
            warnings.append("Archive writer stopped: payloads are not saved")
        if llm.get("degraded"):
            warnings.append("LLM degraded: keyword classification")
        if budget.get("state") == "hard":
            warnings.append("LLM hard cap reached")
        elif budget.get("state") == "soft":
            warnings.append("LLM soft cap: keywords for low-signal sources")
        return {
            "lag_seconds": int(lag) if lag is not None else None,
            "llm_state": (
                "disabled"
                if llm.get("disabled")
                else "degraded"
                if llm.get("degraded")
                else budget.get("state", "unknown")
            ),
            "spent_today": budget.get("spent_today", 0),
            "free_gb": disk.get("free_gb"),
            "disk_level": disk.get("level", "OK"),
            "warnings": warnings,
        }

    # Band 1

    def wire(self, filters: Filters, limit: int = 40) -> list[dict[str, Any]]:
        where, params = self._where(filters, windowed=False)
        rows = self.model.query(
            "wire",
            load_sql("wire").replace("{where}", where),
            {**params, "limit": limit},
        )
        return [decorate_item(row) for row in rows]

    def wire_recent(
        self,
        filters: Filters,
        after: int,
        through: int,
        limit: int = WIRE_PUSH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Items that became relevant in refreshes `after` < seq <= `through`.

        Classification finishes out of order and can lag, so a stream tracks
        when an item became visible, not when it was seen.
        """
        where, params = self._where(filters, windowed=False)
        rows = self.model.rows(
            load_sql("wire").replace(
                "{where}",
                f"{where} AND f.visible_seq > $after "
                "AND f.visible_seq <= $through",
            ),
            {**params, "limit": limit, "after": after, "through": through},
        )
        return [decorate_item(row) for row in rows]

    def rising(self, filters: Filters, limit: int = 6) -> list[dict[str, Any]]:
        """Storylines that meet the rising rule, highest velocity first."""
        where, params = self._where(filters, windowed=False)
        every_copy, _ = self._where(filters, windowed=False, unique=False)
        rows = self.model.query(
            "rising",
            load_sql("rising")
            .replace("{where}", where)
            .replace("{where_every_copy}", every_copy),
            {
                **params,
                "limit": limit,
                "min_score": analytics.RISING_SCORE,
                "min_observed": analytics.RISING_MIN_OBSERVED,
            },
        )
        if not rows:
            return []
        ids = [row["storyline_id"] for row in rows]
        now = filters.now
        bins = self.model.query(
            "rising_bins",
            f"""
            SELECT f.storyline_id,
                   time_bucket(INTERVAL '5 minutes', f.first_seen_at)
                       AS bin_start, count(*) AS n
            FROM facts f
            WHERE {where} AND f.storyline_id IN (SELECT unnest($ids))
              AND f.first_seen_at >= $since
            GROUP BY ALL
            """,
            {
                **params,
                "ids": ids,
                "since": now - timedelta(minutes=BIN_MINUTES * SPARK_BINS),
            },
        )
        lean_rows = self.coverage_by_lean(filters, ids)
        cards = []
        for row in rows:
            series = bin_series(
                [b for b in bins if b["storyline_id"] == row["storyline_id"]],
                now,
            )
            cards.append(
                {
                    **row,
                    "spark": sparkline_points(series),
                    "observed_query": evidence_query(
                        kind="storyline",
                        key=row["storyline_id"],
                        recent=analytics.SHORT_WINDOW_MINUTES,
                    ),
                    "outlets_query": evidence_query(
                        kind="storyline",
                        key=row["storyline_id"],
                        recent=int(RISING_SPAN.total_seconds() // 60),
                        copies="all",
                    ),
                    "is_new": row["first_seen_at"] is not None
                    and analytics.is_new(row["first_seen_at"], now),
                    "lean_segments": lean_rows.get(row["storyline_id"], []),
                    "citations": json.loads(row.get("citations") or "{}"),
                }
            )
        return cards

    def coverage_by_lean(
        self, filters: Filters, storyline_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Share of each lean bucket's coverage per storyline.

        Outlet-level metrics count all copies, because republishing is that
        outlet's coverage choice.
        """
        where, params = self._where(filters, unique=False)
        rows = self.model.query(
            "lean_share",
            load_sql("lean_share").replace("{where}", where),
            {**params, "ids": storyline_ids},
        )
        totals: dict[str, int] = {}
        by_story: dict[str, dict[str, int]] = {}
        for row in rows:
            if row["storyline_id"] is None:
                totals[row["lean_label"]] = row["bucket_total"]
                continue
            totals[row["lean_label"]] = row["bucket_total"]
            by_story.setdefault(row["storyline_id"], {})[row["lean_label"]] = (
                row["n"]
            )
        result = {}
        for storyline_id, counts in by_story.items():
            shares = [
                analytics.coverage_share(
                    counts.get(bucket, 0), totals.get(bucket, 0)
                )
                for bucket in LEAN_BUCKETS
            ]
            peak = max((s for s in shares if s is not None), default=0) or 1
            result[storyline_id] = [
                {
                    "bucket": bucket,
                    "share": share,
                    "intensity": (share or 0) / peak,
                }
                for bucket, share in zip(LEAN_BUCKETS, shares, strict=True)
            ]
        return result

    def latest_brief(
        self, filters: Filters, offset: int = 0
    ) -> dict[str, Any] | None:
        rows = self.model.query(
            "brief",
            """
            SELECT * FROM briefs WHERE created_at <= $as_of
            ORDER BY created_at DESC LIMIT 1 OFFSET $offset
            """,
            {"as_of": filters.now, "offset": offset},
        )
        if not rows:
            return None
        brief = rows[0]
        return {
            **brief,
            "citation_map": json.loads(brief["citations"] or "{}"),
        }

    def brief_count(self, filters: Filters) -> int:
        rows = self.model.query(
            "brief_count",
            "SELECT count(*) AS n FROM briefs WHERE created_at <= $as_of",
            {"as_of": filters.now},
        )
        return rows[0]["n"]

    # Band 2

    def attention_baseline(self, filters: Filters) -> AttentionBaseline:
        """Baseline counts under the Level, Language, and Unique controls.

        Focus chooses which states are shown, and never changes a state's
        value, so it does not apply here or to the window beside it.
        """
        where, params = self._where(filters.with_(focus="all"), windowed=False)
        period = "f.first_seen_at >= $start AND f.first_seen_at < $end"
        params = {**params, "start": BASELINE_START, "end": BASELINE_END}
        rows = self.model.query(
            "attention_baseline",
            f"""
            SELECT s AS usps, count(*) AS n
            FROM (SELECT *, unnest(states) AS s FROM facts) f
            WHERE {where} AND {period}
            GROUP BY 1
            """,
            params,
        )
        total = self.model.query(
            "attention_baseline_total",
            f"SELECT count(*) AS n FROM facts f WHERE {where} AND {period}",
            params,
        )[0]["n"]
        return AttentionBaseline(
            {row["usps"]: row["n"] for row in rows}, total
        )

    def map_data(
        self, filters: Filters, metric: str = "attention"
    ) -> dict[str, Any]:
        """Every state's figures. Focus only dims the states outside it."""
        unfocused = filters.with_(focus="all")
        where, params = self._where(unfocused)
        window_rows = self.model.query(
            "map_window",
            f"""
            SELECT s AS usps, count(*) AS n,
                   sum(f.tone) FILTER (WHERE NOT f.is_opinion) AS tone_sum,
                   sum(f.tone * f.tone) FILTER (WHERE NOT f.is_opinion)
                       AS tone_sq,
                   count(f.tone) FILTER (WHERE NOT f.is_opinion) AS tone_n
            FROM (SELECT *, unnest(states) AS s FROM facts) f
            WHERE {where} GROUP BY 1 ORDER BY 1
            """,
            params,
        )
        total_window = self.model.query(
            "map_total",
            f"SELECT count(*) AS n FROM facts f WHERE {where}",
            params,
        )[0]["n"]
        baseline = self.attention_baseline(filters)
        top_stories = self.model.query(
            "map_top_story",
            f"""
            SELECT usps, label FROM (
                SELECT s AS usps, st.label, count(*) AS n,
                       row_number() OVER (PARTITION BY s
                           ORDER BY count(*) DESC) AS rn
                FROM (SELECT *, unnest(states) AS s FROM facts) f
                JOIN storylines st USING (storyline_id)
                WHERE {where} AND st.label <> ''
                GROUP BY s, st.label)
            WHERE rn = 1
            """,
            params,
        )
        top_by_state = {row["usps"]: row["label"] for row in top_stories}
        sets = self.focus_sets()
        focused = self._focus_states(filters, sets)
        states = []
        for row in window_rows:
            usps = row["usps"]
            attention = analytics.attention_index(
                row["n"],
                total_window,
                baseline.by_state.get(usps, 0),
                baseline.total,
            )
            tone = analytics.tone_aggregate(
                row["tone_sum"] or 0, row["tone_sq"] or 0, row["tone_n"] or 0
            )
            states.append(
                {
                    "usps": usps,
                    "n": row["n"],
                    "attention": round(attention, 3),
                    "tone": None if tone.mean is None else round(tone.mean, 3),
                    "tone_n": row["tone_n"],
                    "tone_low_n": tone.low_n,
                    "top_storyline": top_by_state.get(usps),
                }
            )
        return {
            "metric": metric,
            "states": states,
            "battleground": list(sets.battleground_states),
            "focus_states": sorted(focused) if focused is not None else None,
        }

    def _focus_states(
        self, filters: Filters, sets: FocusSets
    ) -> set[str] | None:
        """States left at full strength on the map; None means all."""
        if filters.focused_state:
            return {filters.focused_state}
        if filters.focus == "battleground":
            return set(sets.battleground_states)
        if filters.focus == "close":
            return {pair.split("|")[0] for pair in sets.close_state_races}
        if filters.focus == "watchlist":
            pairs = {pair.split("|")[0] for pair in sets.watch_state_races}
            return set(sets.watch_states) | pairs
        return None

    def states_rising(
        self, filters: Filters, limit: int = 8
    ) -> list[dict[str, Any]]:
        """States gaining attention, largest change first.

        The change is the attention index of the last 15 minutes with the 2
        hours before them as its baseline. Focus chooses which states are
        listed.
        """
        where, params = self._where(filters.with_(focus="all"), windowed=False)
        now = filters.now
        params = {
            **params,
            "short": now - RISING_SHORT,
            "long": now - RISING_SPAN,
        }
        rows = self.model.query(
            "states_rising",
            f"""
            SELECT s AS usps,
                count(*) FILTER (WHERE f.first_seen_at >= $short) AS observed,
                count(*) FILTER (WHERE f.first_seen_at < $short) AS trailing
            FROM (SELECT *, unnest(states) AS s FROM facts) f
            WHERE {where} AND f.first_seen_at >= $long
            GROUP BY 1
            """,
            params,
        )
        total = self.model.query(
            "states_rising_total",
            f"""
            SELECT count(*) FILTER (WHERE f.first_seen_at >= $short)
                       AS observed,
                   count(*) FILTER (WHERE f.first_seen_at < $short)
                       AS trailing
            FROM facts f
            WHERE {where} AND f.first_seen_at >= $long
            """,
            params,
        )[0]
        shown = self._focus_states(filters, self.focus_sets())
        changes = [
            {
                **row,
                "change": analytics.attention_index(
                    row["observed"],
                    total["observed"],
                    row["trailing"],
                    total["trailing"],
                ),
            }
            for row in rows
            if shown is None or row["usps"] in shown
        ]
        ranked = sorted(
            (row for row in changes if row["change"] > 1),
            key=lambda row: (-row["change"], row["usps"]),
        )[:limit]
        peak = max((row["change"] - 1 for row in ranked), default=0) or 1
        return [
            {
                **row,
                "width": (row["change"] - 1) / peak * 100,
                "query": evidence_query(
                    kind="state",
                    key=row["usps"],
                    recent=analytics.SHORT_WINDOW_MINUTES,
                    focus="all",
                ),
            }
            for row in ranked
        ]

    def race_calls_query(
        self, filters: Filters, clause: str = "TRUE"
    ) -> tuple[str, dict[str, Any]]:
        """The Race Calls SQL and its parameters, with no limit bound."""
        sets = self.focus_sets()
        where, params = filters.where(
            sets, windowed=False, unique=False, relevant=False
        )
        when = "f.seen_raw_at <= $as_of"
        if self.calls_open is not None:
            opened = (
                " AND least(f.seen_raw_at, "
                "coalesce(f.published_at, f.seen_raw_at)) >= $calls_open"
            )
            where += opened
            when += opened
            params = {**params, "calls_open": self.calls_open}
        sql = (
            load_sql("race_calls")
            .replace("{where}", f"{where} AND {clause}")
            .replace("{when}", when)
        )
        return sql, params

    def race_calls(
        self,
        filters: Filters,
        limit: int = 60,
        clause: str = "TRUE",
        extra: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Claims by race and winner; `clause` may use the `rc` alias."""
        sql, params = self.race_calls_query(filters, clause)
        rows = self.model.query(
            "race_calls", sql, {**params, **(extra or {}), "limit": limit}
        )
        return [
            {
                **row,
                "evidence": evidence_query(
                    kind="items",
                    key=",".join(row["item_ids"]),
                    span="all",
                    rel="any",
                    copies="all",
                ),
            }
            for row in rows
        ]

    # Band 3

    def stream_rows(self, filters: Filters) -> list[dict[str, Any]]:
        where, params = self._where(filters)
        return self.model.query(
            "stream",
            load_sql("stream").replace("{where}", where),
            params,
        )

    def poll_rules(
        self, filters: Filters, state: str | None = None
    ) -> list[dict[str, Any]]:
        """Moments marked on time charts, in time order.

        A state's page adds the state's code to the label of its own closing.
        """
        start, end = filters.window_start, filters.now
        rules = [
            (
                close.at,
                f"{close.clock} {state}"
                if state in close.states
                else close.clock,
                f"Polls close: {' '.join(close.states)}",
            )
            for close in self.poll_closes
            if start <= close.at <= end
        ]
        for gap in self.collection_gaps():
            if start <= gap["gap_start"] <= end:
                rules.append(
                    (gap["gap_start"], "Offline", "Collection stopped")
                )
            if start <= gap["gap_end"] <= end:
                rules.append(
                    (gap["gap_end"], "Back online", "Collection resumed")
                )
        return [
            {"time": at.isoformat(), "label": label, "detail": detail}
            for at, label, detail in sorted(rules, key=lambda rule: rule[0])
        ]

    def collection_gaps(self) -> list[dict[str, Any]]:
        """Periods with no collection, newest first."""
        return self.model.query(
            "collection_gaps",
            "SELECT gap_start, gap_end, reason FROM collection_gaps "
            "ORDER BY gap_start DESC",
        )

    def topic_heat(self, filters: Filters) -> list[dict[str, Any]]:
        where, params = self._where(filters, windowed=False)
        rows = self.model.query(
            "topic_heat",
            load_sql("topic_heat").replace("{where}", where),
            {**params, "since": whole_bin(filters.now - TOPIC_HEAT_SPAN)},
        )
        return [
            {
                **row,
                "evidence": evidence_query(
                    kind="topic",
                    key=row["topic"],
                    bin_start=row["bin_start"],
                    bin_minutes=HEAT_BIN_MINUTES,
                    span="all",
                ),
            }
            for row in rows
        ]

    # Band 4

    def bump(self, filters: Filters) -> list[dict[str, Any]]:
        where, params = self._where(filters)
        span = filters.now - filters.window_start
        minutes = BUMP_BIN_LONG if span > BUMP_LONG_SPAN else BUMP_BIN_SHORT
        sql = load_sql("bump").replace("{where}", where)
        rows = self.model.query(
            "bump", sql.replace("{bin_minutes}", str(minutes)), params
        )
        return [
            {
                **row,
                "evidence": evidence_query(
                    kind="entity",
                    key=row["entity_id"],
                    bin_start=row["bin_start"],
                    bin_minutes=minutes,
                ),
            }
            for row in rows
        ]

    def tone_by_lean(self, filters: Filters) -> list[dict[str, Any]]:
        where, params = self._where(filters, unique=False)
        rows = self.model.query(
            "tone_by_lean",
            load_sql("tone_by_lean").replace("{where}", where),
            params,
        )
        result = []
        for row in rows:
            aggregate = analytics.tone_aggregate(
                row["tone_sum"] or 0, row["tone_sq"] or 0, row["tone_n"] or 0
            )
            if aggregate.hidden:
                continue
            result.append(
                {
                    "entity": row["entity"],
                    "entity_id": row["entity_id"],
                    "bucket": row["bucket"],
                    "mean": aggregate.mean,
                    "ci_low": aggregate.ci_low,
                    "ci_high": aggregate.ci_high,
                    "n": aggregate.n,
                    "low_n": aggregate.low_n,
                    "evidence": evidence_query(
                        kind="tone",
                        key=row["entity_id"],
                        copies="all",
                        opinion="exclude",
                        **(
                            {}
                            if row["bucket"] == "All"
                            else {"bucket": row["bucket"]}
                        ),
                    ),
                }
            )
        return result

    # Band 5

    def coverage_matrix(self, filters: Filters) -> list[dict[str, Any]]:
        where, params = self._where(filters, unique=False)
        rows = self.model.query(
            "coverage_matrix",
            load_sql("coverage_matrix").replace("{where}", where),
            params,
        )
        totals: dict[str, int] = {}
        stories: dict[str, dict[str, Any]] = {}
        for row in rows:
            totals[row["bucket"]] = row["bucket_total"]
            if row["storyline_id"] is None:
                continue
            entry = stories.setdefault(
                row["storyline_id"], {"label": row["label"], "counts": {}}
            )
            entry["counts"][row["bucket"]] = row["n"]
        buckets = (*LEAN_BUCKETS, "Non-US")
        ranked = []
        for storyline_id, entry in stories.items():
            shares = {
                bucket: analytics.coverage_share(
                    entry["counts"].get(bucket, 0), totals.get(bucket, 0)
                )
                for bucket in buckets
            }
            ranked.append(
                {
                    "storyline_id": storyline_id,
                    "label": entry["label"],
                    "total": sum(entry["counts"].values()),
                    "shares": shares,
                    "divergence": analytics.divergence(list(shares.values())),
                }
            )
        top = sorted(ranked, key=lambda row: -row["total"])[:12]
        cells = []
        for row in sorted(top, key=lambda row: -row["divergence"]):
            for bucket in buckets:
                cells.append(
                    {
                        "storyline_id": row["storyline_id"],
                        "label": row["label"],
                        "bucket": bucket,
                        "share": None
                        if row["shares"][bucket] is None
                        else round(row["shares"][bucket], 4),
                        "divergence": round(row["divergence"], 4),
                    }
                )
        return cells

    def global_press(self, filters: Filters) -> list[dict[str, Any]]:
        where, params = self._where(filters)
        rows = self.model.query(
            "global_press",
            load_sql("global_press").replace("{where}", where),
            params,
        )
        return [
            {
                **row,
                "evidence": evidence_query(
                    kind="language",
                    key=row["language"],
                    bin_start=row["bin_start"],
                    bin_minutes=PRESS_BIN_MINUTES,
                ),
            }
            for row in rows
        ]

    def latest_translated(
        self, filters: Filters, limit: int = 6
    ) -> list[dict[str, Any]]:
        where, params = self._where(filters, windowed=False)
        rows = self.model.query(
            "translated",
            load_sql("wire").replace(
                "{where}", f"{where} AND f.language <> 'en'"
            ),
            {**params, "limit": limit},
        )
        return [decorate_item(row) for row in rows]

    # Articles

    def article(self, item_id: str) -> dict[str, Any] | None:
        rows = self.model.rows(load_sql("article"), {"item_id": item_id})
        return decorate_item(rows[0]) if rows else None

    # Evidence

    def evidence(
        self,
        filters: Filters,
        clause: str,
        extra: dict[str, Any],
        limit: int = 50,
        offset: int = 0,
        windowed: bool = True,
        relevant: bool = True,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """A page of items, with the counts of all items and their outlets."""
        where, params = self._where(
            filters, windowed=windowed, relevant=relevant
        )
        all_params = {**params, **extra}
        count = self.model.query(
            "evidence_count",
            f"""
            SELECT count(*) AS n, count(DISTINCT f.source_id) AS outlets
            FROM facts f WHERE {where} AND {clause}
            """,
            all_params,
        )[0]
        rows = self.model.query(
            "evidence",
            load_sql("wire").replace("{where}", f"{where} AND {clause}")
            + " OFFSET $offset",
            {**all_params, "limit": limit, "offset": offset},
        )
        return (
            [decorate_item(row) for row in rows],
            count["n"],
            count["outlets"],
        )

    def evidence_mentions(
        self,
        filters: Filters,
        clause: str,
        extra: dict[str, Any],
        entity_id: str,
        toned: bool,
        windowed: bool = True,
        relevant: bool = True,
    ) -> int:
        """Mentions of one entity in the items an evidence list shows."""
        where, params = self._where(
            filters, windowed=windowed, relevant=relevant
        )
        with_tone = "AND m.tone IS NOT NULL" if toned else ""
        return self.model.query(
            "evidence_mentions",
            f"""
            SELECT count(*) AS n
            FROM mentions m JOIN facts f USING (item_id)
            WHERE {where} AND {clause}
              AND m.entity_id = $mention_entity {with_tone}
            """,
            {**params, **extra, "mention_entity": entity_id},
        )[0]["n"]
