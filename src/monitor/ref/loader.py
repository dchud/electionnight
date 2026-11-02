"""Append reference files to the lake's `ref` tables when they change.

The pipeline calls `sync_ref_tables` about once a minute. Rows are only ever
appended, and readers take the newest row per key with `latest_sql`. `state`
holds a content hash per table, so unchanged inputs do nothing.

`ref.counties` and `ref.places` are large. They are re-appended whole when
their file changes, and on a cold start only if the number of keys in the lake
differs from the file. The other tables compare against the newest rows in the
lake and append the rows that differ, so a restart appends nothing and a new
ratings file appends only the ratings that moved. A key that disappears from a
file keeps its last row in the lake.

A file that cannot be read leaves its table's hash unset, so the next call
tries again, and does not affect the other tables.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import xxhash
import yaml

from monitor.ingest.sources import load_sources, parse_bool, sources_path
from monitor.lake import TABLES, BatchWriter, latest_sql
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

ET = ZoneInfo("America/New_York")
ELECTION_DATE = "2026-11-03"
MORNING_ENDS = time(12, 0)
FLOAT_DIGITS = 4

Row = dict[str, Any]


def _digest(paths: Iterable[Path]) -> str:
    """Hash the inputs together. An absent file hashes differently from any."""
    digest = xxhash.xxh64()
    for path in paths:
        digest.update(str(path.name).encode())
        digest.update(path.read_bytes() if path.exists() else b"\x00absent")
    return digest.hexdigest()


def _columns(table: str) -> list[str]:
    columns = []
    for chunk in TABLES[table].split(","):
        columns.append(chunk.split()[0])
    return columns


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _comparable(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, FLOAT_DIGITS)
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


def _differs(row: Row, existing: Row | None, columns: list[str]) -> bool:
    if existing is None:
        return True
    return any(
        _comparable(row.get(column)) != _comparable(existing.get(column))
        for column in columns
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def poll_close_timestamp(clock: str | None, election_date: str) -> str | None:
    """Turn `20:00` into an ISO timestamp. Before 12:00 is the next day."""
    if not clock:
        return None
    parsed = time.fromisoformat(str(clock))
    day = datetime.fromisoformat(election_date).date()
    if parsed < MORNING_ENDS:
        day += timedelta(days=1)
    return datetime.combine(day, parsed, tzinfo=ET).isoformat()


def state_rows(settings: Settings) -> list[Row] | None:
    path = settings.paths.ref / "states.csv"
    if not path.exists():
        return None
    closings = _read_yaml(settings.paths.config / "poll_closings.yaml")
    races = _read_yaml(settings.paths.config / "races.yaml")
    battleground = set(races.get("battleground_states") or [])
    per_state = closings.get("states") or {}
    date = str(closings.get("election_date") or ELECTION_DATE)
    rows = []
    for row in _read_csv(path):
        hours = per_state.get(row["usps"]) or {}
        rows.append(
            {
                "usps": row["usps"],
                "fips": row["fips"],
                "name": row["name"],
                "battleground": row["usps"] in battleground,
                "poll_close_et": poll_close_timestamp(
                    hours.get("first_close"), date
                ),
                "poll_close_all_et": poll_close_timestamp(
                    hours.get("all_close"), date
                ),
            }
        )
    return rows


def entity_rows(settings: Settings) -> list[Row] | None:
    path = settings.paths.ref / "entities.jsonl"
    if not path.exists():
        return None
    columns = _columns("ref.entities")
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                entity = json.loads(line)
                rows.append({column: entity.get(column) for column in columns})
    for row in rows:
        row["aliases"] = list(row.get("aliases") or [])
    return rows


def race_rows(settings: Settings) -> list[Row] | None:
    path = settings.paths.config / "races.yaml"
    if not path.exists():
        return None
    columns = _columns("ref.races")
    rows = []
    for race in _read_yaml(path).get("races") or []:
        row = {column: race.get(column) for column in columns}
        row["candidates"] = list(row.get("candidates") or [])
        row["close_race"] = bool(row.get("close_race"))
        row["watch"] = bool(row.get("watch"))
        rows.append(row)
    return rows


def confirmed_leans(settings: Settings) -> dict[str, dict[str, str]]:
    path = settings.paths.config / "lean_ratings.csv"
    if not path.exists():
        return {}
    return {
        row["source_id"]: row
        for row in _read_csv(path)
        if parse_bool(row.get("confirmed"))
    }


def source_rows(settings: Settings) -> list[Row] | None:
    """Rows from `sources.csv`, with confirmed `lean_ratings.csv` values added.

    An unconfirmed lean row is a proposal awaiting owner review, so it changes
    nothing, including the Ad Fontes columns.
    """
    if not sources_path(settings).exists():
        return None
    leans = confirmed_leans(settings)
    rows = []
    for source in load_sources(settings):
        row: Row = {
            "source_id": source.source_id,
            "name": source.name,
            "homepage": source.homepage,
            "feed_urls": "|".join(source.feed_urls),
            "scope": source.scope,
            "home_state": source.home_state,
            "country": source.country,
            "language": source.language,
            "lean_label": source.lean_label,
            "lean_source": source.lean_source,
            "lean_source_url": source.lean_source_url,
            "lean_retrieved_on": source.lean_retrieved_on,
            "adfontes_bias": None,
            "adfontes_reliability": None,
            "adfontes_url": None,
            "reliability_note": source.reliability_note,
            "ownership": source.ownership,
            "state_affiliated": source.state_affiliated,
            "paywall": source.paywall,
            "poll_tier": source.poll_tier,
            "enabled": source.enabled,
            "notes": source.notes,
        }
        lean = leans.get(source.source_id)
        if lean:
            for column in (
                "lean_label",
                "lean_source",
                "lean_source_url",
                "lean_retrieved_on",
                "adfontes_url",
            ):
                if lean.get(column):
                    row[column] = lean[column]
            row["adfontes_bias"] = _float(lean.get("adfontes_bias"))
            row["adfontes_reliability"] = _float(
                lean.get("adfontes_reliability")
            )
        rows.append({key: _blank_to_none(value) for key, value in row.items()})
    return rows


def _latest_rows(
    conn: duckdb.DuckDBPyConnection, table: str, key: str
) -> dict[Any, Row]:
    cursor = conn.cursor()
    try:
        cursor.execute(latest_sql(table, key))
        names = [column[0] for column in cursor.description]
        return {
            record[names.index(key)]: dict(zip(names, record, strict=True))
            for record in cursor.fetchall()
        }
    finally:
        cursor.close()


def _sync_small(
    conn: duckdb.DuckDBPyConnection,
    writer: BatchWriter,
    state: dict[str, Any],
    table: str,
    key: str,
    inputs: list[Path],
    build: Callable[[], list[Row] | None],
) -> int:
    digest = _digest(inputs)
    if state.get(table) == digest:
        return 0
    rows = build()
    if rows is None:
        return 0
    existing = _latest_rows(conn, table, key)
    columns = _columns(table)
    changed = [
        row for row in rows if _differs(row, existing.get(row[key]), columns)
    ]
    writer.add(table, changed)
    state[table] = digest
    return len(changed)


def _large_row(table: str, row: dict[str, str]) -> Row:
    key = "fips" if table == "ref.counties" else "geoid"
    return {
        key: row[key],
        "name": row["name"],
        "state_usps": row["state_usps"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "aland": int(row["aland"]),
    }


def _sync_large(
    conn: duckdb.DuckDBPyConnection,
    writer: BatchWriter,
    state: dict[str, Any],
    table: str,
    key: str,
    path: Path,
) -> int:
    if not path.exists():
        return 0
    digest = _digest([path])
    if state.get(table) == digest:
        return 0
    rows = [_large_row(table, row) for row in _read_csv(path)]
    if table not in state:
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT count(DISTINCT {key}) FROM lake.{table}")
            found = cursor.fetchone()
            in_lake = found[0] if found else 0
        finally:
            cursor.close()
        if in_lake == len({row[key] for row in rows}):
            state[table] = digest
            return 0
    writer.add(table, rows)
    state[table] = digest
    return len(rows)


def sync_ref_tables(
    conn: duckdb.DuckDBPyConnection,
    writer: BatchWriter,
    settings: Settings,
    state: dict[str, Any],
) -> dict[str, int]:
    """Append changed reference rows. Returns rows appended per table.

    Rows go to the writer's buffer and commit with its next flush.
    """
    ref = settings.paths.ref
    config = settings.paths.config
    states_inputs = [
        ref / "states.csv",
        config / "poll_closings.yaml",
        config / "races.yaml",
    ]
    jobs: list[tuple[str, Callable[[], int]]] = [
        (
            "ref.counties",
            lambda: _sync_large(
                conn,
                writer,
                state,
                "ref.counties",
                "fips",
                ref / "counties.csv",
            ),
        ),
        (
            "ref.places",
            lambda: _sync_large(
                conn,
                writer,
                state,
                "ref.places",
                "geoid",
                ref / "places.csv",
            ),
        ),
        (
            "ref.states",
            lambda: _sync_small(
                conn,
                writer,
                state,
                "ref.states",
                "usps",
                states_inputs,
                lambda: state_rows(settings),
            ),
        ),
        (
            "ref.entities",
            lambda: _sync_small(
                conn,
                writer,
                state,
                "ref.entities",
                "entity_id",
                [ref / "entities.jsonl"],
                lambda: entity_rows(settings),
            ),
        ),
        (
            "ref.races",
            lambda: _sync_small(
                conn,
                writer,
                state,
                "ref.races",
                "race_key",
                [config / "races.yaml"],
                lambda: race_rows(settings),
            ),
        ),
        (
            "ref.sources",
            lambda: _sync_small(
                conn,
                writer,
                state,
                "ref.sources",
                "source_id",
                [sources_path(settings), config / "lean_ratings.csv"],
                lambda: source_rows(settings),
            ),
        ),
    ]
    counts: dict[str, int] = {}
    for table, job in jobs:
        try:
            appended = job()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            csv.Error,
            yaml.YAMLError,
            duckdb.Error,
        ) as error:
            log.warning(
                "ref_sync_failed",
                table=table,
                error=f"{type(error).__name__}: {error}",
            )
            continue
        if appended:
            counts[table] = appended
    return counts
