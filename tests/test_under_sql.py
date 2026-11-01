"""The SQL console. Every mutation attempt here runs on a scratch lake only."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from monitor.lake import BatchWriter, connect
from monitor.web.app import create_app
from monitor.web.under import (
    SANDBOX_EXEC,
    Under,
    console_available,
    run_worker,
    sandbox_profile,
)

needs_sandbox = pytest.mark.skipif(
    not console_available(), reason="needs the macOS sandbox"
)
CANARY = "SECRET_CANARY=abc123"
FUTURE = (datetime.now(UTC) + timedelta(days=1)).isoformat()
HEAVY = "SELECT count(*) FROM range(100000000000) a, range(100000) b"


@pytest.fixture(scope="module")
def scratch(lake_template, tmp_path_factory):
    """A lake with replaced files awaiting cleanup and an unknown file.

    `ducklake_cleanup_old_files` and `ducklake_delete_orphaned_files` each have
    something to delete here, and a canary `.env` sits beside the lake.
    """
    root = tmp_path_factory.mktemp("console")
    settings = clone_lake(lake_template, root)
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    for batch in range(3):
        writer.add(
            "raw.items",
            [
                {
                    "item_id": f"i{batch}-{n}",
                    "source_id": "s",
                    "title": "t",
                    "first_seen_at": datetime.now(UTC),
                }
                for n in range(3000)
            ],
        )
        writer.flush()
    conn.execute("CALL ducklake_merge_adjacent_files('lake')").fetchall()
    conn.close()
    with closing(sqlite3.connect(settings.paths.catalog)) as catalog:
        scheduled = catalog.execute(
            "SELECT count(*) FROM ducklake_files_scheduled_for_deletion"
        ).fetchone()[0]
    assert scheduled >= 3
    folder = settings.paths.data.resolve() / "raw" / "items"
    orphan = folder / "ducklake-orphan-0000.parquet"
    shutil.copyfile(next(folder.glob("*.parquet")), orphan)
    os.utime(orphan, (0, 0))
    (root / ".env").write_text(CANARY + "\n")
    return settings


def fingerprint(settings):
    """Every catalog row and every file, so any write changes this.

    The connection closes, so no `-wal` or `-shm` file stays behind for the
    next reader, which is the harder case for a reader in the sandbox.
    """
    root = settings.paths.catalog.parent
    catalog = settings.paths.catalog
    digest = hashlib.sha256()
    with closing(sqlite3.connect(catalog)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1"
        ).fetchall():
            for row in db.execute(f'SELECT * FROM "{name}" ORDER BY 1'):
                digest.update(repr(row).encode())
    files = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.name.startswith("catalog.sqlite")
    )
    return digest.hexdigest(), files


def console_request(settings, sql):
    return {
        "mode": "console",
        "catalog": str(settings.paths.catalog.resolve()),
        "data_path": str(settings.paths.data.resolve()),
        "sql": sql,
        "max_rows": 1000,
    }


def scan_request(settings, sql):
    return {
        "mode": "scan",
        "catalog": str(settings.paths.catalog.resolve()),
        "data_path": str(settings.paths.data.resolve()),
        "max_rows": 1000,
        "statements": [{"name": "q", "sql": sql}],
    }


def attempts(settings):
    catalog = settings.paths.catalog.resolve()
    data = settings.paths.data.resolve()
    root = catalog.parent
    meta = "__ducklake_metadata_lake.main"
    cleanup = "ducklake_cleanup_old_files('lake', cleanup_all => true)"
    return [
        f"SELECT * FROM ducklake_cleanup_old_files('lake', "
        f"older_than => TIMESTAMPTZ '{FUTURE}')",
        f"SELECT * FROM {cleanup}",
        f"CALL {cleanup}",
        f"EXPLAIN SELECT * FROM {cleanup}",
        f"EXPLAIN ANALYZE SELECT * FROM {cleanup}",
        f"DESCRIBE SELECT * FROM {cleanup}",
        f"SUMMARIZE SELECT * FROM {cleanup}",
        f"FROM {cleanup}",
        f"WITH a AS (SELECT * FROM {cleanup}) SELECT * FROM a",
        f"SELECT 1 WHERE EXISTS (SELECT * FROM {cleanup})",
        f"SELECT * FROM range(1), LATERAL (SELECT * FROM {cleanup})",
        f"SELECT 1 UNION ALL SELECT count(*) FROM {cleanup}",
        "SELECT * FROM ducklake_delete_orphaned_files('lake', "
        "cleanup_all => true)",
        "SELECT * FROM ducklake_merge_adjacent_files('lake')",
        "SELECT * FROM ducklake_flush_inlined_data('lake')",
        f"SELECT * FROM ducklake_expire_snapshots('lake', "
        f"older_than => TIMESTAMPTZ '{FUTURE}')",
        "SELECT * FROM ducklake_expire_snapshots('lake', versions => [1])",
        "SELECT * FROM ducklake_rewrite_data_files('lake')",
        "SELECT * FROM ducklake_set_option('lake', "
        "'data_inlining_row_limit', 5)",
        "SELECT * FROM ducklake_set_commit_message('lake', 'me', 'x')",
        "SELECT * FROM ducklake_add_data_files('lake', 'items', '/x')",
        "SELECT * FROM lake.merge_adjacent_files()",
        "CALL lake.set_option('data_inlining_row_limit', 5)",
        "CALL ducklake_merge_adjacent_files('lake')",
        "INSERT INTO lake.raw.items (item_id) VALUES ('x')",
        "DELETE FROM lake.raw.items",
        "UPDATE lake.raw.items SET title = 'x'",
        "DROP TABLE lake.raw.items",
        "CREATE TABLE lake.raw.x (a INT)",
        "ALTER TABLE lake.raw.items ADD COLUMN x INT",
        f"UPDATE {meta}.ducklake_metadata SET value = 'x' "
        "WHERE key = 'version'",
        f"DELETE FROM {meta}.ducklake_snapshot",
        f"INSERT INTO {meta}.ducklake_metadata VALUES ('a', 'b', NULL, NULL)",
        f"DROP TABLE {meta}.ducklake_metadata",
        f"CREATE TABLE {meta}.x (a INT)",
        "SELECT * FROM sqlite_query('__ducklake_metadata_lake', "
        "'DELETE FROM ducklake_metadata')",
        "SELECT * FROM sqlite_query('__ducklake_metadata_lake', "
        "'SELECT * FROM ducklake_metadata')",
        f"SELECT * FROM sqlite_scan('{catalog}', 'ducklake_metadata')",
        f"CALL sqlite_attach('{catalog}')",
        f"ATTACH '{catalog}' AS c2 (TYPE sqlite)",
        f"ATTACH 'ducklake:sqlite:{catalog}' AS l2 (DATA_PATH '{data}/')",
        "DETACH lake",
        "USE __ducklake_metadata_lake",
        f"COPY (SELECT 1) TO '{data}/x.csv'",
        f"COPY lake.raw.items TO '{data}/x.parquet'",
        f"COPY lake.raw.items TO '{root}/x.parquet'",
        f"EXPORT DATABASE '{data}/x'",
        "SET enable_external_access = true",
        "SET lock_configuration = false",
        "RESET lock_configuration",
        "PRAGMA enable_profiling",
        "INSTALL httpfs",
        "LOAD httpfs",
        "CREATE SECRET s (TYPE S3)",
        "CREATE TEMP TABLE t AS SELECT 1",
        "CREATE MACRO m() AS 1",
        "BEGIN",
        "CHECKPOINT lake",
        "VACUUM lake.raw.items",
        "SELECT 1; DROP TABLE lake.raw.items",
        "SELECT 1; SELECT 2",
        "PIVOT lake.raw.items ON title USING count(*)",
        f"SELECT * FROM range((SELECT count(*) FROM {cleanup}))",
        f"SELECT * FROM unnest((SELECT list(path) FROM {cleanup}))",
        "SELECT * FROM range((SELECT count(*) FROM query('SELECT 1')))",
        "SELECT * FROM range((SELECT count(*) FROM "
        "query_table('lake.raw.items')))",
        "SELECT * FROM range((SELECT count(*) FROM duckdb_settings()))",
        "SELECT * FROM range((SELECT count(*) FROM sqlite_query("
        f"'__ducklake_metadata_lake', 'ATTACH ''{root}/x.sqlite'' AS c2')))",
        "SELECT * FROM range((SELECT sleep_ms(100000)))",
        f"WITH x AS (COPY (SELECT 1) TO '{data}/x.csv') SELECT * FROM x",
        f"WITH x AS (COPY (SELECT 1) TO '{catalog}') SELECT * FROM x",
        f"WITH x AS (COPY (SELECT 1) TO '{catalog}-wal') SELECT * FROM x",
        "WITH x AS (DELETE FROM lake.raw.items RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE lake.raw.items SET title = 'x' RETURNING *) "
        "SELECT * FROM x",
        "WITH x AS (INSERT INTO lake.raw.items (item_id) VALUES ('x') "
        "RETURNING *) SELECT * FROM x",
        "SELECT pg_sleep(1)",
        "SELECT current_setting('home_directory')",
        "SELECT name, setting FROM pg_catalog.pg_settings",
        "SELECT * FROM duckdb_databases()",
        f"SHOW '{root}/.env'",
        "SELECT * FROM query('DELETE FROM lake.raw.items')",
        "SELECT * FROM query('SELECT 1')",
        "SELECT * FROM query_table('lake.raw.items')",
        "SELECT * FROM json_execute_serialized_sql('x')",
        "SELECT * FROM duckdb_secrets()",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM checkpoint()",
        "SELECT * FROM force_checkpoint()",
        "SELECT write_log('x')",
        "SELECT setval('a', 1)",
        "SELECT sleep_ms(100000)",
    ]


def reads_of_dotenv(settings):
    data = settings.paths.data.resolve()
    root = settings.paths.catalog.resolve().parent
    return [
        f"SELECT * FROM read_text('{root}/.env')",
        f"SELECT * FROM unnest((SELECT list(content) FROM "
        f"read_text('{root}/.env')))",
        f"SELECT * FROM range((SELECT count(*) FROM "
        f"read_text('{root}/.env')))",
        f"SELECT * FROM read_text('{data}/../.env')",
        f"SELECT * FROM read_text('{data}/raw/../../.env')",
        f"SELECT * FROM read_csv('{root}/.env')",
        f"SELECT * FROM read_csv_auto('{root}/.env')",
        f"SELECT * FROM '{root}/.env'",
        f"SELECT * FROM read_blob('{root}/.env')",
        f"SELECT * FROM glob('{root}/*')",
        f"SELECT * FROM read_json('{root}/.env')",
        f"SELECT * FROM parquet_metadata('{root}/.env')",
        f"SELECT * FROM read_parquet('{root}/.env')",
        f"SELECT * FROM read_parquet('{data}/../.env')",
        "SELECT getenv('HOME')",
        "SELECT * FROM read_text('https://example.com')",
        "SELECT * FROM read_text('/etc/passwd')",
    ]


def outcome_of(settings, sql, sandbox):
    outcome = run_worker(console_request(settings, sql), 20.0, sandbox=sandbox)
    assert not outcome.timed_out, sql
    records = {record.get("name"): record for record in outcome.records}
    return records.get("console") or {"error": outcome.stderr}


def test_the_scratch_lake_can_be_damaged(lake_template, tmp_path):
    """Why the console exists as it does: a read-only attach is not enough."""
    settings = clone_lake(lake_template, tmp_path)
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    for batch in range(2):
        writer.add(
            "raw.items",
            [{"item_id": f"{batch}-{n}"} for n in range(3000)],
        )
        writer.flush()
    conn.execute("CALL ducklake_merge_adjacent_files('lake')").fetchall()
    conn.close()
    folder = settings.paths.data.resolve() / "raw" / "items"
    before = len(list(folder.glob("*.parquet")))
    assert before >= 3
    run_worker(
        scan_request(
            settings,
            "SELECT * FROM ducklake_cleanup_old_files('lake', "
            "cleanup_all => true)",
        ),
        20.0,
    )
    assert len(list(folder.glob("*.parquet"))) < before


def test_guard_rejects_every_mutation_attempt(scratch):
    """The parsed-statement check alone, with no sandbox."""
    before = fingerprint(scratch)
    for sql in attempts(scratch):
        record = outcome_of(scratch, sql, sandbox=False)
        assert "rejected" in record, f"{sql} -> {record}"
        assert fingerprint(scratch) == before, sql


def test_no_route_reads_the_dotenv_file(scratch):
    before = fingerprint(scratch)
    for sql in reads_of_dotenv(scratch):
        record = outcome_of(scratch, sql, sandbox=False)
        assert "rejected" in record or "error" in record, f"{sql} -> {record}"
        assert "rows" not in record, sql
        assert CANARY not in json.dumps(record), sql
        assert fingerprint(scratch) == before, sql


@needs_sandbox
def test_sandbox_alone_stops_the_writes_that_got_through_duckdb(scratch):
    """With the check bypassed, the kernel sandbox still stops every write."""
    before = fingerprint(scratch)
    data = scratch.paths.data.resolve()
    for sql in (
        "SELECT * FROM ducklake_cleanup_old_files('lake', "
        "cleanup_all => true)",
        "SELECT * FROM ducklake_delete_orphaned_files('lake', "
        "cleanup_all => true)",
        f"COPY (SELECT 1) TO '{data}/x.csv'",
        f"COPY lake.raw.items TO '{data}/x.parquet'",
        f"COPY (SELECT 1) TO '{scratch.paths.catalog.resolve()}'",
        f"EXPORT DATABASE '{data}/x'",
        "CHECKPOINT lake",
    ):
        run_worker(scan_request(scratch, sql), 20.0, sandbox=True)
        assert fingerprint(scratch) == before, sql


def sandboxed_python(settings, code):
    """Run Python directly inside the console's profile, below DuckDB."""
    catalog = str(settings.paths.catalog.resolve())
    data = str(settings.paths.data.resolve())
    return subprocess.run(
        [
            SANDBOX_EXEC,
            "-p",
            sandbox_profile(catalog, data),
            sys.executable,
            "-I",
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@needs_sandbox
def test_sandbox_alone_denies_reading_secrets(scratch):
    canary = scratch.paths.catalog.resolve().parent / ".env"
    canary.write_text("ANTHROPIC_API_KEY=canary-value")
    result = sandboxed_python(scratch, f"print(open({str(canary)!r}).read())")
    assert result.returncode != 0
    assert "canary-value" not in result.stdout
    assert "Operation not permitted" in result.stderr


@needs_sandbox
def test_sandbox_alone_denies_home_directory_reads(scratch):
    target = Path.home() / ".ssh"
    code = f"import os; print(open(os.path.join({str(target)!r}, 'config')))"
    result = sandboxed_python(scratch, code)
    assert result.returncode != 0


@needs_sandbox
def test_sandbox_alone_denies_the_network(scratch):
    code = (
        "import socket; s = socket.socket(); s.settimeout(3); "
        "s.connect(('127.0.0.1', 8000))"
    )
    result = sandboxed_python(scratch, code)
    assert result.returncode != 0
    assert "not permitted" in result.stderr.lower()


@needs_sandbox
def test_sandbox_alone_denies_running_programs(scratch):
    code = "import os; os.execv('/bin/ls', ['ls'])"
    result = sandboxed_python(scratch, code)
    assert result.returncode != 0
    assert "not permitted" in result.stderr.lower()


@needs_sandbox
def test_sandbox_alone_denies_writing_the_lake(scratch):
    target = scratch.paths.data.resolve() / "x.parquet"
    result = sandboxed_python(scratch, f"open({str(target)!r}, 'w')")
    assert result.returncode != 0
    assert not target.exists()


@needs_sandbox
def test_memory_ceiling_stops_a_large_result(scratch):
    outcome = run_worker(
        console_request(
            scratch, "SELECT repeat('x', 500000000) FROM range(20)"
        ),
        20.0,
        sandbox=True,
    )
    assert (
        outcome.failed
        or outcome.timed_out
        or any(record.get("error") for record in outcome.records)
    )


def test_column_names_count_toward_the_limits(scratch):
    name = "n" * 5000
    columns = ", ".join(f'1 AS "{name}{i}"' for i in range(300))
    outcome = run_worker(
        console_request(scratch, f"SELECT {columns}"), 20.0, sandbox=False
    )
    record = outcome.records[0]
    assert len(record["columns"]) <= 200
    assert all(len(column) <= 200 for column in record["columns"])
    assert record["truncated"] is True


@needs_sandbox
def test_console_runs_reads_in_the_sandbox(scratch):
    data = scratch.paths.data.resolve()
    for sql, expected in (
        ("SELECT count(*) AS n FROM lake.raw.items", [[9000]]),
        (
            "SELECT count(*) FROM lake.raw.items WHERE item_id = 'i0-0'",
            [[1]],
        ),
        ("WITH a AS (SELECT 1 AS x) SELECT * FROM a", [[1]]),
        ("SELECT snapshot_id FROM lake.snapshots() LIMIT 1", None),
        ("SELECT * FROM ducklake_table_info('lake') LIMIT 1", None),
        (
            "SELECT * FROM ducklake_list_files('lake', 'items', "
            "schema => 'raw') LIMIT 1",
            None,
        ),
        (
            "SELECT * FROM __ducklake_metadata_lake.main.ducklake_metadata "
            "LIMIT 1",
            None,
        ),
        ("SELECT table_name FROM information_schema.tables LIMIT 1", None),
        ("EXPLAIN SELECT count(*) FROM lake.raw.items", None),
        ("DESCRIBE lake.raw.items", None),
        ("SUMMARIZE lake.raw.items", None),
        (
            f"SELECT count(*) FROM read_parquet('{data}/raw/items/*.parquet')",
            None,
        ),
    ):
        record = outcome_of(scratch, sql, sandbox=True)
        assert "rows" in record, f"{sql} -> {record}"
        if expected is not None:
            assert record["rows"] == expected, sql


@needs_sandbox
def test_sandboxed_read_works_with_no_wal_files_present(scratch):
    fingerprint(scratch)
    catalog = scratch.paths.catalog
    assert not catalog.with_name(f"{catalog.name}-wal").exists()
    record = outcome_of(
        scratch, "SELECT count(*) FROM lake.raw.items", sandbox=True
    )
    assert record["rows"] == [[9000]]


@needs_sandbox
def test_sandboxed_read_and_block_beside_a_live_writer(scratch):
    """The real lake always has a writer, so the scratch lake gets one too."""
    conn = connect(scratch)
    try:
        writer = BatchWriter(conn, scratch)
        writer.add("nlp.health", [{"recorded_at": datetime.now(UTC)}])
        writer.flush()
        record = outcome_of(
            scratch, "SELECT count(*) FROM lake.nlp.health", sandbox=True
        )
        assert record["rows"] == [[1]]
        data = scratch.paths.data.resolve()
        files = sorted(data.rglob("*.parquet"))
        for sql in (
            "SELECT * FROM ducklake_cleanup_old_files('lake', "
            "cleanup_all => true)",
            "SELECT * FROM ducklake_delete_orphaned_files('lake', "
            "cleanup_all => true)",
        ):
            run_worker(scan_request(scratch, sql), 20.0, sandbox=True)
            assert sorted(data.rglob("*.parquet")) == files, sql
    finally:
        conn.close()


@needs_sandbox
def test_results_are_capped(scratch):
    outcome = run_worker(
        {
            **console_request(scratch, "SELECT * FROM range(5000)"),
            "max_rows": 1000,
        },
        20.0,
        sandbox=True,
    )
    record = outcome.records[0]
    assert len(record["rows"]) == 1000
    assert record["truncated"]


@needs_sandbox
def test_large_results_are_cut_by_size(scratch):
    outcome = run_worker(
        console_request(
            scratch, "SELECT repeat('x', 100000) AS a FROM range(1000)"
        ),
        20.0,
        sandbox=True,
    )
    record = outcome.records[0]
    assert record["truncated"]
    assert len(record["rows"][0][0]) == 4000
    assert sum(len(row[0]) for row in record["rows"]) <= 2_000_000


@pytest.fixture(scope="module")
def client(scratch):
    with TestClient(
        create_app(scratch),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000),
    ) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def under(client) -> Under:
    return client.app.state.web.under


@needs_sandbox
def test_route_rejects_and_logs(client):
    with capture_logs() as logs:
        response = client.post(
            "/under/sql",
            data={
                "sql": "SELECT * FROM ducklake_cleanup_old_files('lake', "
                "cleanup_all => true)"
            },
        )
    assert response.status_code == 200
    assert "Rejected before it ran" in response.text
    assert "ducklake_cleanup_old_files" in response.text
    entry = next(log for log in logs if log["event"] == "under_sql")
    assert entry["status"] == "rejected"
    assert "ducklake_cleanup_old_files" in entry["sql"]


@needs_sandbox
def test_route_runs_a_select_and_escapes_results(client):
    response = client.post(
        "/under/sql", data={"sql": "SELECT '<b>x</b>' AS value"}
    )
    assert "&lt;b&gt;x&lt;/b&gt;" in response.text
    assert "<b>x</b>" not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "evil.example"},
        {"origin": "https://evil.example"},
    ],
)
def test_console_refuses_other_hosts_and_origins(client, headers):
    assert client.get("/under/sql", headers=headers).status_code == 403
    response = client.post(
        "/under/sql", data={"sql": "SELECT 1"}, headers=headers
    )
    assert response.status_code == 403


def test_console_refuses_a_remote_peer(scratch):
    with TestClient(
        create_app(scratch), client=("203.0.113.9", 5000)
    ) as remote:
        assert remote.get("/under/sql").status_code == 403
        assert (
            remote.post("/under/sql", data={"sql": "SELECT 1"}).status_code
            == 403
        )


@needs_sandbox
def test_query_killed_at_the_limit_leaves_the_dashboard_responsive(
    client, under, monkeypatch
):
    """One query at a time, stopped at the limit, the dashboard serving."""
    monkeypatch.setattr("monitor.web.under.CHILD_LIMIT_SECONDS", 3.0)
    result = {}

    def run():
        result.update(under.console(HEAVY))

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)
    busy = under.console("SELECT 1")
    assert "Another console query is running" in busy["error"]
    slowest = 0.0
    served = 0
    while thread.is_alive():
        started = time.monotonic()
        response = client.get("/frag/kpis?window=24h")
        slowest = max(slowest, time.monotonic() - started)
        assert response.status_code == 200
        served += 1
        time.sleep(0.05)
    thread.join()
    assert result["status"] == "stopped"
    assert "Stopped at" in result["error"]
    assert 2.5 < result["seconds"] < 6.0
    assert served >= 5
    assert slowest < 1.0
    assert under.console("SELECT 1 AS n")["rows"] == [[1]]
