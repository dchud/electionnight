"""The under-the-hood views, against a scratch lake."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import zstandard
from conftest import clone_lake
from fastapi.testclient import TestClient

from monitor.lake import BatchWriter, connect
from monitor.web.app import create_app
from monitor.web.under import (
    Under,
    child_env,
    key_sql,
    run_worker,
    score_agreement,
    table_key,
)
from monitor.web.under_coverage import (
    failure_group,
    read_replacement_failures,
    read_report,
    state_reading,
)

NOW = datetime.now(UTC) - timedelta(minutes=5)
OLD = NOW - timedelta(hours=3)
CAPTURE = NOW - timedelta(hours=1)
ALERT = "<script>alert(1)</script>"
PAGE_ARCHIVE = "pages-20261101T20-20261101T200000.rec.zst"
FEED_ARCHIVE = "feeds-20261101T20-20261101T200000.rec.zst"
GONE_ARCHIVE = "pages-20261101T19-20261101T190000.rec.zst"
FEED_URL = "https://example.com/feed.xml"
VIEWS = (
    "/under",
    "/under/coverage",
    "/under/inventory",
    "/under/storage",
    "/under/data-model",
    "/under/models",
    "/under/trace",
    "/under/trace/cap1",
    "/under/trace/old1",
    "/under/records",
    "/under/feeds",
    "/under/costs",
    "/under/sql",
)
REPORT = """# Feed validation

Generated 2026-11-01 18:10 UTC.

## Outlets with no healthy feed (3)

| Outlet | Scope | State | Tier | Most common failure |
|---|---|---|---|---|
| Associated Press | wire | - | 1 | HTTP 403 |
| Gone Gazette | local | GA | 2 | HTTP 404 |
| Quiet Courier | state | GA | 3 | only 0 entries |

## Healthy outlets by scope

| Scope | Outlets |
|---|---|
| state | 2 |
"""


def source(source_id, name, **fields):
    return {
        "source_id": source_id,
        "name": name,
        "scope": "state",
        "country": "US",
        "language": "en",
        "lean_label": "Unrated",
        "enabled": True,
        "poll_tier": 2,
        "feed_urls": FEED_URL,
        **fields,
    }


def item(item_id, seen, **fields):
    return {
        "item_id": item_id,
        "source_id": "peach",
        "feed_url": FEED_URL,
        "url": f"https://example.com/{item_id}",
        "canonical_url": f"https://example.com/{item_id}",
        "guid": f"https://example.com/{item_id}",
        "title": f"Title of {item_id} about the Georgia race",
        "summary": "A summary about the election.",
        "first_seen_at": seen,
        "published_at": seen,
        "language": "en",
        "feed_categories": [],
        **fields,
    }


def classified(item_id, **fields):
    return {
        "item_id": item_id,
        "election_relevant": True,
        "relevance_conf": 0.9,
        "us_politics": True,
        "levels": ["state"],
        "primary_level": "state",
        "states": ["GA"],
        "race_types": ["senate"],
        "topics": ["turnout"],
        "free_tags": [],
        "is_opinion": False,
        "is_results_report": False,
        "method": "llm",
        "parser": "classify_a",
        "parser_version": "1",
        **fields,
    }


def write_archive(path, records):
    """Records in the archive format: JSON header, bytes, newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor()
    with path.open("wb") as handle, compressor.stream_writer(handle) as out:
        for key, body in records:
            header = {
                "kind": path.parent.name,
                "key": key,
                "source_id": "peach",
                "fetched_at": NOW.isoformat(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "n_bytes": len(body),
            }
            out.write(json.dumps(header).encode() + b"\n")
            out.write(body)
            out.write(b"\n")


def seed(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "ref.sources",
        [
            source("peach", "Peach Post", home_state="GA", scope="local"),
            source(
                "capitol",
                "Capitol Chronicle",
                home_state="GA",
                notes="Covers GA + AL",
            ),
            source(
                "times",
                "National Times",
                scope="national",
                lean_label="LL",
                lean_source="AllSides",
                lean_source_url="https://www.allsides.com/news-source/times",
            ),
            source(
                "abroad",
                "Abroad News",
                scope="international",
                country="GB",
                lean_label="NA",
                language="fr",
            ),
            source(
                "only-ms",
                "Mississippi One",
                home_state="MS",
            ),
            source(
                "associated-press",
                "Associated Press",
                scope="wire",
                poll_tier=1,
                enabled=False,
                feed_urls="",
            ),
            source(
                "gone",
                "Gone Gazette",
                home_state="GA",
                scope="local",
                enabled=False,
                feed_urls="",
            ),
            source(
                "quiet",
                "Quiet Courier",
                home_state="GA",
                enabled=False,
                feed_urls="",
            ),
        ],
    )
    writer.add(
        "ref.states",
        [
            {"usps": "GA", "name": "Georgia", "battleground": True},
            {"usps": "MS", "name": "Mississippi", "battleground": False},
            {"usps": "DC", "name": "District of Columbia"},
        ],
    )
    writer.add(
        "ref.races",
        [
            {"race_key": f"2026-GA-H{n}", "state": "GA", "office": "house"}
            for n in range(14)
        ]
        + [
            {"race_key": f"2026-MS-H{n}", "state": "MS", "office": "house"}
            for n in range(4)
        ],
    )
    writer.add(
        "raw.items",
        [
            item(
                "cap1",
                NOW,
                raw_entry=json.dumps(
                    {
                        "id": "cap1",
                        "title": "Rich entry title",
                        "summary": "Rich entry summary",
                    }
                ),
            ),
            item("old1", OLD, raw_entry=json.dumps({"id": "old1"})),
            item("xss", NOW, title=ALERT, raw_entry=json.dumps({"a": ALERT})),
            item(
                "backlog",
                NOW,
                published_at=NOW - timedelta(days=4),
                source_id="times",
            ),
        ],
    )
    writer.add(
        "nlp.classify_a",
        [
            classified("cap1"),
            classified("old1", method="keywords"),
            classified("xss"),
            classified("backlog", election_relevant=False, states=[]),
        ],
    )
    writer.add(
        "nlp.lang",
        [
            {
                "item_id": "cap1",
                "language": "en",
                "confidence": 1.0,
                "method": "feed+detected",
                "parser": "lang",
                "parser_version": "1",
            }
        ],
    )
    writer.add(
        "nlp.embeddings",
        [
            {
                "item_id": "cap1",
                "model": "test-model",
                "vec": [0.1] * 384,
                "text_basis": "title+summary",
                "dim": 384,
                "parser": "embed",
                "parser_version": "1",
            }
        ],
    )
    writer.add(
        "nlp.dup_groups",
        [
            {
                "item_id": "cap1",
                "dup_group_id": "g-cap1",
                "is_canonical": True,
                "method": "none",
                "parser": "dedupe",
                "parser_version": "1",
            },
            {
                "item_id": "old1",
                "dup_group_id": "g-old1",
                "is_canonical": True,
                "method": "url",
                "parser": "dedupe",
                "parser_version": "1",
            },
        ],
    )
    writer.add(
        "nlp.storyline_assign",
        [
            {
                "item_id": "cap1",
                "storyline_id": "s1",
                "similarity": 1.0,
                "assigned_at": NOW,
                "source_id": "peach",
                "parser": "storylines",
                "parser_version": "2",
            }
        ],
    )
    writer.add(
        "nlp.storylines",
        [
            {
                "storyline_id": "s1",
                "version": 1,
                "label": "Georgia race",
                "status": "active",
                "first_seen_at": NOW,
                "last_item_at": NOW,
                "n_items": 1,
                "n_sources": 1,
            }
        ],
    )
    writer.add(
        "nlp.cluster_decisions",
        [
            {
                "item_id": "cap1",
                "stage": "storylines",
                "decision": "new",
                "nearest_id": "s0",
                "nearest_similarity": 0.71,
                "threshold": 0.92,
                "parser": "storylines",
                "parser_version": "2",
            }
        ],
    )
    writer.add(
        "nlp.parser_runs",
        [
            {
                "item_id": "cap1",
                "parser": "classify_a",
                "parser_version": "1",
                "status": "ok",
            },
            {
                "item_id": "cap1",
                "parser": "fetch_text",
                "parser_version": "1",
                "status": "skipped",
            },
        ],
    )
    writer.add(
        "nlp.mentions",
        [
            {
                "mention_id": "m1",
                "item_id": "cap1",
                "surface": "Jane Doe",
                "ent_type": "PERSON",
                "entity_id": "e1",
                "tone": -1.0,
                "tone_evidence": "A quote about Jane Doe.",
                "resolved_by": "exact",
                "parser": "extract_b",
                "parser_version": "1",
            },
            {
                "mention_id": "m2",
                "item_id": "xss",
                "surface": ALERT,
                "ent_type": "PERSON",
                "parser": "extract_b",
                "parser_version": "1",
            },
        ],
    )
    writer.add(
        "ref.entities",
        [{"entity_id": "e1", "canonical_name": "Jane Q. Doe", "party": "D"}],
    )
    writer.add(
        "nlp.llm_prompts",
        [
            {
                "prompt_hash": "p1",
                "purpose": "classify_a",
                "text": "You classify news items.",
            },
            {
                "prompt_hash": "p1",
                "purpose": "classify_a",
                "text": "You classify news items.",
            },
        ],
    )
    writer.add(
        "nlp.llm_traces",
        [
            {
                "call_id": "call1",
                "batch_id": "b1",
                "purpose": "classify_a",
                "prompt_hash": "p1",
                "model_requested": "claude-haiku-5-5",
                "model_served": "claude-haiku-5-5",
                "item_ids": ["other", "cap1"],
                "request_text": json.dumps(
                    [
                        {"item_ref": "0", "title": "Other item"},
                        {"item_ref": "1", "title": "This item's title"},
                    ]
                ),
                "response_text": json.dumps(
                    {
                        "items": [
                            {"item_ref": "0", "election_relevant": False},
                            {"item_ref": "1", "election_relevant": True},
                        ]
                    }
                ),
                "stop_reason": "end_turn",
            }
        ],
    )
    writer.add(
        "nlp.llm_calls",
        [
            {
                "call_id": "call1",
                "purpose": "classify_a",
                "model": "claude-haiku-5-5",
                "input_tokens": 1000,
                "cache_read_tokens": 9000,
                "cache_write_tokens": 0,
                "output_tokens": 500,
                "usd_est": 0.00044,
                "latency_ms": 1200,
                "status": "ok",
            }
        ],
    )
    writer.add(
        "raw.sightings",
        [
            {
                "item_id": "cap1",
                "source_id": "peach",
                "feed_url": FEED_URL,
                "seen_at": NOW,
                "title_hash": "t1",
            },
            # Capture started between the old item and the newer ones.
            {
                "item_id": "old1",
                "source_id": "peach",
                "feed_url": FEED_URL,
                "seen_at": CAPTURE,
                "title_hash": "t0",
                "created_at": CAPTURE,
            },
        ],
    )
    writer.add(
        "raw.fetches",
        [
            {
                "fetch_id": "f1",
                "source_id": "peach",
                "feed_url": FEED_URL,
                "started_at": NOW,
                "duration_ms": 90,
                "http_status": 200,
                "not_modified": False,
                "n_entries": 20,
                "n_new": 3,
            },
            {
                "fetch_id": "f2",
                "source_id": "peach",
                "feed_url": FEED_URL,
                "started_at": NOW,
                "duration_ms": 40,
                "http_status": 304,
                "not_modified": True,
                "n_entries": 0,
                "n_new": 0,
            },
        ],
    )
    writer.add(
        "raw.fetch_details",
        [
            {
                "fetch_id": "f1",
                "n_bytes": 5000,
                "bozo": False,
                "content_type": "application/xml",
                "unchanged": False,
            }
        ],
    )
    page = f"<html><title>{ALERT}</title></html>".encode()
    feed = (
        b"<rss><channel><item><title>Elsewhere</title></item>"
        b"<item><title>Mine</title><guid>https://example.com/cap1</guid>"
        b"</item></channel></rss>"
    )
    archive = settings.paths.archive
    write_archive(archive / "pages" / PAGE_ARCHIVE, [("cap1", page)])
    write_archive(archive / "feeds" / FEED_ARCHIVE, [(FEED_URL, feed)])
    writer.add(
        "raw.archive_index",
        [
            {
                "kind": "pages",
                "key": "cap1",
                "source_id": "peach",
                "fetched_at": NOW,
                "sha256": hashlib.sha256(page).hexdigest(),
                "n_bytes": len(page),
                "archive_file": PAGE_ARCHIVE,
            },
            {
                "kind": "feeds",
                "key": FEED_URL,
                "source_id": "peach",
                "fetched_at": NOW,
                "sha256": hashlib.sha256(feed).hexdigest(),
                "n_bytes": len(feed),
                "archive_file": FEED_ARCHIVE,
            },
            {
                "kind": "pages",
                "key": "xss",
                "source_id": "peach",
                "fetched_at": NOW,
                "sha256": "0" * 64,
                "n_bytes": 10,
                "archive_file": GONE_ARCHIVE,
            },
        ],
    )
    writer.add(
        "gold.source_bins",
        [
            {
                "bin_start": NOW.replace(minute=0, second=0, microsecond=0),
                "source_id": "peach",
                "n_items": n,
                "n_relevant": n,
            }
            for n in (1, 2, 3)
        ],
    )
    writer.add(
        "nlp.maintenance_runs",
        [
            {
                "recorded_at": NOW,
                "kind": "maintenance",
                "tables_merged": 3,
                "files_removed": 1,
                "duration_ms": 700,
            }
        ],
    )
    writer.flush()
    conn.close()
    settings.paths.reports.mkdir(parents=True, exist_ok=True)
    (settings.paths.reports / "feed_validation.md").write_text(REPORT)
    settings.paths.status.write_text(
        json.dumps(
            {
                "updated_at": NOW.isoformat(),
                "started_at": OLD.isoformat(),
                "feeds": {"healthy": 1, "total": 2},
                "feed_details": [
                    {
                        "source_id": "peach",
                        "feed_url": FEED_URL,
                        "failures": 0,
                        "open": False,
                        "last_error": None,
                        "last_success_at": NOW.isoformat(),
                    },
                    {
                        "source_id": "capitol",
                        "feed_url": "https://example.com/c.xml",
                        "failures": 6,
                        "open": True,
                        "last_error": "HTTP 429",
                        "last_success_at": None,
                    },
                ],
                "llm": {
                    "budget": {
                        "state": "ok",
                        "spent_today": 0.1,
                        "spent_total": 0.2,
                        "daily_cap": 40.0,
                        "hard_cap": 75.0,
                    }
                },
            }
        )
    )


@pytest.fixture(scope="module")
def lake_settings(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("under"))
    seed(settings)
    return settings


@pytest.fixture(scope="module")
def client(lake_settings):
    """One app for this module's tests, which only read."""
    with TestClient(create_app(lake_settings)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def under(client) -> Under:
    return client.app.state.web.under


def lake_state(settings):
    """Everything a write would change: catalog rows and data files."""
    with sqlite3.connect(settings.paths.catalog) as catalog:
        names = catalog.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        rows = {
            name: catalog.execute(f'SELECT count(*) FROM "{name}"').fetchone()
            for (name,) in names
        }
    files = sorted(str(p) for p in settings.paths.data.rglob("*.parquet"))
    return rows, files


def test_home_has_one_footer_link_and_no_header_entry(client):
    html = client.get("/").text
    assert html.count('href="/under"') == 1
    assert html.index('href="/under"') > html.index('id="band-5"')
    header = client.get("/status").text.split("<main")[0]
    assert "/under" not in header


@pytest.mark.parametrize("path", VIEWS)
def test_view_renders_with_no_sse_connection(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "sse-connect" not in response.text
    assert "/events" not in response.text
    assert "data-under-refresh" in response.text
    assert 'id="u-snapshot"' in response.text
    assert "Snapshot " in response.text


@pytest.mark.parametrize(
    "path", [v for v in VIEWS if v not in {"/under/sql", "/under/trace"}]
)
def test_view_discloses_its_queries(client, path):
    assert "Show query" in client.get(path).text


def test_views_do_not_write(client, lake_settings):
    before = lake_state(lake_settings)
    for path in VIEWS:
        client.get(path)
    assert lake_state(lake_settings) == before


def test_local_query_discloses_the_string_that_ran(under):
    sql = "SELECT count(*) AS n FROM facts WHERE language = $language"
    ran = under.local(sql, {"language": "en"})
    assert ran.first["n"] == 4
    assert ran.query.sql == sql
    assert ran.query.params == {"language": "en"}


def test_scan_runs_in_a_child_with_the_shown_sql(under):
    sql = "SELECT count(*) AS n FROM lake.raw.items WHERE item_id = $id"
    ran = under.scan("test", [("one", sql, {"id": "cap1"})])["one"]
    assert ran.first["n"] == 1
    assert ran.query.sql == sql
    assert "child process" in ran.query.where


def test_child_environment_holds_no_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    env = child_env()
    assert set(env) == {"HOME", "PATH", "TZ", "LANG"}
    assert "secret-value" not in env.values()


def test_child_is_stopped_at_the_limit_and_keeps_finished_results(
    lake_settings,
):
    outcome = run_worker(
        {
            "mode": "scan",
            "catalog": str(lake_settings.paths.catalog.resolve()),
            "data_path": str(lake_settings.paths.data.resolve()),
            "max_rows": 10,
            "statements": [
                {"name": "quick", "sql": "SELECT 1 AS n"},
                {
                    "name": "slow",
                    "sql": "SELECT count(*) FROM range(100000000000) a, "
                    "range(100000) b",
                },
            ],
        },
        3.0,
    )
    assert outcome.timed_out
    assert [record["name"] for record in outcome.records] == ["quick"]


def test_stopped_scan_says_why(under, monkeypatch):
    monkeypatch.setattr("monitor.web.under.CHILD_LIMIT_SECONDS", 2.0)
    slow = "SELECT count(*) FROM range(100000000000) a, range(100000) b"
    ran = under.scan("stopped", [("slow", slow, None)])["slow"]
    assert "Not finished" in ran.error
    assert ran.rows == []


# Coverage


def test_failure_groups():
    assert failure_group("HTTP 403") == "blocked"
    assert failure_group("HTTP 402") == "blocked"
    assert failure_group("HTTP 429") == "blocked"
    assert failure_group("HTTP 404") == "no_feed"
    assert failure_group("only 0 entries") == "stale"
    assert failure_group("newest entry is 40 days old") == "stale"
    assert failure_group("connection failed") == "other"
    assert failure_group(None) == "other"


def test_report_parsing(tmp_path):
    path = tmp_path / "report.md"
    path.write_text(REPORT)
    report = read_report(path)
    assert report["reasons"]["Associated Press"] == "HTTP 403"
    assert report["reasons"]["Quiet Courier"] == "only 0 entries"
    assert "Scope" not in report["reasons"]
    assert report["generated"] == "2026-11-01 18:10 UTC"
    assert not read_report(tmp_path / "missing.md")["found"]
    assert read_replacement_failures(tmp_path / "missing.md") == {}


def test_state_reading_does_not_hide_a_large_state_with_few_stories():
    assert state_reading(0.08, 1, 12.6) == "light"
    assert state_reading(0.5, 2, 3.0) == "too few"
    assert state_reading(2.0, 12, 6.0) == "heavy"
    assert state_reading(2.0, 2, 1.0) == "too few"
    assert state_reading(1.0, 10, 10.0) == ""
    assert state_reading(None, 0, 0.0) == ""


def test_coverage_names_the_gaps(client):
    html = client.get("/under/coverage?window=24h").text
    assert "Associated Press" in html
    assert "District of Columbia" in html
    assert "Mississippi One" in html
    assert "Blocked by the site" in html
    assert "No feed found" in html
    assert "Stale or empty feed" in html
    assert "AllSides search-result text" in html
    assert "Seats stand in for population" in html
    assert "Conclusions this data cannot support" in html
    assert "Tone by lean in local coverage" in html
    assert "Covers GA + AL" in html


def test_coverage_leads_with_the_unrated_share(client):
    html = client.get("/under/coverage?window=24h").text
    hero = html[html.index("u-stat-hero") :]
    # Five enabled outlets; three unrated, one rated, one non-US.
    assert hero.index("60%") < 200
    assert "3 of 5" in hero[:400]
    assert "1 carry a rating" in hero[:400]


def test_coverage_counts_are_right(under, client):
    from monitor.web.filters import Filters
    from monitor.web.under_coverage import coverage

    data = coverage(under, Filters.parse({}, "24h"))
    by_state = {row["usps"]: row for row in data["states_alpha"]}
    assert by_state["GA"]["enabled_outlets"] == 2
    assert by_state["GA"]["listed_outlets"] == 4
    assert by_state["GA"]["target"] == 4
    assert by_state["GA"]["gap"] == 2
    assert by_state["MS"]["target"] == 2
    assert by_state["MS"]["gap"] == 1
    assert by_state["DC"]["enabled_outlets"] == 0
    assert by_state["GA"]["seat_share"] == pytest.approx(14 / 18)
    assert by_state["GA"]["stories"] == 3
    assert {s["usps"] for s in data["sparse"]} == {"DC", "MS"}
    groups = {g["key"]: g["n"] for g in data["groups"]["groups"]}
    assert groups == {"blocked": 1, "no_feed": 1, "stale": 1}
    lean = {row["bucket"]: row for row in data["lean"]}
    assert lean["Unrated"]["enabled_outlets"] == 3
    assert lean["LL"]["enabled_outlets"] == 1
    assert lean["Non-US"]["enabled_outlets"] == 1
    assert data["rated_total"] == 1


# Inventory


def test_inventory_shows_more_rows_than_keys(under):
    from monitor.web.filters import Filters

    data = under.inventory(Filters.parse({}, "24h"))
    rows = {row["table"]: row for row in data["table_rows"]}
    assert rows["gold.source_bins"]["scan_rows"] == 3
    assert rows["gold.source_bins"]["keys"] == 1
    assert rows["gold.source_bins"]["extra"] == 2
    assert rows["nlp.llm_prompts"]["extra"] == 1
    assert rows["raw.items"]["rows"] == 4
    assert rows["raw.items"]["extra"] == 0


def test_catalog_row_counts_match_scans(under):
    from monitor.web.filters import Filters

    data = under.inventory(Filters.parse({}, "24h"))
    for row in data["table_rows"]:
        assert row["rows"] == row["scan_rows"], row["table"]


def test_inventory_labels_rows_and_backlog(client):
    html = client.get("/under/inventory?window=24h").text
    assert "Rows are not records" in html
    assert "Most early items were backlog" in html
    assert "first feed to serve" in html
    assert "<code>gold.*</code> bins use" in html


def test_a_table_with_no_listed_key_still_renders():
    assert table_key("raw.something_new")[0] == ""
    assert key_sql("raw.something_new") == "NULL"
    assert key_sql("raw.items") == "item_id"
    assert key_sql("gold.source_bins") == "(bin_start, source_id)"


# Storage and data model


def test_storage_reports_inlined_and_parquet_rows(under):
    rows = {
        f"{row['schema_name']}.{row['table_name']}": row
        for row in under.catalog_tables().rows
    }
    assert rows["raw.items"]["total_rows"] == 4
    assert rows["raw.items"]["inlined_rows"] == 4
    assert rows["raw.items"]["parquet_rows"] == 0
    embeddings = rows["nlp.embeddings"]
    assert embeddings["parquet_rows"] == 1
    assert embeddings["inlined_rows"] == 0
    assert embeddings["parquet_files"] == 1


def test_storage_shows_column_compression_for_a_parquet_table(client):
    html = client.get("/under/storage?table=nlp.embeddings").text
    assert "ZSTD" in html
    assert "_ducklake_internal_snapshot_id" in html
    assert "AT (VERSION =&gt; " in html


def test_storage_says_when_a_table_has_no_file(client):
    html = client.get("/under/storage?table=raw.items").text
    assert "has no Parquet file" in html


def test_time_travel_compares_two_snapshots(client, under):
    first = under.catalog(
        "SELECT min(snapshot_id) AS a, max(snapshot_id) AS b "
        "FROM lake.snapshots()"
    ).first
    html = client.get(
        f"/under/storage?table=nlp.mentions&a={first['b']}&b={first['b']}"
    ).text
    assert f"AT (VERSION =&gt; {first['b']})" in html
    assert "0 rows were added" in html


def test_a_table_name_outside_the_list_is_ignored(client):
    html = client.get(
        "/under/storage", params={"table": "raw.items; DROP TABLE x"}
    ).text
    assert "DROP TABLE" not in html
    assert "lake.raw.items" in html


def test_data_model_shows_three_views_of_a_table(client):
    html = client.get("/under/data-model?table=nlp.embeddings").text
    assert "vec FLOAT[]" in html
    assert "information_schema.columns" in html
    assert "FLOAT[]" in html
    assert "parquet_schema" in html
    assert "(384 values)" in html
    assert "BatchA" in html
    assert "additionalProperties" in html


def test_data_model_with_no_file_says_so(client):
    html = client.get("/under/data-model?table=nlp.race_calls").text
    assert "has no Parquet file" in html
    assert "This table has no rows." in html


# Model map


def test_model_map_labels_misleading_values(client):
    html = client.get("/under/models").text
    assert "No URL was compared" in html
    assert "placeholder for an item that started a storyline" in html
    assert "means no match" in html
    assert "You classify news items." in html


def test_keyword_agreement_runs_on_stored_items(client, under):
    data = under.agreement()
    assert "error" not in data
    assert data["all"]["n"] >= 2
    assert "Neither column is the truth" in client.get("/under/models").text


def test_score_agreement_counts_the_table():
    rows = [
        {
            "item_id": str(n),
            "language": language,
            "llm_relevant": llm,
            "kw_relevant": keyword,
            "llm_states": ["GA"],
            "kw_states": ["GA"] if keyword else [],
            "llm_topics": [],
            "kw_topics": [],
            "llm_level": "state",
            "kw_level": "state",
        }
        for n, (language, llm, keyword) in enumerate(
            [
                ("en", True, True),
                ("en", True, False),
                ("fr", False, True),
                ("fr", False, False),
            ]
        )
    ]
    score = score_agreement(rows)
    assert score["all"]["both"] == 1
    assert score["all"]["llm_only"] == 1
    assert score["all"]["kw_only"] == 1
    assert score["all"]["neither"] == 1
    assert score["all"]["agree"] == 0.5
    assert score["all"]["kappa"] == pytest.approx(0.0)
    assert score["all"]["recall"] == 0.5
    assert score["english"]["n"] == 2
    assert score["other"]["n"] == 2
    assert score["state_exact"] == 1.0


# Trace


def test_trace_follows_every_stage(client):
    html = client.get("/under/trace/cap1").text
    assert "nlp.classify_a" in html
    assert "raw.article_text" in html
    assert "Rich entry title" in html
    assert "ducklake_inlined_data" in html
    assert "You classify news items." in html
    assert "This item&#39;s title" in html
    assert "call1" in html
    assert "usd_est" in html
    assert "Jane Q. Doe" in html
    assert "exact" in html
    assert "placeholder" in html
    assert "0.710" in html
    assert "Panels read the fact table" in html
    assert "older than capture" not in html


def test_trace_of_a_batch_item_finds_its_slice(under):
    from monitor.web.filters import Filters
    from monitor.web.under_trace import trace

    data = trace(under, "cap1", Filters.parse({}, "24h"))
    parts = data["calls"][0]["parts"]
    assert parts["index"] == "1"
    assert "This item's title" in parts["request"]
    assert '"election_relevant": true' in parts["response"]


def test_trace_of_an_old_item_says_what_is_absent(client):
    html = client.get("/under/trace/old1").text
    assert "older than capture" in html
    assert "rich raw entries started when capture did" in html
    assert "no request or response was recorded" in html
    assert "No URL was compared" in html
    assert "No decision is recorded" in html


def test_trace_counts_panels_with_each_panels_filter(under):
    from monitor.web.filters import Filters
    from monitor.web.under_trace import panel_checks

    filters = Filters.parse({}, "24h")
    checks = {
        row["panel"]: row for row in panel_checks(under, "cap1", filters)
    }
    assert checks["Live Wire"]["counts"]
    assert checks["US Map"]["counts"]
    assert checks["Storyline Stream"]["counts"]
    assert not checks["Global Press"]["counts"]
    assert not checks["Race Calls in the News"]["counts"]
    assert "FROM facts f" in checks["Live Wire"]["query"].sql
    out = panel_checks(under, "backlog", filters)
    assert not any(row["counts"] for row in out)


def test_trace_of_a_moved_item_explains_the_axis(client):
    html = client.get("/under/trace/backlog").text
    assert "over 2 hours before it was first seen" in html


def test_unknown_item_is_a_clear_404(client):
    response = client.get("/under/trace/nope")
    assert response.status_code == 404
    assert "No such item" in response.text
    assert client.get("/under/trace/a%20b%3B").status_code == 404


def test_archive_record_is_read_and_verified(client):
    html = client.get("/under/trace/cap1/archive/pages").text
    assert "matches the index" in html
    assert "&lt;html&gt;" in html
    html = client.get("/under/trace/cap1/archive/feeds").text
    assert "The entry for this item" in html
    assert "&lt;title&gt;Mine&lt;/title&gt;" in html
    assert "Elsewhere" not in html


def test_missing_archive_file_says_so(client):
    html = client.get("/under/trace/xss/archive/pages").text
    assert "is gone" in html
    assert "does not fetch from S3" in html


def test_item_with_no_archive_row_says_so(client):
    html = client.get("/under/trace/old1/archive/pages").text
    assert "No archived page is recorded" in html


def test_stored_text_is_escaped(client):
    for path in (
        "/under/trace/xss",
        "/under/records",
        "/under/trace/cap1/archive/pages",
    ):
        html = client.get(path).text
        assert ALERT not in html, path
        assert "&lt;script&gt;alert(1)" in html, path
    for table in ("raw.items", "nlp.mentions"):
        html = client.get(f"/under/data-model?table={table}").text
        assert ALERT not in html, table


# Records


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, {"cap1", "old1", "xss", "backlog"}),
        ({"id": "cap"}, {"cap1"}),
        ({"url": "EXAMPLE.COM/OLD"}, {"old1"}),
        ({"outlet": "times"}, {"backlog"}),
        ({"feed": "example.com/feed"}, {"cap1", "old1", "xss", "backlog"}),
        ({"feed": "no-such-feed"}, set()),
        ({"language": "en"}, {"cap1", "old1", "xss", "backlog"}),
        ({"language": "fr"}, set()),
        ({"method": "keywords"}, {"old1"}),
        ({"status": "not_relevant"}, {"backlog"}),
        ({"status": "in_storyline"}, {"cap1"}),
        ({"run": "fetch_text:skipped"}, {"cap1"}),
        ({"text": "script"}, {"xss"}),
        ({"text": "georgia", "method": "llm"}, {"cap1", "backlog"}),
    ],
)
def test_record_filters(under, params, expected):
    from monitor.web.under_trace import records

    data = records(under, params)
    assert {row["item_id"] for row in data["rows"]} == expected
    assert data["total"] == len(expected)


def test_records_show_stored_values_and_link_to_the_trace(client):
    html = client.get("/under/records?id=old1").text
    assert 'href="/under/trace/old1"' in html
    assert FEED_URL in html
    assert "keywords" in html
    assert "first feed to serve" in html


def test_a_bad_filter_value_is_data_not_sql(client):
    response = client.get("/under/records", params={"text": "'; DROP --"})
    assert response.status_code == 200
    assert "0 items match" in response.text


def test_article_page_links_to_the_trace(client):
    assert 'href="/under/trace/cap1"' in client.get("/article/cap1").text


# Feeds and costs


def test_feeds_view(client):
    html = client.get("/under/feeds").text
    assert "304 not modified" in html
    assert "200 with new items" in html
    assert "HTTP 429" in html
    assert "circuit open" in html
    assert "first feed" in html or "whichever feed served it first" in html


def test_costs_compute_the_caching_saving(under, client):
    data = under.costs()
    row = data["rows"][0]
    assert row["purpose"] == "classify_a"
    # Haiku at $0.10 and $0.50 per million; cache reads cost a tenth.
    assert row["uncached"] == pytest.approx(
        (10_000 * 0.10 + 500 * 0.50) / 1_000_000
    )
    assert row["actual"] == pytest.approx(
        (1000 * 0.10 + 9000 * 0.01 + 500 * 0.50) / 1_000_000
    )
    assert row["saved"] > 0
    stages = {stage["purpose"]: stage for stage in data["stage_rows"]}
    assert stages["classify_a"]["n"] == 3
    html = client.get("/under/costs").text
    assert "not an invoice" in html
    assert "SDK retries are not counted" in html


def test_refresh_script_has_no_sse_or_email(client):
    script = client.get("/static/js/under.js").text
    assert "EventSource" not in script
    assert "mailto" not in script


def test_trace_cuts_article_text_to_an_excerpt():
    from monitor.web.under_trace import (
        ARTICLE_EXCERPT_CHARS,
        trim_article_text,
    )

    text = "word " * 1000
    stored = json.dumps({"outlet": "Wire", "title": "A title", "text": text})
    request = json.loads(trim_article_text(stored))
    assert request["title"] == "A title"
    assert request["text"].startswith(text[:ARTICLE_EXCERPT_CHARS])
    assert (
        f"{len(text) - ARTICLE_EXCERPT_CHARS} more characters"
        in request["text"]
    )
    assert len(request["text"]) < ARTICLE_EXCERPT_CHARS + 80


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "not json",
        json.dumps([{"title": "A batch"}]),
        json.dumps({"text": "Short."}),
        json.dumps({"title": "No text"}),
    ],
)
def test_trace_leaves_other_requests_alone(stored):
    from monitor.web.under_trace import trim_article_text

    assert trim_article_text(stored) == stored
