"""Gold rollups: 5-minute bins of `first_seen_at`.

Each cycle recomputes recent bins and compares them with what is stored. Only
rows that changed are appended, including zero rows for keys that vanished.
Readers take the latest row per key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

from monitor.lake import BatchWriter, connect
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

RECENT_WINDOW = timedelta(hours=2)

FACTS_SQL = """
WITH items AS (
    SELECT item_id, source_id, first_seen_at,
           time_bucket(INTERVAL '5 minutes', first_seen_at) AS bin_start
    FROM lake.raw.items
    WHERE first_seen_at >= $since
),
class AS (
    SELECT item_id, election_relevant, primary_level, states, topics,
           is_opinion
    FROM lake.nlp.classify_a
    WHERE item_id IN (SELECT item_id FROM items)
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
),
dups AS (
    SELECT item_id, is_canonical FROM lake.nlp.dup_groups
    WHERE item_id IN (SELECT item_id FROM items)
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
),
stories AS (
    SELECT a.item_id, coalesce(m.merged_into, a.storyline_id) AS storyline_id
    FROM (
        SELECT item_id, storyline_id FROM lake.nlp.storyline_assign
        WHERE item_id IN (SELECT item_id FROM items)
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY assigned_at DESC) = 1
    ) a
    LEFT JOIN (
        SELECT storyline_id, merged_into FROM lake.nlp.storylines
        QUALIFY row_number() OVER (
            PARTITION BY storyline_id ORDER BY version DESC) = 1
    ) m USING (storyline_id)
),
tone AS (
    SELECT item_id, article_tone FROM lake.nlp.extract_b
    WHERE item_id IN (SELECT item_id FROM items)
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
),
lean AS (
    SELECT source_id, lean_label FROM lake.ref.sources
    QUALIFY row_number() OVER (
        PARTITION BY source_id ORDER BY created_at DESC) = 1
)
SELECT i.item_id, i.source_id, i.bin_start,
       coalesce(c.election_relevant, FALSE) AS relevant,
       coalesce(c.primary_level, 'none') AS level,
       coalesce(c.states, []) AS states,
       coalesce(c.topics, []) AS topics,
       coalesce(c.is_opinion, FALSE) AS is_opinion,
       coalesce(d.is_canonical, TRUE) AS canonical,
       s.storyline_id,
       t.article_tone AS tone,
       coalesce(l.lean_label, 'Unrated') AS lean_label
FROM items i
LEFT JOIN class c USING (item_id)
LEFT JOIN dups d USING (item_id)
LEFT JOIN stories s USING (item_id)
LEFT JOIN tone t USING (item_id)
LEFT JOIN lean l USING (source_id)
"""

MENTIONS_SQL = """
SELECT m.item_id, m.mention_id, m.entity_id, m.tone
FROM lake.nlp.mentions m
WHERE m.item_id IN (SELECT item_id FROM facts)
QUALIFY row_number() OVER (
    PARTITION BY m.mention_id ORDER BY m.created_at DESC) = 1
"""


@dataclass(frozen=True)
class Rollup:
    table: str
    keys: tuple[str, ...]
    values: tuple[str, ...]
    sql: str


ROLLUPS = (
    Rollup(
        "gold.bin_counts",
        ("bin_start", "level"),
        ("n_items", "n_relevant", "n_unique"),
        """
        SELECT bin_start, level, count(*) AS n_items,
               count(*) FILTER (WHERE relevant) AS n_relevant,
               count(*) FILTER (WHERE relevant AND canonical) AS n_unique
        FROM facts GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.storyline_bins",
        ("bin_start", "storyline_id"),
        ("n_items", "n_unique", "n_sources", "mean_tone"),
        """
        SELECT bin_start, storyline_id, count(*) AS n_items,
               count(*) FILTER (WHERE canonical) AS n_unique,
               count(DISTINCT source_id) AS n_sources,
               avg(tone) AS mean_tone
        FROM facts WHERE storyline_id IS NOT NULL GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.state_bins",
        ("bin_start", "state_usps"),
        ("n_items", "n_unique", "tone_sum", "tone_n"),
        """
        SELECT bin_start, state_usps, count(*) AS n_items,
               count(*) FILTER (WHERE canonical) AS n_unique,
               coalesce(sum(tone), 0) AS tone_sum, count(tone) AS tone_n
        FROM (SELECT *, unnest(states) AS state_usps FROM facts
              WHERE relevant) GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.source_bins",
        ("bin_start", "source_id"),
        ("n_items", "n_relevant"),
        """
        SELECT bin_start, source_id, count(*) AS n_items,
               count(*) FILTER (WHERE relevant) AS n_relevant
        FROM facts GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.lean_storyline_bins",
        ("bin_start", "storyline_id", "lean_label"),
        ("n_items",),
        """
        SELECT bin_start, storyline_id, lean_label, count(*) AS n_items
        FROM facts WHERE storyline_id IS NOT NULL GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.topic_bins",
        ("bin_start", "topic"),
        ("n_items",),
        """
        SELECT bin_start, topic, count(*) AS n_items
        FROM (SELECT *, unnest(topics) AS topic FROM facts WHERE relevant)
        GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.entity_bins",
        ("bin_start", "entity_id"),
        ("n_mentions", "n_items", "tone_sum", "tone_sq_sum", "tone_n"),
        """
        SELECT f.bin_start, m.entity_id, count(*) AS n_mentions,
               count(DISTINCT m.item_id) AS n_items,
               coalesce(sum(m.tone), 0) AS tone_sum,
               coalesce(sum(m.tone * m.tone), 0) AS tone_sq_sum,
               count(m.tone) AS tone_n
        FROM mentions m JOIN facts f USING (item_id)
        WHERE m.entity_id IS NOT NULL GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.entity_lean_bins",
        ("bin_start", "entity_id", "lean_label"),
        ("n_mentions", "n_items", "tone_sum", "tone_sq_sum", "tone_n"),
        """
        SELECT f.bin_start, m.entity_id, f.lean_label,
               count(*) AS n_mentions,
               count(DISTINCT m.item_id) AS n_items,
               coalesce(sum(m.tone), 0) AS tone_sum,
               coalesce(sum(m.tone * m.tone), 0) AS tone_sq_sum,
               count(m.tone) AS tone_n
        FROM mentions m JOIN facts f USING (item_id)
        WHERE m.entity_id IS NOT NULL AND NOT f.is_opinion GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.county_bins",
        ("bin_start", "county_fips"),
        ("n_items",),
        """
        SELECT f.bin_start, p.county_fips,
               count(DISTINCT p.item_id) AS n_items
        FROM (
            SELECT item_id, county_fips FROM lake.nlp.places
            WHERE county_fips IS NOT NULL
              AND item_id IN (SELECT item_id FROM facts)
        ) p JOIN facts f USING (item_id)
        GROUP BY ALL
        """,
    ),
    Rollup(
        "gold.comention_edges",
        ("bin_start", "entity_a", "entity_b"),
        ("n",),
        """
        SELECT f.bin_start, a.entity_id AS entity_a, b.entity_id AS entity_b,
               count(DISTINCT a.item_id) AS n
        FROM mentions a
        JOIN mentions b ON a.item_id = b.item_id
                       AND a.entity_id < b.entity_id
        JOIN facts f ON f.item_id = a.item_id
        GROUP BY ALL
        """,
    ),
)


def _normalize_time(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return value


def diff_rows(
    rollup: Rollup,
    computed: list[tuple[Any, ...]],
    stored: dict[tuple[Any, ...], tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], dict[tuple[Any, ...], tuple[Any, ...]]]:
    """Rows to append, and the new stored state.

    A key in `stored` that is missing from `computed` gets a zero row, so a
    reader taking the latest row per key sees it vanish.
    """
    width = len(rollup.keys)
    current: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for row in computed:
        key = tuple(_normalize_time(v) for v in row[:width])
        current[key] = tuple(row[width:])
    rows: list[dict[str, Any]] = []
    columns = rollup.keys + rollup.values
    for key, values in current.items():
        if stored.get(key) != values:
            rows.append(dict(zip(columns, key + values, strict=True)))
    zeros = tuple(0 for _ in rollup.values)
    merged = dict(current)
    for key, values in stored.items():
        if key in current:
            continue
        if values != zeros:
            rows.append(dict(zip(columns, key + zeros, strict=True)))
        merged[key] = zeros
    return rows, merged


class RollupEngine:
    def __init__(self, settings: Settings, writer: BatchWriter) -> None:
        self.settings = settings
        self.writer = writer
        self.stored: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]] = {}

    def _load_stored(
        self, conn: duckdb.DuckDBPyConnection, rollup: Rollup, since: datetime
    ) -> dict[tuple[Any, ...], tuple[Any, ...]]:
        keys = ", ".join(rollup.keys)
        values = ", ".join(rollup.values)
        rows = conn.execute(
            f"""
            SELECT {keys}, {values} FROM lake.{rollup.table}
            WHERE bin_start >= $since
            QUALIFY row_number() OVER (
                PARTITION BY {keys} ORDER BY created_at DESC) = 1
            """,
            {"since": since},
        ).fetchall()
        width = len(rollup.keys)
        return {
            tuple(_normalize_time(v) for v in row[:width]): tuple(row[width:])
            for row in rows
        }

    def run(
        self,
        conn: duckdb.DuckDBPyConnection,
        now: datetime | None = None,
        rebuild: bool = False,
    ) -> dict[str, int]:
        """Recompute a window of bins. Returns rows appended per table."""
        now = now or datetime.now(UTC)
        since = (
            datetime(2000, 1, 1, tzinfo=UTC)
            if rebuild
            else (now - RECENT_WINDOW)
        )
        cursor = conn.cursor()
        counts: dict[str, int] = {}
        try:
            cursor.execute(
                f"CREATE OR REPLACE TEMP TABLE facts AS {FACTS_SQL}",
                {"since": since},
            )
            cursor.execute(
                f"CREATE OR REPLACE TEMP TABLE mentions AS {MENTIONS_SQL}"
            )
            for rollup in ROLLUPS:
                computed = cursor.execute(rollup.sql).fetchall()
                stored = self._stored_for(cursor, rollup, since, rebuild)
                rows, merged = diff_rows(rollup, computed, stored)
                self.stored[rollup.table] = {
                    key: value
                    for key, value in merged.items()
                    if key[0] >= since
                }
                if rows:
                    self.writer.add(rollup.table, rows)
                    counts[rollup.table] = len(rows)
        finally:
            cursor.close()
        return counts

    def _stored_for(
        self,
        conn: duckdb.DuckDBPyConnection,
        rollup: Rollup,
        since: datetime,
        rebuild: bool,
    ) -> dict[tuple[Any, ...], tuple[Any, ...]]:
        if rebuild or rollup.table not in self.stored:
            return self._load_stored(conn, rollup, since)
        return {
            key: value
            for key, value in self.stored[rollup.table].items()
            if key[0] >= since
        }


def rollups_standalone(settings: Settings, rebuild: bool = False) -> str:
    """Run one cycle with its own connection, when no pipeline is running."""
    conn = connect(settings)
    try:
        writer = BatchWriter(conn, settings)
        counts = RollupEngine(settings, writer).run(conn, rebuild=rebuild)
        writer.flush()
    finally:
        conn.close()
    return f"appended {sum(counts.values())} rows: {counts}"


def replay_report(settings: Settings, start: datetime, end: datetime) -> str:
    """Item counts by bin for a time range, for demo preparation."""
    conn = connect(settings, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT time_bucket(INTERVAL '5 minutes', i.first_seen_at) AS bin,
                   count(*) AS items,
                   count(*) FILTER (WHERE c.election_relevant) AS relevant,
                   count(DISTINCT i.source_id) AS sources
            FROM lake.raw.items i
            LEFT JOIN lake.nlp.classify_a c USING (item_id)
            WHERE i.first_seen_at >= $start AND i.first_seen_at < $end
            GROUP BY 1 ORDER BY 1
            """,
            {
                "start": start.astimezone(UTC),
                "end": end.astimezone(UTC),
            },
        ).fetchall()
    finally:
        conn.close()
    lines = [f"{'bin':<27}{'items':>7}{'relevant':>10}{'sources':>9}"]
    lines += [
        f"{bin_start.isoformat():<27}{items:>7}{relevant:>10}{sources:>9}"
        for bin_start, items, relevant, sources in rows
    ]
    empty = _missing_bins(rows, start, end)
    lines.append(f"{len(rows)} bins with data; {empty} empty")
    return "\n".join(lines)


def _missing_bins(
    rows: list[tuple[Any, ...]], start: datetime, end: datetime
) -> int:
    expected = int((end - start).total_seconds() // 300)
    return max(expected - len(rows), 0)
