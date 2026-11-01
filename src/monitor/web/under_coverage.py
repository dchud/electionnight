"""The coverage self-assessment: where the source list is heavy, light, or
missing. Written to state the gaps, not to reassure.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from monitor.web.filters import Filters
from monitor.web.panels import load_sql
from monitor.web.reference import feed_state
from monitor.web.under import Under, parse_time

BATTLEGROUND_TARGET = 4
OTHER_TARGET = 2
HEAVY_RATIO = 1.5
LIGHT_RATIO = 0.67
MIN_STORIES_FOR_RATIO = 5
LEAN_ORDER = ("L", "LL", "C", "LR", "R", "Non-US", "Unrated")
LEAN_NAMES = {
    "L": "Left",
    "LL": "Lean Left",
    "C": "Center",
    "LR": "Lean Right",
    "R": "Right",
    "Non-US": "Non-US",
    "Unrated": "Unrated",
}
# The build specification asks for about 4 to 6 outlets per language in the
# sample. The feed validation report checks a floor of 3.
LANGUAGE_TARGETS = {
    "ar": ("Arabic", "4 to 6"),
    "zh": ("Chinese", "4 to 6"),
    "fr": ("French", "4 to 6"),
    "ru": ("Russian", "4 to 6"),
    "es": ("Spanish", "4 to 6"),
    "en": ("English", "no sample target"),
}
REASONS = {
    "blocked": "Blocked by the site",
    "no_feed": "No feed found",
    "stale": "Stale or empty feed",
    "other": "Other or not recorded",
}
BLOCK_CODES = {"401", "402", "403", "406", "429"}
MISSING_CODES = {"404", "410"}
FILED_RE = re.compile(r"\b[A-Z]{2}\s*[+/]\s*[A-Z]{2}\b|multi-state", re.I)
GENERATED_RE = re.compile(r"^Generated (.+)\.$", re.M)


def failure_group(reason: str | None) -> str:
    """Sort a failure from the feed validation report into a group."""
    text = (reason or "").strip()
    match = re.match(r"HTTP (\d+)", text)
    if match:
        code = match.group(1)
        if code in BLOCK_CODES:
            return "blocked"
        if code in MISSING_CODES:
            return "no_feed"
        return "other"
    if text.startswith(("only ", "newest entry")):
        return "stale"
    return "other"


def read_report(path: Path) -> dict[str, Any]:
    """The failure table in `reports/feed_validation.md`, by outlet name."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"found": False, "reasons": {}, "generated": ""}
    generated = GENERATED_RE.search(text)
    reasons: dict[str, str] = {}
    in_table = False
    for line in text.splitlines():
        if line.startswith("## Outlets with no healthy feed"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if in_table and line.startswith("|"):
            cells = [
                cell.strip() for cell in line.strip().strip("|").split("|")
            ]
            if len(cells) == 5 and cells[0] not in {"Outlet"}:
                if not set(cells[0]) <= {"-"}:
                    reasons[cells[0]] = cells[4]
    return {
        "found": True,
        "reasons": reasons,
        "generated": generated.group(1) if generated else "",
    }


def read_replacement_failures(path: Path) -> dict[str, str]:
    """Named lists of replacements that were tried and also failed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    found: dict[str, str] = {}
    for label, key in (
        ("New candidates:", "new_candidates"),
        ("Seed outlets retried with other feed URLs:", "retried"),
    ):
        for line in text.splitlines():
            if line.startswith(label):
                found[key] = line[len(label) :].strip().rstrip(".")
    return found


def name_list(names: list[str], limit: int = 12) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])}, and {len(names) - limit} more"


def state_reading(ratio: float | None, stories: int, expected: float) -> str:
    """Heavy or light against seats, once the counts can say so.

    A state expecting 12 stories and getting 1 is light, even though 1 is a
    small count, so a light reading looks at what was expected.
    """
    if ratio is None:
        return ""
    if ratio >= HEAVY_RATIO and stories >= MIN_STORIES_FOR_RATIO:
        return "heavy"
    if ratio <= LIGHT_RATIO and expected >= MIN_STORIES_FOR_RATIO:
        return "light"
    if stories < MIN_STORIES_FOR_RATIO and expected < MIN_STORIES_FOR_RATIO:
        return "too few"
    return ""


def state_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_seats = sum(row["house_seats"] for row in rows) or 1
    total_stories = sum(row["stories"] for row in rows) or 1
    total_outlets = sum(row["enabled_outlets"] for row in rows) or 1
    result = []
    for row in rows:
        target = BATTLEGROUND_TARGET if row["battleground"] else OTHER_TARGET
        seat_share = row["house_seats"] / total_seats
        story_share = row["stories"] / total_stories
        outlet_share = row["enabled_outlets"] / total_outlets
        ratio = story_share / seat_share if seat_share else None
        expected = seat_share * total_stories
        reading = state_reading(ratio, row["stories"], expected)
        result.append(
            {
                **row,
                "target": target,
                "gap": max(0, target - row["enabled_outlets"]),
                "seat_share": seat_share,
                "story_share": story_share,
                "outlet_share": outlet_share,
                "ratio": ratio,
                "expected": expected,
                "reading": reading,
                "seats_per_outlet": row["house_seats"] / row["enabled_outlets"]
                if row["enabled_outlets"]
                else None,
            }
        )
    return result


def no_feed_groups(
    outlets: list[dict[str, Any]], report: dict[str, Any]
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for outlet in outlets:
        if outlet["enabled"]:
            continue
        reason = report["reasons"].get(outlet["name"])
        groups[failure_group(reason)].append(
            {**outlet, "reason": reason or "not in the report"}
        )
    ordered = [
        {
            "key": key,
            "label": label,
            "n": len(groups[key]),
            "outlets": sorted(
                groups[key],
                key=lambda o: (o["poll_tier"], o["scope"], o["name"]),
            ),
        }
        for key, label in REASONS.items()
    ]
    return {
        "groups": [group for group in ordered if group["n"]],
        "total": sum(len(values) for values in groups.values()),
    }


def lean_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_bucket = {row["bucket"]: row for row in rows}
    outlets = sum(row["enabled_outlets"] for row in rows) or 1
    stories = sum(row["stories"] for row in rows) or 1
    peak = max((row["enabled_outlets"] for row in rows), default=1) or 1
    story_peak = max((row["stories"] for row in rows), default=1) or 1
    result = []
    for bucket in LEAN_ORDER:
        row = by_bucket.get(bucket)
        if row is None:
            continue
        result.append(
            {
                **row,
                "label": LEAN_NAMES[bucket],
                "outlet_share": row["enabled_outlets"] / outlets,
                "story_share": row["stories"] / stories,
                "outlet_width": row["enabled_outlets"] / peak * 100,
                "story_width": row["stories"] / story_peak * 100,
                "top_share": row["top_outlet_stories"] / row["stories"]
                if row["stories"]
                else None,
            }
        )
    return result


def language_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        name, target = LANGUAGE_TARGETS.get(
            row["language"], (row["language"], "no sample target")
        )
        result.append({**row, "name": name, "target": target})
    return result


def conclusions(
    states: list[dict[str, Any]],
    lean: list[dict[str, Any]],
    scope: list[dict[str, Any]],
    outlets: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """What the data cannot support, with the numbers that say why."""
    by_scope = {row["scope"]: row for row in scope}
    local = by_scope.get("local", {})
    state_level = by_scope.get("state", {})
    local_outlets = local.get("outlets", 0) + state_level.get("outlets", 0)
    local_rated = local.get("rated", 0) + state_level.get("rated", 0)
    by_bucket = {row["bucket"]: row for row in lean}
    majors = [
        o["name"]
        for o in outlets
        if not o["enabled"] and o["scope"] in {"wire", "national"}
    ]
    spanish = [
        o["name"]
        for o in outlets
        if not o["enabled"] and o["language"] == "es" and o["country"] == "US"
    ]
    seated = [s for s in states if s["house_seats"] and s["enabled_outlets"]]
    most = max(seated, key=lambda s: s["seats_per_outlet"], default=None)
    least = min(seated, key=lambda s: s["seats_per_outlet"], default=None)
    right = by_bucket.get("R", {})
    left = by_bucket.get("L", {})
    claims = [
        {
            "claim": "Tone by lean in local coverage.",
            "why": f"{local_rated} of {local_outlets} enabled state and local "
            "outlets have a lean rating. Tone by Outlet Lean rests on "
            "national and analysis outlets only.",
        },
        {
            "claim": "That one side of the press covered a story more.",
            "why": f"The Right bucket has {right.get('enabled_outlets', 0)} "
            f"outlets and the Left bucket "
            f"{left.get('enabled_outlets', 0)}. "
            + (
                f"{right.get('top_outlet')} alone is "
                f"{right.get('top_share', 0) * 100:.0f}% of Right stories "
                "in this window, so that bucket can move with one newsroom."
                if right.get("top_share") is not None
                else "Neither has stories in this window."
            ),
        },
        {
            "claim": "What the major wires and national outlets reported.",
            "why": f"No working feed: {name_list(majors)}. Their stories "
            "appear only when another outlet republishes them, and then under "
            "that outlet's name.",
        },
        {
            "claim": "US Spanish-language coverage.",
            "why": f"No working feed: {name_list(spanish) or 'none'}. The "
            "Spanish sample is mostly outlets outside the US.",
        },
        {
            "claim": "Comparing states by raw story counts.",
            "why": (
                f"Outlets per seat ranges from {most['usps']}, with "
                f"{most['house_seats']} seats and "
                f"{most['enabled_outlets']} outlets, to {least['usps']}, with "
                f"{least['house_seats']} seats and "
                f"{least['enabled_outlets']} outlets."
                if most and least
                else "No state has both seats and outlets."
            )
            + " The attention index compares a state with its own baseline "
            "for this reason.",
        },
        {
            "claim": "Which outlet reported first.",
            "why": "Feeds are polled every 60, 180, or 600 seconds by tier, "
            "and `source_id` is the first feed to serve an item. A fast "
            "poller wins ties it did not earn.",
        },
        {
            "claim": "Volume trends across the first fetch.",
            "why": "Most early items were feed backlog from the first fetch. "
            "Feeds serve only their newest entries, so earlier days are a "
            "partial sample, not a baseline.",
        },
        {
            "claim": "What the world's press thinks of the election.",
            "why": "The non-English sample is a handful of outlets per "
            "language, chosen by hand. It shows what those outlets ran.",
        },
    ]
    return claims


def coverage(under: Under, filters: Filters) -> dict[str, Any]:
    settings = under.settings
    window = {"start": filters.window_start, "end": filters.now}
    sets = under.panels.focus_sets()
    states_ran = under.local(
        load_sql("under_coverage_states"),
        {**window, "battleground": list(sets.battleground_states)},
    )
    totals = under.local(load_sql("under_coverage_totals"), window)
    outlets_ran = under.local(load_sql("under_coverage_outlets"))
    lean_ran = under.local(load_sql("under_coverage_lean"), window)
    scope_ran = under.local(load_sql("under_coverage_scope"))
    language_ran = under.local(load_sql("under_coverage_language"), window)
    elsewhere = under.local(load_sql("under_coverage_elsewhere"))

    reports = settings.paths.reports
    report = read_report(reports / "feed_validation.md")
    tried = read_replacement_failures(reports / "feed_replacements.md")

    states = state_rows(states_ran.rows)
    outlets = outlets_ran.rows
    lean = lean_table(lean_ran.rows)
    scope = scope_ran.rows
    groups = no_feed_groups(outlets, report)
    enabled = [o for o in outlets if o["enabled"]]
    unrated = next((r for r in lean if r["bucket"] == "Unrated"), {})
    rated = [r for r in lean if r["bucket"] not in {"Unrated", "Non-US"}]
    non_us = next((r for r in lean if r["bucket"] == "Non-US"), {})
    us_outlets = len(enabled) - non_us.get("enabled_outlets", 0)
    silent = [
        o for o in enabled if o["items"] == 0 and o["scope"] != "international"
    ] + [
        o for o in enabled if o["items"] == 0 and o["scope"] == "international"
    ]
    never_relevant = [
        o for o in enabled if o["items"] > 0 and o["relevant"] == 0
    ]

    sparse = sorted(
        (s for s in states if s["enabled_outlets"] <= 1),
        key=lambda s: (s["enabled_outlets"], -s["house_seats"]),
    )
    under_target = sorted(
        (s for s in states if s["gap"] > 0),
        key=lambda s: (not s["battleground"], -s["house_seats"]),
    )
    ranked = sorted(
        (s for s in states if s["ratio"] is not None),
        key=lambda s: -(s["ratio"] or 0),
    )
    battleground = [s for s in states if s["battleground"]]
    filed = [
        o
        for o in outlets
        if o.get("notes") and FILED_RE.search(o["notes"] or "")
    ]
    major_missing = sorted(
        (
            o
            for o in outlets
            if not o["enabled"]
            and (o["poll_tier"] == 1 or o["scope"] in {"wire", "national"})
        ),
        key=lambda o: (o["scope"] not in {"wire", "national"}, o["name"]),
    )

    feeds_now = []
    for feed in under.model.read_status().get("feed_details") or []:
        state = feed_state(feed)
        if state in {"failing", "down"}:
            feeds_now.append(
                {
                    **feed,
                    "state": state,
                    "last_success": parse_time(feed.get("last_success_at")),
                }
            )
    return {
        "window": filters.window,
        "window_start": filters.window_start,
        "window_end": filters.now,
        "states": sorted(
            states,
            key=lambda s: -(s["ratio"] if s["ratio"] is not None else -1),
        ),
        "states_alpha": states,
        "ranked": ranked,
        "heavy": [s for s in ranked if s["reading"] == "heavy"][:8],
        "light": [s for s in reversed(ranked) if s["reading"] == "light"][:8],
        "sparse": sparse,
        "under_target": under_target,
        "battleground_seat_share": sum(s["seat_share"] for s in battleground),
        "battleground_outlet_share": sum(
            s["outlet_share"] for s in battleground
        ),
        "battleground_story_share": sum(
            s["story_share"] for s in battleground
        ),
        "battleground_n": len(battleground),
        "total_seats": sum(s["house_seats"] for s in states),
        "totals": totals.first,
        "listed": len(outlets),
        "enabled": len(enabled),
        "groups": groups,
        "report": report,
        "tried": tried,
        "major_missing": major_missing,
        "feeds_now": feeds_now,
        "silent": silent,
        "never_relevant": never_relevant[:20],
        "never_relevant_n": len(never_relevant),
        "filed": filed,
        "elsewhere": elsewhere,
        "no_home_state": sorted(
            o["name"]
            for o in enabled
            if o["scope"] in {"state", "local"} and not o["home_state"]
        ),
        "lean": lean,
        "scope": scope,
        "unrated": unrated,
        "rated_total": sum(r["enabled_outlets"] for r in rated),
        "rated_stories": sum(r["stories"] for r in rated),
        "non_us": non_us,
        "us_outlets": us_outlets,
        "languages": language_rows(language_ran.rows),
        "conclusions": conclusions(states, lean, scope, outlets),
        "queries": {
            "states": [states_ran.query, totals.query],
            "outlets": [outlets_ran.query],
            "lean": [lean_ran.query, scope_ran.query],
            "language": [language_ran.query],
            "elsewhere": [elsewhere.query],
        },
    }
