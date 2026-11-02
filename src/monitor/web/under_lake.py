"""What DuckLake is here, and what it stores, read from its own metadata.

The list of tables comes from the catalog, so a table added to `lake.TABLES`
appears with no change here. Scans of a table's rows run in a child process.
Everything else reads the catalog.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from monitor.web.methods_doc import diagram
from monitor.web.under import (
    Disclosed,
    Ran,
    Under,
    failure_lines,
    key_sql,
    table_key,
)
from monitor.web.under_map import relation_rows, table_map

CATALOG = "__ducklake_metadata_lake.main"
SAMPLE_ROWS = 3
EXCERPT_CHARS = 160
STATISTIC_CHARS = 40
LIST_VALUES_SHOWN = 3
SAFE_TABLE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
NUMBER_TYPE_RE = re.compile(r"^(u?int|float|decimal)")
TEXT_TYPES = {"varchar", "json", "blob"}
NOT_SAMPLED = "-- Not run, because the column list could not be read."

# Every table in the catalog, and what one row in it is. A test compares this
# list with a new catalog.
CATALOG_TABLES = (
    ("ducklake_metadata", "A lake setting, such as the inlining limit."),
    ("ducklake_snapshot", "One commit."),
    ("ducklake_snapshot_changes", "What one commit changed, as text."),
    ("ducklake_schema", "A schema, with the snapshots it was live in."),
    ("ducklake_schema_versions", "A commit that changed a table definition."),
    ("ducklake_table", "A table, with the snapshots it was live in."),
    (
        "ducklake_column",
        "A column. Each part of a nested type is a row tied by parent_column.",
    ),
    (
        "ducklake_data_file",
        "A Parquet file: its path, rows, size, and snapshots.",
    ),
    (
        "ducklake_file_column_stats",
        "One column in one file: size, values, nulls, minimum, maximum.",
    ),
    ("ducklake_table_stats", "One table: rows, next row id, and file size."),
    (
        "ducklake_table_column_stats",
        "One column across its table: nulls, minimum, maximum.",
    ),
    (
        "ducklake_inlined_data_tables",
        "The catalog table holding a table's inlined rows, per definition.",
    ),
    (
        "ducklake_files_scheduled_for_deletion",
        "A replaced file waiting for cleanup.",
    ),
    ("ducklake_delete_file", "A file marking rows as deleted."),
    ("ducklake_file_variant_stats", "Statistics for a VARIANT column."),
    ("ducklake_file_partition_value", "A file's value for a partition key."),
    ("ducklake_partition_info", "A table's partition definition."),
    ("ducklake_partition_column", "A column in a partition definition."),
    ("ducklake_sort_info", "A table's sort definition."),
    ("ducklake_sort_expression", "An expression in a sort definition."),
    ("ducklake_column_mapping", "A map for files written outside DuckLake."),
    ("ducklake_name_mapping", "A column name in such a map."),
    ("ducklake_tag", "A tag on a table or a view."),
    ("ducklake_column_tag", "A tag on a column."),
    ("ducklake_view", "A view."),
    ("ducklake_view_column_tag", "A tag on a view's column."),
    ("ducklake_macro", "A stored macro."),
    ("ducklake_macro_impl", "The body of a stored macro."),
    ("ducklake_macro_parameters", "A parameter of a stored macro."),
)

# Which stage writes each table. A test fails if `lake.TABLES` has a table
# missing here.
GOLD_SOURCE = "Rollups, from raw and nlp rows. No model."
TABLE_SOURCES = {
    "raw.fetches": "Feed poller, one row per poll. No model.",
    "raw.items": "Feed poller, an item's first sighting. No model.",
    "raw.sightings": "Feed poller. No model.",
    "raw.collection_gaps": "Pipeline's sleep and network watch. No model.",
    "raw.fetch_details": "Feed poller, for each 200 response. No model.",
    "raw.archive_index": "Feed poller and fetch_text. No model.",
    "raw.article_text": "fetch_text, with trafilatura. No model.",
    "nlp.parser_runs": "Every parser stage.",
    "nlp.lang": "lang, with the local lingua detector.",
    "nlp.classify_a": "classify_a: the bulk model, or keyword lists.",
    "nlp.extract_b": "extract_b: the bulk model.",
    "nlp.mentions": "extract_b, then the resolver and the entity pass.",
    "nlp.places": "extract_b, then the Census gazetteer.",
    "nlp.embeddings": "embed: the local embedding model.",
    "nlp.dup_groups": "dedupe: title SimHash and embedding cosine.",
    "nlp.storyline_assign": "storylines: cosine against centroids.",
    "nlp.storylines": "storylines. The bulk model writes labels.",
    "nlp.race_calls": "extract_b: the bulk model, then checks in code.",
    "nlp.briefs": "Brief writer: the brief model.",
    "nlp.brief_stats": "Brief writer: what the citation check kept.",
    "nlp.llm_calls": "The LLM client, one row per call.",
    "nlp.llm_prompts": "The LLM client, once per pipeline start.",
    "nlp.llm_traces": "The LLM client, one row per call.",
    "nlp.cluster_decisions": "dedupe and storylines.",
    "nlp.rejected_quotes": "extract_b: quotes its checks threw out.",
    "nlp.health": "The pipeline's health job. No model.",
    "nlp.maintenance_runs": "The maintenance and backup jobs. No model.",
    "ref.sources": "Reference sync, from config/sources.csv and ratings.",
    "ref.states": "Reference sync, from the state list and poll closings.",
    "ref.counties": "Reference sync, from the Census gazetteer.",
    "ref.places": "Reference sync, from the Census gazetteer.",
    "ref.entities": "Reference sync, and the entity pass with the bulk model.",
    "ref.races": "Reference sync, from config/races.yaml.",
    "gold.bin_counts": GOLD_SOURCE,
    "gold.storyline_bins": GOLD_SOURCE,
    "gold.entity_bins": GOLD_SOURCE,
    "gold.entity_lean_bins": GOLD_SOURCE,
    "gold.state_bins": GOLD_SOURCE,
    "gold.county_bins": GOLD_SOURCE,
    "gold.source_bins": GOLD_SOURCE,
    "gold.lean_storyline_bins": GOLD_SOURCE,
    "gold.comention_edges": GOLD_SOURCE,
    "gold.topic_bins": GOLD_SOURCE,
}
NO_SOURCE = "No source is listed for this table."


def catalog_counts_sql() -> str:
    return "\nUNION ALL\n".join(
        f"SELECT '{name}' AS catalog_table, count(*) AS n "
        f"FROM {CATALOG}.{name}"
        for name, _ in CATALOG_TABLES
    )


def catalog_rows(under: Under) -> dict[str, Any]:
    ran = under.catalog(catalog_counts_sql())
    counts = {row["catalog_table"]: row["n"] for row in ran.rows}
    return {
        "ran": ran,
        "rows": [
            {"name": name, "n": counts.get(name), "means": means}
            for name, means in CATALOG_TABLES
        ],
    }


def schema_tree(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Columns in order, each with its depth in a nested type."""
    parents = {row["column_id"]: row["parent_column"] for row in rows}

    def depth(column_id: Any) -> int:
        steps = 0
        parent = parents.get(column_id)
        while parent is not None and steps < 8:
            steps += 1
            parent = parents.get(parent)
        return steps

    return [{**row, "depth": depth(row["column_id"])} for row in rows]


def statistic(row: dict[str, Any], which: str) -> Any:
    """A column's minimum or maximum, in the column's own order.

    The catalog stores these as text, so "10" sorts before "9" unless the
    numeric form is used.
    """
    if NUMBER_TYPE_RE.match(row["column_type"]):
        return row.get(f"{which}_number")
    text = row.get(f"{which}_text")
    return text[:STATISTIC_CHARS] if isinstance(text, str) else text


def with_stats(
    columns: list[dict[str, Any]], stats: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_column = {row["column_id"]: row for row in stats}
    merged = []
    for column in schema_tree(columns):
        stat = by_column.get(column["column_id"], {})
        joined = {**column, **stat}
        merged.append(
            {
                **joined,
                "has_stats": bool(stat),
                "smallest": statistic(joined, "min"),
                "largest": statistic(joined, "max"),
            }
        )
    return merged


def sample_sql(table: str, columns: list[dict[str, Any]]) -> str:
    """Newest rows, with long text cut and lists shown by length.

    The cut happens in SQL, so full text never leaves the child process.
    """
    parts = []
    names = set()
    for column in columns:
        if column["parent_column"] is not None:
            continue
        name = column["column_name"]
        names.add(name)
        quoted = '"' + name.replace('"', '""') + '"'
        kind = column["column_type"]
        if kind in TEXT_TYPES:
            parts.append(
                f"left(CAST({quoted} AS VARCHAR), {EXCERPT_CHARS}) AS {quoted}"
            )
        elif kind == "list":
            parts.append(
                f"struct_pack(length := len({quoted}), "
                f"first := {quoted}[1:{LIST_VALUES_SHOWN}]) AS {quoted}"
            )
        else:
            parts.append(quoted)
    parts += ["snapshot_id AS _snapshot", "filename AS _file"]
    order = "ORDER BY created_at DESC " if "created_at" in names else ""
    body = ",\n       ".join(parts)
    return f"SELECT {body}\nFROM lake.{table}\n{order}LIMIT {SAMPLE_ROWS}"


def sample_cell(value: Any) -> str:
    """One stored value, as text a person can read on a projector."""
    if isinstance(value, dict) and "length" in value and "first" in value:
        if value["length"] is None:
            return "NULL"
        first = ", ".join(_short(part) for part in value["first"] or [])
        return f"{value['length']} values; first: {first}"
    return _short(value)


def _short(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.4g}"
    text = str(value)
    if len(text) > EXCERPT_CHARS:
        return text[:EXCERPT_CHARS]
    return text


def table_detail(under: Under, row: dict[str, Any]) -> dict[str, Any]:
    table = f"{row['schema_name']}.{row['table_name']}"
    binds = {"table_id": row["table_id"]}
    columns = under.catalog(
        "SELECT column_id, column_order, column_name, column_type, "
        f"nulls_allowed, parent_column FROM {CATALOG}.ducklake_column "
        "WHERE table_id = $table_id AND end_snapshot IS NULL "
        "ORDER BY column_order",
        binds,
    )
    stats = under.catalog(
        "SELECT st.column_id, sum(st.value_count) AS n_values, "
        "sum(st.null_count) AS n_nulls, "
        "sum(st.column_size_bytes) AS n_bytes, count(*) AS n_files, "
        "min(TRY_CAST(st.min_value AS DOUBLE)) AS min_number, "
        "max(TRY_CAST(st.max_value AS DOUBLE)) AS max_number, "
        f"left(min(st.min_value), {STATISTIC_CHARS}) AS min_text, "
        f"left(max(st.max_value), {STATISTIC_CHARS}) AS max_text "
        f"FROM {CATALOG}.ducklake_file_column_stats st "
        f"JOIN {CATALOG}.ducklake_data_file f USING (data_file_id, table_id) "
        "WHERE st.table_id = $table_id AND f.end_snapshot IS NULL "
        "GROUP BY st.column_id",
        binds,
    )
    keys_sql = (
        f"SELECT count(*) AS n_rows, count(DISTINCT {key_sql(table)}) "
        f"AS n_keys FROM lake.{table}"
    )
    if columns.ok:
        scans = under.scan(
            f"lake:{table}",
            [
                ("keys", keys_sql, None),
                ("sample", sample_sql(table, columns.rows), None),
            ],
        )
    else:
        reason = columns.error or "The column list could not be read."
        scans = {
            "keys": Ran([], Disclosed(keys_sql, "not run"), reason),
            "sample": Ran([], Disclosed(NOT_SAMPLED, "not run"), reason),
        }
    sample = scans["sample"]
    return {
        "table": table,
        "row": row,
        "source": TABLE_SOURCES.get(table, NO_SOURCE),
        "key": table_key(table)[0],
        "meaning": table_key(table)[1],
        "columns": columns,
        "stats": stats,
        "schema": with_stats(columns.rows, stats.rows),
        "has_lists": any(c["column_type"] == "list" for c in columns.rows),
        "key_scan": scans["keys"],
        "sample": sample,
        "sample_rows": [
            [
                (
                    name,
                    Path(str(value)).name
                    if name == "_file"
                    else sample_cell(value),
                )
                for name, value in entry.items()
            ]
            for entry in sample.rows
        ],
        "failures": failure_lines(
            {"schema": columns, "statistics": stats, **scans}
        ),
    }


def lake_view(under: Under, params: Any) -> dict[str, Any]:
    tables = under.catalog_tables()
    choices = {
        f"{row['schema_name']}.{row['table_name']}": row
        for row in tables.rows
        if SAFE_TABLE_RE.match(f"{row['schema_name']}.{row['table_name']}")
    }
    schemas: dict[str, list[dict[str, Any]]] = {}
    for name, row in choices.items():
        schemas.setdefault(row["schema_name"], []).append(
            {
                **row,
                "name": name,
                "source": TABLE_SOURCES.get(name, NO_SOURCE),
            }
        )
    wanted = params.get("table")
    chosen = wanted if wanted in choices else "raw.items"
    if chosen not in choices:
        chosen = next(iter(choices), "")
    catalog = catalog_rows(under)
    return {
        "table_map": table_map(),
        "relations": relation_rows(),
        "diagram": diagram("ducklake"),
        "catalog": catalog,
        "tables_query": tables.query,
        "tables_error": tables.error,
        "schemas": schemas,
        "n_tables": len(choices),
        "chosen": chosen,
        "detail": table_detail(under, choices[chosen]) if chosen else None,
    }
