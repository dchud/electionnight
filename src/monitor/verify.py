"""Scriptable environment checks, recorded in `docs/verify_log.md`.

`monitor verify` rewrites the rows it can check and keeps every other row and
all text outside the table.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from monitor.lake import BatchWriter, connect, init_lake
from monitor.settings import Paths, Settings, get_settings

LOG_PATH = Path("docs/verify_log.md")
SCRATCH = Path("tmp/verify")

CHECKS_DESCRIBED = {
    "V1": "DuckDB, ducklake, and sqlite extension versions",
    "V2": "Read-only attach works and rejects writes",
    "V3": "Lake option names accepted",
    "V4": "Maintenance function names",
    "V5": "Snapshot listing function",
    "V6": "Embedding model available through fastembed",
    "V7": "Anthropic key, model ids, request settings, prompt caching",
    "V8": "htmx 2 SSE extension attribute syntax",
    "V9": "Vendored asset versions",
    "V10": "OpenFEC endpoint and filters for 2026 candidates",
    "V11": "Race ratings sources reachable and parsed",
    "V12": "Poll-closing times confirmed",
    "V13": "Lean ratings confirmed with citations",
    "V14": "Battleground and close-race list confirmed",
}


@dataclass
class Result:
    ok: bool
    detail: str


def _scratch_settings(settings: Settings) -> Settings:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    return settings.model_copy(update={"paths": Paths.under(SCRATCH)})


def check_versions(_: Settings, conn: duckdb.DuckDBPyConnection) -> Result:
    rows = conn.execute(
        "SELECT extension_name, extension_version FROM duckdb_extensions() "
        "WHERE extension_name IN ('ducklake', 'sqlite_scanner') AND loaded "
        "ORDER BY 1"
    ).fetchall()
    versions = ", ".join(f"{name} {version}" for name, version in rows)
    format_version = conn.execute(
        "SELECT value FROM lake.options() WHERE option_name = 'version'"
    ).fetchall()
    return Result(
        len(rows) == 2,
        f"duckdb {duckdb.__version__}, {versions}, "
        f"DuckLake format {format_version[0][0]}",
    )


def check_read_only(settings: Settings, _: object) -> Result:
    reader = connect(settings, read_only=True)
    try:
        reader.execute("SELECT count(*) FROM lake.raw.items").fetchall()
        try:
            reader.execute("INSERT INTO lake.raw.items (item_id) VALUES ('z')")
        except duckdb.Error:
            return Result(True, "`READ_ONLY` attach reads and rejects INSERT")
        return Result(False, "read-only connection accepted a write")
    finally:
        reader.close()


def check_options(_: Settings, conn: duckdb.DuckDBPyConnection) -> Result:
    names = {
        row[0]
        for row in conn.execute(
            "SELECT option_name FROM lake.options()"
        ).fetchall()
    }
    wanted = {
        "parquet_compression",
        "parquet_compression_level",
        "target_file_size",
        "data_inlining_row_limit",
    }
    missing = wanted - names
    return Result(
        not missing,
        f"missing {sorted(missing)}" if missing else ", ".join(sorted(wanted)),
    )


def check_maintenance(_: Settings, conn: duckdb.DuckDBPyConnection) -> Result:
    calls = (
        "ducklake_flush_inlined_data('lake')",
        "ducklake_merge_adjacent_files('lake')",
        "ducklake_expire_snapshots('lake', dry_run => true, "
        "older_than => now())",
        "ducklake_cleanup_old_files('lake', dry_run => true, "
        "cleanup_all => true)",
    )
    for call in calls:
        conn.execute(f"CALL {call}").fetchall()
    return Result(True, "; ".join(call.split("(")[0] for call in calls))


def check_snapshots(_: Settings, conn: duckdb.DuckDBPyConnection) -> Result:
    columns = conn.execute(
        "SELECT * FROM lake.snapshots() LIMIT 1"
    ).description
    names = [column[0] for column in columns]
    return Result("changes" in names, f"`lake.snapshots()` columns: {names}")


def check_embedding(settings: Settings, _: object) -> Result:
    from fastembed import TextEmbedding

    from monitor.parsers.embed import EMBED_DIM, load_embedding_model

    name = settings.nlp.embed_model
    listed = {m["model"] for m in TextEmbedding.list_supported_models()}
    origin = "listed by fastembed" if name in listed else "custom ONNX export"
    real = settings.model_copy(update={"paths": get_settings().paths})
    model = load_embedding_model(real)
    vector = next(iter(model.embed(["passage: polls close at 7 pm"])))
    return Result(
        len(vector) == EMBED_DIM,
        f"{name}, {origin}, returns {len(vector)} dimensions",
    )


def check_anthropic(settings: Settings, _: object) -> Result:
    import anthropic
    from pydantic import BaseModel

    if settings.anthropic_api_key is None:
        return Result(False, "ANTHROPIC_API_KEY is not set")

    class Reply(BaseModel):
        word: str

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), max_retries=1
    )
    system = [
        {
            "type": "text",
            "text": "Reply with the requested word. " * 300,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    usage = None
    for _attempt in range(2):
        response = client.messages.parse(
            model=settings.llm.model_bulk,
            max_tokens=256,
            system=system,  # type: ignore[arg-type]
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            output_format=Reply,
            messages=[{"role": "user", "content": "The word is: ok"}],
        )
        usage = response.usage
    parsed = response.parsed_output
    brief = client.messages.create(
        model=settings.llm.model_brief,
        max_tokens=256,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": "Reply with: ok"}],
    )
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    return Result(
        parsed is not None and cached > 0 and brief.stop_reason == "end_turn",
        f"{settings.llm.model_bulk} parse with effort low and thinking "
        f"disabled OK, cache read {cached} tokens on the second call; "
        f"{settings.llm.model_brief} adaptive at medium OK; "
        f"SDK {anthropic.__version__}",
    )


def _read_rows(text: str) -> dict[str, str]:
    rows = {}
    for line in text.splitlines():
        match = re.match(r"\|\s*(V\d+)\s*\|", line)
        if match:
            rows[match.group(1)] = line
    return rows


def write_log(results: dict[str, Result], path: Path = LOG_PATH) -> None:
    """Update table rows for the checks that ran, keeping everything else."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    rows = _read_rows(existing)
    for check, result in results.items():
        status = "pass" if result.ok else "FAIL"
        detail = result.detail.replace("|", "/")
        rows[check] = (
            f"| {check} | {CHECKS_DESCRIBED[check]} | {status} {today}: "
            f"{detail} |"
        )
    table = ["| # | Check | Result |", "|---|---|---|"]
    for check in CHECKS_DESCRIBED:
        table.append(
            rows.get(check, f"| {check} | {CHECKS_DESCRIBED[check]} | |")
        )
    body = "\n".join(table)
    if existing and "| # | Check |" in existing:
        before = existing.split("| # | Check |")[0]
        after_lines = existing.split("| # | Check |")[1].splitlines()[1:]
        tail = "\n".join(
            line for line in after_lines if not line.startswith("|")
        )
        text = f"{before}{body}\n{tail}\n"
    else:
        text = f"# Verification log\n\n{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def run_verify(settings: Settings) -> int:
    """Run the checks on a scratch lake. Returns the process exit code."""
    scratch = _scratch_settings(settings)
    init_lake(scratch)
    conn = connect(scratch)
    writer = BatchWriter(conn, scratch)
    writer.add("raw.fetches", [{"fetch_id": "verify"}])
    writer.flush()
    checks: dict[str, Callable[..., Result]] = {
        "V1": check_versions,
        "V2": check_read_only,
        "V3": check_options,
        "V4": check_maintenance,
        "V5": check_snapshots,
        "V6": check_embedding,
    }
    results: dict[str, Result] = {}
    for name, check in checks.items():
        try:
            results[name] = check(scratch, conn)
        except Exception as error:
            results[name] = Result(False, f"{type(error).__name__}: {error}")
    conn.close()
    try:
        results["V7"] = check_anthropic(settings, None)
    except Exception as error:
        results["V7"] = Result(False, f"{type(error).__name__}: {error}")
    write_log(results)
    for name, result in results.items():
        print(f"{name} {'pass' if result.ok else 'FAIL'}: {result.detail}")
    return 0 if all(result.ok for result in results.values()) else 1
