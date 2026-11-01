"""DuckLake connections, table definitions, and the batched writer.

DuckLake rejects fixed-size arrays, so vectors are stored as `FLOAT[]`. Cast
them to `FLOAT[384]` in queries that use array functions.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import xxhash

from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

SCHEMAS = ("raw", "nlp", "gold", "ref")
MAX_FLUSH_FAILURES = 3

PARSER_COLS = "parser TEXT, parser_version TEXT,"

TABLES: dict[str, str] = {
    "raw.fetches": """
        fetch_id TEXT, source_id TEXT, feed_url TEXT, started_at TIMESTAMPTZ,
        duration_ms INT, http_status INT, not_modified BOOLEAN, n_entries INT,
        n_new INT, error TEXT, etag TEXT, last_modified TEXT""",
    "raw.items": """
        item_id TEXT, source_id TEXT, feed_url TEXT, url TEXT,
        canonical_url TEXT, guid TEXT, title TEXT, summary TEXT, author TEXT,
        published_at TIMESTAMPTZ, first_seen_at TIMESTAMPTZ, language TEXT,
        feed_categories TEXT[], raw_entry JSON""",
    "raw.article_text": """
        item_id TEXT, fetched_at TIMESTAMPTZ, http_status INT, text TEXT,
        n_chars INT, extractor TEXT, paywalled BOOLEAN, canonical_url TEXT""",
    "nlp.parser_runs": """
        item_id TEXT, parser TEXT, parser_version TEXT, status TEXT,
        error TEXT""",
    "nlp.lang": f"""
        item_id TEXT, language TEXT, confidence FLOAT, {PARSER_COLS}
        method TEXT""",
    "nlp.classify_a": f"""
        item_id TEXT, election_relevant BOOLEAN, relevance_conf FLOAT,
        us_politics BOOLEAN, levels TEXT[], primary_level TEXT, states TEXT[],
        race_types TEXT[], topics TEXT[], free_tags TEXT[],
        is_results_report BOOLEAN, is_opinion BOOLEAN, headline_en TEXT,
        summary_en TEXT, method TEXT, {PARSER_COLS} batch_id TEXT""",
    "nlp.extract_b": f"""
        item_id TEXT, summary_en TEXT, article_tone FLOAT,
        topics_refined TEXT[], text_basis TEXT, raw JSON, {PARSER_COLS}
        model TEXT""",
    "nlp.mentions": f"""
        mention_id TEXT, item_id TEXT, surface TEXT, char_start INT,
        char_end INT, ent_type TEXT, role TEXT, entity_id TEXT, tone FLOAT,
        tone_evidence TEXT, party TEXT, state TEXT, office TEXT,
        district TEXT, {PARSER_COLS} resolved_by TEXT""",
    "nlp.places": f"""
        item_id TEXT, surface TEXT, place_type TEXT, state_usps TEXT,
        county_fips TEXT, place_geoid TEXT, cd_code TEXT, match_score FLOAT,
        method TEXT, {PARSER_COLS} place_id TEXT""",
    "nlp.embeddings": f"""
        item_id TEXT, model TEXT, vec FLOAT[], text_basis TEXT, {PARSER_COLS}
        dim INT""",
    "nlp.dup_groups": f"""
        item_id TEXT, dup_group_id TEXT, is_canonical BOOLEAN, method TEXT,
        {PARSER_COLS} similarity FLOAT""",
    "nlp.storyline_assign": f"""
        item_id TEXT, storyline_id TEXT, similarity FLOAT,
        assigned_at TIMESTAMPTZ, {PARSER_COLS} source_id TEXT""",
    "nlp.storylines": f"""
        storyline_id TEXT, version INT, label TEXT, summary TEXT,
        citations JSON, keywords TEXT[], centroid FLOAT[],
        first_seen_at TIMESTAMPTZ, last_item_at TIMESTAMPTZ, n_items INT,
        n_sources INT, states TEXT[], primary_level TEXT, status TEXT,
        merged_into TEXT, {PARSER_COLS} labeled_at_items INT""",
    "nlp.race_calls": f"""
        call_id TEXT, item_id TEXT, race_key TEXT, race_desc TEXT,
        winner_name TEXT, winner_entity_id TEXT, party TEXT, called_by TEXT,
        status TEXT, claimed_at_text TEXT, confidence FLOAT, evidence TEXT,
        state TEXT, office TEXT, district TEXT, {PARSER_COLS} first_seen_at
        TIMESTAMPTZ""",
    "nlp.briefs": """
        brief_id TEXT, window_start TIMESTAMPTZ, window_end TIMESTAMPTZ,
        focus TEXT, markdown TEXT, citations JSON, model TEXT""",
    "nlp.llm_calls": """
        call_id TEXT, purpose TEXT, model TEXT, input_tokens INT,
        cache_read_tokens INT, cache_write_tokens INT, output_tokens INT,
        usd_est DOUBLE, latency_ms INT, status TEXT""",
    "ref.sources": """
        source_id TEXT, name TEXT, homepage TEXT, feed_urls TEXT, scope TEXT,
        home_state TEXT, country TEXT, language TEXT, lean_label TEXT,
        lean_source TEXT, lean_source_url TEXT, lean_retrieved_on TEXT,
        adfontes_bias FLOAT, adfontes_reliability FLOAT,
        adfontes_url TEXT, reliability_note TEXT, ownership TEXT,
        state_affiliated BOOLEAN, paywall TEXT, poll_tier INT,
        enabled BOOLEAN, notes TEXT""",
    "ref.states": """
        usps TEXT, fips TEXT, name TEXT, battleground BOOLEAN,
        poll_close_et TEXT, poll_close_all_et TEXT""",
    "ref.counties": """
        fips TEXT, name TEXT, state_usps TEXT, lat DOUBLE, lon DOUBLE,
        aland BIGINT""",
    "ref.places": """
        geoid TEXT, name TEXT, state_usps TEXT, lat DOUBLE, lon DOUBLE,
        aland BIGINT""",
    "ref.entities": """
        entity_id TEXT, canonical_name TEXT, ent_type TEXT, party TEXT,
        state TEXT, office TEXT, race_key TEXT, incumbent BOOLEAN,
        wikidata_qid TEXT, aliases TEXT[], origin TEXT""",
    "ref.races": """
        race_key TEXT, state TEXT, office TEXT, district TEXT, rating TEXT,
        rating_source TEXT, rating_url TEXT, close_race BOOLEAN,
        candidates TEXT[], watch BOOLEAN""",
    "gold.bin_counts": """
        bin_start TIMESTAMPTZ, level TEXT, n_items BIGINT, n_relevant BIGINT,
        n_unique BIGINT""",
    "gold.storyline_bins": """
        bin_start TIMESTAMPTZ, storyline_id TEXT, n_items BIGINT,
        n_unique BIGINT, n_sources BIGINT, mean_tone DOUBLE""",
    "gold.entity_bins": """
        bin_start TIMESTAMPTZ, entity_id TEXT, n_mentions BIGINT,
        n_items BIGINT, tone_sum DOUBLE, tone_sq_sum DOUBLE, tone_n BIGINT""",
    "gold.entity_lean_bins": """
        bin_start TIMESTAMPTZ, entity_id TEXT, lean_label TEXT,
        n_mentions BIGINT, n_items BIGINT, tone_sum DOUBLE,
        tone_sq_sum DOUBLE, tone_n BIGINT""",
    "gold.state_bins": """
        bin_start TIMESTAMPTZ, state_usps TEXT, n_items BIGINT,
        n_unique BIGINT, tone_sum DOUBLE, tone_n BIGINT""",
    "gold.county_bins": """
        bin_start TIMESTAMPTZ, county_fips TEXT, n_items BIGINT""",
    "gold.source_bins": """
        bin_start TIMESTAMPTZ, source_id TEXT, n_items BIGINT,
        n_relevant BIGINT""",
    "gold.lean_storyline_bins": """
        bin_start TIMESTAMPTZ, storyline_id TEXT, lean_label TEXT,
        n_items BIGINT""",
    "gold.comention_edges": """
        bin_start TIMESTAMPTZ, entity_a TEXT, entity_b TEXT, n BIGINT""",
    "gold.topic_bins": """
        bin_start TIMESTAMPTZ, topic TEXT, n_items BIGINT""",
}

# Large rows go straight to Parquet. Keeping them out of the catalog keeps the
# catalog and its backups small.
NO_INLINE = {("nlp", "embeddings"), ("raw", "article_text")}


def hash_id(*parts: object) -> str:
    """Deterministic xxhash64 hex id, so reprocessing is idempotent."""
    text = "\x1f".join("" if p is None else str(p) for p in parts)
    return xxhash.xxh64(text.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


def _attach_sql(settings: Settings, read_only: bool) -> str:
    catalog = settings.paths.catalog.resolve()
    data = settings.paths.data.resolve()
    mode = ", READ_ONLY" if read_only else ""
    return (
        f"ATTACH 'ducklake:sqlite:{catalog}' AS lake "
        f"(DATA_PATH '{data}/'{mode})"
    )


def connect(
    settings: Settings, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """Open a connection with the lake attached as `lake`, in UTC."""
    conn = duckdb.connect()
    for extension in ("ducklake", "sqlite"):
        try:
            conn.execute(f"LOAD {extension}")
        except duckdb.Error:
            conn.execute(f"INSTALL {extension}; LOAD {extension}")
    conn.execute("SET TimeZone = 'UTC'")
    conn.execute(_attach_sql(settings, read_only))
    return conn


def ensure_dirs(settings: Settings) -> None:
    paths = settings.paths
    for directory in (
        paths.catalog.parent,
        paths.data,
        paths.models,
        paths.backups,
        paths.ref,
        paths.control,
        paths.logs,
        paths.reports,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def enable_wal(settings: Settings) -> None:
    """Put the catalog in WAL mode so readers never block a writer's commit.

    The setting persists in the database file.
    """
    with sqlite3.connect(settings.paths.catalog) as catalog:
        catalog.execute("PRAGMA journal_mode=WAL")


def init_lake(settings: Settings) -> None:
    """Create schemas and tables and set options. Safe to run repeatedly."""
    ensure_dirs(settings)
    conn = connect(settings)
    conn.close()
    enable_wal(settings)
    conn = connect(settings)
    try:
        lake = settings.lake
        options = (
            ("parquet_compression", f"'{lake.parquet_compression}'"),
            ("parquet_compression_level", lake.parquet_compression_level),
            ("target_file_size", f"'{lake.target_file_size}'"),
            ("data_inlining_row_limit", lake.data_inlining_row_limit),
        )
        for name, value in options:
            conn.execute(f"CALL lake.set_option('{name}', {value})")
        for schema in SCHEMAS:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS lake.{schema}")
        for table, columns in TABLES.items():
            body = columns.strip()
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS lake.{table} "
                f"({body}, created_at TIMESTAMPTZ)"
            )
        for schema, table in sorted(NO_INLINE):
            conn.execute(
                "CALL lake.set_option('data_inlining_row_limit', 0, "
                f"schema => '{schema}', table_name => '{table}')"
            )
    finally:
        conn.close()


def latest_sql(table: str, key: str, where: str = "") -> str:
    """SQL returning the newest row per key in an append-only table."""
    clause = f"WHERE {where}" if where else ""
    return (
        f"SELECT * FROM lake.{table} {clause} QUALIFY row_number() "
        f"OVER (PARTITION BY {key} ORDER BY created_at DESC) = 1"
    )


def current_snapshot(conn: duckdb.DuckDBPyConnection) -> int:
    rows = conn.execute(
        "SELECT max(snapshot_id) FROM lake.snapshots()"
    ).fetchall()
    return int(rows[0][0])


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def rows_to_arrow(rows: Iterable[dict[str, Any]]) -> pa.Table:
    """Build an Arrow table whose columns cover every key in any row."""
    materialized = list(rows)
    keys = sorted({key for row in materialized for key in row})
    columns: dict[str, list[Any]] = {
        key: [_clean(row.get(key)) for row in materialized] for key in keys
    }
    return pa.table(columns)


class BatchWriter:
    """Buffers rows and commits them together in one transaction."""

    def __init__(
        self, conn: duckdb.DuckDBPyConnection, settings: Settings
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._lock = threading.Lock()
        self.last_commit_at: datetime | None = None
        self.in_flight = False
        self._failures: dict[str, int] = {}

    def add(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        if table not in TABLES:
            raise KeyError(table)
        now = utcnow()
        stamped = [{"created_at": now, **row} for row in rows]
        if stamped:
            with self._lock:
                self._buffers[table].extend(stamped)

    def pending(self) -> int:
        with self._lock:
            return sum(len(rows) for rows in self._buffers.values())

    def _commit(self, batch: dict[str, list[dict[str, Any]]]) -> None:
        cursor = self._conn.cursor()
        self.in_flight = True
        try:
            cursor.execute("BEGIN")
            for table, rows in batch.items():
                arrow = rows_to_arrow(rows)
                cursor.register("_batch", arrow)
                cursor.execute(
                    f"INSERT INTO lake.{table} BY NAME SELECT * FROM _batch"
                )
                cursor.unregister("_batch")
            cursor.execute("COMMIT")
        except Exception:
            try:
                cursor.execute("ROLLBACK")
            except duckdb.Error:
                log.warning("rollback_failed")
            raise
        finally:
            self.in_flight = False
            cursor.close()

    def flush(self) -> dict[str, int]:
        """Commit everything buffered in one transaction.

        If that fails, each table is tried on its own, so one table's bad rows
        cannot hold back the rest. Rows that fail because of their content are
        set aside after repeated failures; a locked catalog just retries.
        """
        with self._lock:
            batch = {k: v for k, v in self._buffers.items() if v}
            self._buffers = defaultdict(list)
        if not batch:
            return {}
        try:
            self._commit(batch)
            counts = {table: len(rows) for table, rows in batch.items()}
        except Exception as error:
            log.warning(
                "flush_failed",
                tables=sorted(batch),
                error=f"{type(error).__name__}: {error}"[:300],
            )
            counts = self._flush_each(batch)
        if counts:
            self.last_commit_at = utcnow()
            self._write_signal(counts)
        return counts

    def _flush_each(
        self, batch: dict[str, list[dict[str, Any]]]
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table, rows in batch.items():
            try:
                self._commit({table: rows})
            except Exception as error:
                self._hold_back(table, rows, error)
            else:
                counts[table] = len(rows)
                self._failures.pop(table, None)
        return counts

    def _hold_back(
        self, table: str, rows: list[dict[str, Any]], error: Exception
    ) -> None:
        transient = isinstance(
            error, duckdb.TransactionException | duckdb.IOException
        )
        failures = self._failures.get(table, 0) + 1
        if transient or failures < MAX_FLUSH_FAILURES:
            self._failures[table] = failures
            with self._lock:
                self._buffers[table] = rows + self._buffers[table]
            return
        self._failures.pop(table, None)
        path = self._settings.paths.control / f"rejected-{table}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str) + "\n")
        log.error(
            "rows_rejected",
            table=table,
            n=len(rows),
            path=str(path),
            error=f"{type(error).__name__}: {error}"[:300],
        )

    def _write_signal(self, counts: dict[str, int]) -> None:
        path = self._settings.paths.last_commit
        temporary = path.with_suffix(".tmp")
        payload = {
            "committed_at": utcnow().isoformat(),
            "changed": sorted(counts),
            "rows": sum(counts.values()),
        }
        temporary.write_text(json.dumps(payload))
        os.replace(temporary, path)


class WriterBusy(RuntimeError):
    """Another process holds the writer lock."""


@contextmanager
def writer_lock(settings: Settings) -> Iterator[None]:
    """Hold the single-writer lock for the life of the block."""
    path: Path = settings.paths.writer_lock
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WriterBusy(str(path)) from error
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield


def writer_is_running(settings: Settings) -> bool:
    try:
        with writer_lock(settings):
            return False
    except WriterBusy:
        return True
