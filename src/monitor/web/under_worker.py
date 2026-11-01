"""A short-lived child process for work that may touch data files.

The web process starts one per request and kills it at a time limit, so a slow
scan costs this process and nothing else. It lowers its own priority, attaches
the lake read-only, and locks its configuration before it runs anything.

It reads no settings file, and the parent passes it a minimal environment, so
it holds no API keys. A request is one JSON object on stdin. Each result is one
JSON line on stdout, flushed as it finishes, so results from before a kill
survive the kill.

A read-only attach does not make arbitrary SQL safe. On a scratch lake,
`SELECT * FROM ducklake_cleanup_old_files(...)` deleted scheduled Parquet files
before its catalog write failed, and `ATTACH` of the catalog file for writing,
`COPY` into the lake directory, and `DETACH` all succeeded with the
configuration locked. So `check_console_sql` decides what the SQL console may
run, from the parsed statement, and the console runs inside a kernel sandbox
that denies network access and every file write except the catalog's `-shm`
and `-wal` files, which a SQLite reader in WAL mode needs.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal
from typing import Any
from uuid import UUID

NICE = 10
MAX_CELL_CHARS = 200_000
MAX_OUTPUT_BYTES = 20_000_000
CONSOLE_MAX_CELL_CHARS = 4000
CONSOLE_MAX_OUTPUT_BYTES = 2_000_000

# Table functions the console may call. Everything else is denied, including
# every DuckLake maintenance call, `sqlite_scan`, `sqlite_query`, `query`,
# `read_text`, `glob`, and `json_execute_serialized_sql`.
ALLOWED_TABLE_FUNCTIONS = frozenset(
    {
        "duckdb_columns",
        "duckdb_databases",
        "duckdb_functions",
        "duckdb_schemas",
        "duckdb_tables",
        "duckdb_types",
        "duckdb_views",
        "ducklake_current_snapshot",
        "ducklake_last_committed_snapshot",
        "ducklake_list_files",
        "ducklake_options",
        "ducklake_settings",
        "ducklake_snapshots",
        "ducklake_table_changes",
        "ducklake_table_deletions",
        "ducklake_table_info",
        "ducklake_table_insertions",
        "generate_series",
        "parquet_file_metadata",
        "parquet_kv_metadata",
        "parquet_metadata",
        "parquet_scan",
        "parquet_schema",
        "pragma_table_info",
        "range",
        "read_parquet",
        "snapshots",
        "unnest",
    }
)
DENIED_SCALAR_FUNCTIONS = frozenset(
    {
        "getenv",
        "json_execute_serialized_sql",
        "nextval",
        "setval",
        "sleep_ms",
        "write_log",
    }
)
# Tables reachable by name. A bare name may be a CTE; the binder rejects it if
# it is not, and a path-like name never reaches a replacement scan.
ALLOWED_CATALOGS = frozenset({"lake", "__ducklake_metadata_lake"})
ALLOWED_SCHEMAS = frozenset({"information_schema", "pg_catalog"})
EXPLAIN_RE = re.compile(r"^\s*EXPLAIN\s+(?P<rest>.*)$", re.I | re.S)
EXPLAIN_BODY_RE = re.compile(r"^\s*(SELECT|WITH|FROM|VALUES)\b", re.I)
PATH_LIKE_RE = re.compile(r"[/\\.:]")


class ConsoleRejected(Exception):
    """The statement is not one the console will run."""


def plain(value: Any) -> Any:
    """A JSON value for anything DuckDB's Python client returns."""
    if isinstance(value, datetime | date | day_time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


def clip_cell(value: Any, limit: int = MAX_CELL_CHARS) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def emit(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record, default=plain) + "\n")
    sys.stdout.flush()


def lock_settings(conn: Any, lake_dir: str) -> None:
    """Apply the limits for this connection. They cannot be changed after."""
    for statement in (
        "SET threads = 2",
        "SET memory_limit = '1GB'",
        "SET max_temp_directory_size = '0B'",
        "SET python_enable_replacements = false",
        f"SET allowed_directories = ['{lake_dir}']",
        "SET enable_external_access = false",
        "SET lock_configuration = true",
    ):
        conn.execute(statement)


def open_lake(request: dict[str, Any]) -> Any:
    import duckdb

    catalog = request["catalog"]
    data_path = request["data_path"].rstrip("/") + "/"
    conn = duckdb.connect()
    conn.execute("LOAD ducklake")
    conn.execute("LOAD sqlite")
    conn.execute("SET TimeZone = 'UTC'")
    conn.execute(
        f"ATTACH 'ducklake:sqlite:{catalog}' AS lake "
        f"(DATA_PATH '{data_path}', READ_ONLY)"
    )
    lock_settings(conn, data_path)
    return conn


def fetch(
    conn: Any,
    sql: str,
    params: Any,
    max_rows: int,
    max_cell: int = MAX_CELL_CHARS,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Run one statement, bounded in rows, in cell size, and in total bytes."""
    started = time.monotonic()
    cursor = conn.execute(sql, params) if params else conn.execute(sql)
    columns = [column[0] for column in cursor.description or []]
    rows = cursor.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    kept = []
    size = 0
    for row in rows[:max_rows]:
        clipped = [clip_cell(cell, max_cell) for cell in row]
        size += len(json.dumps(clipped, default=plain))
        if size > max_bytes:
            truncated = True
            break
        kept.append(clipped)
    return {
        "columns": columns,
        "rows": kept,
        "truncated": truncated,
        "ms": int((time.monotonic() - started) * 1000),
    }


def run_scan(request: dict[str, Any]) -> None:
    """Run the web process's own fixed statements, reporting each one."""
    conn = open_lake(request)
    max_rows = int(request.get("max_rows", 1000))
    for statement in request["statements"]:
        name = statement["name"]
        try:
            result = fetch(
                conn, statement["sql"], statement.get("params"), max_rows
            )
        except Exception as error:
            emit({"name": name, "error": str(error)[:600]})
        else:
            emit({"name": name, **result})


def walk_ast(node: Any, seen: dict[str, set[str]]) -> None:
    """Collect every table function, scalar function, and table reference."""
    if isinstance(node, list):
        for value in node:
            walk_ast(value, seen)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind == "TABLE_FUNCTION":
        function = node.get("function") or {}
        seen["table_functions"].add(
            str(function.get("function_name", "")).lower()
        )
        walk_ast(function.get("children"), seen)
        for key, value in node.items():
            if key != "function":
                walk_ast(value, seen)
        return
    if kind == "BASE_TABLE":
        seen["tables"].add(
            json.dumps(
                [
                    node.get("catalog_name") or "",
                    node.get("schema_name") or "",
                    node.get("table_name") or "",
                ]
            )
        )
    if node.get("class") == "FUNCTION" or "function_name" in node:
        name = node.get("function_name")
        if name:
            seen["scalar_functions"].add(str(name).lower())
    for value in node.values():
        walk_ast(value, seen)


def check_select(conn: Any, sql: str) -> None:
    """Reject unless `sql` is exactly one SELECT using allowed functions."""
    import duckdb

    statements = conn.extract_statements(sql)
    if len(statements) != 1:
        raise ConsoleRejected("One statement at a time.")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise ConsoleRejected("Only SELECT and EXPLAIN run here.")
    serialized = conn.execute(
        "SELECT json_serialize_sql($1)", [sql]
    ).fetchone()
    tree = json.loads(serialized[0])
    if tree.get("error"):
        raise ConsoleRejected(
            f"Could not read this statement: {tree.get('error_message')}"
        )
    seen: dict[str, set[str]] = {
        "table_functions": set(),
        "scalar_functions": set(),
        "tables": set(),
    }
    walk_ast(tree.get("statements"), seen)
    denied = sorted(seen["table_functions"] - ALLOWED_TABLE_FUNCTIONS)
    if denied:
        raise ConsoleRejected(
            f"Table function not allowed here: {', '.join(denied)}."
        )
    scalars = sorted(seen["scalar_functions"] & DENIED_SCALAR_FUNCTIONS)
    if scalars:
        raise ConsoleRejected(f"Function not allowed: {', '.join(scalars)}.")
    for reference in seen["tables"]:
        catalog, schema, table = json.loads(reference)
        if PATH_LIKE_RE.search(table):
            raise ConsoleRejected("File paths are not allowed as tables.")
        if (
            catalog
            and catalog not in ALLOWED_CATALOGS
            and catalog not in ALLOWED_SCHEMAS
        ):
            raise ConsoleRejected(f"Catalog not allowed: {catalog}.")
        if catalog and catalog in ALLOWED_SCHEMAS:
            continue
        if not catalog and schema and schema not in ALLOWED_SCHEMAS:
            raise ConsoleRejected(
                f"Name tables in full, starting with lake: {schema}.{table}."
            )


def check_console_sql(conn: Any, sql: str) -> None:
    """Raise `ConsoleRejected` unless the console may run this statement."""
    import duckdb

    try:
        statements = conn.extract_statements(sql)
    except duckdb.Error as error:
        raise ConsoleRejected(str(error)[:400]) from error
    if len(statements) != 1:
        raise ConsoleRejected("One statement at a time.")
    kind = statements[0].type
    try:
        if kind == duckdb.StatementType.EXPLAIN:
            match = EXPLAIN_RE.match(sql)
            rest = match.group("rest") if match else ""
            if not EXPLAIN_BODY_RE.match(rest):
                raise ConsoleRejected("EXPLAIN takes a plain SELECT here.")
            check_select(conn, rest)
            return
        check_select(conn, sql)
    except duckdb.Error as error:
        raise ConsoleRejected(str(error)[:400]) from error


def run_console(request: dict[str, Any]) -> None:
    conn = open_lake(request)
    sql = request["sql"]
    try:
        check_console_sql(conn, sql)
    except ConsoleRejected as rejected:
        emit({"name": "console", "rejected": str(rejected)})
        return
    try:
        result = fetch(
            conn,
            sql,
            None,
            int(request.get("max_rows", 1000)),
            CONSOLE_MAX_CELL_CHARS,
            CONSOLE_MAX_OUTPUT_BYTES,
        )
    except Exception as error:
        emit({"name": "console", "error": str(error)[:600]})
    else:
        emit({"name": "console", **result})


def feed_entry_excerpt(body: bytes, needles: list[str], limit: int) -> bytes:
    """The `<item>` or `<entry>` element naming the item, from a feed body."""
    for needle in needles:
        if not needle:
            continue
        at = body.find(needle.encode("utf-8", "ignore"))
        if at < 0:
            # Feeds escape ampersands in links.
            at = body.find(
                needle.replace("&", "&amp;").encode("utf-8", "ignore")
            )
        if at < 0:
            continue
        starts = [body.rfind(tag, 0, at) for tag in (b"<item", b"<entry")]
        start = max(starts)
        if start < 0:
            start = max(0, at - limit // 4)
        ends = [body.find(tag, at) for tag in (b"</item>", b"</entry>")]
        found = [end for end in ends if end >= 0]
        end = (min(found) + 8) if found else at + limit // 2
        return body[start : min(end, start + limit)]
    return b""


def run_archive(request: dict[str, Any]) -> None:
    """Find one record in an archive file, reading from its start."""
    import hashlib
    from pathlib import Path

    from monitor.archive import read_archive

    path = Path(request["path"])
    wanted_key = request["key"]
    wanted_hash = request["sha256"]
    limit = int(request.get("limit", 4096))
    scanned = 0
    for header, body in read_archive(path):
        scanned += 1
        if header.get("key") != wanted_key or (
            header.get("sha256") != wanted_hash
        ):
            continue
        excerpt = feed_entry_excerpt(body, request.get("needles") or [], limit)
        located = bool(excerpt)
        if not excerpt:
            excerpt = body[:limit]
        emit(
            {
                "name": "record",
                "header": header,
                "scanned": scanned,
                "n_bytes": len(body),
                "sha256_ok": hashlib.sha256(body).hexdigest() == wanted_hash,
                "located": located,
                "excerpt": excerpt.decode("utf-8", "replace"),
            }
        )
        return
    emit({"name": "record", "missing": True, "scanned": scanned})


def run_agreement(request: dict[str, Any]) -> None:
    """Run the keyword classifier over a sample the model already classified.

    This is pure Python over local text. It makes no API calls and costs
    nothing, and it runs here so the regular expressions cannot compete with
    the web process for its interpreter lock.
    """
    import csv
    from pathlib import Path

    from monitor.ingest.sources import source_from_row
    from monitor.parsers.base import Item
    from monitor.parsers.classify_a import (
        classify_by_keywords,
        load_vocabulary,
    )

    config = Path(request["config_dir"])
    vocabulary = load_vocabulary(config)
    with (config / "sources.csv").open(
        newline="", encoding="utf-8"
    ) as csvfile:
        sources = {
            row["source_id"]: source_from_row(row)
            for row in csv.DictReader(csvfile)
        }
    conn = open_lake(request)
    result = fetch(conn, request["sql"], None, int(request["max_rows"]))
    columns = result["columns"]
    keyword_rows = []
    for values in result["rows"]:
        row = dict(zip(columns, values, strict=True))
        item = Item(
            item_id=row["item_id"],
            source_id=row["source_id"] or "",
            title=row["title"] or "",
            summary=row["summary"] or "",
            url=row["url"] or "",
            language=row["language"] or "en",
            feed_categories=list(row["feed_categories"] or []),
        )
        fields = classify_by_keywords(
            item, sources.get(item.source_id), vocabulary
        )
        keyword_rows.append(
            {
                "item_id": row["item_id"],
                "language": row["language"],
                "llm_relevant": row["election_relevant"],
                "kw_relevant": fields["election_relevant"],
                "llm_states": row["states"] or [],
                "kw_states": fields["states"],
                "llm_topics": row["topics"] or [],
                "kw_topics": fields["topics"],
                "llm_level": row["primary_level"],
                "kw_level": fields["primary_level"],
            }
        )
    emit({"name": "agreement", "rows": keyword_rows, "ms": result["ms"]})


MODES = {
    "scan": run_scan,
    "console": run_console,
    "archive": run_archive,
    "agreement": run_agreement,
}


def main() -> int:
    os.nice(NICE)
    request = json.load(sys.stdin)
    # The parent kills at its limit. This covers a parent that has gone away.
    signal.alarm(int(request.get("limit_seconds", 10)) + 5)
    try:
        MODES[request["mode"]](request)
    except Exception as error:
        emit({"name": "_fatal", "error": str(error)[:600]})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
