"""Data for the under-the-hood teaching views. Read-only against the lake.

Three kinds of query run here, and every view says which it used:

- local: the web process's in-memory tables, the same ones the dashboard reads.
- catalog: the DuckLake catalog on this module's own read-only connection. No
  data file is opened, so these stay cheap as the lake grows.
- child: anything that opens a data file runs in a short-lived child process
  at low priority, killed at a time limit, so it cannot stall the dashboard.

Every disclosed SQL string is the string that ran. Queries here do not go
through `ReadModel.query`, whose timings feed the dashboard's slow-mode
decision.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
from jinja2 import Undefined

from monitor.lake import NO_INLINE, TABLES, connect
from monitor.llm.budget import estimate_usd
from monitor.logs import get
from monitor.settings import Settings
from monitor.web.filters import Filters
from monitor.web.panels import Panels, load_sql
from monitor.web.readmodel import ReadModel
from monitor.web.reference import (
    STALE_STATUS_SECONDS,
    Reference,
    feed_state,
)

log = get(__name__)

ET = ZoneInfo("America/New_York")
WORKER = Path(__file__).with_name("under_worker.py")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
CONSOLE_MAX_RSS_KB = 2_000_000
MEMORY_WATCH_SECONDS = 0.25
CHILD_LIMIT_SECONDS = 10.0
ARCHIVE_LIMIT_SECONDS = 15.0
AGREEMENT_LIMIT_SECONDS = 30.0
SCAN_TTL_SECONDS = 120.0
SLOW_SCAN_TTL_SECONDS = 600.0
GATE_BUSY = "Another scan is running."
CACHE_SIZE = 256
CACHE_MAX_ROWS = 5000
WORKING_WITHIN_SECONDS = 3600.0
CONSOLE_MAX_ROWS = 1000
AGREEMENT_SAMPLE = 2000
READ_RETRIES = 3

LOCAL = "local tables in the web process"
CATALOG = "catalog only, no data file opened"
CHILD = (
    "child process: low priority, 2 threads, 1 GB, "
    f"stopped at {CHILD_LIMIT_SECONDS:.0f} s"
)

# What identifies one record in each table, and what a row means.
TABLE_KEYS: dict[str, tuple[str, str]] = {
    "raw.fetches": ("fetch_id", "One poll of one feed."),
    "raw.items": ("item_id", "One article URL, first time seen."),
    "raw.sightings": (
        "item_id, feed_url, title_hash",
        "A feed served an item, or changed its title.",
    ),
    "raw.collection_gaps": (
        "gap_start",
        "One period with no collection, from sleep or network loss.",
    ),
    "raw.fetch_details": ("fetch_id", "Payload details of a 200 response."),
    "raw.archive_index": (
        "kind, key, sha256",
        "Where original bytes sit in an archive file.",
    ),
    "raw.article_text": ("item_id", "One fetch attempt of article text."),
    "nlp.parser_runs": (
        "item_id, parser, parser_version",
        "A parser finished, or stopped, on one item.",
    ),
    "nlp.lang": ("item_id", "Detected language of one item."),
    "nlp.classify_a": ("item_id", "One classification of one item."),
    "nlp.extract_b": ("item_id", "One extraction of one item."),
    "nlp.mentions": ("mention_id", "One entity named in one item."),
    "nlp.places": ("place_id", "One place named in one item."),
    "nlp.embeddings": ("item_id", "One vector for one item."),
    "nlp.dup_groups": ("item_id", "One item's duplicate group."),
    "nlp.storyline_assign": ("item_id", "One item placed in a storyline."),
    "nlp.storylines": (
        "storyline_id",
        "A version of a storyline, written when it grows or is relabeled.",
    ),
    "nlp.race_calls": ("call_id", "One claim of a race result in the news."),
    "nlp.briefs": ("brief_id", "One written brief."),
    "nlp.llm_calls": ("call_id", "One API call, in the ledger."),
    "nlp.llm_prompts": (
        "prompt_hash",
        "A system prompt, written once per pipeline start.",
    ),
    "nlp.llm_traces": ("call_id", "Exact request and response of one call."),
    "nlp.cluster_decisions": (
        "item_id, stage",
        "A dedupe or storyline decision for one item.",
    ),
    "nlp.rejected_quotes": (
        "item_id, quote",
        "A quote the citation check threw out.",
    ),
    "nlp.brief_stats": ("brief_id", "Sentences kept and dropped in a brief."),
    "nlp.health": ("recorded_at", "One sample of pipeline health."),
    "nlp.maintenance_runs": ("recorded_at, kind", "One maintenance run."),
    "ref.sources": ("source_id", "A source's settings as of one sync."),
    "ref.states": ("usps", "A state's settings as of one sync."),
    "ref.counties": ("fips", "One county."),
    "ref.places": ("geoid", "One place in the gazetteer."),
    "ref.entities": ("entity_id", "An entity as of one write."),
    "ref.races": ("race_key", "A race as of one sync."),
    "gold.bin_counts": ("bin_start, level", "A level's 5-minute count."),
    "gold.storyline_bins": (
        "bin_start, storyline_id",
        "A storyline's 5-minute count.",
    ),
    "gold.entity_bins": (
        "bin_start, entity_id",
        "An entity's 5-minute count.",
    ),
    "gold.entity_lean_bins": (
        "bin_start, entity_id, lean_label",
        "An entity's 5-minute count per lean.",
    ),
    "gold.state_bins": ("bin_start, state_usps", "A state's 5-minute count."),
    "gold.county_bins": (
        "bin_start, county_fips",
        "A county's 5-minute count.",
    ),
    "gold.source_bins": (
        "bin_start, source_id",
        "An outlet's 5-minute count.",
    ),
    "gold.lean_storyline_bins": (
        "bin_start, storyline_id, lean_label",
        "A storyline's 5-minute count per lean.",
    ),
    "gold.comention_edges": (
        "bin_start, entity_a, entity_b",
        "Two entities named together, per 5 minutes.",
    ),
    "gold.topic_bins": ("bin_start, topic", "A topic's 5-minute count."),
}

# Each table with extracted values: the columns, and what produced them.
PRODUCERS: tuple[dict[str, Any], ...] = (
    {
        "table": "nlp.lang",
        "columns": "language, confidence",
        "tool": "lingua, a local library. No API call.",
        "model_note": "lingua, local",
        "group": "parser, parser_version, method, NULL AS model",
    },
    {
        "table": "nlp.classify_a",
        "columns": (
            "election_relevant, relevance_conf, us_politics, levels, "
            "primary_level, states, race_types, topics, free_tags, "
            "is_results_report, is_opinion, headline_en, summary_en"
        ),
        "tool": "Claude, or keyword lists when the budget or API forces it.",
        "group": "parser, parser_version, method, NULL AS model",
    },
    {
        "table": "nlp.embeddings",
        "columns": "vec, dim",
        "tool": "Local ONNX model on the CPU. No API call.",
        "group": "parser, parser_version, text_basis AS method, model",
    },
    {
        "table": "nlp.dup_groups",
        "columns": "dup_group_id, is_canonical, similarity",
        "tool": "Title SimHash and embedding cosine. No API call.",
        "model_note": "no model",
        "group": "parser, parser_version, method, NULL AS model",
    },
    {
        "table": "nlp.storyline_assign",
        "columns": "storyline_id, similarity",
        "tool": "Cosine against storyline centroids. No API call.",
        "model_note": "no model",
        "group": (
            "parser, parser_version, "
            "CASE WHEN similarity = 1.0 THEN 'started a storyline' "
            "ELSE 'joined a storyline' END AS method, NULL AS model"
        ),
    },
    {
        "table": "nlp.storylines",
        "columns": "label, summary, citations, keywords, centroid",
        "tool": "Claude writes label and summary; the rest is computed.",
        "model_note": "not stored on this table",
        "group": "parser, parser_version, status AS method, NULL AS model",
    },
    {
        "table": "nlp.extract_b",
        "columns": "summary_en, article_tone, topics_refined, raw",
        "tool": "Claude, from article text or the feed summary.",
        "group": "parser, parser_version, text_basis AS method, model",
    },
    {
        "table": "nlp.mentions",
        "columns": "surface, ent_type, role, tone, tone_evidence, entity_id",
        "tool": "Claude finds names; `resolved_by` says how each was matched.",
        "model_note": "on the item's nlp.extract_b row",
        "group": (
            "parser, parser_version, "
            "coalesce(resolved_by, 'not resolved') AS method, NULL AS model"
        ),
    },
    {
        "table": "nlp.places",
        "columns": "surface, state_usps, county_fips, place_geoid, cd_code",
        "tool": "Claude finds names; the Census gazetteer resolves them.",
        "model_note": "on the item's nlp.extract_b row",
        "group": "parser, parser_version, method, NULL AS model",
    },
    {
        "table": "nlp.race_calls",
        "columns": "race_key, winner_name, called_by, status, evidence",
        "tool": "Claude, requiring explicit language in the text.",
        "model_note": "on the item's nlp.extract_b row",
        "group": "parser, parser_version, status AS method, NULL AS model",
    },
    {
        "table": "nlp.briefs",
        "columns": "markdown, citations",
        "tool": "Claude. Sentences without a valid citation are removed.",
        "group": "'briefs' AS parser, NULL AS parser_version, "
        "focus AS method, model",
    },
)

SCHEMA_PURPOSES = (
    ("classify_a", "BatchA", "nlp.classify_a"),
    ("extract_b", "ItemB", "nlp.extract_b, nlp.mentions, nlp.places"),
    ("storylines", "StorylineLabel", "nlp.storylines"),
    ("entities", "EntityResolutionBatch", "ref.entities"),
    ("aliases", "AliasBatch", "ref.entities"),
    ("briefs", "Brief", "nlp.briefs"),
)

# Why rows outnumber keys, where the code settles the reason.
EXTRA_ROWS_WHY = {
    "nlp.storyline_assign": "Items were reassigned when the storyline parser "
    "changed version. Each version's assignment is a row.",
    "nlp.storylines": "Each growth or relabel writes a new version.",
    "nlp.llm_prompts": "Each pipeline start writes its prompts again.",
    "ref.sources": "A reference sync appends a row when a source changes.",
    "ref.states": "A reference sync appends a row when a state changes.",
    "raw.sightings": "A key repeats when a feed returns to an earlier title.",
    "raw.archive_index": "A key repeats when a feed returns to an earlier "
    "body.",
}

# Purpose in the ledger, label, column of `under_costs_items`, and unit.
COST_STAGES = (
    ("classify_a", "Classification", "classified_llm", "item"),
    ("extract_b", "Extraction", "extracted", "item"),
    ("storylines", "Storyline labels", "labeled", "labeled storyline"),
    ("entities", "Entity resolution", "mentions", "mention"),
    ("aliases", "Entity aliases", "entities", "entity"),
    ("briefs", "Briefs", "briefs", "brief"),
)

CAPTURE_TABLES = (
    "raw.sightings",
    "raw.fetch_details",
    "raw.archive_index",
    "nlp.llm_traces",
    "nlp.cluster_decisions",
)


@dataclass
class Disclosed:
    """A query as it ran, for the Show query component."""

    sql: str
    where: str
    params: dict[str, Any] | None = None
    ms: int | None = None
    rows: int | None = None
    note: str = ""

    @property
    def params_text(self) -> str:
        if not self.params:
            return ""
        shown = {
            key: (
                f"[{len(value)} values]"
                if isinstance(value, list | tuple) and len(value) > 20
                else value
            )
            for key, value in self.params.items()
        }
        return json.dumps(shown, default=str)


@dataclass
class Ran:
    rows: list[dict[str, Any]]
    query: Disclosed
    error: str | None = None
    truncated: bool = False
    age_seconds: float | None = None
    scanned_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def first(self) -> dict[str, Any]:
        return self.rows[0] if self.rows else {}


@dataclass
class ChildOutcome:
    records: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    seconds: float = 0.0
    stderr: str = ""
    failed: str | None = None


def human_bytes(value: float | int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return (
                f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            )
        size /= 1024
    return f"{size:.1f} GB"


def thousands(value: Any) -> str:
    if value is None or isinstance(value, Undefined):
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def percent(value: Any, places: int = 1) -> str:
    if value is None or isinstance(value, Undefined):
        return "—"
    return f"{value * 100:.{places}f}%"


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def et_label(value: Any) -> str:
    """A stored time in Eastern time, with the date."""
    parsed = parse_time(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(ET).strftime("%b %-d, %-I:%M:%S %p ET")


def seconds_label(value: Any) -> str:
    if value is None or isinstance(value, Undefined):
        return "—"
    if abs(value) < 120:
        return f"{value:.1f} s"
    if abs(value) < 7200:
        return f"{value / 60:.1f} min"
    if abs(value) < 172800:
        return f"{value / 3600:.1f} h"
    return f"{value / 86400:.1f} d"


def register_filters(env: Any) -> None:
    env.filters["u_bytes"] = human_bytes
    env.filters["u_int"] = thousands
    env.filters["u_pct"] = percent
    env.filters["u_et"] = et_label
    env.filters["u_secs"] = seconds_label


def quoted(text: str) -> str:
    """A SQL string literal, for catalog paths and checked names only."""
    return "'" + text.replace("'", "''") + "'"


def split_table(table: str) -> tuple[str, str]:
    schema, name = table.split(".", 1)
    return schema, name


def checked_table(table: str | None, default: str = "raw.items") -> str:
    """A table in `lake.TABLES`. Anything else falls back to `default`."""
    return table if table in TABLES else default


def table_key(table: str) -> tuple[str, str]:
    """The key columns and row meaning. A table added later has neither."""
    return TABLE_KEYS.get(
        table, ("", "No record key is listed for this table.")
    )


def key_sql(table: str) -> str:
    """The distinct-key expression, or NULL when no key is listed."""
    columns = table_key(table)[0]
    if not columns:
        return "NULL"
    return f"({columns})" if "," in columns else columns


def shorten_cell(value: Any, limit: int = 600) -> Any:
    """A readable stored value. Long lists show how many they hold."""
    if isinstance(value, list) and len(value) > 8:
        head = ", ".join(str(part)[:12] for part in value[:6])
        return f"[{head}, ...] ({len(value)} values)"
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... ({len(value):,} characters)"
    return value


def pretty_json(text: Any) -> str:
    """Stored JSON, indented. Text that is not JSON comes back unchanged."""
    if text is None:
        return ""
    if not isinstance(text, str):
        return json.dumps(text, indent=2, ensure_ascii=False, default=str)
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


def child_env() -> dict[str, str]:
    """Only what DuckDB needs to find its extensions. No keys."""
    return {
        "HOME": os.environ.get("HOME", ""),
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
        "LANG": "en_US.UTF-8",
    }


def _sbpl(path: Path | str) -> str:
    """A path quoted for a sandbox profile, with symlinks resolved."""
    text = str(Path(path).resolve())
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(catalog: str, data_path: str) -> str:
    """No network, no new programs, and little to read or write.

    Under the home directory, the child reads only the Python install, this
    worker, DuckDB's extensions, the lake, and the catalog, so `.env` and SSH
    keys stay out of reach whatever DuckDB allows. It can still see file names.

    A SQLite reader of a database in WAL mode needs write access to the `-shm`
    and `-wal` files: it takes its locks through one and creates both if no
    other connection has. The catalog file and the lake stay read-only.
    """
    home = Path.home()
    readable = [
        f"(subpath {_sbpl(sys.prefix)})",
        f"(subpath {_sbpl(sys.base_prefix)})",
        f"(subpath {_sbpl(home / '.duckdb')})",
        f"(subpath {_sbpl(data_path)})",
        f"(literal {_sbpl(WORKER)})",
        f"(literal {_sbpl(catalog)})",
        f"(literal {_sbpl(str(catalog) + '-shm')})",
        f"(literal {_sbpl(str(catalog) + '-wal')})",
    ]
    programs = [
        f"(subpath {_sbpl(sys.prefix)})",
        f"(subpath {_sbpl(sys.base_prefix)})",
    ]
    return (
        "(version 1)(allow default)(deny network*)(deny file-write*)"
        '(allow file-write* (literal "/dev/null"))'
        f"(allow file-write* (literal {_sbpl(str(catalog) + '-shm')}))"
        f"(allow file-write* (literal {_sbpl(str(catalog) + '-wal')}))"
        f"(deny file-read-data (subpath {_sbpl(home)}))"
        f"(allow file-read-data {' '.join(readable)})"
        "(deny process-fork)"
        "(deny process-exec*)"
        f"(allow process-exec* {' '.join(programs)})"
        "(deny signal)(allow signal (target self))"
    )


def console_available() -> bool:
    return shutil.which(SANDBOX_EXEC) is not None


def _watch_memory(
    process: subprocess.Popen[bytes],
    over_memory: threading.Event,
    done: threading.Event,
) -> None:
    """Stop a child past the ceiling. DuckDB's limit misses Python objects."""
    while not done.wait(MEMORY_WATCH_SECONDS):
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            rss = int(result.stdout.strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        if rss > CONSOLE_MAX_RSS_KB:
            over_memory.set()
            process.kill()
            return


def run_worker(
    request: dict[str, Any], limit_seconds: float, sandbox: bool = False
) -> ChildOutcome:
    """Run one child to its end or to the limit, and read its output."""
    command = [sys.executable, "-I", str(WORKER)]
    if sandbox:
        profile = sandbox_profile(request["catalog"], request["data_path"])
        command = [SANDBOX_EXEC, "-p", profile, *command]
    payload = json.dumps(
        {**request, "limit_seconds": limit_seconds}, default=str
    ).encode()
    started = time.monotonic()
    outcome = ChildOutcome()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env(),
            start_new_session=True,
            # DuckDB reads its working directory, which the profile must allow.
            cwd=request["data_path"] if sandbox else None,
        )
    except OSError as error:
        outcome.failed = f"Could not start the child process: {error}"
        return outcome
    over_memory = threading.Event()
    done = threading.Event()
    if sandbox:
        threading.Thread(
            target=_watch_memory,
            args=(process, over_memory, done),
            daemon=True,
        ).start()
    try:
        stdout, stderr = process.communicate(payload, timeout=limit_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        outcome.timed_out = True
        stdout, stderr = process.communicate()
    finally:
        done.set()
    if over_memory.is_set():
        outcome.failed = (
            "The query used more than "
            f"{CONSOLE_MAX_RSS_KB // 1_000_000} GB of memory and was stopped."
        )
    outcome.seconds = time.monotonic() - started
    outcome.stderr = stderr.decode("utf-8", "replace")[-600:]
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            outcome.records.append(json.loads(line))
        except ValueError:
            continue
    return outcome


def worth_keeping(outcome: ChildOutcome) -> bool:
    """A finished answer. Other outcomes can change on the next try."""
    return (
        bool(outcome.records)
        and not outcome.timed_out
        and not any(
            record.get("name") == "_fatal" or record.get("missing")
            for record in outcome.records
        )
    )


def failure_lines(results: dict[str, Ran]) -> list[str]:
    """Each reason a scan did not finish, with what it left unread."""
    unread: dict[str, list[str]] = {}
    for name, ran in results.items():
        if ran.error:
            unread.setdefault(ran.error, []).append(name)
    return [
        f"{reason} Not read: {', '.join(names)}."
        for reason, names in unread.items()
    ]


class Under:
    """Query helpers and the data for each teaching view."""

    def __init__(
        self,
        settings: Settings,
        model: ReadModel,
        panels: Panels,
        reference: Reference,
    ) -> None:
        self.settings = settings
        self.model = model
        self.panels = panels
        self.reference = reference
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._conn_lock = threading.Lock()
        self._cache: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._gate = threading.Semaphore(1)
        self._console_gate = threading.Semaphore(1)

    # Query helpers

    @property
    def snapshot_id(self) -> int:
        return self.model.snapshot.snapshot_id

    def header(self) -> dict[str, Any]:
        snapshot = self.model.snapshot
        return {
            "snapshot_id": snapshot.snapshot_id,
            "committed_at": snapshot.committed_at,
        }

    def _remember(self, key: tuple[Any, ...], value: Any) -> None:
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)

    def _recall(self, key: tuple[Any, ...]) -> Any:
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def local(self, sql: str, params: dict[str, Any] | None = None) -> Ran:
        """Query the local tables, cached for the current snapshot.

        A query bound to a very long list is not cached, because its key and
        its disclosure would each hold the whole list.
        """
        keep = not any(
            isinstance(value, list | tuple) and len(value) > CACHE_MAX_ROWS
            for value in (params or {}).values()
        )
        key: tuple[Any, ...] = ()
        if keep:
            frozen = tuple(
                sorted((k, str(v)) for k, v in (params or {}).items())
            )
            key = ("local", self.snapshot_id, sql, frozen)
            cached = self._recall(key)
            if cached is not None:
                return cached
        started = time.monotonic()
        rows = self.model.rows(sql, params)
        ran = Ran(
            rows,
            Disclosed(
                sql.strip(),
                LOCAL,
                params,
                int((time.monotonic() - started) * 1000),
                len(rows),
            ),
        )
        if keep:
            self._remember(key, ran)
        return ran

    def _catalog_conn(self) -> duckdb.DuckDBPyConnection:
        with self._conn_lock:
            if self._conn is None:
                conn = connect(self.settings, read_only=True)
                conn.execute("SET threads = 1")
                conn.execute("SET memory_limit = '256MB'")
                self._conn = conn
            return self._conn

    def catalog(self, sql: str, params: dict[str, Any] | None = None) -> Ran:
        """A catalog call, cached for the current snapshot."""
        frozen = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
        key = ("catalog", self.snapshot_id, sql, frozen)
        cached = self._recall(key)
        if cached is not None:
            return cached
        conn = self._catalog_conn()
        started = time.monotonic()
        rows: list[dict[str, Any]] = []
        error = None
        for attempt in range(READ_RETRIES):
            cursor = conn.cursor()
            try:
                result = cursor.execute(sql, params or {})
                columns = [column[0] for column in result.description]
                rows = [
                    dict(zip(columns, row, strict=True))
                    for row in result.fetchall()
                ]
                error = None
                break
            except duckdb.Error as failure:
                error = str(failure)[:400]
                time.sleep(0.1 * (attempt + 1))
            finally:
                cursor.close()
        ran = Ran(
            rows,
            Disclosed(
                sql.strip(),
                CATALOG,
                params,
                int((time.monotonic() - started) * 1000),
                len(rows),
            ),
            error,
        )
        if error is None:
            self._remember(key, ran)
        return ran

    def _lake_request(self, **fields: Any) -> dict[str, Any]:
        return {
            "catalog": str(self.settings.paths.catalog.resolve()),
            "data_path": str(self.settings.paths.data.resolve()),
            **fields,
        }

    def scan(
        self,
        key: str,
        statements: list[tuple[str, str, dict[str, Any] | None]],
        ttl: float = SCAN_TTL_SECONDS,
        max_rows: int = 1000,
    ) -> dict[str, Ran]:
        """Run fixed statements in a child process, one child at a time.

        Each result is kept for `ttl` seconds, since snapshots arrive every
        30 s. A run covers only the statements with no current result, so a
        retry continues where a stopped child left off. If another scan holds
        the gate, this returns at once. A statement that does not finish keeps
        its last result, or says why it has none.
        """
        cache_key = ("scan", key)
        held, pending = self._pending(cache_key, statements, ttl)
        if not pending:
            return self._settled(cache_key, statements, held, {})
        if not self._gate.acquire(blocking=False):
            busy = self._unfinished(pending, GATE_BUSY)
            return self._settled(cache_key, statements, held, busy)
        try:
            held, pending = self._pending(cache_key, statements, ttl)
            if not pending:
                return self._settled(cache_key, statements, held, {})
            outcome = run_worker(
                self._lake_request(
                    mode="scan",
                    max_rows=max_rows,
                    statements=[
                        {"name": name, "sql": sql, "params": params}
                        for name, sql, params in pending
                    ],
                ),
                CHILD_LIMIT_SECONDS,
            )
        finally:
            self._gate.release()
        fresh = self._scan_results(pending, outcome)
        return self._settled(cache_key, statements, held, fresh)

    def _pending(
        self,
        cache_key: tuple[Any, ...],
        statements: list[tuple[str, str, dict[str, Any] | None]],
        ttl: float,
    ) -> tuple[dict[str, Ran], list[tuple[str, str, dict[str, Any] | None]]]:
        """The results held, and the statements with no current result."""
        held: dict[str, Ran] = self._recall(cache_key) or {}
        now = time.time()
        pending = [
            statement
            for statement in statements
            if statement[0] not in held
            or now - held[statement[0]].scanned_at >= ttl
        ]
        return held, pending

    def _settled(
        self,
        cache_key: tuple[Any, ...],
        statements: list[tuple[str, str, dict[str, Any] | None]],
        held: dict[str, Ran],
        fresh: dict[str, Ran],
    ) -> dict[str, Ran]:
        """Fresh results over held ones. A failure never replaces a result.

        Ages use the wall clock, because the monotonic clock stops while the
        laptop sleeps. A very long result is not kept, so the cache stays
        small.
        """
        now = time.time()
        settled: dict[str, Ran] = {}
        for name, _, _ in statements:
            ran = fresh.get(name)
            if ran is None or (not ran.ok and name in held):
                ran = held[name]
                ran.age_seconds = now - ran.scanned_at
            settled[name] = ran
        if fresh:
            self._remember(
                cache_key,
                {
                    name: ran
                    for name, ran in settled.items()
                    if ran.ok and len(ran.rows) <= CACHE_MAX_ROWS
                },
            )
        return settled

    @staticmethod
    def _unfinished(
        statements: list[tuple[str, str, dict[str, Any] | None]], reason: str
    ) -> dict[str, Ran]:
        return {
            name: Ran([], Disclosed(sql, CHILD, params), reason)
            for name, sql, params in statements
        }

    @staticmethod
    def _scan_results(
        statements: list[tuple[str, str, dict[str, Any] | None]],
        outcome: ChildOutcome,
    ) -> dict[str, Ran]:
        by_name = {record.get("name"): record for record in outcome.records}
        fatal = by_name.get("_fatal", {}).get("error")
        reason = (
            f"Not finished when the child process stopped at "
            f"{CHILD_LIMIT_SECONDS:.0f} s."
            if outcome.timed_out
            else (
                fatal
                or outcome.failed
                or "The child process ended without a result."
            )
        )
        results: dict[str, Ran] = {}
        for name, sql, params in statements:
            record = by_name.get(name)
            if record is None:
                results[name] = Ran([], Disclosed(sql, CHILD, params), reason)
            elif "error" in record:
                results[name] = Ran(
                    [], Disclosed(sql, CHILD, params), record["error"]
                )
            else:
                columns = record["columns"]
                rows = [
                    dict(zip(columns, row, strict=True))
                    for row in record["rows"]
                ]
                results[name] = Ran(
                    rows,
                    Disclosed(sql, CHILD, params, record.get("ms"), len(rows)),
                    truncated=record.get("truncated", False),
                    scanned_at=time.time(),
                )
        return results

    def worker_call(
        self,
        cache_key: tuple[Any, ...],
        request: dict[str, Any],
        limit_seconds: float,
        ttl: float,
    ) -> ChildOutcome:
        """One child process for work other than a list of statements."""
        held = self._recall(cache_key)
        if held and time.time() - held[0] < ttl:
            return held[1]
        if not self._gate.acquire(blocking=False):
            return held[1] if held else ChildOutcome(failed=GATE_BUSY)
        try:
            outcome = run_worker(request, limit_seconds)
        finally:
            self._gate.release()
        if worth_keeping(outcome):
            self._remember(cache_key, (time.time(), outcome))
        return outcome

    # Shared lookups

    def catalog_tables(self) -> Ran:
        return self.catalog(load_sql("under_catalog_tables"))

    def capture_start(self) -> Ran:
        """When the rich capture tables first received a row."""
        sql = "\nUNION ALL\n".join(
            f"SELECT '{table}' AS table_name, min(created_at) AS first_row "
            f"FROM lake.{table}"
            for table in CAPTURE_TABLES
        )
        return self.scan(
            "capture_start",
            [("capture", sql, None)],
            ttl=SLOW_SCAN_TTL_SECONDS,
        )["capture"]

    def capture_time(self) -> datetime | None:
        times = [
            parse_time(row["first_row"]) for row in self.capture_start().rows
        ]
        known = [when for when in times if when]
        return min(known) if known else None

    def pipeline_status(self) -> dict[str, Any]:
        """The status file, and whether it describes the pipeline now."""
        status = self.model.read_status()
        updated = parse_time(status.get("updated_at"))
        reason = ""
        if updated is None:
            reason = "The status file is missing."
        else:
            age = (datetime.now(UTC) - updated).total_seconds()
            if age > STALE_STATUS_SECONDS:
                reason = (
                    f"The status file was last written {seconds_label(age)} "
                    f"ago, at {et_label(updated)}."
                )
        return {
            "available": not reason,
            "reason": reason,
            "updated": updated,
            "status": {} if reason else status,
        }

    def working_outlets(
        self, enabled: set[str], pipeline: dict[str, Any]
    ) -> set[str] | None:
        """Enabled outlets with a feed that succeeded recently.

        None means the status file cannot say.
        """
        if not pipeline["available"]:
            return None
        now = datetime.now(UTC)
        working = set()
        for feed in pipeline["status"].get("feed_details") or []:
            success = parse_time(feed.get("last_success_at"))
            if success and (
                (now - success).total_seconds() <= WORKING_WITHIN_SECONDS
            ):
                working.add(feed.get("source_id"))
        return working & enabled

    # Landing page

    def overview(self) -> dict[str, Any]:
        tables = self.catalog_tables()
        rows = tables.rows
        counts = self.local(load_sql("under_overview"))
        spend = self.local(load_sql("under_costs_total"))
        enabled = self.local("SELECT source_id FROM sources WHERE enabled")
        pipeline = self.pipeline_status()
        working = self.working_outlets(
            {row["source_id"] for row in enabled.rows}, pipeline
        )
        return {
            "tables": len(rows),
            "total_rows": sum(row["total_rows"] for row in rows),
            "parquet_files": sum(row["parquet_files"] for row in rows),
            "parquet_bytes": sum(row["parquet_bytes"] for row in rows),
            "counts": counts.first,
            "spend": spend.first.get("usd", 0.0),
            "working": None if working is None else len(working),
            "pipeline": pipeline,
            "queries": [
                tables.query,
                counts.query,
                spend.query,
                enabled.query,
            ],
            "error": tables.error,
        }

    # Inventory

    def inventory(self, filters: Filters) -> dict[str, Any]:
        tables = self.catalog_tables()
        statements: list[tuple[str, str, dict[str, Any] | None]] = [
            (
                table,
                f"SELECT count(*) AS n_rows, "
                f"count(DISTINCT {key_sql(table)}) AS n_keys "
                f"FROM lake.{table}",
                None,
            )
            for table in TABLES
        ]
        keys = self.scan("inventory_keys", statements)
        by_table = {
            f"{row['schema_name']}.{row['table_name']}": row
            for row in tables.rows
        }
        rows: list[dict[str, Any]] = []
        for table in TABLES:
            catalog_row = by_table.get(table, {})
            key_ran = keys[table]
            scan_row = key_ran.first
            n_rows = scan_row.get("n_rows")
            n_keys = scan_row.get("n_keys")
            rows.append(
                {
                    "table": table,
                    "schema": table.split(".")[0],
                    "rows": catalog_row.get(
                        "total_rows", 0 if tables.ok else None
                    ),
                    "scan_rows": n_rows,
                    "keys": n_keys,
                    "extra": (n_rows - n_keys)
                    if n_rows is not None and n_keys is not None
                    else None,
                    "per_key": (n_rows / n_keys) if n_keys else None,
                    "key": table_key(table)[0],
                    "meaning": table_key(table)[1],
                    "why": EXTRA_ROWS_WHY.get(table)
                    or (
                        "Recent bins are recomputed each cycle, and a changed "
                        "count appends a row."
                        if table.startswith("gold.")
                        else ""
                    ),
                }
            )
        key_query = Disclosed(
            ";\n".join(sql for _, sql, _ in statements) + ";",
            CHILD,
            ms=sum(keys[name].query.ms or 0 for name in keys),
            rows=len(statements),
        )
        widest = sorted(
            (row for row in rows if row["extra"]),
            key=lambda row: -(row["per_key"] or 0),
        )
        ages = [ran.age_seconds for ran in keys.values() if ran.age_seconds]
        totals = self.local(load_sql("under_overview"))
        by_source = self.local(load_sql("under_items_by_source"))
        by_language = self.local(load_sql("under_items_by_language"))
        by_feed = self.scan(
            "inventory_feeds",
            [("feeds", load_sql("under_items_by_feed"), None)],
        )["feeds"]
        return {
            "table_rows": rows,
            "widest": widest[:6],
            "key_age": max(ages) if ages else None,
            "key_failures": failure_lines(keys),
            "catalog_error": tables.error,
            "table_queries": [tables.query, key_query],
            "totals": totals.first,
            "totals_query": totals.query,
            "by_source": by_source,
            "by_language": by_language,
            "by_feed": by_feed,
            "arrivals": self.arrivals(),
        }

    def arrivals(self) -> dict[str, Any]:
        span = self.local(load_sql("under_arrival_span"))
        first = span.first
        start = first.get("first_seen")
        end = first.get("last_seen")
        if start is None or end is None:
            return {"empty": True, "queries": [span.query]}
        hours = (end - start).total_seconds() / 3600
        minutes = next(
            (m for m in (15, 30, 60, 120, 240) if hours * 60 / m <= 96),
            480,
        )
        bins = self.local(
            load_sql("under_arrivals").replace("{minutes}", str(minutes))
        )
        peak = max((row["n"] for row in bins.rows), default=0) or 1
        fresh_peak = max((row["fresh"] for row in bins.rows), default=0) or 1
        axis_peak = max((row["on_axis"] for row in bins.rows), default=0) or 1
        bars = []
        label_every = max(1, len(bins.rows) // 7)
        for index, row in enumerate(bins.rows):
            bars.append(
                {
                    **row,
                    "label": row["bin_start"]
                    .astimezone(ET)
                    .strftime("%-I:%M %p")
                    .lower()
                    if index % label_every == 0
                    else "",
                    "when": row["bin_start"]
                    .astimezone(ET)
                    .strftime("%b %-d, %-I:%M %p ET"),
                    "backlog_h": row["backlog"] / peak * 100,
                    "fresh_h": row["fresh"] / peak * 100,
                    "fresh_only_h": row["fresh"] / fresh_peak * 100,
                    "axis_h": row["on_axis"] / axis_peak * 100,
                }
            )
        spikes = [
            bar
            for bar in bars
            if bar["outlets_first"] >= 3 and bar["backlog"] > bar["fresh"]
        ]
        return {
            "empty": False,
            "minutes": minutes,
            "bars": bars,
            "peak": peak,
            "fresh_peak": fresh_peak,
            "axis_peak": axis_peak,
            "spikes": spikes,
            "summary": first,
            "first_seen": start,
            "before_chart": first.get("before_first_seen", 0),
            "queries": [span.query, bins.query],
        }

    # Storage

    def storage(self, params: Any) -> dict[str, Any]:
        tables = self.catalog_tables()
        rows = tables.rows
        options = self.catalog(load_sql("under_lake_options"))
        table = checked_table(params.get("table"))
        schema, name = split_table(table)
        files = self.catalog(
            "SELECT data_file, data_file_size_bytes, data_file_footer_size "
            "FROM ducklake_list_files('lake', $table, schema => $schema) "
            "ORDER BY data_file_size_bytes DESC",
            {"table": name, "schema": schema},
        )
        snapshots = self.catalog(
            "SELECT snapshot_id, snapshot_time, changes FROM lake.snapshots() "
            "ORDER BY snapshot_id DESC LIMIT 12"
        )
        kinds = self.catalog(load_sql("under_snapshot_kinds"))
        scheduled = self.catalog(load_sql("under_scheduled_files"))
        table_names = {row["table_id"]: row for row in rows}
        return {
            "rows": rows,
            "catalog_failures": failure_lines(
                {
                    "tables": tables,
                    "options": options,
                    "files": files,
                    "snapshots": snapshots,
                    "kinds": kinds,
                    "scheduled": scheduled,
                }
            ),
            "totals": {
                "inlined": sum(row["inlined_rows"] for row in rows),
                "parquet": sum(row["parquet_rows"] for row in rows),
                "files": sum(row["parquet_files"] for row in rows),
                "bytes": sum(row["parquet_bytes"] for row in rows),
            },
            "tables_query": tables.query,
            "options": options,
            "no_inline": sorted(f"{s}.{t}" for s, t in NO_INLINE),
            "table": table,
            "tables": list(TABLES),
            "files": files,
            "columns": self.column_compression(table, files),
            "snapshots": snapshots,
            "snapshot_rows": [
                self._snapshot_row(row, table_names) for row in snapshots.rows
            ],
            "kinds": kinds,
            "scheduled": scheduled,
            "disk": self.reference.disk(),
            "maintenance": self.scan(
                "maintenance",
                [("runs", load_sql("under_maintenance"), None)],
            )["runs"],
            "travel": self.time_travel(table, params),
        }

    @staticmethod
    def _snapshot_row(
        row: dict[str, Any], tables: dict[int, dict[str, Any]]
    ) -> dict[str, Any]:
        changes = row.get("changes") or {}
        pairs = changes.items() if hasattr(changes, "items") else changes
        parts = []
        for kind, values in pairs:
            names = []
            for value in values or []:
                if str(value).isdigit() and int(value) in tables:
                    entry = tables[int(value)]
                    names.append(
                        f"{entry['schema_name']}.{entry['table_name']}"
                    )
                else:
                    names.append(str(value))
            parts.append(
                {"kind": kind, "n": len(names), "tables": ", ".join(names)}
            )
        return {
            "snapshot_id": row["snapshot_id"],
            "snapshot_time": row["snapshot_time"],
            "parts": parts,
        }

    def column_compression(self, table: str, files: Ran) -> dict[str, Any]:
        """Per-column sizes from the footer of the table's largest file."""
        if files.error:
            return {"empty": True, "failed": files.error}
        if not files.rows:
            return {
                "empty": True,
                "note": f"{table} has no Parquet file. Its rows are inlined "
                "in the catalog, or it has no rows.",
            }
        path = files.rows[0]["data_file"]
        sql = (
            load_sql("under_parquet_columns").replace("{path}", quoted(path))
        ).strip()
        ran = self.scan(
            f"parquet_columns:{path}",
            [("columns", sql, None)],
            ttl=SLOW_SCAN_TTL_SECONDS,
        )["columns"]
        rows = [
            {
                **row,
                "ratio": (row["uncompressed"] / row["compressed"])
                if row["compressed"]
                else None,
            }
            for row in ran.rows
        ]
        total = sum(row["compressed"] for row in rows) or 1
        for row in rows:
            row["share"] = row["compressed"] / total
            row["width"] = row["share"] * 100
        return {
            "empty": False,
            "path": path,
            "file_name": Path(path).name,
            "n_files": len(files.rows),
            "rows": rows,
            "ran": ran,
            "total_compressed": sum(row["compressed"] for row in rows),
            "total_uncompressed": sum(row["uncompressed"] for row in rows),
        }

    def time_travel(self, table: str, params: Any) -> dict[str, Any]:
        latest = self.snapshot_id
        snapshots = self.catalog(
            "SELECT min(snapshot_id) AS first, max(snapshot_id) AS last "
            "FROM lake.snapshots()"
        ).first
        last = snapshots.get("last") or latest
        first = snapshots.get("first") or 0

        def version(name: str, default: int) -> int:
            try:
                value = int(params.get(name, default))
            except (TypeError, ValueError):
                value = default
            return max(first, min(last, value))

        older = version("a", max(first, last - 60))
        newer = version("b", last)
        if older > newer:
            older, newer = newer, older
        key = key_sql(table)

        def at(snapshot: int) -> str:
            return (
                f"SELECT {snapshot} AS version, count(*) AS n_rows, "
                f"count(DISTINCT {key}) AS n_keys, "
                "count(DISTINCT filename) AS n_files, "
                "max(created_at) AS newest_row, "
                "max(snapshot_id) AS newest_snapshot "
                f"FROM lake.{table} AT (VERSION => {snapshot})"
            )

        key_columns = table_key(table)[0]
        shown = f"{key_columns}, " if key_columns else ""
        added = (
            f"SELECT snapshot_id, rowid, filename, {shown}created_at "
            f"FROM lake.{table} AT (VERSION => {newer}) "
            f"WHERE snapshot_id > {older} ORDER BY snapshot_id DESC LIMIT 6"
        )
        results = self.scan(
            f"travel:{table}:{older}:{newer}",
            [
                ("older", at(older), None),
                ("newer", at(newer), None),
                ("added", added, None),
            ],
            ttl=SLOW_SCAN_TTL_SECONDS,
        )
        low = results["older"].first
        high = results["newer"].first
        counted = results["older"].ok and results["newer"].ok
        return {
            "table": table,
            "a": older,
            "b": newer,
            "first": first,
            "last": last,
            "older": low,
            "newer": high,
            "added": results["added"],
            "difference": high["n_rows"] - low["n_rows"] if counted else None,
            "queries": [results[name].query for name in results],
            "failures": failure_lines(results),
        }

    # Data model

    def data_model(self, params: Any) -> dict[str, Any]:
        table = checked_table(params.get("table"), "nlp.embeddings")
        schema, name = split_table(table)
        columns = self.catalog(
            "SELECT column_name, data_type, is_nullable, ordinal_position "
            "FROM information_schema.columns WHERE table_catalog = 'lake' "
            "AND table_schema = $schema AND table_name = $table "
            "ORDER BY ordinal_position",
            {"schema": schema, "table": name},
        )
        files = self.catalog(
            "SELECT data_file, data_file_size_bytes "
            "FROM ducklake_list_files('lake', $table, schema => $schema) "
            "ORDER BY data_file_size_bytes DESC",
            {"table": name, "schema": schema},
        )
        physical = None
        if files.rows:
            path = files.rows[0]["data_file"]
            sql = load_sql("under_parquet_schema").replace(
                "{path}", quoted(path)
            )
            physical = self.scan(
                f"parquet_schema:{path}",
                [("schema", sql.strip(), None)],
                ttl=SLOW_SCAN_TTL_SECONDS,
            )["schema"]
        sample_sql = (
            f"SELECT *, snapshot_id AS _snapshot, filename AS _file "
            f"FROM lake.{table} ORDER BY created_at DESC LIMIT 1"
        )
        sample = self.scan(f"sample:{table}", [("sample", sample_sql, None)])[
            "sample"
        ]
        sample_rows = [
            {"column": column, "value": shorten_cell(value)}
            for column, value in sample.first.items()
        ]
        return {
            "table": table,
            "tables": list(TABLES),
            "declared": TABLES[table].strip(),
            "no_inline": (schema, name) in NO_INLINE,
            "key": table_key(table)[0],
            "meaning": table_key(table)[1],
            "columns": columns,
            "files": files,
            "physical": physical,
            "file_name": Path(files.rows[0]["data_file"]).name
            if files.rows
            else "",
            "sample": sample,
            "sample_rows": sample_rows,
            "schemas": self.llm_schemas(),
        }

    def llm_schemas(self) -> list[dict[str, Any]]:
        """The JSON schema sent with each structured call."""
        cached = self._recall(("llm_schemas",))
        if cached is not None:
            return cached
        from anthropic import transform_schema

        from monitor.llm import schemas

        result = [
            {
                "purpose": purpose,
                "name": name,
                "tables": tables,
                "json": json.dumps(
                    transform_schema(getattr(schemas, name)),
                    indent=2,
                    ensure_ascii=False,
                ),
            }
            for purpose, name, tables in SCHEMA_PURPOSES
        ]
        self._remember(("llm_schemas",), result)
        return result

    # Model map

    def model_map(self) -> dict[str, Any]:
        statements: list[tuple[str, str, dict[str, Any] | None]] = [
            (
                producer["table"],
                f"SELECT {producer['group']}, count(*) AS n, "
                f"min(created_at) AS first_row, max(created_at) AS last_row "
                f"FROM lake.{producer['table']} GROUP BY ALL "
                "ORDER BY n DESC",
                None,
            )
            for producer in PRODUCERS
        ]
        extras: list[tuple[str, str, dict[str, Any] | None]] = [
            ("served", load_sql("under_models_served"), None),
            ("prompts", load_sql("under_prompts"), None),
            ("thresholds", load_sql("under_placeholder_counts"), None),
        ]
        results = self.scan("model_map", statements + extras)
        rows = []
        for producer in PRODUCERS:
            ran = results[producer["table"]]
            for row in ran.rows:
                rows.append({**producer, **row})
            if ran.ok and not ran.rows:
                rows.append(
                    {
                        **producer,
                        "parser": "",
                        "parser_version": "",
                        "method": "",
                        "model": "",
                        "n": 0,
                    }
                )
        return {
            "rows": rows,
            "producers": PRODUCERS,
            "served": results["served"],
            "prompts": results["prompts"],
            "placeholder": results["thresholds"].first,
            "placeholder_query": results["thresholds"].query,
            "queries": [results[p["table"]].query for p in PRODUCERS],
            "errors": failure_lines(results),
            "settings": self.settings,
            "agreement": self.agreement(),
        }

    def agreement(self) -> dict[str, Any]:
        """Keyword path against the LLM path, on one stable sample."""
        sql = load_sql("under_agreement_sample").replace(
            "{sample}", str(AGREEMENT_SAMPLE)
        )
        outcome = self.worker_call(
            ("agreement",),
            self._lake_request(
                mode="agreement",
                sql=sql,
                max_rows=AGREEMENT_SAMPLE,
                config_dir=str(self.settings.paths.config.resolve()),
            ),
            AGREEMENT_LIMIT_SECONDS,
            SLOW_SCAN_TTL_SECONDS,
        )
        query = Disclosed(
            sql.strip(),
            f"child process: low priority, stopped at "
            f"{AGREEMENT_LIMIT_SECONDS:.0f} s",
            note="Then classify_by_keywords runs over each row in the same "
            "child process. It reads local text and makes no API call.",
        )
        records = {record.get("name"): record for record in outcome.records}
        record = records.get("agreement")
        if not record:
            reason = (
                "The comparison did not finish before its limit."
                if outcome.timed_out
                else records.get("_fatal", {}).get("error")
                or outcome.failed
                or outcome.stderr
                or "The comparison returned nothing."
            )
            return {"error": reason, "query": query}
        return {**score_agreement(record["rows"]), "query": query}

    # Feed tracking

    def feeds(self) -> dict[str, Any]:
        names = ("mix", "conditional", "tiers", "top_feeds", "idle_feeds")
        results = self.scan(
            "feeds",
            [(name, load_sql(f"under_feeds_{name}"), None) for name in names],
        )
        pipeline = self.pipeline_status()
        status = pipeline["status"]
        details = status.get("feed_details") or []
        states = {"ok": 0, "waiting": 0, "failing": 0, "down": 0}
        feeds = []
        for feed in details:
            state = feed_state(feed)
            states[state] += 1
            feeds.append(
                {
                    **feed,
                    "state": state,
                    "last_success": parse_time(feed.get("last_success_at")),
                }
            )
        far_past = datetime.min.replace(tzinfo=UTC)
        oldest = sorted(feeds, key=lambda f: f["last_success"] or far_past)
        trouble = [feed for feed in feeds if feed["state"] != "ok"]
        mix = results["mix"].rows
        total = sum(row["n"] for row in mix) or 1
        recent_total = sum(row["last_hour"] or 0 for row in mix) or 1
        return {
            "mix": [
                {
                    **row,
                    "share": row["n"] / total,
                    "recent_share": (row["last_hour"] or 0) / recent_total,
                }
                for row in mix
            ],
            "mix_query": results["mix"].query,
            "mix_ok": results["mix"].ok,
            "conditional": results["conditional"],
            "tiers": results["tiers"],
            "top_feeds": results["top_feeds"],
            "idle_feeds": results["idle_feeds"],
            "states": states,
            "feeds_total": len(feeds),
            "trouble": trouble,
            "oldest": oldest[:15],
            "pipeline": pipeline,
            "started_at": parse_time(status.get("started_at")),
            "poll": self.settings.poll,
            "errors": failure_lines(results),
        }

    # API cost

    def _usd(self, row: dict[str, Any], uncached: bool = False) -> float:
        reads = row["cache_read_tokens"] or 0
        writes = row["cache_write_tokens"] or 0
        tokens = row["input_tokens"] or 0
        if uncached:
            return estimate_usd(
                self.settings,
                row["model"],
                tokens + reads + writes,
                row["output_tokens"] or 0,
            )
        return estimate_usd(
            self.settings,
            row["model"],
            tokens,
            row["output_tokens"] or 0,
            reads,
            writes,
        )

    def costs(self) -> dict[str, Any]:
        ledger = self.local(load_sql("under_costs_ledger"))
        by_day = self.local(load_sql("under_costs_by_day"))
        by_hour = self.local(load_sql("under_costs_by_hour"))
        items = self.local(load_sql("under_costs_items"))
        pipeline = self.pipeline_status()
        llm = pipeline["status"].get("llm") or {}
        rows = []
        for row in ledger.rows:
            uncached = self._usd(row, uncached=True)
            actual = self._usd(row)
            rows.append(
                {
                    **row,
                    "uncached": uncached,
                    "actual": actual,
                    "saved": uncached - actual,
                    "saved_share": (uncached - actual) / uncached
                    if uncached
                    else None,
                }
            )
        total_usd = sum(row["usd"] for row in rows)
        uncached_total = sum(row["uncached"] for row in rows)
        tokens = {
            "input": sum(row["input_tokens"] for row in rows),
            "cache_read": sum(row["cache_read_tokens"] for row in rows),
            "cache_write": sum(row["cache_write_tokens"] for row in rows),
            "output": sum(row["output_tokens"] for row in rows),
        }
        input_total = tokens["input"] + tokens["cache_read"]
        input_total += tokens["cache_write"]
        peak = max(tokens.values()) or 1
        token_rows = [
            {
                "kind": kind,
                "n": value,
                "width": value / peak * 100,
            }
            for kind, value in (
                ("Input, not cached", tokens["input"]),
                ("Input, read from cache", tokens["cache_read"]),
                ("Input, written to cache", tokens["cache_write"]),
                ("Output", tokens["output"]),
            )
        ]
        by_purpose: dict[str, float] = {}
        for row in rows:
            by_purpose[row["purpose"]] = (
                by_purpose.get(row["purpose"], 0) + row["usd"]
            )
        counts = items.first
        stage_rows = [
            {
                "purpose": purpose,
                "label": label,
                "usd": by_purpose.get(purpose, 0.0),
                "n": counts.get(column) or 0,
                "unit": unit,
                "per": by_purpose.get(purpose, 0.0) / counts[column]
                if counts.get(column)
                else None,
            }
            for purpose, label, column, unit in COST_STAGES
        ]
        day_peak = max((row["usd"] for row in by_day.rows), default=0) or 1
        hour_peak = max((row["usd"] for row in by_hour.rows), default=0) or 1
        return {
            "rows": rows,
            "total_usd": total_usd,
            "uncached_total": uncached_total,
            "saved_total": uncached_total - total_usd,
            "saved_share": (uncached_total - total_usd) / uncached_total
            if uncached_total
            else None,
            "calls": sum(row["calls"] for row in rows),
            "not_ok": sum(row["not_ok"] for row in rows),
            "token_rows": token_rows,
            "cache_read_share": tokens["cache_read"] / input_total
            if input_total
            else None,
            "stage_rows": stage_rows,
            "by_day": [
                {**row, "width": row["usd"] / day_peak * 100}
                for row in by_day.rows
            ],
            "by_hour": [
                {**row, "width": row["usd"] / hour_peak * 100}
                for row in by_hour.rows
            ],
            "pipeline": pipeline,
            "budget": llm.get("budget") or {},
            "hard_cap": self.settings.llm.hard_usd_cap,
            "degraded": llm.get("degraded"),
            "prices": self.settings.llm.prices_per_mtok,
            "read_multiplier": self.settings.llm.cache_read_multiplier,
            "write_multiplier": self.settings.llm.cache_write_multiplier,
            "queries": {
                "ledger": ledger.query,
                "by_day": by_day.query,
                "by_hour": by_hour.query,
                "items": items.query,
            },
        }

    # SQL console

    def console(self, sql: str) -> dict[str, Any]:
        """Run one console statement in a sandboxed child, one at a time."""
        text = sql.strip()
        if not text:
            return {"sql": "", "empty": True}
        if not console_available():
            return {
                "sql": text,
                "error": "The console needs the macOS sandbox, which is not "
                "available here.",
            }
        if not self._console_gate.acquire(blocking=False):
            return {
                "sql": text,
                "error": "Another console query is running. It stops within "
                f"{CHILD_LIMIT_SECONDS:.0f} s.",
            }
        try:
            outcome = run_worker(
                self._lake_request(
                    mode="console", sql=text, max_rows=CONSOLE_MAX_ROWS
                ),
                CHILD_LIMIT_SECONDS,
                sandbox=True,
            )
        finally:
            self._console_gate.release()
        result = console_result(text, outcome)
        log.info(
            "under_sql",
            sql=text[:2000],
            status=result["status"],
            rows=len(result.get("rows") or []),
            seconds=round(outcome.seconds, 2),
        )
        return result


def console_result(text: str, outcome: ChildOutcome) -> dict[str, Any]:
    records = {record.get("name"): record for record in outcome.records}
    record = records.get("console")
    result: dict[str, Any] = {"sql": text, "seconds": outcome.seconds}
    if outcome.failed:
        result.update(status="stopped", error=outcome.failed)
    elif outcome.timed_out:
        result.update(
            status="stopped",
            error=f"Stopped at {CHILD_LIMIT_SECONDS:.0f} s. The query ran in "
            "its own process, which is gone.",
        )
    elif record is None:
        reason = records.get("_fatal", {}).get("error") or outcome.stderr
        result.update(status="failed", error=reason or "No result.")
    elif "rejected" in record:
        result.update(status="rejected", rejected=record["rejected"])
    elif "error" in record:
        error = record["error"]
        if ".tmp" in error or "Out of Memory" in error:
            error = (
                "The query needed more than its 1 GB of memory, and spilling "
                f"to disk is off. DuckDB said: {error}"
            )
        result.update(status="error", error=error)
    else:
        result.update(
            status="ok",
            columns=record["columns"],
            rows=[
                [shorten_cell(cell, 400) for cell in row]
                for row in record["rows"]
            ],
            truncated=record["truncated"],
            ms=record["ms"],
            max_rows=CONSOLE_MAX_ROWS,
        )
    return result


def jaccard(left: list[str], right: list[str]) -> float:
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def relevance_table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    both = sum(1 for r in rows if r["llm_relevant"] and r["kw_relevant"])
    llm_only = sum(
        1 for r in rows if r["llm_relevant"] and not r["kw_relevant"]
    )
    kw_only = sum(
        1 for r in rows if r["kw_relevant"] and not r["llm_relevant"]
    )
    neither = sum(
        1 for r in rows if not r["llm_relevant"] and not r["kw_relevant"]
    )
    n = len(rows)
    agree = (both + neither) / n if n else None
    expected = (
        (
            (both + llm_only) * (both + kw_only)
            + (neither + kw_only) * (neither + llm_only)
        )
        / (n * n)
        if n
        else None
    )
    kappa = (
        (agree - expected) / (1 - expected)
        if agree is not None and expected is not None and expected < 1
        else None
    )
    return {
        "n": n,
        "both": both,
        "llm_only": llm_only,
        "kw_only": kw_only,
        "neither": neither,
        "agree": agree,
        "kappa": kappa,
        "recall": both / (both + llm_only) if both + llm_only else None,
        "precision": both / (both + kw_only) if both + kw_only else None,
    }


def score_agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the paths. Neither is ground truth; the labels say so."""
    english = [r for r in rows if r["language"] == "en"]
    other = [r for r in rows if r["language"] != "en"]
    relevant = [r for r in rows if r["llm_relevant"] and r["kw_relevant"]]
    state_scores = [jaccard(r["llm_states"], r["kw_states"]) for r in relevant]
    topic_scores = [jaccard(r["llm_topics"], r["kw_topics"]) for r in relevant]
    same_level = sum(1 for r in relevant if r["llm_level"] == r["kw_level"])
    missed = [
        r["item_id"]
        for r in rows
        if r["llm_relevant"] and not r["kw_relevant"]
    ][:8]
    extra = [
        r["item_id"]
        for r in rows
        if r["kw_relevant"] and not r["llm_relevant"]
    ][:8]
    return {
        "all": relevance_table(rows),
        "english": relevance_table(english),
        "other": relevance_table(other),
        "n_both": len(relevant),
        "state_exact": sum(1 for s in state_scores if s == 1.0)
        / len(state_scores)
        if state_scores
        else None,
        "state_jaccard": sum(state_scores) / len(state_scores)
        if state_scores
        else None,
        "topic_jaccard": sum(topic_scores) / len(topic_scores)
        if topic_scores
        else None,
        "level_same": same_level / len(relevant) if relevant else None,
        "missed": missed,
        "extra": extra,
    }


def valid_item_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-zA-Z_-]{1,64}", value))
