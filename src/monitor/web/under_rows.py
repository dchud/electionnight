"""One table's rows, opened in place: the newest 25, or a sample of 25.

The query pins a snapshot with `AT (VERSION => n)`, so a pager stays on the
same rows while new ones arrive, and the query shown reproduces them.

A sample orders by `hash(rowid, seed)`. On a multi-file DuckLake table,
`USING SAMPLE reservoir(25 ROWS) REPEATABLE (seed)` repeated its answer, but
over 40 seeds on a table of 500,000 rows in 25 files it returned 1,000 rows
all from the first 200,000. A hash of `rowid` has no such limit: `rowid` covers
Parquet and inlined rows alike, and the same seed always gives the same rows.

Both orders read only their sort column for every row. DuckDB then reads the
other columns only for the rows kept, so a wide table costs about what a narrow
one does.

A table, a mode, a page, and a snapshot reach SQL only after a check against a
fixed list or a bound.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from monitor.lake import TABLES
from monitor.web.under import Disclosed, Ran, Under, table_key
from monitor.web.under_lake import CATALOG, cut_columns, sample_cell
from monitor.web.under_worker import CONSOLE_MAX_CELL_CHARS

PAGE_ROWS = 25
MAX_PAGE = 1_000_000
RECENT = "recent"
SAMPLE = "sample"
MODES = (RECENT, SAMPLE)
TIME_COLUMN = "created_at"
SHOWN_CHARS = 160
# One more than the panel shows, so a longer value can be told from one that
# fits. The panel shows the first `SHOWN_CHARS` of it.
SQL_CHARS = SHOWN_CHARS + 1
NUMBER_RE = re.compile(r"^[0-9]{1,12}$")


def checked_number(text: str | None, low: int, high: int) -> int | None:
    """A whole number in a range, or None. Digits only, with a length limit."""
    if text is None or not NUMBER_RE.match(text):
        return None
    value = int(text)
    return value if low <= value <= high else None


def table_columns(under: Under, table: str) -> tuple[Ran | None, int | None]:
    """A table's catalog columns, and its row count at the latest snapshot."""
    tables = under.catalog_tables()
    row = next(
        (
            entry
            for entry in tables.rows
            if f"{entry['schema_name']}.{entry['table_name']}" == table
        ),
        None,
    )
    if row is None:
        return None, None
    columns = under.catalog(
        "SELECT column_id, column_name, column_type, parent_column "
        f"FROM {CATALOG}.ducklake_column "
        "WHERE table_id = $table_id AND end_snapshot IS NULL "
        "ORDER BY column_order",
        {"table_id": row["table_id"]},
    )
    return columns, row["total_rows"]


def lake_source(table: str, snapshot: int | None) -> str:
    version = f" AT (VERSION => {snapshot})" if snapshot else ""
    return f"lake.{table}{version}"


def rows_sql(
    table: str,
    columns: list[dict[str, Any]],
    mode: str,
    page: int,
    snapshot: int | None,
) -> str:
    """The statement for one page. `page` is the seed in a sample."""
    parts = cut_columns(columns, SQL_CHARS)
    parts += ["snapshot_id AS _snapshot", "filename AS _file"]
    body = ",\n       ".join(parts)
    source = lake_source(table, snapshot)
    if mode == SAMPLE:
        order = f"ORDER BY hash(rowid, {page})\nLIMIT {PAGE_ROWS}"
    else:
        offset = (page - 1) * PAGE_ROWS
        limit = f"LIMIT {PAGE_ROWS}" + (f" OFFSET {offset}" if offset else "")
        order = f"ORDER BY {TIME_COLUMN} DESC, rowid DESC\n{limit}"
    return f"SELECT {body}\nFROM {source}\n{order}"


def count_sql(table: str, snapshot: int | None) -> str:
    return f"SELECT count(*) AS n_rows FROM {lake_source(table, snapshot)}"


def rows_url(table: str, mode: str, page: int, snapshot: int | None) -> str:
    query: dict[str, Any] = {"table": table, "mode": mode, "page": page}
    if snapshot:
        query["at"] = snapshot
    return f"/under/rows?{urlencode(query)}"


def was_cut(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) > SHOWN_CHARS
    if isinstance(value, dict) and "first" in value:
        return any(was_cut(part) for part in value["first"] or [])
    return False


def shown_cell(name: str, value: Any) -> dict[str, Any]:
    """One value as the panel shows it, and whether the SQL cut it."""
    if name == "_file" and value is not None:
        return {"text": Path(str(value)).name, "cut": False}
    return {
        "text": sample_cell(value),
        "cut": was_cut(value),
    }


def failure(status_code: int, message: str) -> dict[str, Any]:
    return {"status": "error", "status_code": status_code, "error": message}


def not_read(
    under: Under,
    base: dict[str, Any],
    pinned: bool,
    error: str | None,
    query: Disclosed | None = None,
) -> dict[str, Any]:
    """Why no rows came back. A snapshot that is gone cannot be retried."""
    if pinned and not under.snapshot_exists(base["at_snapshot"]):
        return {**base, "status": "snapshot_gone", "status_code": 200}
    return {
        **base,
        "status": "failed",
        "status_code": 200,
        "error": error,
        "query": query,
    }


def rows_view(under: Under, params: Any) -> dict[str, Any]:
    """What the panel shows for one request."""
    table = params.get("table")
    if table not in TABLES:
        return failure(400, "That is not a table in this lake.")
    mode = params.get("mode") or RECENT
    if mode not in MODES:
        return failure(400, "Choose the most recent rows or a sample.")
    page = checked_number(params.get("page") or "1", 1, MAX_PAGE)
    if page is None:
        return failure(
            400, f"A page is a whole number from 1 to {MAX_PAGE:,}."
        )
    latest = under.snapshot_id or None
    snapshot = latest
    pinned = params.get("at") is not None
    if pinned:
        snapshot = checked_number(params.get("at"), 1, latest or 0)
        if snapshot is None:
            return failure(
                400,
                "That snapshot is not in this lake. Close this and reopen.",
            )
    columns, catalog_rows = table_columns(under, table)
    if columns is None:
        return failure(404, "The catalog does not list this table.")
    base = {
        "table": table,
        "mode": mode,
        "page": page,
        "at_snapshot": snapshot,
        "page_rows": PAGE_ROWS,
        "shown_chars": SHOWN_CHARS,
        "console_cell_chars": CONSOLE_MAX_CELL_CHARS,
        "time_column": TIME_COLUMN,
        "record_key": table_key(table)[0],
        "retry_url": rows_url(table, mode, page, snapshot),
    }
    if not columns.ok:
        return not_read(under, base, False, columns.error)
    names = {column["column_name"] for column in columns.rows}
    # A feed's first fetch brings many rows at once to a table with a row per
    # item. It does so to no other table.
    base["backlog_applies"] = "item_id" in names
    if mode == RECENT and TIME_COLUMN not in names:
        return {
            **base,
            "status": "no_time",
            "status_code": 200,
            "sample_url": rows_url(table, SAMPLE, 1, snapshot),
        }
    if mode == RECENT and page > last_page_of(catalog_rows or 0):
        # The catalog counts the latest snapshot, so there no scan runs. An
        # older snapshot has a count of its own, which only a scan can read.
        if snapshot == latest:
            return past_end(base, catalog_rows or 0)
        counted = under.scan_once(
            [("count", count_sql(table, snapshot), None)], PAGE_ROWS
        )["count"]
        if not counted.ok:
            return not_read(under, base, pinned, counted.error)
        total = counted.first["n_rows"]
        if page > last_page_of(total):
            return past_end(base, total)
    sql = rows_sql(table, columns.rows, mode, page, snapshot)
    results = under.scan_once(
        [
            ("count", count_sql(table, snapshot), None),
            ("rows", sql, None),
        ],
        PAGE_ROWS,
    )
    counted, ran = results["count"], results["rows"]
    query: Disclosed = ran.query
    if not ran.ok or not counted.ok:
        return not_read(under, base, pinned, ran.error or counted.error, query)
    total = counted.first["n_rows"]
    if mode == RECENT and page > 1 and not ran.rows:
        return past_end(base, total, query)
    return {
        **base,
        **page_context(table, mode, page, snapshot, total, ran),
        "status": "ok",
        "status_code": 200,
        "query": query,
        "count_sql": counted.query.sql,
    }


def last_page_of(total: int) -> int:
    return max(1, math.ceil(total / PAGE_ROWS))


def past_end(
    base: dict[str, Any], total: int, query: Disclosed | None = None
) -> dict[str, Any]:
    return {
        **base,
        "status": "past_end",
        "status_code": 200,
        "total": total,
        "query": query,
        "last_url": rows_url(
            base["table"], RECENT, last_page_of(total), base["at_snapshot"]
        ),
    }


def page_context(
    table: str,
    mode: str,
    page: int,
    snapshot: int | None,
    total: int,
    ran: Ran,
) -> dict[str, Any]:
    """The rows as cells, the range they cover, and where the pagers go."""
    offset = (page - 1) * PAGE_ROWS if mode == RECENT else 0
    columns = list(ran.rows[0]) if ran.rows else []
    cells = [
        [shown_cell(name, entry[name]) for name in columns]
        for entry in ran.rows
    ]
    cut = [
        name
        for number, name in enumerate(columns)
        if any(row[number]["cut"] for row in cells)
    ]
    more = offset + len(ran.rows) < total
    return {
        "columns": columns,
        "rows": cells,
        "cut_columns": cut,
        "total": total,
        "first_row": offset + 1,
        "last_row": offset + len(ran.rows),
        "newer_url": rows_url(table, mode, page - 1, snapshot)
        if page > 1
        else None,
        "older_url": rows_url(table, mode, page + 1, snapshot)
        if mode == RECENT and more and page < MAX_PAGE
        else None,
        "another_url": rows_url(table, mode, page + 1, snapshot)
        if mode == SAMPLE and page < MAX_PAGE
        else None,
    }
