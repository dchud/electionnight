"""An in-memory copy of the facts the dashboard queries.

Every panel honors the Focus, Level, Language, and Unique filters, which
pre-aggregated bins cannot serve. So the web process keeps one row per item in
its own in-memory database, refreshed from the lake when a new snapshot
appears. It attaches the lake read-only and never writes to it.

The time axis is `first_seen_at`, with one exception. An item published more
than 2 hours before it was first seen is backlog from a feed's first fetch, so
it is placed at its published time. Otherwise a feed's first fetch would show
as one spike of days-old stories.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from monitor.disk import disk_figures
from monitor.lake import connect
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

# Rows are stamped when added and committed up to one batch interval later, so
# each refresh looks back further than that.
WATERMARK_OVERLAP = timedelta(minutes=3)
MERGE_HOPS = 8
READ_RETRIES = 3
QUERY_WINDOW_SECONDS = 60.0
QUERY_TIMES_KEPT = 512
VECTOR_DIM = 384

FACTS_COLUMNS = """
    item_id TEXT, source_id TEXT, language TEXT, title TEXT, headline TEXT,
    summary TEXT, url TEXT, first_seen_at TIMESTAMPTZ,
    seen_raw_at TIMESTAMPTZ, published_at TIMESTAMPTZ, relevant BOOLEAN,
    relevance_conf FLOAT,
    us_politics BOOLEAN, levels TEXT[], primary_level TEXT, states TEXT[],
    race_types TEXT[], topics TEXT[], is_opinion BOOLEAN, is_results BOOLEAN,
    method TEXT, canonical BOOLEAN, dup_group_id TEXT, storyline_id TEXT,
    similarity FLOAT, tone FLOAT, race_keys TEXT[], state_races TEXT[],
    visible_seq BIGINT
"""

CHANGED_IDS = """
    SELECT item_id FROM lake.raw.items WHERE created_at >= $since
    UNION SELECT item_id FROM lake.nlp.lang WHERE created_at >= $since
    UNION SELECT item_id FROM lake.nlp.classify_a WHERE created_at >= $since
    UNION SELECT item_id FROM lake.nlp.dup_groups WHERE created_at >= $since
    UNION SELECT item_id FROM lake.nlp.storyline_assign
        WHERE created_at >= $since
    UNION SELECT item_id FROM lake.nlp.dup_groups
        WHERE dup_group_id IN (
            SELECT d.dup_group_id FROM lake.nlp.dup_groups d
            JOIN lake.nlp.storyline_assign s USING (item_id)
            WHERE s.created_at >= $since)
    UNION SELECT item_id FROM lake.nlp.extract_b WHERE created_at >= $since
"""

FACTS_SELECT = """
    WITH changed AS ({changed}),
    latest_lang AS (
        SELECT item_id, language FROM lake.nlp.lang
        WHERE item_id IN (SELECT item_id FROM changed)
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY created_at DESC) = 1),
    latest_class AS (
        SELECT * FROM lake.nlp.classify_a
        WHERE item_id IN (SELECT item_id FROM changed)
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY created_at DESC) = 1),
    latest_dup AS (
        SELECT item_id, is_canonical, dup_group_id FROM lake.nlp.dup_groups
        WHERE item_id IN (SELECT item_id FROM changed)
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY created_at DESC) = 1),
    latest_story AS (
        SELECT item_id, storyline_id, similarity
        FROM lake.nlp.storyline_assign
        WHERE item_id IN (SELECT item_id FROM changed)
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY assigned_at DESC) = 1),
    group_story AS (
        SELECT d.dup_group_id, s.storyline_id
        FROM lake.nlp.dup_groups d
        JOIN lake.nlp.storyline_assign s USING (item_id)
        WHERE d.is_canonical
          AND d.dup_group_id IN (SELECT dup_group_id FROM latest_dup)
        QUALIFY row_number() OVER (
            PARTITION BY d.dup_group_id ORDER BY s.assigned_at DESC) = 1),
    latest_extract AS (
        SELECT item_id, article_tone, raw FROM lake.nlp.extract_b
        WHERE item_id IN (SELECT item_id FROM changed)
        QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY created_at DESC) = 1)
    SELECT i.item_id, i.source_id,
           coalesce(l.language, i.language, 'en') AS language,
           i.title,
           coalesce(c.headline_en, i.title) AS headline,
           coalesce(c.summary_en, i.summary) AS summary,
           i.url,
           CASE
               WHEN i.published_at < i.first_seen_at - INTERVAL 2 HOUR
                   THEN i.published_at
               WHEN EXISTS (
                   SELECT 1 FROM lake.raw.collection_gaps g
                   WHERE i.first_seen_at BETWEEN g.gap_end
                         AND g.gap_end + INTERVAL 15 MINUTE
                     AND i.published_at
                         BETWEEN g.gap_start - INTERVAL 20 MINUTE AND g.gap_end
               ) THEN i.published_at
               ELSE i.first_seen_at
           END AS first_seen_at,
           i.first_seen_at AS seen_raw_at,
           i.published_at,
           coalesce(c.election_relevant, FALSE)
               AND coalesce(
                   i.published_at <= i.first_seen_at + INTERVAL 1 HOUR, TRUE
               ) AS relevant,
           coalesce(c.relevance_conf, 0) AS relevance_conf,
           coalesce(c.us_politics, FALSE) AS us_politics,
           coalesce(c.levels, []) AS levels,
           coalesce(c.primary_level, 'none') AS primary_level,
           coalesce(c.states, []) AS states,
           coalesce(c.race_types, []) AS race_types,
           coalesce(c.topics, []) AS topics,
           coalesce(c.is_opinion, FALSE) AS is_opinion,
           coalesce(c.is_results_report, FALSE) AS is_results,
           c.method,
           coalesce(d.is_canonical, TRUE) AS canonical,
           d.dup_group_id,
           coalesce(s.storyline_id, g.storyline_id) AS storyline_id,
           s.similarity,
           x.article_tone AS tone,
           coalesce(
               from_json(x.raw -> '$.race_keys', '["VARCHAR"]'), []
           ) AS race_keys,
           flatten([
               [concat(st, '|', replace(rt, 'senate_special', 'senate'))
                FOR rt IN coalesce(c.race_types, [])]
               FOR st IN coalesce(c.states, [])
           ]) AS state_races
    FROM lake.raw.items i
    LEFT JOIN latest_lang l USING (item_id)
    LEFT JOIN latest_class c USING (item_id)
    LEFT JOIN latest_dup d USING (item_id)
    LEFT JOIN latest_story s USING (item_id)
    LEFT JOIN group_story g ON g.dup_group_id = d.dup_group_id
    LEFT JOIN latest_extract x USING (item_id)
    WHERE i.item_id IN (SELECT item_id FROM changed)
"""

# Reference and slowly changing tables, copied whole on each refresh that
# touches them. Each maps a local name to the latest row per key.
COPIED_TABLES = {
    "sources": "SELECT * FROM lake.ref.sources QUALIFY row_number() OVER "
    "(PARTITION BY source_id ORDER BY created_at DESC) = 1",
    "states": "SELECT * FROM lake.ref.states QUALIFY row_number() OVER "
    "(PARTITION BY usps ORDER BY created_at DESC) = 1",
    "races": "SELECT * FROM lake.ref.races QUALIFY row_number() OVER "
    "(PARTITION BY race_key ORDER BY created_at DESC) = 1",
    "entities": "SELECT * FROM lake.ref.entities QUALIFY row_number() OVER "
    "(PARTITION BY entity_id ORDER BY created_at DESC) = 1",
    "storylines": "SELECT storyline_id, version, label, summary, citations, "
    "keywords, first_seen_at, last_item_at, n_items, n_sources, states, "
    "primary_level, status, merged_into FROM lake.nlp.storylines "
    "QUALIFY row_number() OVER (PARTITION BY storyline_id "
    "ORDER BY version DESC, created_at DESC) = 1",
    "storyline_versions": "SELECT storyline_id, version, status, created_at "
    "FROM lake.nlp.storylines",
    "briefs": "SELECT * FROM lake.nlp.briefs",
    "collection_gaps": "SELECT gap_start, gap_end, reason "
    "FROM lake.raw.collection_gaps",
    "race_calls": "SELECT * FROM lake.nlp.race_calls QUALIFY row_number() "
    "OVER (PARTITION BY call_id ORDER BY created_at DESC) = 1",
}
# Large tables are refreshed by key from rows newer than the watermark.
INCREMENTAL_TABLES = {
    "mentions": (
        "mention_id",
        "SELECT mention_id, item_id, surface, char_start, char_end, ent_type, "
        "role, entity_id, tone, tone_evidence, party, state, office "
        "FROM lake.nlp.mentions WHERE created_at >= $since "
        "QUALIFY row_number() OVER (PARTITION BY mention_id "
        "ORDER BY created_at DESC) = 1",
    ),
    "places": (
        "place_id",
        "SELECT * EXCLUDE (parser, parser_version) FROM lake.nlp.places "
        "WHERE created_at >= $since QUALIFY row_number() OVER "
        "(PARTITION BY place_id ORDER BY created_at DESC) = 1",
    ),
    "llm_calls": (
        "call_id",
        "SELECT call_id, purpose, model, input_tokens, cache_read_tokens, "
        "cache_write_tokens, output_tokens, usd_est, latency_ms, status, "
        "created_at FROM lake.nlp.llm_calls WHERE created_at >= $since "
        "QUALIFY row_number() OVER (PARTITION BY call_id "
        "ORDER BY created_at DESC) = 1",
    ),
    "fetch_log": (
        "fetch_id",
        "SELECT fetch_id, source_id, started_at, "
        "error IS NOT NULL AS failed FROM lake.raw.fetches "
        "WHERE created_at >= $since QUALIFY row_number() OVER "
        "(PARTITION BY fetch_id ORDER BY created_at DESC) = 1",
    ),
}
# The raw view of one item. Vectors and article text are left out: the first
# is unreadable and the second is shown only as an extract.
PARSER_TABLES = {
    "raw.items": "*",
    "raw.article_text": "* EXCLUDE (text)",
    "nlp.lang": "*",
    "nlp.classify_a": "*",
    "nlp.embeddings": "* EXCLUDE (vec)",
    "nlp.dup_groups": "*",
    "nlp.storyline_assign": "*",
    "nlp.extract_b": "*",
    "nlp.mentions": "*",
    "nlp.places": "*",
    "nlp.race_calls": "*",
}


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: int
    committed_at: datetime | None
    changed: tuple[str, ...]


class QueryCache:
    """LRU of query results, keyed by snapshot, query name, and params."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._data: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[Any, ...]) -> Any:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]
            self.misses += 1
            return None

    def put(self, key: tuple[Any, ...], value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)


class ReadModel:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lake = connect(settings, read_only=True)
        self.db = duckdb.connect()
        self.db.execute("SET TimeZone = 'UTC'")
        self.db.execute(f"CREATE TABLE facts ({FACTS_COLUMNS})")
        self.cache = QueryCache(settings.web.query_cache_size)
        self.query_times: deque[tuple[float, float]] = deque(
            maxlen=QUERY_TIMES_KEPT
        )
        self.snapshot = Snapshot(0, None, ())
        self.watermark: datetime | None = None
        self.refreshed_at: datetime | None = None
        self.refresh_failed_since: datetime | None = None
        self.refresh_seq = 0
        self.refresh_ms = 0
        self._refresh_lock = threading.Lock()
        self._create_copies()

    def _lake_rows(self, sql: str, params: Any = None) -> Any:
        """Read from the lake, retrying transient catalog errors."""
        for attempt in range(READ_RETRIES):
            cursor = self.lake.cursor()
            try:
                return cursor.sql(sql, params=params).to_arrow_table()
            except duckdb.Error:
                if attempt == READ_RETRIES - 1:
                    raise
                time.sleep(0.2 * (attempt + 1))
            finally:
                cursor.close()
        raise RuntimeError("unreachable")

    def _create_copies(self) -> None:
        """Create empty local tables so queries work before any data exists."""
        epoch = {"since": datetime(2100, 1, 1, tzinfo=UTC)}
        shapes: list[tuple[str, str, dict[str, datetime] | None]] = [
            (n, f"SELECT * FROM ({q}) LIMIT 0", None)
            for n, q in COPIED_TABLES.items()
        ]
        shapes += [(n, q, epoch) for n, (_, q) in INCREMENTAL_TABLES.items()]
        for name, sql, params in shapes:
            arrow = self._lake_rows(sql, params)
            self.db.register("_copy", arrow)
            self.db.execute(
                f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _copy"
            )
            self.db.unregister("_copy")

    def latest_snapshot(self) -> Snapshot:
        rows = self._lake_rows(
            "SELECT snapshot_id, snapshot_time, changes FROM lake.snapshots() "
            "ORDER BY snapshot_id DESC LIMIT 1"
        ).to_pylist()
        row = rows[0]
        changes = row.get("changes") or {}
        pairs = changes.items() if hasattr(changes, "items") else changes
        changed: list[str] = []
        for _kind, values in pairs:
            changed.extend(str(value) for value in values or [])
        return Snapshot(
            row["snapshot_id"], row["snapshot_time"], tuple(sorted(changed))
        )

    def refresh(self, full: bool = False) -> bool:
        """Bring the local tables up to the lake's latest snapshot."""
        with self._refresh_lock:
            try:
                changed = self._refresh(full)
            except Exception:
                if self.refresh_failed_since is None:
                    self.refresh_failed_since = datetime.now(UTC)
                raise
            self.refresh_failed_since = None
            return changed

    def _refresh(self, full: bool) -> bool:
        latest = self.latest_snapshot()
        if not full and latest.snapshot_id == self.snapshot.snapshot_id:
            return False
        started = time.monotonic()
        now = datetime.now(UTC)
        since = (
            datetime(2000, 1, 1, tzinfo=UTC)
            if full or self.watermark is None
            else self.watermark - WATERMARK_OVERLAP
        )
        self._refresh_facts(since)
        self._refresh_copies()
        self._refresh_incremental(since)
        self._apply_merges()
        self.watermark = now
        self.snapshot = latest
        self.refreshed_at = now
        self.refresh_ms = int((time.monotonic() - started) * 1000)
        return True

    def _refresh_facts(self, since: datetime) -> None:
        """Replace the changed items.

        `visible_seq` is the refresh in which an item last became relevant, so
        a stream can tell what is new to readers however late it was classified
        and whatever its place on the time axis.
        """
        sql = FACTS_SELECT.format(changed=CHANGED_IDS)
        arrow = self._lake_rows(sql, {"since": since})
        if arrow.num_rows == 0:
            return
        seq = self.refresh_seq + 1
        cursor = self.db.cursor()
        try:
            cursor.register("_fresh", arrow)
            cursor.execute("BEGIN")
            cursor.execute(
                "CREATE OR REPLACE TEMP TABLE _kept AS "
                "SELECT item_id, visible_seq FROM facts "
                "WHERE relevant AND item_id IN (SELECT item_id FROM _fresh)"
            )
            cursor.execute(
                "DELETE FROM facts WHERE item_id IN "
                "(SELECT item_id FROM _fresh)"
            )
            cursor.execute(
                "INSERT INTO facts BY NAME "
                "SELECT _fresh.*, "
                "coalesce(_kept.visible_seq, $seq) AS visible_seq "
                "FROM _fresh LEFT JOIN _kept USING (item_id)",
                {"seq": seq},
            )
            cursor.execute("DROP TABLE _kept")
            cursor.execute("COMMIT")
            cursor.unregister("_fresh")
            self.refresh_seq = seq
        finally:
            cursor.close()

    def _refresh_incremental(self, since: datetime) -> None:
        cursor = self.db.cursor()
        try:
            for name, (key, sql) in INCREMENTAL_TABLES.items():
                arrow = self._lake_rows(sql, {"since": since})
                if arrow.num_rows == 0:
                    continue
                cursor.register("_fresh", arrow)
                cursor.execute("BEGIN")
                cursor.execute(
                    f"DELETE FROM {name} WHERE {key} IN "
                    f"(SELECT {key} FROM _fresh)"
                )
                cursor.execute(
                    f"INSERT INTO {name} BY NAME SELECT * FROM _fresh"
                )
                cursor.execute("COMMIT")
                cursor.unregister("_fresh")
        finally:
            cursor.close()

    def _refresh_copies(self) -> None:
        cursor = self.db.cursor()
        try:
            for name, sql in COPIED_TABLES.items():
                arrow = self._lake_rows(sql)
                cursor.register("_copy", arrow)
                cursor.execute(
                    f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _copy"
                )
                cursor.unregister("_copy")
        finally:
            cursor.close()

    def _apply_merges(self) -> None:
        """Point items at the storyline their storyline ended up merged into.

        Each pass moves items one hop along a chain of merges.
        """
        cursor = self.db.cursor()
        try:
            for _ in range(MERGE_HOPS):
                moved = cursor.execute(
                    """
                    UPDATE facts SET storyline_id = s.merged_into
                    FROM storylines s
                    WHERE facts.storyline_id = s.storyline_id
                      AND s.merged_into IS NOT NULL
                      AND s.merged_into <> s.storyline_id
                    """
                ).fetchone()
                if not moved or not moved[0]:
                    return
        finally:
            cursor.close()

    def query(
        self, name: str, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a named query, cached for the current snapshot."""
        frozen = tuple(
            sorted(
                (key, _freeze(value)) for key, value in (params or {}).items()
            )
        )
        key = (self.snapshot.snapshot_id, name, sql, frozen)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        started = time.monotonic()
        rows = self.rows(sql, params)
        finished = time.monotonic()
        self.query_times.append((finished, (finished - started) * 1000))
        self.cache.put(key, rows)
        return rows

    def recent_p95_ms(self, seconds: float = QUERY_WINDOW_SECONDS) -> float:
        """The 95th percentile of uncached query time, recent queries only."""
        since = time.monotonic() - seconds
        times = sorted(ms for at, ms in list(self.query_times) if at >= since)
        if not times:
            return 0.0
        return times[min(len(times) - 1, int(len(times) * 0.95))]

    def rows(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a query against local tables without the cache."""
        cursor = self.db.cursor()
        try:
            result = cursor.execute(sql, params or {})
            columns = [column[0] for column in result.description]
            return [
                dict(zip(columns, row, strict=True))
                for row in result.fetchall()
            ]
        finally:
            cursor.close()

    def column(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """The first column of a query, without building a dict per row."""
        cursor = self.db.cursor()
        try:
            return [
                row[0] for row in cursor.execute(sql, params or {}).fetchall()
            ]
        finally:
            cursor.close()

    def article_text(self, item_id: str) -> dict[str, Any] | None:
        """The newest fetch of one item's text, cached for the snapshot."""
        key = (self.snapshot.snapshot_id, "article_text", item_id)
        cached = self.cache.get(key)
        if cached is not None:
            return cached or None
        rows = self._lake_rows(
            """
            SELECT text, n_chars, paywalled, http_status, extractor, fetched_at
            FROM lake.raw.article_text WHERE item_id = $item_id
            ORDER BY coalesce(text, '') <> '' DESC, created_at DESC LIMIT 1
            """,
            {"item_id": item_id},
        ).to_pylist()
        row = rows[0] if rows else {}
        self.cache.put(key, row)
        return row or None

    def parser_outputs(self, item_id: str) -> dict[str, list[dict[str, Any]]]:
        """Every parser's rows for one item, oldest first."""
        return {
            table: self._lake_rows(
                f"SELECT {columns} FROM lake.{table} "
                "WHERE item_id = $item_id ORDER BY created_at",
                {"item_id": item_id},
            ).to_pylist()
            for table, columns in PARSER_TABLES.items()
        }

    def embeddings_since(self, since: datetime, model_name: str) -> Any:
        """Arrow table of `id` and `vec`, newest row per item."""
        return self._lake_rows(
            """
            SELECT item_id AS id, vec FROM lake.nlp.embeddings
            WHERE created_at >= $since AND model = $model AND len(vec) = $dim
            QUALIFY row_number() OVER (
                PARTITION BY item_id ORDER BY created_at DESC) = 1
            """,
            {"since": since, "model": model_name, "dim": VECTOR_DIM},
        )

    def centroids_since(self, since: datetime) -> Any:
        """Arrow table of `id` and `vec`, newest row per storyline."""
        return self._lake_rows(
            """
            SELECT storyline_id AS id, centroid AS vec FROM lake.nlp.storylines
            WHERE created_at >= $since AND len(centroid) = $dim
            QUALIFY row_number() OVER (
                PARTITION BY storyline_id
                ORDER BY version DESC, created_at DESC) = 1
            """,
            {"since": since, "dim": VECTOR_DIM},
        )

    def disk_figures(self) -> dict[str, float]:
        """Disk use per category, in GB."""
        cursor = self.lake.cursor()
        try:
            return disk_figures(self.settings, cursor)
        finally:
            cursor.close()

    def read_status(self) -> dict[str, Any]:
        """The pipeline's latest status file, or an empty dict."""
        return read_json(self.settings.paths.status)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _freeze(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(value)
    return value
