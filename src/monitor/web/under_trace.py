"""One article followed through every stage, and record search.

Stored text is data. Templates escape everything shown here, and nothing read
from an article or a feed is acted on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from monitor.lake import hash_id
from monitor.web.filters import Filters
from monitor.web.panels import load_sql, safe_url
from monitor.web.under import (
    ARCHIVE_LIMIT_SECONDS,
    SLOW_SCAN_TTL_SECONDS,
    Disclosed,
    Ran,
    Under,
    failure_lines,
    parse_time,
    pretty_json,
    shorten_cell,
)

RESULTS_PER_PAGE = 100
FEED_MATCH_LIMIT = 50_000
TRACE_TTL_SECONDS = 45.0
INLINED_PREFIX = "ducklake_inlined_data"
# Archive names come from a table, so check their shape before using them.
ARCHIVE_NAME_RE = re.compile(
    r"^(feeds|pages)-\d{8}T\d{2}-\d{8}T\d{6}\.rec\.zst$"
)
ARCHIVE_EXCERPT_BYTES = 6000
ARTICLE_EXCERPT_CHARS = 600
BACKFILL_SECONDS = 600
BACKLOG_AGE = timedelta(hours=2)

# Stage name, table, parser in `nlp.parser_runs`, and columns to show.
STAGES = (
    ("Feed entry", "raw.items", "", "*"),
    ("Language", "nlp.lang", "lang", "*"),
    ("Classification", "nlp.classify_a", "classify_a", "*"),
    (
        "Embedding",
        "nlp.embeddings",
        "embed",
        "* EXCLUDE (vec), len(vec) AS vec_length, vec[1:4] AS vec_start",
    ),
    ("Dedupe", "nlp.dup_groups", "dedupe", "*"),
    ("Storyline", "nlp.storyline_assign", "storylines", "*"),
    (
        "Article text",
        "raw.article_text",
        "fetch_text",
        "* EXCLUDE (text), left(text, 300) AS text_start",
    ),
    ("Extraction", "nlp.extract_b", "extract_b", "*"),
    ("Entities", "nlp.mentions", "extract_b", "*"),
    ("Places", "nlp.places", "extract_b", "*"),
    ("Race calls", "nlp.race_calls", "extract_b", "*"),
)

STATUS_CLAUSES = {
    "relevant": "f.relevant",
    "not_relevant": "NOT f.relevant AND f.method IS NOT NULL",
    "canonical": "f.canonical AND f.dup_group_id IS NOT NULL",
    "copy": "NOT f.canonical",
    "in_storyline": "f.storyline_id IS NOT NULL",
    "no_storyline": "f.storyline_id IS NULL",
    "extracted": "f.tone IS NOT NULL",
    "not_extracted": "f.relevant AND f.tone IS NULL",
    "no_classification": "f.method IS NULL",
}
STATUS_LABELS = {
    "relevant": "Relevant",
    "not_relevant": "Not relevant",
    "canonical": "Canonical",
    "copy": "Copy of another item",
    "in_storyline": "In a storyline",
    "no_storyline": "No storyline",
    "extracted": "Extracted",
    "not_extracted": "Relevant, not extracted",
    "no_classification": "Not classified",
}


def item_statements(item_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    params = {"item_id": item_id}
    statements = [
        (
            table,
            f"SELECT {columns}, snapshot_id AS _snapshot, filename AS _file "
            f"FROM lake.{table} WHERE item_id = $item_id ORDER BY created_at",
            params,
        )
        for _, table, _, columns in STAGES
    ]
    in_traces = (
        "SELECT call_id FROM lake.nlp.llm_traces "
        "WHERE list_contains(item_ids, $item_id)"
    )
    return [
        *statements,
        (
            "parser_runs",
            "SELECT parser, parser_version, status, error, created_at "
            "FROM lake.nlp.parser_runs WHERE item_id = $item_id "
            "ORDER BY created_at",
            params,
        ),
        (
            "sightings",
            "SELECT source_id, feed_url, seen_at, title_hash "
            "FROM lake.raw.sightings WHERE item_id = $item_id "
            "ORDER BY seen_at",
            params,
        ),
        (
            "decisions",
            "SELECT stage, decision, method, nearest_id, nearest_similarity, "
            "threshold, simhash_distance, created_at "
            "FROM lake.nlp.cluster_decisions WHERE item_id = $item_id "
            "ORDER BY created_at",
            params,
        ),
        (
            "rejected",
            "SELECT kind, quote, reason, model, created_at "
            "FROM lake.nlp.rejected_quotes WHERE item_id = $item_id "
            "ORDER BY created_at",
            params,
        ),
        (
            "traces",
            "SELECT *, snapshot_id AS _snapshot, filename AS _file "
            "FROM lake.nlp.llm_traces "
            "WHERE list_contains(item_ids, $item_id) ORDER BY created_at",
            params,
        ),
        (
            "ledger",
            "SELECT call_id, purpose, model, input_tokens, cache_read_tokens, "
            "cache_write_tokens, output_tokens, usd_est, latency_ms, status "
            f"FROM lake.nlp.llm_calls WHERE call_id IN ({in_traces})",
            params,
        ),
        (
            "prompts",
            "SELECT prompt_hash, any_value(purpose) AS purpose, "
            "any_value(text) AS text FROM lake.nlp.llm_prompts "
            "WHERE prompt_hash IN (SELECT prompt_hash "
            "FROM lake.nlp.llm_traces "
            "WHERE list_contains(item_ids, $item_id)) GROUP BY 1",
            params,
        ),
        (
            "page_archive",
            "SELECT * FROM lake.raw.archive_index "
            "WHERE kind = 'pages' AND key = $item_id "
            "ORDER BY fetched_at DESC LIMIT 1",
            params,
        ),
        ("feed_archive", load_sql("under_trace_feed_archive"), params),
    ]


def file_label(name: Any) -> dict[str, str]:
    text = str(name or "")
    if not text:
        return {"kind": "", "name": ""}
    if text.startswith(INLINED_PREFIX):
        return {"kind": "inlined in the catalog", "name": text}
    return {"kind": "Parquet file", "name": Path(text).name}


def dup_reading(row: dict[str, Any]) -> str:
    method = row.get("method")
    if method == "url":
        return (
            "Older row. `url` here means no match was found. No URL was "
            "compared."
        )
    if method == "none":
        return "No match. This item is the canonical member of its own group."
    if method == "title_hash":
        return "Matched another item by title SimHash."
    if method == "embedding":
        return "Matched another item by embedding cosine, on the same day."
    return ""


def batch_parts(trace: dict[str, Any], item_id: str) -> dict[str, str]:
    """This item's slice of a batched request and response."""
    ids = trace.get("item_ids") or []
    if len(ids) < 2 or item_id not in ids:
        return {}
    index = ids.index(item_id)
    parts = {"index": str(index), "n": str(len(ids))}
    try:
        request = json.loads(trace.get("request_text") or "")
        parts["request"] = json.dumps(
            request[index], indent=2, ensure_ascii=False
        )
    except (ValueError, IndexError, TypeError, KeyError):
        pass
    try:
        response = json.loads(trace.get("response_text") or "")
        for entry in response.get("items", []):
            if str(entry.get("item_ref")) == str(index):
                parts["response"] = json.dumps(
                    entry, indent=2, ensure_ascii=False
                )
    except (ValueError, TypeError, AttributeError):
        pass
    return parts


def trim_article_text(request_text: Any) -> Any:
    """A stored request with its article text cut to an excerpt.

    The dashboard shows extracts and links to the original, never full text.
    """
    if not isinstance(request_text, str):
        return request_text
    try:
        request = json.loads(request_text)
    except ValueError:
        return request_text
    if not isinstance(request, dict):
        return request_text
    text = request.get("text")
    if not isinstance(text, str) or len(text) <= ARTICLE_EXCERPT_CHARS:
        return request_text
    hidden = len(text) - ARTICLE_EXCERPT_CHARS
    request["text"] = (
        f"{text[:ARTICLE_EXCERPT_CHARS]} [{hidden} more characters went to "
        "the model and are not shown here]"
    )
    return json.dumps(request, ensure_ascii=False)


def panel_checks(
    under: Under, item_id: str, filters: Filters
) -> list[dict[str, Any]]:
    """Run each panel's own filter on this one item.

    The clauses come from `Filters.where` with the options each panel uses, so
    the answer is the filter the panel applied, for the same controls.
    """
    sets = under.panels.focus_sets()

    def where(**options: Any) -> tuple[str, dict[str, Any]]:
        return filters.where(sets, **options)

    labeled = (
        "EXISTS (SELECT 1 FROM storylines s "
        "WHERE s.storyline_id = f.storyline_id "
        "AND coalesce(s.label, '') <> '')"
    )
    has_lean_cell = (
        "EXISTS (SELECT 1 FROM sources s WHERE s.source_id = f.source_id "
        "AND s.lean_label IN ('L', 'LL', 'C', 'LR', 'R', 'NA'))"
    )
    active = (
        "EXISTS (SELECT 1 FROM storylines s "
        "WHERE s.storyline_id = f.storyline_id AND s.status = 'active' "
        "AND coalesce(s.label, '') <> '')"
    )
    named = (
        "EXISTS (SELECT 1 FROM mentions m WHERE m.item_id = f.item_id "
        "AND m.entity_id IS NOT NULL)"
    )
    calls = (
        "EXISTS (SELECT 1 FROM race_calls rc WHERE rc.item_id = f.item_id "
        "AND rc.status IN ('projected', 'declared', 'conceded', 'leading', "
        "'too_close'))"
    )
    checks: list[tuple[str, tuple[str, dict[str, Any]], str, str]] = [
        (
            "Live Wire",
            where(windowed=False),
            "TRUE",
            "Relevant and canonical. Shows the newest 40.",
        ),
        (
            "Header: stories per minute",
            where(windowed=False),
            "f.first_seen_at >= $as_of - INTERVAL 15 MINUTE",
            "Relevant and canonical, in the last 15 minutes.",
        ),
        (
            "Rising Storylines",
            where(windowed=False),
            f"f.first_seen_at >= $as_of - INTERVAL 135 MINUTE AND {active}",
            "In an active, labeled storyline, in the last 135 minutes.",
        ),
        (
            "US Map",
            where(),
            "len(f.states) > 0",
            "Relevant, canonical, in the window, tagged with a state.",
        ),
        (
            "Race Calls in the News",
            where(windowed=False, unique=False, relevant=False),
            calls,
            "Has a race call row with a reported status. Copies count.",
        ),
        (
            "Storyline Stream",
            where(),
            "f.storyline_id IS NOT NULL",
            "In a storyline, in the window. The 10 largest labeled storylines "
            "are named. Every other storyline, labeled or not, counts in "
            "Other.",
        ),
        (
            "Topic Heat",
            where(windowed=False),
            "len(f.topics) > 0 "
            "AND f.first_seen_at >= $as_of - INTERVAL 3 HOUR",
            "Has a topic, in the last 3 hours. Only the 14 busiest show.",
        ),
        (
            "Entity Rank",
            where(),
            named,
            "Names a resolved entity, in the window. Only the top 12 show.",
        ),
        (
            "Tone by Outlet Lean",
            where(unique=False),
            f"NOT f.is_opinion AND {named}",
            "Names a resolved entity, is not opinion, in the window. Copies "
            "count. Only the 10 most named entities show.",
        ),
        (
            "Coverage by Lean",
            where(unique=False),
            f"{labeled} AND {has_lean_cell}",
            "In a labeled storyline, in the window, from a rated or Non-US "
            "outlet. An Unrated outlet's item has no cell. Copies count. Only "
            "the 12 largest storylines show.",
        ),
        (
            "Global Press",
            where(),
            "f.language <> 'en'",
            "Relevant, canonical, not English, in the window.",
        ),
    ]
    results = []
    for panel, (clause, params), extra, rule in checks:
        sql = (
            "SELECT count(*) AS n FROM facts f "
            f"WHERE f.item_id = $item_id AND {clause} AND {extra}"
        )
        ran = under.local(sql, {**params, "item_id": item_id})
        results.append(
            {
                "panel": panel,
                "rule": rule,
                "counts": bool(ran.first.get("n")),
                "query": ran.query,
            }
        )
    return results


def trace(
    under: Under, item_id: str, filters: Filters
) -> dict[str, Any] | None:
    fact = under.local(load_sql("article"), {"item_id": item_id})
    if not fact.rows:
        return None
    item = fact.first
    statements = item_statements(item_id)
    results = under.scan(
        f"trace:{item_id}", list(statements), ttl=TRACE_TTL_SECONDS
    )
    first_seen = item["seen_raw_at"]
    snapshots = _snapshot_times(under, results)
    runs = results["parser_runs"].rows
    capture_ran = under.capture_start()
    capture = under.capture_time()
    before_capture = bool(capture and first_seen and first_seen < capture)
    stages = []
    for name, table, parser, _ in STAGES:
        ran = results[table]
        stages.append(
            _stage(name, table, parser, ran, runs, first_seen, snapshots)
        )
    raw_rows = results["raw.items"].rows
    raw_row = raw_rows[0] if raw_rows else {}
    entry_text = raw_row.get("raw_entry")
    entry = _parse_json(entry_text)
    rich_entry = isinstance(entry, dict) and (
        "title" in entry or "summary" in entry
    )
    calls = _llm_calls(results, item_id)
    mentions = _mentions(under, results)
    dup_rows = [
        {**row, "reading": dup_reading(row)}
        for row in results["nlp.dup_groups"].rows
    ]
    story_rows = [
        {
            **row,
            "placeholder": row.get("similarity") == 1.0,
        }
        for row in results["nlp.storyline_assign"].rows
    ]
    group = []
    fact_queries = []
    if item.get("dup_group_id"):
        group_ran = under.local(
            "SELECT f.item_id, f.source_id, f.title, f.canonical, "
            "f.seen_raw_at FROM facts f WHERE f.dup_group_id = $group "
            "ORDER BY f.seen_raw_at LIMIT 12",
            {"group": item["dup_group_id"]},
        )
        group = group_ran.rows
        fact_queries.append(group_ran.query)
    storyline = None
    if item.get("storyline_id"):
        storyline_ran = under.local(
            "SELECT storyline_id, label, status, n_items, n_sources "
            "FROM storylines WHERE storyline_id = $id",
            {"id": item["storyline_id"]},
        )
        storyline = storyline_ran.first or None
        fact_queries.append(storyline_ran.query)
    briefs = under.local(
        "SELECT brief_id, created_at FROM briefs "
        "WHERE contains(citations, $item_id) ORDER BY created_at DESC",
        {"item_id": item_id},
    )
    moved = item["first_seen_at"] != item["seen_raw_at"]
    published = item.get("published_at")
    return {
        "item": {**item, "url": safe_url(item.get("url"))},
        "before_capture": before_capture,
        "capture_time": capture,
        "moved_on_axis": moved,
        "moved_for_backlog": moved
        and published is not None
        and published < first_seen - BACKLOG_AGE,
        "stages": stages,
        "raw_ran": results["raw.items"],
        "raw_row": raw_row,
        "entry": pretty_json(entry_text),
        "rich_entry": rich_entry,
        "sightings": results["sightings"],
        "calls": calls,
        "traces_ran": results["traces"],
        "mentions": mentions,
        "mentions_ran": results["nlp.mentions"],
        "places": results["nlp.places"].rows,
        "places_ran": results["nlp.places"],
        "rejected": results["rejected"].rows,
        "dup_rows": dup_rows,
        "dup_ran": results["nlp.dup_groups"],
        "story_rows": story_rows,
        "story_ran": results["nlp.storyline_assign"],
        "decisions": results["decisions"].rows,
        "decisions_ran": results["decisions"],
        "group": group,
        "storyline": storyline,
        "briefs": briefs,
        "race_calls": results["nlp.race_calls"].rows,
        "panels": panel_checks(under, item_id, filters),
        "window": filters.window,
        "page_archive": results["page_archive"],
        "feed_archive": results["feed_archive"],
        "stage_queries": [fact.query, capture_ran.query]
        + [results[table].query for _, table, _, _ in STAGES]
        + [results["parser_runs"].query, snapshots["query"]],
        "feed_queries": [
            results["raw.items"].query,
            results["sightings"].query,
            results["feed_archive"].query,
        ],
        "llm_queries": [
            results["traces"].query,
            results["ledger"].query,
            results["prompts"].query,
        ],
        "resolution_queries": [
            results["nlp.mentions"].query,
            results["nlp.places"].query,
            results["rejected"].query,
            mentions["query"],
        ],
        "decision_queries": [
            results["nlp.dup_groups"].query,
            results["nlp.storyline_assign"].query,
            results["decisions"].query,
            *fact_queries,
        ],
        "errors": failure_lines(
            {
                **results,
                "capture start": capture_ran,
                **snapshots["ran"],
            }
        ),
    }


def _parse_json(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return None


def _snapshot_times(under: Under, results: dict[str, Ran]) -> dict[str, Any]:
    ids = sorted(
        {
            row["_snapshot"]
            for _, table, _, _ in STAGES
            for row in results[table].rows
            if row.get("_snapshot") is not None
        }
    )
    if not ids:
        return {
            "times": {},
            "ran": {},
            "query": Disclosed(
                "-- No stage rows, so no snapshot times were read.", "none"
            ),
        }
    ran = under.catalog(
        "SELECT snapshot_id, snapshot_time FROM lake.snapshots() "
        "WHERE snapshot_id IN (SELECT unnest($ids))",
        {"ids": ids},
    )
    return {
        "times": {
            row["snapshot_id"]: row["snapshot_time"] for row in ran.rows
        },
        "ran": {"snapshot times": ran},
        "query": ran.query,
    }


def _stage(
    name: str,
    table: str,
    parser: str,
    ran: Ran,
    runs: list[dict[str, Any]],
    first_seen: Any,
    snapshots: dict[str, Any],
) -> dict[str, Any]:
    own_runs = [run for run in runs if run["parser"] == parser]
    last_run = own_runs[-1] if own_runs else {}
    rows = ran.rows
    row = rows[-1] if rows else {}
    created = parse_time(row.get("created_at"))
    latency = (created - first_seen).total_seconds() if created else None
    snapshot = row.get("_snapshot")
    committed = snapshots["times"].get(snapshot)
    tidy = [
        {
            key: shorten_cell(value, 4000)
            for key, value in entry.items()
            if key not in {"_snapshot", "_file"}
        }
        for entry in rows
    ]
    for entry in tidy:
        for key, value in entry.items():
            if key in {"raw", "raw_entry"} and isinstance(value, str):
                entry[key] = _parse_json(value) or value
    return {
        "name": name,
        "table": table,
        "n": len(rows),
        "status": last_run.get("status") or ("stored" if rows else ""),
        "run_error": last_run.get("error"),
        "version": last_run.get("parser_version"),
        "created": created,
        "latency": latency,
        "backfill": latency is not None and latency > BACKFILL_SECONDS,
        "snapshot": snapshot,
        "committed": committed,
        "commit_latency": (committed - first_seen).total_seconds()
        if committed
        else None,
        "file": file_label(row.get("_file")),
        "error": ran.error,
        "json": json.dumps(tidy, indent=2, ensure_ascii=False, default=str),
    }


def _llm_calls(results: dict[str, Ran], item_id: str) -> list[dict[str, Any]]:
    ledger = {row["call_id"]: row for row in results["ledger"].rows}
    prompts = {row["prompt_hash"]: row for row in results["prompts"].rows}
    calls = []
    for row in results["traces"].rows:
        calls.append(
            {
                **row,
                "ledger": ledger.get(row["call_id"], {}),
                "prompt": prompts.get(row["prompt_hash"], {}),
                "request": pretty_json(
                    trim_article_text(row.get("request_text"))
                ),
                "response": pretty_json(row.get("response_text")),
                "parts": batch_parts(row, item_id),
                "file": file_label(row.get("_file")),
            }
        )
    return calls


def _mentions(under: Under, results: dict[str, Ran]) -> dict[str, Any]:
    rows = results["nlp.mentions"].rows
    ids = sorted({r["entity_id"] for r in rows if r.get("entity_id")})
    names = under.local(
        "SELECT entity_id, canonical_name, party, office, origin "
        "FROM entities WHERE entity_id IN (SELECT unnest($ids))",
        {"ids": ids},
    )
    by_id = {row["entity_id"]: row for row in names.rows}
    return {
        "rows": [
            {**row, "entity": by_id.get(row.get("entity_id"), {})}
            for row in rows
        ],
        "query": names.query,
    }


def archive_record(under: Under, item_id: str, kind: str) -> dict[str, Any]:
    """One original payload from a local archive file, or why it is not here.

    Archive files are one compressed stream per hour, so finding a record means
    reading from the start of the file. That runs in a child process with a
    time limit. Files offloaded to S3 are not fetched.
    """
    fact = under.local(load_sql("article"), {"item_id": item_id})
    if not fact.rows or kind not in {"feeds", "pages"}:
        return {"status": "no_item", "kind": kind}
    results = under.scan(
        f"trace:{item_id}",
        list(item_statements(item_id)),
        ttl=TRACE_TTL_SECONDS,
    )
    name = "feed_archive" if kind == "feeds" else "page_archive"
    index = results[name].first
    base = {
        "item_id": item_id,
        "kind": kind,
        "query": results[name].query,
    }
    # The feed search needs the item's row as well as the index row.
    needed = [results[name]]
    if kind == "feeds":
        needed.append(results["raw.items"])
    failed = next((ran.error for ran in needed if ran.error), None)
    if failed:
        return {**base, "status": "scan_failed", "error": failed}
    if not index:
        return {**base, "status": "no_index"}
    archive_file = str(index.get("archive_file") or "")
    if not ARCHIVE_NAME_RE.match(archive_file):
        return {**base, "status": "bad_name", "archive_file": archive_file}
    folder = under.settings.paths.archive.resolve() / kind
    path = folder / archive_file
    open_file = folder / f"{archive_file}.part"
    chosen = path if path.exists() else open_file
    base = {
        **base,
        "index": index,
        "archive_file": archive_file,
        "still_open": chosen == open_file,
    }
    if not chosen.exists():
        return {**base, "status": "gone"}
    raw_rows = results["raw.items"].rows
    raw_row = raw_rows[0] if raw_rows else {}
    needles = (
        [raw_row.get("guid"), raw_row.get("url"), raw_row.get("canonical_url")]
        if kind == "feeds"
        else []
    )
    outcome = under.worker_call(
        ("archive", kind, item_id, index["sha256"]),
        {
            "mode": "archive",
            "path": str(chosen),
            "key": index["key"],
            "sha256": index["sha256"],
            "limit": ARCHIVE_EXCERPT_BYTES,
            "needles": [needle for needle in needles if needle],
        },
        ARCHIVE_LIMIT_SECONDS,
        SLOW_SCAN_TTL_SECONDS,
    )
    records = {record.get("name"): record for record in outcome.records}
    record = records.get("record")
    if outcome.timed_out:
        return {**base, "status": "timeout", "limit": ARCHIVE_LIMIT_SECONDS}
    if record is None:
        reason = (
            records.get("_fatal", {}).get("error")
            or outcome.failed
            or outcome.stderr
        )
        return {**base, "status": "error", "error": reason}
    if record.get("missing"):
        return {**base, "status": "not_flushed", "scanned": record["scanned"]}
    return {
        **base,
        "status": "found",
        "record": record,
        "seconds": outcome.seconds,
        "limit_bytes": ARCHIVE_EXCERPT_BYTES,
    }


def trace_candidates(under: Under) -> dict[str, Any]:
    """Recent items worth tracing: an extraction trace and an archived page."""
    ran = under.scan(
        "trace_candidates",
        [("candidates", load_sql("under_trace_candidates"), None)],
    )["candidates"]
    ids = [row["item_id"] for row in ran.rows]
    items = under.local(
        "SELECT f.item_id, f.title, f.headline, f.seen_raw_at, f.language, "
        "coalesce(s.name, f.source_id) AS outlet FROM facts f "
        "LEFT JOIN sources s USING (source_id) "
        "WHERE f.item_id IN (SELECT unnest($ids)) "
        "ORDER BY f.seen_raw_at DESC",
        {"ids": ids},
    )
    return {
        "items": items.rows,
        "scanned": ran.ok,
        "queries": [ran.query, items.query],
        "error": ran.error,
    }


@dataclass
class RecordQuery:
    item_id: str = ""
    url: str = ""
    outlet: str = ""
    feed: str = ""
    language: str = ""
    method: str = ""
    status: str = ""
    run: str = ""
    text: str = ""
    page: int = 0

    @classmethod
    def parse(cls, params: Any) -> RecordQuery:
        def value(name: str) -> str:
            return (params.get(name) or "").strip()[:300]

        try:
            page = max(0, int(params.get("page") or 0))
        except ValueError:
            page = 0
        status = value("status")
        return cls(
            item_id=value("id"),
            url=value("url"),
            outlet=value("outlet"),
            feed=value("feed"),
            language=value("language"),
            method=value("method"),
            status=status if status in STATUS_CLAUSES else "",
            run=value("run"),
            text=value("text"),
            page=page,
        )

    def pairs(self) -> dict[str, str]:
        pairs = {
            "id": self.item_id,
            "url": self.url,
            "outlet": self.outlet,
            "feed": self.feed,
            "language": self.language,
            "method": self.method,
            "status": self.status,
            "run": self.run,
            "text": self.text,
        }
        return {key: value for key, value in pairs.items() if value}


def _ids_from_scan(
    under: Under, label: str, sql: str, params: dict[str, Any]
) -> Ran:
    key = hash_id(label, sql, json.dumps(params, sort_keys=True))
    return under.scan(
        f"records:{label}:{key}",
        [("ids", sql, params)],
        max_rows=FEED_MATCH_LIMIT,
    )["ids"]


def records(under: Under, params: Any) -> dict[str, Any]:
    """Filter the fact table and show stored values, not rendered ones."""
    query = RecordQuery.parse(params)
    clauses = ["TRUE"]
    binds: dict[str, Any] = {}
    scans: list[Ran] = []
    errors: list[str] = []
    if query.item_id:
        clauses.append("starts_with(f.item_id, $id)")
        binds["id"] = query.item_id
    if query.url:
        clauses.append("contains(lower(f.url), $url)")
        binds["url"] = query.url.lower()
    if query.outlet:
        clauses.append("f.source_id = $outlet")
        binds["outlet"] = query.outlet
    if query.language:
        clauses.append("f.language = $language")
        binds["language"] = query.language
    if query.method == "none":
        clauses.append("f.method IS NULL")
    elif query.method:
        clauses.append("f.method = $method")
        binds["method"] = query.method
    if query.status:
        clauses.append(STATUS_CLAUSES[query.status])
    if query.text:
        clauses.append(
            "(contains(lower(f.title), $text) "
            "OR contains(lower(f.headline), $text) "
            "OR contains(lower(f.summary), $text))"
        )
        binds["text"] = query.text.lower()
    if query.feed:
        ran = _ids_from_scan(
            under,
            "feed",
            "SELECT item_id FROM lake.raw.items "
            "WHERE contains(lower(feed_url), $feed)",
            {"feed": query.feed.lower()},
        )
        scans.append(ran)
        if ran.error:
            errors.append(ran.error)
        clauses.append("f.item_id IN (SELECT unnest($feed_ids))")
        binds["feed_ids"] = [row["item_id"] for row in ran.rows]
    run_parts = query.run.split(":") if query.run else []
    if len(run_parts) == 2:
        ran = _ids_from_scan(
            under,
            "run",
            "SELECT item_id FROM (SELECT item_id, status "
            "FROM lake.nlp.parser_runs WHERE parser = $parser "
            "QUALIFY row_number() OVER (PARTITION BY item_id "
            "ORDER BY created_at DESC) = 1) WHERE status = $status",
            {"parser": run_parts[0], "status": run_parts[1]},
        )
        scans.append(ran)
        if ran.error:
            errors.append(ran.error)
        clauses.append("f.item_id IN (SELECT unnest($run_ids))")
        binds["run_ids"] = [row["item_id"] for row in ran.rows]
    capped = any(ran.truncated for ran in scans)
    where = " AND ".join(clauses)
    total = under.local(
        f"SELECT count(*) AS n FROM facts f WHERE {where}", binds
    )
    rows = under.local(
        "SELECT f.item_id, f.source_id, coalesce(s.name, f.source_id) AS "
        "outlet, f.language, f.title, f.url, f.seen_raw_at, f.first_seen_at, "
        "f.published_at, f.relevant, f.relevance_conf, f.method, f.canonical, "
        "f.dup_group_id, f.storyline_id, f.similarity, f.tone, f.states "
        "FROM facts f LEFT JOIN sources s USING (source_id) "
        f"WHERE {where} ORDER BY f.seen_raw_at DESC, f.item_id "
        f"LIMIT {RESULTS_PER_PAGE} OFFSET {query.page * RESULTS_PER_PAGE}",
        binds,
    )
    shown = [row["item_id"] for row in rows.rows]
    feeds_ran = under.scan(
        f"records_feeds:{hash_id(*shown)}",
        [
            (
                "feeds",
                "SELECT item_id, feed_url FROM lake.raw.items "
                "WHERE item_id IN (SELECT unnest($ids))",
                {"ids": shown},
            )
        ],
    )["feeds"]
    feeds = {row["item_id"]: row["feed_url"] for row in feeds_ran.rows}
    outlets = under.local("SELECT source_id, name FROM sources ORDER BY name")
    languages = under.local(
        "SELECT language, count(*) AS n FROM facts GROUP BY 1 ORDER BY n DESC"
    )
    methods = under.local(
        "SELECT coalesce(method, 'none') AS method, count(*) AS n "
        "FROM facts GROUP BY 1 ORDER BY n DESC"
    )
    run_choices = under.scan(
        "record_run_choices",
        [
            (
                "runs",
                "SELECT parser, status, count(*) AS n FROM (SELECT parser, "
                "status FROM lake.nlp.parser_runs QUALIFY row_number() OVER "
                "(PARTITION BY item_id, parser ORDER BY created_at DESC) = 1) "
                "GROUP BY ALL ORDER BY parser, status",
                None,
            )
        ],
    )["runs"]
    n = total.first.get("n", 0)
    return {
        "query": query,
        "pairs": query.pairs(),
        "rows": [
            {
                **row,
                "feed_url": feeds.get(row["item_id"]),
                "moved": row["first_seen_at"] != row["seen_raw_at"],
            }
            for row in rows.rows
        ],
        "total": n,
        "page": query.page,
        "per_page": RESULTS_PER_PAGE,
        "has_next": (query.page + 1) * RESULTS_PER_PAGE < n,
        "outlets": outlets.rows,
        "languages": languages.rows,
        "methods": methods.rows,
        "status_labels": STATUS_LABELS,
        "run_choices": run_choices.rows,
        "errors": errors
        + failure_lines({"feed URLs": feeds_ran, "parser runs": run_choices}),
        "incomplete": bool(errors),
        "capped": capped,
        "id_limit": FEED_MATCH_LIMIT,
        "queries": [
            *[ran.query for ran in scans],
            total.query,
            rows.query,
            feeds_ran.query,
            outlets.query,
            languages.query,
            methods.query,
            run_choices.query,
        ],
    }
