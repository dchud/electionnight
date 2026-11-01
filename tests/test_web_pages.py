"""The drill-down and reference pages, against a scratch lake."""

import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient

from monitor.lake import BatchWriter, connect
from monitor.web import reference
from monitor.web.app import create_app
from monitor.web.charts import specs
from monitor.web.drilldown import (
    EXTRACT_CHARS,
    cut_extract,
    highlight,
    locate_spans,
    race_label,
)
from monitor.web.filters import race_state_pair, read_unwatched, read_watch
from monitor.web.reference import censorship_note, feed_state, lean_display
from monitor.web.vectors import SearchUnavailable, VectorIndex, Vectors

NOW = datetime.now(UTC) - timedelta(minutes=5)
DIM = 384
LONG_TAIL = "THE-LAST-WORDS-OF-THE-ARTICLE"
ARTICLE_TEXT = (
    "Jane Doe visited Fulton County in Georgia on Tuesday. "
    "Officials said <b>lines</b> were long. Jane Doe thanked voters. "
    + "Filler sentence about the polls. " * 200
    + LONG_TAIL
)
ALERT = "<script>alert(1)</script>"


def axis(index: int, wobble: float = 0.0, other: int = 383) -> list[float]:
    """A vector along one axis, tilted slightly toward another."""
    vector = [0.0] * DIM
    vector[index] = 1.0
    vector[other] = wobble
    return vector


def item(item_id: str, source_id: str, title: str, **fields) -> dict:
    return {
        "item_id": item_id,
        "source_id": source_id,
        "title": title,
        "summary": fields.pop("summary", f"Summary of {item_id}."),
        "url": fields.pop("url", f"https://example.com/{item_id}"),
        "first_seen_at": fields.pop("first_seen_at", NOW),
        "published_at": fields.pop("published_at", NOW),
        "language": fields.pop("language", "en"),
        "feed_categories": [],
        **fields,
    }


def classify(item_id: str, **fields) -> dict:
    return {
        "item_id": item_id,
        "election_relevant": fields.pop("relevant", True),
        "relevance_conf": 0.9,
        "us_politics": True,
        "levels": fields.pop("levels", ["state"]),
        "primary_level": fields.pop("primary_level", "state"),
        "states": fields.pop("states", ["GA"]),
        "race_types": fields.pop("race_types", ["senate"]),
        "topics": fields.pop("topics", ["turnout"]),
        "free_tags": [],
        "is_opinion": fields.pop("is_opinion", False),
        "is_results_report": False,
        "method": "llm",
        **fields,
    }


def mention(
    mention_id: str,
    item_id: str,
    entity_id: str | None,
    surface: str = "Jane Doe",
    tone: float | None = None,
    evidence: str | None = None,
    **fields,
) -> dict:
    return {
        "mention_id": mention_id,
        "item_id": item_id,
        "surface": surface,
        "ent_type": fields.pop("ent_type", "PERSON"),
        "role": fields.pop("role", "candidate"),
        "entity_id": entity_id,
        "tone": tone,
        "tone_evidence": evidence,
        **fields,
    }


def seed(settings) -> None:
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "ref.sources",
        [
            {
                "source_id": "wire",
                "name": "Wire Service",
                "scope": "wire",
                "country": "US",
                "language": "en",
                "lean_label": "C",
                "lean_source": "AllSides",
                "lean_source_url": "https://www.allsides.com/wire",
                "lean_retrieved_on": "2026-11-01",
                "paywall": "none",
                "feed_urls": "https://wire.example/a|https://wire.example/b",
                "enabled": True,
                "state_affiliated": False,
            },
            {
                "source_id": "left-daily",
                "name": "Left Daily",
                "scope": "national",
                "country": "US",
                "language": "en",
                "lean_label": "L",
                "lean_source": "AllSides",
                "lean_source_url": "https://www.allsides.com/left",
                "lean_retrieved_on": "2026-10-31",
                "adfontes_bias": -7.8,
                "adfontes_reliability": 40.9,
                "adfontes_url": "https://adfontesmedia.com/left",
                "ownership": "Family trust",
                "paywall": "metered",
                "enabled": True,
                "state_affiliated": False,
            },
            {
                "source_id": "peach-news",
                "name": "Peach News",
                "scope": "state",
                "home_state": "GA",
                "country": "US",
                "language": "en",
                "lean_label": "Unrated",
                "paywall": "none",
                "enabled": True,
                "state_affiliated": False,
            },
            {
                "source_id": "kommersant",
                "name": "Kommersant",
                "scope": "international",
                "country": "RU",
                "language": "ru",
                "lean_label": "NA",
                "notes": "Russian domestic press under censorship; "
                "note on Sources page",
                "enabled": True,
                "state_affiliated": False,
            },
            {
                "source_id": "gulf-tv",
                "name": "Gulf TV",
                "scope": "international",
                "country": "QA",
                "language": "es",
                "lean_label": "NA",
                "enabled": True,
                "state_affiliated": True,
            },
            {
                "source_id": "uncited",
                "name": "Uncited Paper",
                "scope": "national",
                "country": "US",
                "language": "en",
                "lean_label": "R",
                "enabled": False,
                "state_affiliated": False,
            },
            {
                "source_id": "badlink",
                "name": "Bad Link Paper",
                "scope": "national",
                "country": "US",
                "language": "en",
                "lean_label": "LL",
                "lean_source_url": "javascript:alert(1)",
                "enabled": True,
                "state_affiliated": False,
            },
        ],
    )
    writer.add(
        "ref.states",
        [
            {
                "usps": "GA",
                "fips": "13",
                "name": "Georgia",
                "battleground": True,
                "poll_close_et": "2026-11-03T19:00:00-05:00",
                "poll_close_all_et": "2026-11-03T20:00:00-05:00",
            },
            {
                "usps": "OH",
                "fips": "39",
                "name": "Ohio",
                "battleground": False,
                "poll_close_et": "2026-11-03T19:30:00-05:00",
                "poll_close_all_et": "2026-11-03T19:30:00-05:00",
            },
            {
                "usps": "PR",
                "fips": "72",
                "name": "Puerto Rico",
                "battleground": False,
            },
        ],
    )
    writer.add(
        "ref.races",
        [
            {
                "race_key": "2026-GA-SEN",
                "state": "GA",
                "office": "senate",
                "rating": "Toss-up",
                "rating_source": "Cook",
                "rating_url": "https://example.com/ratings",
                "close_race": True,
                "watch": True,
                "candidates": ["e1", "e2"],
            },
            {
                "race_key": "2026-GA-H07",
                "state": "GA",
                "office": "house",
                "district": "07",
                "close_race": False,
                "watch": False,
                "candidates": [],
            },
            {
                "race_key": "2026-OH-GOV",
                "state": "OH",
                "office": "governor",
                "rating": "Likely R",
                "close_race": False,
                "watch": False,
                "candidates": [],
            },
            {
                "race_key": "2026-OH-M-school-funding-amendment",
                "state": "OH",
                "office": "ballot_measure",
                "close_race": False,
                "watch": False,
                "candidates": [],
            },
        ],
    )
    writer.add(
        "ref.entities",
        [
            {
                "entity_id": "e1",
                "canonical_name": "Jane Doe",
                "ent_type": "PERSON",
                "party": "Democratic",
                "state": "GA",
                "office": "senate",
                "race_key": "2026-GA-SEN",
                "incumbent": True,
                "aliases": ["Sen. Doe"],
            },
            {
                "entity_id": "e2",
                "canonical_name": "John Roe",
                "ent_type": "PERSON",
                "party": "Republican",
                "state": "GA",
                "race_key": "2026-GA-SEN",
                "incumbent": False,
                "aliases": [],
            },
            {
                "entity_id": "e3",
                "canonical_name": "Quiet Candidate",
                "ent_type": "PERSON",
                "incumbent": False,
                "aliases": [],
            },
        ],
    )
    tone_items = [f"t{n}" for n in range(8)]
    writer.add(
        "raw.items",
        [
            item(
                "a1",
                "wire",
                "Georgia lines stretch around the block",
                published_at=NOW - timedelta(minutes=30),
            ),
            item("a2", "left-daily", "Georgia lines stretch, a copy"),
            item("a3", "peach-news", "Fulton County precincts stay open"),
            item(
                "es1",
                "gulf-tv",
                "Filas largas en Georgia",
                language="es",
            ),
            item("o1", "left-daily", "Ohio governor debate draws a crowd"),
            item(
                "x1",
                "wire",
                f"{ALERT} Hostile headline",
                summary='<img src=x onerror="alert(2)"> Hostile summary.',
                url="javascript:alert(3)",
            ),
            item(
                "old1",
                "wire",
                "Backlog story from days ago",
                published_at=NOW - timedelta(days=3),
            ),
            item("irr1", "wire", "Local bakery wins a pie contest"),
            *[
                item(key, "wire", f"Tone story {key}", summary="Toned.")
                for key in tone_items
            ],
        ],
    )
    writer.add(
        "nlp.lang", [{"item_id": "es1", "language": "es", "confidence": 0.99}]
    )
    writer.add(
        "nlp.classify_a",
        [
            classify("a1", levels=["state", "local"]),
            classify("a2"),
            classify("a3", primary_level="local", levels=["local"]),
            classify(
                "es1",
                headline_en="Long lines in Georgia",
                summary_en="Voters waited in Georgia.",
                levels=["national"],
                primary_level="national",
            ),
            classify("o1", states=["OH"], race_types=["governor"]),
            classify("x1", states=["OH"], race_types=[]),
            classify("old1", states=["OH"], race_types=[]),
            classify("irr1", relevant=False, states=[], race_types=[]),
            *[
                classify(key, states=["OH"], race_types=[])
                for key in tone_items
            ],
        ],
    )
    writer.add(
        "nlp.dup_groups",
        [
            {"item_id": "a1", "dup_group_id": "g1", "is_canonical": True},
            {"item_id": "a2", "dup_group_id": "g1", "is_canonical": False},
        ],
    )
    writer.add(
        "nlp.embeddings",
        [
            {
                "item_id": key,
                "model": settings.nlp.embed_model,
                "vec": vector,
                "dim": DIM,
                "text_basis": "title+summary",
            }
            for key, vector in {
                "a1": axis(0),
                "a2": axis(0, 0.01),
                "a3": axis(0, 0.3),
                "es1": axis(0, 0.2),
                "o1": axis(1),
                "x1": axis(2),
                "old1": axis(3),
                "irr1": axis(4),
                **{key: axis(5, 0.1 * n) for n, key in enumerate(tone_items)},
            }.items()
        ],
    )
    writer.add(
        "nlp.storyline_assign",
        [
            *[
                {
                    "item_id": key,
                    "storyline_id": "s1",
                    "similarity": 0.95,
                    "assigned_at": NOW,
                }
                for key in ("a1", "a2", "a3", "es1")
            ],
            {
                "item_id": "o1",
                "storyline_id": "s3",
                "similarity": 0.93,
                "assigned_at": NOW,
            },
        ],
    )
    writer.add(
        "nlp.storylines",
        [
            {
                "storyline_id": "s1",
                "version": 1,
                "label": "Georgia polling delays",
                "summary": "Delays were reported.[^1]",
                "citations": '{"1": "a1"}',
                "centroid": axis(0),
                "first_seen_at": NOW,
                "last_item_at": NOW,
                "n_items": 4,
                "status": "active",
                "states": ["GA"],
            },
            {
                "storyline_id": "s2",
                "version": 1,
                "label": "Merged away",
                "centroid": axis(0, 0.1),
                "first_seen_at": NOW,
                "last_item_at": NOW,
                "status": "merged",
                "merged_into": "s1",
            },
            {
                "storyline_id": "s3",
                "version": 1,
                "label": "Ohio governor debate",
                "centroid": axis(0, 0.5),
                "first_seen_at": NOW,
                "last_item_at": NOW,
                "n_items": 1,
                "status": "active",
                "states": ["OH"],
            },
            {
                "storyline_id": "s4",
                "version": 1,
                "label": "",
                "centroid": axis(0, 0.05),
                "first_seen_at": NOW,
                "last_item_at": NOW,
                "status": "candidate",
            },
        ],
    )
    writer.add(
        "raw.article_text",
        [
            {
                "item_id": "a1",
                "fetched_at": NOW,
                "http_status": 200,
                "text": ARTICLE_TEXT,
                "n_chars": len(ARTICLE_TEXT),
                "extractor": "trafilatura",
                "paywalled": False,
            },
            {
                "item_id": "a3",
                "fetched_at": NOW,
                "http_status": 402,
                "text": "",
                "n_chars": 0,
                "paywalled": True,
            },
        ],
    )
    writer.add(
        "nlp.extract_b",
        [
            {
                "item_id": "a1",
                "summary_en": "Lines were long.",
                "article_tone": -0.5,
                "topics_refined": ["turnout"],
                "text_basis": "full",
                "raw": {"race_keys": ["2026-GA-SEN"], "note": ALERT},
            }
        ],
    )
    writer.add(
        "nlp.mentions",
        [
            mention(
                "m1",
                "a1",
                "e1",
                tone=1.0,
                evidence="Jane Doe thanked voters.",
                char_start=0,
                char_end=8,
                party="Democratic",
            ),
            mention("m2", "a1", "e2", surface="John Roe", tone=None),
            mention(
                "m3",
                "a1",
                None,
                surface="Georgia",
                ent_type="GPE",
                role="other",
                char_start=500,
                char_end=507,
            ),
            mention(
                "m4",
                "a3",
                "e2",
                surface="John Roe",
                tone=-0.5,
                evidence="Roe declined to comment.",
            ),
            *[
                mention(
                    f"tm{n}",
                    key,
                    "e1",
                    tone=1.5 if n % 2 else -1.5,
                    evidence=f"Quote number {n} about <Doe>.",
                )
                for n, key in enumerate(tone_items)
            ],
        ],
    )
    writer.add(
        "nlp.places",
        [
            {
                "place_id": "p1",
                "item_id": "a1",
                "surface": "Fulton County",
                "place_type": "county",
                "state_usps": "GA",
                "county_fips": "13121",
                "method": "gazetteer",
            },
            {
                "place_id": "p2",
                "item_id": "a1",
                "surface": "Georgia",
                "place_type": "state",
                "state_usps": "GA",
                "method": "gazetteer",
            },
            {
                "place_id": "p3",
                "item_id": "a3",
                "surface": "Fulton County",
                "place_type": "county",
                "state_usps": "GA",
                "county_fips": "13121",
                "method": "gazetteer",
            },
            {
                "place_id": "p4",
                "item_id": "a3",
                "surface": "GA",
                "place_type": "state",
                "state_usps": "GA",
                "method": "source_home",
            },
        ],
    )
    writer.add(
        "nlp.race_calls",
        [
            {
                "call_id": "c1",
                "item_id": "a1",
                "race_key": "2026-GA-SEN",
                "race_desc": "Georgia Senate",
                "winner_name": "Jane Doe",
                "winner_entity_id": "e1",
                "party": "Democratic",
                "called_by": "AP",
                "status": "projected",
                "state": "GA",
            }
        ],
    )
    writer.add(
        "nlp.llm_calls",
        [
            {
                "call_id": "l1",
                "purpose": "classify_a",
                "model": "claude-haiku-5-5",
                "input_tokens": 100,
                "cache_read_tokens": 900,
                "cache_write_tokens": 0,
                "output_tokens": 50,
                "usd_est": 1.25,
                "latency_ms": 800,
                "status": "ok",
            },
            {
                "call_id": "l2",
                "purpose": "classify_a",
                "model": "claude-haiku-5-5",
                "input_tokens": 10,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "usd_est": 0.25,
                "latency_ms": 1200,
                "status": "max_tokens",
            },
            {
                "call_id": "l3",
                "purpose": "briefs",
                "model": "claude-sonnet-5-5",
                "input_tokens": 1000,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 500,
                "usd_est": 0.5,
                "latency_ms": 9000,
                "status": "ok",
            },
        ],
    )
    writer.add(
        "raw.fetches",
        [
            {
                "fetch_id": f"f{n}",
                "source_id": "wire",
                "feed_url": "https://wire.example/a",
                "started_at": NOW,
                "error": "HTTP 500" if n == 0 else None,
            }
            for n in range(4)
        ],
    )
    writer.flush()
    conn.close()


STATUS = {
    "updated_at": datetime.now(UTC).isoformat(),
    "started_at": NOW.isoformat(),
    "last_commit_at": NOW.isoformat(),
    "last_maintenance_at": NOW.isoformat(),
    "last_backup_at": None,
    "last_rollup_at": NOW.isoformat(),
    "shedding": True,
    "feeds": {"healthy": 2, "total": 4},
    "feed_details": [
        {
            "source_id": "wire",
            "feed_url": "https://wire.example/a",
            "failures": 0,
            "open": False,
            "last_error": None,
            "last_success_at": NOW.isoformat(),
            "n_new": 3,
        },
        {
            "source_id": "wire",
            "feed_url": "https://wire.example/b",
            "failures": 0,
            "open": False,
            "last_error": None,
            "last_success_at": (NOW - timedelta(hours=1)).isoformat(),
            "n_new": 0,
        },
        {
            "source_id": "left-daily",
            "feed_url": "https://left.example/rss",
            "failures": 2,
            "open": False,
            "last_error": f"HTTP 503 {ALERT}",
            "last_success_at": None,
            "n_new": 0,
        },
        {
            "source_id": "kommersant",
            "feed_url": "https://k.example/rss",
            "failures": 6,
            "open": True,
            "last_error": "HTTP 403",
            "last_success_at": None,
            "n_new": 0,
        },
    ],
    "stages": [
        {
            "parser": "classify_a",
            "version": "1",
            "depth": 40,
            "finished": 900,
            "errors": 1,
            "lag_seconds": 12.5,
            "paused": False,
            "backfill_paused": True,
            "oldest_live_age": 77.5,
        },
        {
            "parser": "fetch_text",
            "version": "1",
            "depth": 7,
            "finished": 50,
            "errors": 0,
            "lag_seconds": 3.0,
            "paused": False,
            "backfill_paused": False,
            "oldest_live_age": 0.0,
        },
        {
            "parser": "extract_b",
            "version": "1",
            "depth": 9,
            "finished": 25,
            "errors": 0,
            "lag_seconds": 4.0,
            "paused": True,
            "backfill_paused": True,
            "oldest_live_age": 12.0,
        },
    ],
    "llm": {
        "degraded": False,
        "last_error": None,
        "budget": {
            "state": "ok",
            "spent_today": 2.0,
            "spent_total": 2.0,
            "daily_cap": 40.0,
            "hard_cap": 75.0,
        },
    },
    "disk": {"level": "OK", "free_gb": 38.3},
}


class FakeEmbedder:
    """Maps a query to a known axis and records what it was given."""

    AXES = {"lines": 0, "colas": 0, "ohio": 1, "tone": 5}

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, texts):
        for text in texts:
            self.seen.append(text)
            word = next(
                (key for key in self.AXES if key in text.lower()), "lines"
            )
            yield np.array(axis(self.AXES[word]), dtype=np.float32)


@pytest.fixture(scope="module")
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture(scope="module")
def page_settings(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("pages"))
    seed(settings)
    settings.paths.status.write_text(json.dumps(STATUS), encoding="utf-8")
    return settings


@pytest.fixture(scope="module")
def client(page_settings, embedder):
    app = create_app(page_settings, lambda _settings: embedder)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clean_ui(client, page_settings):
    """A watchlist with no edits, before and after a test."""

    def wipe() -> None:
        with sqlite3.connect(page_settings.paths.ui_db) as db:
            db.execute("DELETE FROM watch")
            db.execute("DELETE FROM battleground_override")

    wipe()
    yield
    wipe()


def evidence_total(client, query: str) -> int:
    text = client.get(f"/frag/evidence?{query}").text
    return int(re.search(r"(\d+) stories", text).group(1))


# Article


def test_article_page_and_drawer_share_their_content(client):
    page = client.get("/article/a1")
    drawer = client.get("/frag/article/a1")
    assert page.status_code == drawer.status_code == 200
    assert "app-header" in page.text
    assert "app-header" not in drawer.text
    for text in (page.text, drawer.text):
        assert "Georgia lines stretch around the block" in text
        assert "Open original" in text
        assert "Georgia polling delays" in text


def test_article_shows_translation_and_original(client):
    text = client.get("/article/es1").text
    assert "Long lines in Georgia" in text
    assert "Filas largas en Georgia" in text


def test_article_outlet_links_to_its_sources_row(client):
    text = client.get("/article/a1").text
    assert "Wire Service" in text
    assert "○○●○○" in text
    assert re.search(r'href="/sources\?[^"]*#wire"', text)


def test_article_shows_true_first_seen_for_a_backlog_item(
    client, page_settings
):
    text = client.get("/article/old1").text
    assert "time charts place it at its published time" in text
    et = NOW.astimezone(
        reference.parse_iso("2026-01-01T00:00:00-05:00").tzinfo
    )
    assert et.strftime("%b %-d") in text
    assert "time charts place" not in client.get("/article/a1").text


def test_article_text_is_an_extract_with_a_link_to_the_original(client):
    text = client.get("/article/a1").text
    assert LONG_TAIL not in text
    assert "Read the full article at the outlet" in text
    assert 'href="https://example.com/a1"' in text


def test_article_underlines_entities_and_links_places(client):
    text = client.get("/article/a1").text
    assert re.search(
        r'<a class="entity-span ent-PERSON"[^>]*href="/entity/e1[^"]*">'
        r"Jane Doe</a>",
        text,
    )
    assert re.search(
        r'<a class="entity-span ent-GPE"[^>]*href="/state/GA[^"]*">'
        r"Fulton County</a>",
        text,
    )
    assert "&lt;b&gt;lines&lt;/b&gt;" in text


def test_article_lists_entity_tone_with_evidence(client):
    text = client.get("/article/a1").text
    assert "+1.0" in text
    assert "Jane Doe thanked voters." in text
    assert "John Roe" in text


def test_article_shows_every_classification_badge(client):
    text = client.get("/article/a1").text
    for word in ("primary: state", "local", "senate", "turnout", "GA"):
        assert word in text


def test_article_neighbors_are_nearest_and_leave_out_copies(client):
    text = client.get("/frag/article/a1").text
    near = text.split("Nearest items")[1].split("Syndication group")[0]
    assert "Long lines in Georgia" in near
    assert near.index("Long lines in Georgia") < near.index(
        "Fulton County precincts"
    )
    assert "a copy" not in near
    assert "Georgia lines stretch around" not in near


def test_article_neighbors_honor_filters_and_as_of(client):
    earlier = (NOW - timedelta(hours=1)).isoformat()
    text = client.get("/frag/article/a1", params={"as_of": earlier}).text
    near = text.split("Nearest items")[1].split("Syndication group")[0]
    assert "Long lines in Georgia" not in near
    english = client.get("/frag/article/a1?lang=en").text
    assert (
        "Long lines in Georgia"
        not in english.split("Nearest items")[1].split("Syndication")[0]
    )


def test_article_lists_its_syndication_group(client):
    text = client.get("/frag/article/a2").text
    group = text.split("Syndication group")[1]
    assert "Georgia lines stretch around the block" in group
    assert "canonical" in group
    assert "this item" in group
    alone = client.get("/frag/article/o1").text
    assert "No other outlet carried a copy" in alone


def test_article_empty_states_without_extraction(client):
    text = client.get("/article/o1").text
    assert "No entities have been extracted" in text
    assert "No article text is stored" in text
    assert "Feed summary" in text


def test_article_reports_a_paywall(client):
    assert "paywall blocked" in client.get("/article/a3").text


def test_hostile_feed_text_is_escaped_and_bad_links_dropped(client):
    for path in ("/article/x1", "/frag/article/x1"):
        text = client.get(path).text
        assert ALERT not in text
        assert "&lt;script&gt;" in text
        assert "<img src=x" not in text
        assert "javascript:" not in text
        assert "Open original" not in text


def test_raw_parser_json_is_escaped_and_leaves_out_text_and_vectors(client):
    response = client.get("/frag/article/a1/raw")
    assert response.status_code == 200
    assert '<pre class="raw">' in response.text
    assert ALERT not in response.text
    assert "nlp.classify_a" in response.text
    assert "nlp.mentions" in response.text
    assert LONG_TAIL not in response.text
    assert '"vec"' not in response.text


def test_unknown_article_is_a_clean_404(client):
    page = client.get("/article/none")
    assert page.status_code == 404
    assert "app-header" in page.text
    assert "No such article" in page.text
    assert client.get("/frag/article/none").status_code == 404
    assert client.get("/frag/article/none/raw").status_code == 404


def test_cut_extract_keeps_short_text_and_ends_on_a_boundary():
    assert cut_extract("Short.") == ("Short.", False)
    text = "One sentence here. " * 200
    extract, truncated = cut_extract(text)
    assert truncated
    assert len(extract) <= EXTRACT_CHARS
    assert extract.endswith(".")


def test_spans_resolve_overlaps_and_stale_offsets():
    text = "Jane Doe met Doe in New York City and New York."
    mentions = [
        {
            "surface": "Jane Doe",
            "ent_type": "PERSON",
            "entity_id": "e1",
            "char_start": 0,
            "char_end": 8,
        },
        {"surface": "Doe", "ent_type": "PERSON", "entity_id": "e1"},
        {
            "surface": "New York",
            "ent_type": "GPE",
            "char_start": 900,
            "char_end": 908,
        },
        {"surface": "absent", "ent_type": "ORG"},
    ]
    places = [
        {
            "surface": "New York City",
            "state_usps": "NY",
            "method": "gazetteer",
        },
        {"surface": "met", "state_usps": "NY", "method": "source_home"},
    ]
    spans = locate_spans(text, mentions, places, "window=3h")
    found = [text[s.start : s.end] for s in spans]
    assert found == ["Jane Doe", "Doe", "New York", "New York"]
    assert spans[0].href == "/entity/e1?window=3h"
    assert all(
        a.end <= b.start for a, b in zip(spans, spans[1:], strict=False)
    )


def test_spans_link_a_place_that_is_also_an_entity():
    text = "Voters in Georgia waited."
    spans = locate_spans(
        text,
        [{"surface": "Georgia", "ent_type": "GPE"}],
        [{"surface": "Georgia", "state_usps": "GA", "method": "gazetteer"}],
    )
    assert len(spans) == 1
    assert spans[0].href == "/state/GA"


def test_highlight_escapes_text_titles_and_unknown_types():
    text = 'A <b>"Name"</b> & more'
    spans = locate_spans(
        text,
        [
            {
                "surface": '<b>"Name"</b>',
                "ent_type": '"><script>',
                "canonical_name": '"><img>',
            }
        ],
        [],
    )
    html = str(highlight(text, spans))
    assert "<b>" not in html
    assert "<img>" not in html
    assert "<script>" not in html
    assert "ent-EVENT" in html
    assert "&amp; more" in html


# Vectors


def test_vector_index_replaces_grows_and_ranks():
    index = VectorIndex(dim=3)
    index.upsert(["a", "b"], np.array([[1, 0, 0], [0, 1, 0]], dtype=float))
    index.upsert(["b", "c"], np.array([[0, 0, 2], [1, 1, 0]], dtype=float))
    assert len(index) == 3
    query = np.array([0, 0, 1], dtype=np.float32)
    ranked = index.rank(query, ["a", "b", "c", "unknown"], 2)
    assert ranked[0][0] == "b"
    assert ranked[0][1] == pytest.approx(1.0)
    assert len(ranked) == 2
    assert index.rank(query, [], 5) == []
    assert index.vector("missing") is None
    index.upsert([f"k{n}" for n in range(2000)], np.ones((2000, 3)))
    assert len(index) == 2003
    assert index.rank(query, ["b"], 1)[0][0] == "b"


def test_vector_index_ranks_only_eligible_ids():
    index = VectorIndex(dim=2)
    index.upsert(
        ["near", "far"], np.array([[1.0, 0.0], [0.0, 1.0]], dtype=float)
    )
    ranked = index.rank(np.array([1.0, 0.0], dtype=np.float32), ["far"], 5)
    assert [key for key, _ in ranked] == ["far"]


def test_vector_index_survives_a_zero_vector():
    index = VectorIndex(dim=2)
    index.upsert(["zero"], np.zeros((1, 2)))
    assert index.vector("zero").tolist() == [0.0, 0.0]


def test_vectors_refresh_is_incremental(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    model_name = settings.nlp.embed_model

    def add(item_id: str, index: int) -> None:
        writer.add(
            "nlp.embeddings",
            [
                {
                    "item_id": item_id,
                    "model": model_name,
                    "vec": axis(index),
                    "dim": DIM,
                },
                {
                    "item_id": f"other-{item_id}",
                    "model": "some/other-model",
                    "vec": axis(index),
                    "dim": DIM,
                },
            ],
        )
        writer.flush()

    from monitor.web.readmodel import ReadModel

    add("v1", 0)
    model = ReadModel(settings)
    model.refresh(True)
    vectors = Vectors(settings, model, lambda _settings: None)
    vectors.refresh()
    assert len(vectors.items) == 1
    add("v2", 1)
    add("v1", 2)
    model.refresh()
    vectors.refresh()
    assert len(vectors.items) == 2
    assert vectors.items.vector("v1")[2] == pytest.approx(1.0)
    assert vectors.items.vector("other-v1") is None
    conn.close()


def test_a_failed_refresh_keeps_the_indexes(settings, monkeypatch):
    import duckdb

    from monitor.web.readmodel import ReadModel

    model = ReadModel(settings)
    model.refresh(True)
    vectors = Vectors(settings, model, lambda _settings: None)
    vectors.items.upsert(["kept"], np.array([axis(0)]))

    def fail(*_args):
        raise duckdb.IOException("gone")

    monkeypatch.setattr(model, "embeddings_since", fail)
    vectors.refresh()
    assert vectors.items.vector("kept") is not None


# Search


def test_search_page_loads_without_the_model(lake_template, tmp_path):
    settings = clone_lake(lake_template, tmp_path)
    seed(settings)
    calls = []

    def loader(_settings):
        calls.append(1)
        return FakeEmbedder()

    with TestClient(create_app(settings, loader)) as test_client:
        assert test_client.get("/search").status_code == 200
        assert test_client.get("/frag/article/a1").status_code == 200
        assert calls == []
        assert test_client.get("/search?q=lines").status_code == 200
        assert test_client.get("/search?q=ohio").status_code == 200
        assert calls == [1]


def test_search_embeds_with_the_query_prefix(client, embedder):
    client.get("/search?q=colas+largas")
    assert embedder.seen[-1] == "query: colas largas"


def test_search_finds_english_items_from_another_language(client):
    text = client.get("/search?q=colas").text
    assert "Georgia lines stretch around the block" in text
    assert text.index("Georgia lines stretch around") < text.index(
        "Ohio governor debate"
    )


def test_search_reports_similarity_and_opens_the_drawer(client):
    text = client.get("/search?q=lines").text
    assert 'data-article="a1"' in text
    assert "1.000" in text


def test_search_collapses_copies_unless_asked(client):
    assert "a copy" not in client.get("/search?q=lines").text
    assert "a copy" in client.get("/search?q=lines&copies=all").text


def test_search_keyword_filter_narrows_and_treats_wildcards_literally(client):
    text = client.get("/search?q=lines&kw=fulton").text
    assert "Fulton County precincts" in text
    assert "Georgia lines stretch around" not in text
    assert "No items match" in client.get("/search?q=lines&kw=%25").text


def test_keyword_search_needs_no_query(client):
    text = client.get("/search?kw=debate").text
    assert "Ohio governor debate" in text
    assert "Newest first" in text


@pytest.mark.parametrize(
    ("facet", "shown", "hidden"),
    [
        ("state=OH", "Ohio governor debate", "Georgia lines stretch"),
        ("source=peach-news", "Fulton County precincts", "Ohio governor"),
        ("lean=L", "Ohio governor debate", "Fulton County precincts"),
        ("lean=Unrated", "Fulton County precincts", "Ohio governor"),
        ("lean=NA", "Long lines in Georgia", "Ohio governor"),
        ("level=local", "Fulton County precincts", "Ohio governor"),
    ],
)
def test_search_facets(client, facet, shown, hidden):
    text = client.get(f"/search?q=lines&{facet}").text
    assert shown in text
    assert hidden not in text


def test_search_time_facet_and_global_window(client):
    assert "Georgia lines" in client.get("/search?q=lines&time=1h").text
    backlog = "Backlog story from days ago"
    assert backlog in client.get("/search?q=lines").text
    assert backlog not in client.get("/search?q=lines&time=24h").text
    assert backlog not in client.get("/search?q=lines&time=1h").text


def test_search_as_of_excludes_later_items(client):
    earlier = (NOW - timedelta(hours=1)).isoformat()
    response = client.get("/search", params={"q": "lines", "as_of": earlier})
    assert "Georgia lines" not in response.text
    assert "Backlog story" in response.text


def test_search_skips_irrelevant_items_unless_asked(client):
    assert "pie contest" not in client.get("/search?q=lines").text
    assert "pie contest" in client.get("/search?q=lines&all=1").text


def test_search_shows_facet_counts(client):
    text = client.get("/search?q=lines").text
    assert re.search(r"GA \(\d+\)", text)
    assert re.search(r"Wire Service \(\d+\)", text)


def test_header_controls_keep_the_search(client):
    text = client.get("/search?q=lines&state=GA").text
    assert 'href="?level=state&amp;q=lines&amp;state=GA"' in text
    assert "window=3h&amp;q=lines&amp;state=GA&time=3h" in text


def test_search_without_a_model_says_so(lake_template, tmp_path):
    settings = clone_lake(lake_template, tmp_path)
    seed(settings)

    def broken(_settings):
        raise OSError("model files are missing")

    with TestClient(create_app(settings, broken)) as test_client:
        response = test_client.get("/search?q=lines")
        assert response.status_code == 200
        assert "could not be loaded" in response.text
        assert "Ohio governor" in test_client.get("/search?kw=debate").text


def test_embed_query_raises_search_unavailable(settings):
    from monitor.web.readmodel import ReadModel

    def broken(_settings):
        raise OSError("gone")

    vectors = Vectors(settings, ReadModel(settings), broken)
    with pytest.raises(SearchUnavailable):
        vectors.embed_query("anything")


def test_search_does_not_stall_other_requests(lake_template, tmp_path):
    settings = clone_lake(lake_template, tmp_path)
    seed(settings)
    entered = threading.Event()
    release = threading.Event()

    class Blocking(FakeEmbedder):
        def embed(self, texts):
            entered.set()
            release.wait(20)
            yield from super().embed(texts)

    with TestClient(create_app(settings, lambda _: Blocking())) as test_client:
        thread = threading.Thread(
            target=lambda: test_client.get("/search?q=lines")
        )
        thread.start()
        try:
            assert entered.wait(10)
            started = time.monotonic()
            other = test_client.get("/frag/status-strip")
            assert other.status_code == 200
            assert time.monotonic() - started < 5
            assert thread.is_alive()
        finally:
            release.set()
            thread.join(20)


# Storyline


def test_story_page_shows_each_section(client):
    text = client.get("/story/s1").text
    for word in (
        "Georgia polling delays",
        "Volume by level",
        "States involved",
        "Outlets",
        "Entities",
        "Coverage by lean",
        "Articles",
        "Related storylines",
    ):
        assert word in text
    assert 'data-item="a1"' in text


def test_story_counts_match_their_evidence(client):
    text = client.get("/story/s1").text
    total = int(
        re.search(r"unique</span><span class=\"kpi-value\">(\d+)", text).group(
            1
        )
    )
    assert total == evidence_total(client, "kind=storyline&key=s1")
    assert total == 3
    copies = evidence_total(client, "kind=storyline&key=s1&copies=all")
    assert copies == 4
    assert f'kpi-value">{copies}<' in text


def test_story_outlet_counts_match_their_evidence(client):
    frag = client.get("/frag/section/story/s1/outlets").text
    assert "Left Daily" in frag
    assert "kind=storyline&key=s1&also=source:left-daily" in frag
    assert (
        evidence_total(
            client, "kind=storyline&key=s1&also=source:left-daily&copies=all"
        )
        == 1
    )


def test_story_entity_counts_match_their_evidence(client):
    frag = client.get("/frag/section/story/s1/entities").text
    assert "Jane Doe" in frag
    assert 'href="/entity/e1' in frag
    assert "n under 5" in frag
    assert evidence_total(client, "kind=storyline&key=s1&also=entity:e2") == 2


def test_story_coverage_uses_each_buckets_total(client):
    frag = client.get("/frag/section/story/s1/coverage").text
    assert "1 of 1" in frag
    assert "kind=storyline&key=s1&bucket=L&copies=all" in frag
    assert "Non-US outlet" in frag
    assert "Unrated" in frag


def test_story_articles_collapse_duplicate_groups(client):
    unique = client.get("/frag/section/story/s1/articles").text
    assert "Georgia lines stretch around the block" in unique
    assert "a copy" not in unique
    assert "2 copies" in unique
    assert "kind=dup&key=g1" in unique
    every = client.get("/frag/section/story/s1/articles?copies=all").text
    assert "a copy" in every


def test_story_articles_filter(client):
    frag = client.get("/frag/section/story/s1/articles?kw=fulton").text
    assert "Fulton County precincts" in frag
    assert "Georgia lines stretch" not in frag
    frag = client.get("/frag/section/story/s1/articles?lean=NA").text
    assert "Long lines in Georgia" in frag
    assert "Fulton County precincts" not in frag


def test_story_related_uses_centroids_and_skips_merged_and_unlabeled(client):
    frag = client.get("/frag/section/story/s1/related").text
    assert "Ohio governor debate" in frag
    assert "Merged away" not in frag
    assert "Unlabeled" not in frag


def test_merged_story_redirects_to_the_survivor(client):
    response = client.get("/story/s2?window=3h", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/story/s1?window=3h"


def test_story_without_a_label_or_summary_renders(client):
    text = client.get("/story/s4").text
    assert "Unlabeled storyline" in text
    assert "no summary" in text


def test_unknown_story_is_a_clean_404(client):
    page = client.get("/story/none")
    assert page.status_code == 404
    assert "app-header" in page.text
    assert client.get("/frag/section/story/none/head").status_code == 404
    assert client.get("/frag/section/story/s1/none").status_code == 404
    assert client.get("/frag/section/none/s1/head").status_code == 404


def test_story_honors_as_of(client):
    earlier = (NOW - timedelta(hours=2)).isoformat()
    text = client.get("/story/s1", params={"as_of": earlier}).text
    assert 'unique</span><span class="kpi-value">0<' in text


def test_story_says_when_the_window_is_empty(client):
    later = (NOW + timedelta(hours=3)).isoformat()
    text = client.get("/story/s1", params={"as_of": later}).text
    assert "fall in the 1 h window" in text
    assert 'all time</span><span class="kpi-value">3<' in text


def test_story_volume_chart_has_every_level_and_matches_the_total(client):
    data = client.get("/api/data/story_volume?key=s1").json()
    assert {row["level_name"] for row in data["table"]} == {
        "National",
        "State",
        "Local",
        "Unclassified",
    }
    assert sum(row["n"] for row in data["table"]) == 3
    assert data["table"][0]["bin_minutes"] == 5
    assert "rules" in data
    wide = client.get("/api/data/story_volume?key=s1&window=24h").json()
    assert wide["table"][0]["bin_minutes"] == 30


def test_time_charts_with_no_items_return_no_rows(client):
    earlier = (NOW - timedelta(hours=3)).isoformat()
    for chart, key in (
        ("story_volume", "s1"),
        ("entity_mentions", "e1"),
        ("state_attention", "GA"),
    ):
        data = client.get(
            f"/api/data/{chart}", params={"key": key, "as_of": earlier}
        ).json()
        assert data["table"] == [], chart


def test_tone_chart_carries_the_whole_window(client):
    data = client.get("/api/data/entity_tone?key=e1&window=24h").json()
    assert len(data["span"]) == 2
    assert data["span"][0]["bin_start"] < data["span"][1]["bin_start"]


def test_evidence_names_what_it_lists_in_eastern_time(client):
    text = client.get(
        "/frag/evidence",
        params={
            "kind": "storyline",
            "key": "s1",
            "also": "source:left-daily",
            "bin_start": "2026-11-04T01:00:00+00:00",
            "bin_minutes": 15,
            "as_of": "2026-11-04T02:00:00+00:00",
            "window": "24h",
        },
    ).text
    assert (
        "Georgia polling delays · Left Daily · 8:00 pm–8:15 pm · 24 h" in text
    )


def test_story_map_lists_states(client):
    data = client.get("/api/data/story_map?key=s1").json()
    assert data["table"] == [{"usps": "GA", "n": 3}]


@pytest.mark.parametrize(
    "chart",
    [
        "story_volume",
        "entity_mentions",
        "entity_tone",
        "entity_tone_lean",
        "state_attention",
        "lean_distribution",
    ],
)
def test_page_chart_specs_are_vega_lite_with_named_data(client, chart):
    spec = client.get(f"/api/spec/{chart}").json()
    assert "vega-lite" in spec["$schema"]
    assert "table" in str(spec)
    assert chart in specs.SPECS


def test_unknown_page_chart_is_404(client):
    assert client.get("/api/data/nope?key=s1").status_code == 404


# Entity


def test_entity_profile(client):
    text = client.get("/entity/e1").text
    assert "Jane Doe" in text
    assert "Democratic" in text
    assert "Senate" in text
    assert "Toss-up" in text
    assert "Yes" in text
    assert "Sen. Doe" in text


def test_entity_counts_match_their_evidence(client):
    text = client.get("/entity/e1").text
    stories = int(
        re.search(
            r"Stories mentioning, 1 h</span><span class=\"kpi-value\">(\d+)",
            text,
        ).group(1)
    )
    assert stories == 9
    assert stories == evidence_total(client, "kind=entity&key=e1")


def test_entity_mention_chart_matches_the_total(client):
    data = client.get("/api/data/entity_mentions?key=e1").json()
    assert sum(row["n"] for row in data["table"]) == 9


def test_entity_tone_has_a_band_and_hides_small_bins(client):
    data = client.get("/api/data/entity_tone?key=e1").json()
    assert data["table"]
    row = data["table"][0]
    assert row["ci_low"] < row["mean"] < row["ci_high"]
    assert row["n"] == 9
    assert row["low_n"] is True
    empty = client.get("/api/data/entity_tone?key=e2").json()
    assert empty["table"] == []


def test_entity_tone_by_lean_applies_the_hide_and_low_n_rules(client):
    frag = client.get("/frag/section/entity/e1/lean").text
    assert "low n" in frag
    assert "kind=entity&key=e1&copies=all&bucket=C" in frag
    chart = client.get("/api/data/entity_tone_lean?key=e1").json()["table"]
    assert {row["bucket"] for row in chart} == {"C", "All"}
    roe = client.get("/frag/section/entity/e2/lean").text
    assert "Hidden: n under 5" in roe


def test_entity_tone_excludes_opinion(lake_template, tmp_path):
    settings = clone_lake(lake_template, tmp_path)
    seed(settings)
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    writer.add(
        "nlp.classify_a",
        [
            classify(f"t{n}", states=["OH"], race_types=[], is_opinion=True)
            for n in range(8)
        ],
    )
    writer.flush()
    conn.close()
    with TestClient(create_app(settings)) as test_client:
        table = test_client.get("/api/data/entity_tone?key=e1").json()["table"]
        assert table == []
        quotes = test_client.get("/frag/section/entity/e1/quotes").text
        assert "Quote number" not in quotes


def test_entity_quotes_show_both_directions_and_are_escaped(client):
    frag = client.get("/frag/section/entity/e1/quotes").text
    negative, positive = frag.split("Most positive")
    assert "-1.5" in negative
    assert "+1.5" not in negative
    assert "+1.5" in positive
    assert "&lt;Doe&gt;" in frag
    assert "<Doe>" not in frag
    assert 'data-article="t0"' in negative


def test_entity_comentions_and_storylines(client):
    comentions = client.get("/frag/section/entity/e1/comentions").text
    assert "John Roe" in comentions
    assert "kind=entity&key=e1&also=entity:e2" in comentions
    assert evidence_total(client, "kind=entity&key=e1&also=entity:e2") == 1
    stories = client.get("/frag/section/entity/e1/storylines").text
    assert "Georgia polling delays" in stories


def test_entity_race_calls_carry_the_claims_banner(client):
    text = client.get("/entity/e1").text
    assert "not official results" in text
    assert "projected" in text
    assert "projected" not in client.get("/entity/e2").text


def test_entity_without_mentions_has_empty_states(client):
    text = client.get("/entity/e3").text
    assert "Quiet Candidate" in text
    assert "Mentions come from full-text extraction" in text
    assert "No other entities appear" in text
    assert "No negative evidence quotes" in text
    assert "No race-call claims" in text


def test_unknown_entity_is_a_clean_404(client):
    page = client.get("/entity/none")
    assert page.status_code == 404
    assert "No such entity" in page.text


# State


def test_state_page_shows_each_section(client):
    text = client.get("/state/GA").text
    for word in (
        "Georgia",
        "Races",
        "Counties",
        "Attention index over time",
        "Storylines",
        "Local outlets",
        "Race-call claims",
        "battleground",
    ):
        assert word in text
    assert 'data-county-map data-usps="GA" data-fips="13"' in text


def test_state_poll_close_is_read_as_a_timestamp(client):
    text = client.get("/state/GA").text
    assert "Nov 3, 7:00 PM ET" in text
    assert "All polls close Nov 3, 8:00 PM ET" in text
    ohio = client.get("/state/OH").text
    assert "Nov 3, 7:30 PM ET" in ohio
    assert "All polls close" not in ohio
    assert "Not listed" in client.get("/state/PR").text


def test_state_accepts_lowercase_and_rejects_unknown(client):
    assert client.get("/state/ga").status_code == 200
    page = client.get("/state/XX")
    assert page.status_code == 404
    assert "No such state" in page.text


def test_state_races_show_ratings_candidates_and_watch(client, clean_ui):
    frag = client.get("/frag/section/state/GA/races").text
    assert frag.index("Senate") < frag.index("House 07")
    assert "Toss-up" in frag
    assert 'href="https://example.com/ratings"' in frag
    assert "Jane Doe" in frag
    assert "John Roe" in frag
    assert 'hx-post="/watchlist/race?key=2026-GA-SEN&watched=false"' in frag
    assert 'hx-post="/watchlist/race?key=2026-GA-H07&watched=true"' in frag
    assert "Not rated" in frag


def test_race_label_names_measures_and_districts():
    assert (
        race_label(
            {
                "office": "ballot_measure",
                "race_key": "2026-OH-M-school-funding-amendment",
            }
        )
        == "School funding amendment"
    )
    assert race_label({"office": "house", "district": "07"}) == "House 07"
    assert race_label({"office": "lt_governor"}) == "Lt. Governor"


def test_state_counts_match_their_evidence(client):
    text = client.get("/state/GA").text
    n = int(
        re.search(r"unique</span><span class=\"kpi-value\">(\d+)", text).group(
            1
        )
    )
    assert n == 3
    assert n == evidence_total(client, "kind=state&key=GA")


def test_state_attention_chart_has_bins_and_totals(client):
    data = client.get("/api/data/state_attention?key=GA&window=3h").json()
    assert data["table"]
    assert sum(row["n"] for row in data["table"]) == 3
    assert {"attention", "n", "total", "bin_minutes"} <= set(data["table"][0])
    assert data["table"][0]["bin_minutes"] == 15


def test_state_counties_count_items_and_match_evidence(client):
    data = client.get("/api/data/state_counties?key=GA").json()
    assert data["table"] == [{"fips": "13121", "n": 2}]
    assert evidence_total(client, "kind=county&key=13121") == 2
    assert client.get("/api/data/state_counties?key=OH").json()["table"] == []


def test_state_outlets_list_latest_items(client):
    frag = client.get("/frag/section/state/GA/outlets").text
    assert "Peach News" in frag
    assert "Fulton County precincts" in frag
    assert "kind=source&key=peach-news" in frag
    assert evidence_total(client, "kind=source&key=peach-news") == 1
    assert "No outlets are based" in client.get("/state/OH").text


def test_state_storylines_and_calls(client):
    frag = client.get("/frag/section/state/GA/storylines").text
    assert "Georgia polling delays" in frag
    assert "kind=state&key=GA&also=storyline:s1" in frag
    assert evidence_total(client, "kind=state&key=GA&also=storyline:s1") == 3
    assert "projected" in client.get("/frag/section/state/GA/calls").text
    assert "No race-call claims" in client.get("/state/OH").text


def test_evidence_can_drop_the_window_and_the_relevance_filter(client):
    assert evidence_total(client, "kind=source&key=wire") == 10
    text = client.get(
        "/frag/evidence?kind=source&key=wire&span=all&rel=any&copies=all"
    ).text
    assert "12 stories" in text
    assert "all time" in text
    assert "including items not election relevant" in text
    assert "pie contest" in text


# Sources


def test_sources_table_has_every_column_and_anchor(client):
    text = client.get("/sources").text
    for heading in (
        "Outlet",
        "Scope",
        "State",
        "Country",
        "Lang",
        "Lean and citation",
        "Ad Fontes",
        "Ownership",
        "Paywall",
        "Feeds",
        "Last success",
        "Errors",
        "Items",
        "Relevant",
    ):
        assert f">{heading}</button>" in text
    for source_id in ("wire", "left-daily", "kommersant"):
        assert f'id="{source_id}"' in text
    assert "bias -7.8, reliability 40.9" in text
    assert "Family trust" in text


def test_every_lean_label_has_a_citation_or_a_neutral_reading(client):
    text = client.get("/sources").text
    rows = re.findall(
        r'<tr id="([\w-]+)" class="anchor-row">(.*?)</tr>', text, re.S
    )
    assert len(rows) == 7
    for _source, row in rows:
        lean_cell = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)[5]
        if "lean-neutral" in lean_cell:
            assert "Unrated" in lean_cell or "Non-US outlet" in lean_cell
        else:
            assert "https://www.allsides.com/" in lean_cell
            assert "retrieved 2026-" in lean_cell


def test_rated_outlet_without_a_valid_citation_reads_unrated():
    assert lean_display({"lean_label": "R"})["lean_shown"] == "Unrated"
    bad = {"lean_label": "LL", "lean_source_url": "javascript:alert(1)"}
    shown = lean_display(bad)
    assert (shown["lean_shown"], shown["lean_url"]) == ("Unrated", "")
    na = lean_display({"lean_label": "NA", "country": "RU"})
    assert na["lean_shown"] == "Non-US outlet"
    good = lean_display(
        {"lean_label": "C", "lean_source_url": "https://a.example/c"}
    )
    assert (good["lean_shown"], good["lean_glyph"]) == ("C", "○○●○○")


def test_sources_flags_state_affiliation_and_censorship(client):
    text = client.get("/sources").text
    assert "state-affiliated" in text
    assert "Russian domestic press under censorship" in text
    assert "note on Sources page" not in text
    assert censorship_note("Politics feed") == ""


def test_sources_shows_criteria(client):
    text = client.get("/sources").text
    assert "Nonprofit state newsrooms" in text
    assert "Conspiracy or fabrication-prone sites" in text
    assert "Partisan campaign or party sites" in text


def test_sources_fetch_health_comes_from_status_and_fetch_log(client):
    text = client.get("/sources").text
    row = re.search(r'<tr id="wire".*?</tr>', text, re.S).group(0)
    assert "25.0%" in row
    assert "1 of 4 fetches failed" in row
    failing = re.search(r'<tr id="left-daily".*?</tr>', text, re.S).group(0)
    assert "failing" in failing
    assert ALERT not in text


def test_sources_counts_match_their_evidence(client):
    text = client.get("/sources").text
    row = re.search(r'<tr id="wire".*?</tr>', text, re.S).group(0)
    assert "kind=source&key=wire&span=all&rel=any" in row
    assert 'tabindex="0">12</span>' in row
    assert "11 of 12" in row
    assert (
        evidence_total(
            client, "kind=source&key=wire&span=all&rel=any&copies=all"
        )
        == 12
    )
    assert evidence_total(client, "kind=source&key=wire&span=all") == 11


def test_lean_distribution_counts_outlets_by_shown_label(client):
    table = client.get("/api/data/lean_distribution").json()["table"]
    counts = {row["bucket"]: row["n"] for row in table}
    assert counts == {"L": 1, "C": 1, "Non-US": 2, "Unrated": 3}
    assert {row["key"] for row in table} == {"L", "C", "NA", "Unrated"}


# Methods


def test_methods_renders_the_document_and_the_ledger(client):
    text = client.get("/methods").text
    assert "<h2>Metric definitions</h2>" in text
    assert "<table>" in text
    assert "app-header" in text
    assert "$2.00" in text
    assert "classify_a" in text
    assert "claude-sonnet-5-5" in text


def test_methods_escapes_raw_html(client, tmp_path, monkeypatch):
    path = tmp_path / "methods.md"
    path.write_text(
        "# Title\n\n<script>alert(1)</script>\n\n"
        "[bad](javascript:alert(2))\n\n| a |\n|---|\n| b |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(reference, "METHODS_PATH", path)
    text = client.get("/methods").text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;alert(1)" in text
    assert 'href="javascript' not in text
    assert "<td>b</td>" in text


def test_methods_page_survives_a_missing_document(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(reference, "METHODS_PATH", tmp_path / "absent.md")
    response = client.get("/methods")
    assert response.status_code == 200
    assert "document is missing" in response.text


# Status


def test_status_shows_stages_with_load_shedding_fields(client):
    text = client.get("/status").text
    for parser in ("classify_a", "fetch_text", "extract_b"):
        assert f"<td>{parser}</td>" in text
    assert "Shedding load" in text
    assert "77.5" in text
    assert "Backfill paused" in text
    assert "Oldest live item" in text


def test_status_shows_feed_grid_spend_and_maintenance(client):
    text = client.get("/status").text
    assert "feed-grid" in text
    assert "2 ok" in text
    assert "1 failing" in text
    assert "1 circuit open" in text
    assert text.count("feed-cell") == 4
    assert "$1.500" in text
    assert "Last maintenance" in text
    assert "None recorded" in text
    assert ALERT not in text
    assert "HTTP 503" in text


def test_status_labels_disk_categories(client, monkeypatch):
    web = client.app.state.web
    web.reference._disk = None
    monkeypatch.setattr(
        web.model,
        "disk_figures",
        lambda: {
            "lake_live": 1.0,
            "awaiting_cleanup": 0.5,
            "catalog": 0.1,
            "archive": 0.3,
            "new_category": 0.7,
            "backups": 0.2,
            "models": 0.4,
            "reference": 0.01,
            "logs": 0.02,
            "free": 38.0,
        },
    )
    text = client.get("/status").text
    assert "compacted, awaiting cleanup" in text
    assert "0.500 GB" in text
    assert "archive" in text
    assert "0.300 GB" in text
    assert "new category" in text
    assert "38.0 GB" in text
    web.reference._disk = None


def test_disk_figures_read_through_the_read_only_connection(client):
    web = client.app.state.web
    figures = web.model.disk_figures()
    assert "awaiting_cleanup" in figures
    assert figures["free"] > 0


def test_a_failed_disk_walk_keeps_the_last_figures(client, monkeypatch):
    web = client.app.state.web
    web.reference._disk = (0.0, {"lake_live": 2.0})

    def fail():
        raise FileNotFoundError("removed during the walk")

    monkeypatch.setattr(web.model, "disk_figures", fail)
    assert web.reference.disk() == {"lake_live": 2.0}
    web.reference._disk = None


def test_status_handles_a_missing_or_older_status_file(client, page_settings):
    path = page_settings.paths.status
    original = path.read_text(encoding="utf-8")
    try:
        path.unlink()
        text = client.get("/status").text
        assert "status file is missing" in text
        assert "No stages reported" in text
        older = {
            "updated_at": datetime.now(UTC).isoformat(),
            "stages": [
                {
                    "parser": "lang",
                    "version": "1",
                    "depth": 0,
                    "finished": 5,
                    "errors": 0,
                    "lag_seconds": 0.1,
                    "paused": False,
                }
            ],
        }
        path.write_text(json.dumps(older), encoding="utf-8")
        text = client.get("/status").text
        assert "<td>lang</td>" in text
        assert "<td>—</td>" in text
        assert "Shedding load" not in text
    finally:
        path.write_text(original, encoding="utf-8")


def test_status_flags_a_stale_status_file(client, page_settings):
    path = page_settings.paths.status
    original = path.read_text(encoding="utf-8")
    try:
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        path.write_text(json.dumps({"updated_at": old}), encoding="utf-8")
        assert "may have stopped" in client.get("/status").text
    finally:
        path.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    ("feed", "state"),
    [
        ({"open": True, "failures": 5}, "down"),
        ({"failures": 1, "last_success_at": "x"}, "failing"),
        ({"failures": 0}, "waiting"),
        ({"failures": 0, "last_success_at": "x"}, "ok"),
    ],
)
def test_feed_state(feed, state):
    assert feed_state(feed) == state


# Watchlist


def snapshot_of(client) -> int:
    return client.app.state.web.model.snapshot.snapshot_id


def test_watching_a_state_changes_focus_at_once(client, clean_ui):
    before = snapshot_of(client)
    assert (
        "Ohio governor"
        not in client.get("/frag/wire?focus=watchlist&window=1h").text
    )
    response = client.post("/watchlist/state?key=OH&watched=true")
    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "watch-changed"
    assert "Watching" in response.text
    assert (
        "Ohio governor"
        in client.get("/frag/wire?focus=watchlist&window=1h").text
    )
    client.post("/watchlist/state?key=OH&watched=false")
    assert (
        "Ohio governor"
        not in client.get("/frag/wire?focus=watchlist&window=1h").text
    )
    assert snapshot_of(client) == before


def test_watching_a_race_changes_focus_at_once(client, clean_ui):
    before = client.get("/frag/wire?focus=watchlist").text
    assert "Ohio governor" not in before
    response = client.post("/watchlist/race?key=2026-OH-GOV&watched=true")
    assert "Watching" in response.text
    assert "Ohio governor" in client.get("/frag/wire?focus=watchlist").text


def test_unwatching_a_race_the_lake_lists_changes_focus_at_once(
    client, clean_ui, page_settings
):
    assert "Georgia lines" in client.get("/frag/wire?focus=watchlist").text
    response = client.post("/watchlist/race?key=2026-GA-SEN&watched=false")
    assert 'aria-pressed="false"' in response.text
    assert "Georgia lines" not in client.get("/frag/wire?focus=watchlist").text
    assert read_unwatched(page_settings.paths.ui_db) == {"2026-GA-SEN"}
    response = client.post("/watchlist/race?key=2026-GA-SEN&watched=true")
    assert 'aria-pressed="true"' in response.text
    assert read_unwatched(page_settings.paths.ui_db) == set()
    assert "Georgia lines" in client.get("/frag/wire?focus=watchlist").text


def test_watched_race_is_visible_on_every_focus_panel(client, clean_ui):
    client.post("/watchlist/state?key=OH&watched=true")
    data = client.get("/api/data/map?focus=watchlist").json()
    assert "OH" in data["focus_states"]
    assert "GA" in data["focus_states"]
    client.post("/watchlist/race?key=2026-GA-SEN&watched=false")
    data = client.get("/api/data/map?focus=watchlist").json()
    assert "GA" not in data["focus_states"]


def test_empty_watchlist_returns_nothing_rather_than_an_error(
    client, clean_ui
):
    client.post("/watchlist/race?key=2026-GA-SEN&watched=false")
    response = client.get("/frag/wire?focus=watchlist")
    assert response.status_code == 200
    assert "No relevant stories" in response.text
    assert client.get("/frag/kpis?focus=watchlist").status_code == 200
    assert client.get("/api/data/stream?focus=watchlist").status_code == 200


def test_battleground_override_changes_focus_at_once(client, clean_ui):
    text = client.get("/frag/wire?focus=battleground").text
    assert "Ohio governor" not in text
    response = client.post("/watchlist/battleground?key=OH&value=in")
    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "watch-changed"
    assert "Ohio governor" in client.get("/frag/wire?focus=battleground").text
    client.post("/watchlist/battleground?key=OH&value=default")
    assert (
        "Ohio governor" not in client.get("/frag/wire?focus=battleground").text
    )


def test_battleground_override_removes_a_listed_state(client, clean_ui):
    assert client.get("/api/data/map").json()["battleground"] == ["GA"]
    client.post("/watchlist/battleground?key=GA&value=out")
    assert client.get("/api/data/map").json()["battleground"] == []
    response = client.post("/watchlist/battleground?key=GA&value=default")
    assert "Default (on)" in response.text
    assert client.get("/api/data/map").json()["battleground"] == ["GA"]


def test_watchlist_page_lists_races_states_and_focus_counts(client, clean_ui):
    text = client.get("/watchlist").text
    assert "2026-GA-SEN" in text
    assert "School funding amendment" in text
    assert "Puerto Rico" in text
    assert "Default (on)" in text
    assert "Default (off)" in text
    counts = client.get("/watchlist/counts").text
    assert 'id="count-watchlist">3<' in counts
    client.post("/watchlist/state?key=OH&watched=true")
    counts = client.get("/watchlist/counts").text
    assert 'id="count-watchlist">13<' in counts


def test_watch_edits_reject_unknown_keys_and_values(client, clean_ui):
    assert client.post("/watchlist/race?key=x&watched=true").status_code == 404
    assert (
        client.post("/watchlist/state?key=XX&watched=true").status_code == 404
    )
    assert (
        client.post("/watchlist/battleground?key=GA&value=x").status_code
        == 404
    )
    assert (
        client.post("/watchlist/battleground?key=XX&value=in").status_code
        == 404
    )
    states, races = read_watch(client.app.state.web.settings.paths.ui_db)
    assert (states, races) == ([], [])


@pytest.mark.parametrize(
    ("office", "pair"),
    [
        ("senate", "GA|senate"),
        ("governor", "GA|governor"),
        ("house", "GA|house"),
        ("attorney_general", "GA|attorney_general"),
        ("secretary_of_state", "GA|secretary_of_state"),
        ("lt_governor", "GA|lt_governor"),
        ("ballot_measure", "GA|ballot_measure"),
    ],
)
def test_race_state_pair_reads_reference_office_names(office, pair):
    assert race_state_pair({"state": "GA", "office": office}) == pair


def test_close_race_focus_matches_a_senate_race(client):
    assert "Georgia lines" in client.get("/frag/wire?focus=close").text
    assert "Ohio governor" not in client.get("/frag/wire?focus=close").text


# Whole site


def test_every_page_keeps_the_header_and_global_controls(client):
    for path in (
        "/article/a1",
        "/search",
        "/search?q=lines",
        "/story/s1",
        "/entity/e1",
        "/state/GA",
        "/sources",
        "/methods",
        "/status",
        "/watchlist",
    ):
        text = client.get(path).text
        assert "app-header" in text, path
        assert 'title="Window (w)"' in text, path
        assert 'id="drawer"' in text, path


def test_pages_carry_global_filters_into_their_links(client):
    text = client.get("/story/s1?window=3h&level=state").text
    assert "/entity/e1?window=3h&amp;level=state" in text
    assert 'data-qs="window=3h&amp;level=state"' in text


def test_pages_never_write_to_the_lake(lake_template, tmp_path):
    settings = clone_lake(lake_template, tmp_path)
    seed(settings)
    conn = connect(settings, read_only=True)
    query = "SELECT max(snapshot_id) FROM lake.snapshots()"
    before = conn.execute(query).fetchall()
    app = create_app(settings, lambda _settings: FakeEmbedder())
    with TestClient(app) as test_client:
        for path in (
            "/article/a1",
            "/frag/article/a1/raw",
            "/search?q=lines",
            "/story/s1",
            "/entity/e1",
            "/state/GA",
            "/sources",
            "/methods",
            "/status",
            "/watchlist",
        ):
            assert test_client.get(path).status_code == 200, path
        test_client.post("/watchlist/state?key=OH&watched=true")
        test_client.post("/watchlist/race?key=2026-GA-SEN&watched=false")
        test_client.post("/watchlist/battleground?key=OH&value=in")
    assert conn.execute(query).fetchall() == before
    conn.close()
