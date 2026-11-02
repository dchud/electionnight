"""The Methods document: its navigation, its values, and its diagrams."""

import importlib
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient

from monitor.lake import NO_INLINE
from monitor.parsers.classify_a import ClassifyParser
from monitor.parsers.dedupe import DedupeParser
from monitor.parsers.embed import EmbedParser
from monitor.parsers.extract_b import ExtractParser
from monitor.parsers.fetch_text import FetchTextParser
from monitor.parsers.lang import LangParser
from monitor.parsers.storylines import StorylineParser
from monitor.settings import Settings
from monitor.web.app import create_app
from monitor.web.methods_doc import (
    DIAGRAMS_DIR,
    diagram,
    drifted,
    render,
    setting_at,
    stated_values,
    value_text,
)

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "docs" / "methods.md").read_text(encoding="utf-8")
NAMES = ("feed-to-screen", "processes", "ducklake")
SVG = "{http://www.w3.org/2000/svg}"


def diagram_text(name):
    return (DIAGRAMS_DIR / f"{name}.svg").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def client(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("methods"))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# The navigation


def test_every_heading_is_reachable_from_the_navigation(client):
    html = client.get("/methods").text
    nav = html[html.index("data-doc-nav") : html.index('class="doc-main"')]
    headings = [h for h in render(TEXT).headings if h.level > 1]
    assert len(headings) > 30
    for heading in headings:
        assert f'href="#{heading.anchor}"' in nav, heading.text
        assert f'id="{heading.anchor}"' in html, heading.text
    for anchor in ("in-service", "cost-ledger"):
        assert f'href="#{anchor}"' in nav
        assert f'id="{anchor}"' in html


def test_a_repeated_heading_gets_its_own_anchor():
    anchors = [h.anchor for h in render("## A\n\n## A\n\n## A\n").headings]
    assert anchors == ["a", "a-2", "a-3"]


def test_the_page_marks_the_section_in_view(client):
    assert "/static/js/methods.js" in client.get("/methods").text
    script = client.get("/static/js/methods.js").text
    assert "aria-current" in script
    assert "EventSource" not in script


def test_each_section_links_to_a_live_view_that_exists(client):
    sections = re.split(r"^## ", TEXT, flags=re.M)[1:]
    for section in sections:
        title = section.splitlines()[0]
        if title == "Values in this document":
            continue
        links = re.findall(r"\]\((/under[^)#]*)", section)
        assert links, title
        for link in links:
            assert client.get(link).status_code == 200, link


def test_no_wrapped_line_reads_as_a_list_marker():
    assert not re.search(r"^\+ ", TEXT, re.M)
    html = str(render(TEXT).html)
    assert "<li>5), where" not in html


def test_raw_html_in_the_document_stays_escaped():
    html = str(render("# T\n\n<script>alert(1)</script>\n").html)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# The values


@pytest.mark.parametrize(("path", "stated"), stated_values(TEXT))
def test_a_stated_value_matches_the_code(path, stated):
    if path.startswith("monitor."):
        module, _, name = path.rpartition(".")
        actual = getattr(importlib.import_module(module), name)
    else:
        actual = setting_at(Settings(), path)
    assert value_text(actual) == stated


def test_the_values_table_is_read():
    assert len(stated_values(TEXT)) > 60


def test_a_setting_in_service_that_differs_is_flagged():
    settings = Settings()
    assert drifted(TEXT, settings) == []
    settings.commit.batch_seconds = 45
    assert drifted(TEXT, settings) == ["commit.batch_seconds is 45, not 30"]


def test_the_page_warns_when_a_setting_differs(client, monkeypatch):
    web = client.app.state.web
    monkeypatch.setattr(web.settings.commit, "batch_seconds", 45)
    web.reference._methods = None
    html = client.get("/methods").text
    web.reference._methods = None
    assert "commit.batch_seconds is 45, not 30" in html


@pytest.mark.parametrize(
    ("stage", "batch", "workers"),
    [
        ("lang", LangParser.batch_size, LangParser.max_concurrency),
        (
            "embed",
            Settings().nlp.embed_batch_size,
            EmbedParser.max_concurrency,
        ),
        ("dedupe", DedupeParser.batch_size, DedupeParser.max_concurrency),
        (
            "storylines",
            StorylineParser.batch_size,
            StorylineParser.max_concurrency,
        ),
        (
            "classify_a",
            Settings().llm.batch_a,
            Settings().llm.concurrency_a,
        ),
        (
            "fetch_text",
            FetchTextParser.batch_size,
            FetchTextParser.max_concurrency,
        ),
        ("extract_b", ExtractParser.batch_size, Settings().llm.concurrency_b),
    ],
)
def test_the_stage_table_matches_each_parser(stage, batch, workers):
    match = re.search(
        rf"^\| `{stage}` \| [^|]+ \| (\d+) \| (\d+) \|", TEXT, re.M
    )
    assert match, stage
    assert int(match.group(1)) == batch
    assert int(match.group(2)) == workers
    assert ClassifyParser.name == "classify_a"


def test_prose_states_the_settings():
    settings = Settings()
    nlp = settings.nlp
    poll = settings.poll
    tiers = poll.tier_seconds
    for phrase in (
        f"polling tier of {tiers[0]}, {tiers[1]}, or {tiers[2]} s",
        f"{poll.jitter_pct}% jitter",
        f"at least {nlp.relevance_threshold}",
        f"within {nlp.dedupe_window_hours} hours",
        f"Hamming distance of {nlp.dedupe_simhash_distance} or less",
        f"cosine of at least {nlp.dedupe_cosine}",
        f"cosine of at least {nlp.storyline_join}",
        f"at {nlp.storyline_promote_items} items from "
        f"{nlp.storyline_promote_sources} outlets",
        f"commits a batch to DuckLake every {settings.commit.batch_seconds} s",
        f"every {settings.web.snapshot_poll_seconds:.0f} s",
        f"join threshold of {nlp.storyline_join}",
    ):
        assert phrase in re.sub(r"\s+", " ", TEXT), phrase


# The diagrams


@pytest.mark.parametrize("name", NAMES)
def test_a_diagram_follows_the_tokens_and_has_fallbacks(name):
    text = diagram_text(name)
    tokens = re.findall(r"var\(([^)]*)\)", text)
    assert tokens
    assert all("," in token for token in tokens), tokens
    ElementTree.fromstring(text)
    assert "<script" not in text
    assert "href=" not in text
    assert 'role="img"' in text
    assert "<title" in text


@pytest.mark.parametrize("name", NAMES)
def test_the_document_inlines_a_diagram(name):
    assert f"diagrams/{name}.svg" in TEXT
    html = str(render(TEXT).html)
    title = re.search(r'<title id="([^"]+)"', diagram_text(name)).group(1)
    assert f'id="{title}"' in html


def test_diagram_ids_do_not_collide_on_one_page():
    ids = []
    for name in NAMES:
        ids += re.findall(r'\bid="([^"]+)"', diagram_text(name))
    assert len(ids) == len(set(ids))


def test_a_missing_or_odd_diagram_name_gives_nothing():
    assert diagram("no-such-diagram") == ""
    assert diagram("../secret") == ""


def test_diagrams_state_the_settings():
    settings = Settings()
    tiers = settings.poll.tier_seconds
    feed = diagram_text("feed-to-screen")
    processes = diagram_text("processes")
    lake = diagram_text("ducklake")
    for phrase in (
        f"every {tiers[0]}, {tiers[1]}, or",
        f"{tiers[2]} s by tier",
        f"every {settings.commit.batch_seconds} s",
        f"polls every {settings.web.snapshot_poll_seconds:.0f} s",
    ):
        assert phrase in feed, phrase
    assert "written every 10 s" in processes
    assert "read every 2 s" in processes
    limit = settings.lake.data_inlining_row_limit
    for phrase in (
        f"up to {limit:,} rows",
        f"zstd level {settings.lake.parquet_compression_level}",
        settings.lake.target_file_size,
        f"every {settings.commit.maintenance_minutes} minutes",
    ):
        assert phrase in lake, phrase
    assert len(NO_INLINE) == 4
    for word in ("embeddings", "prompts", "traces", "article text"):
        assert word in lake


def diagram_arrows(name):
    """Each arrow as (start box, end box), from the SVG's own geometry."""
    root = ElementTree.fromstring(diagram_text(name))
    texts = [
        (float(t.get("x")), float(t.get("y")), t.text)
        for t in root.iter(f"{SVG}text")
    ]
    boxes = []
    for rect in root.iter(f"{SVG}rect"):
        if "bg" in rect.get("class", ""):
            continue
        x, y = float(rect.get("x")), float(rect.get("y"))
        w, h = float(rect.get("width")), float(rect.get("height"))
        inside = [
            t for t in texts if x <= t[0] <= x + w and y <= t[1] <= y + h
        ]
        label = min(inside, key=lambda t: (t[1], t[0]))[2] if inside else ""
        boxes.append((x, y, w, h, label))

    def box_at(px, py):
        hits = [
            b
            for b in boxes
            if b[0] - 1 <= px <= b[0] + b[2] + 1
            and b[1] - 1 <= py <= b[1] + b[3] + 1
        ]
        return min(hits, key=lambda b: b[2] * b[3])[4] if hits else None

    arrows = set()
    for path in root.iter(f"{SVG}path"):
        if not path.get("marker-end"):
            continue
        x = y = 0.0
        points = []
        for op, value in re.findall(r"([MLHV])\s*([-\d. ]+)", path.get("d")):
            numbers = [float(n) for n in value.split()]
            if op in "ML":
                x, y = numbers[0], numbers[1]
            elif op == "H":
                x = numbers[0]
            else:
                y = numbers[0]
            points.append((x, y))
        arrows.add((box_at(*points[0]), box_at(*points[-1])))
    return arrows


def test_feed_diagram_arrows_match_the_stage_links_in_the_code():
    source = (ROOT / "src/monitor/pipeline.py").read_text(encoding="utf-8")
    names = {
        "lang": "lang",
        "classify": "classify_a",
        "embed": "embed",
        "dedupe": "dedupe",
        "story": "storylines",
        "fetch": "fetch_text",
        "extract": "extract_b",
    }
    links = re.findall(
        r"\((\w+), (\w+)\),?", source[source.index("links = [") :]
    )
    coded = {
        (names[first], names[second])
        for first, second in links
        if first in names and second in names
    }
    assert ("classify_a", "fetch_text") in coded
    assert len(coded) == 6
    arrows = diagram_arrows("feed-to-screen")
    drawn = {
        arrow
        for arrow in arrows
        if arrow[0] in names.values() and arrow[1] in names.values()
    }
    assert drawn == coded


def test_feed_diagram_draws_the_rest_of_the_path():
    arrows = diagram_arrows("feed-to-screen")
    for edge in (
        ("Outlet feeds", "Feed poller"),
        ("Feed poller", "lang"),
        ("Feed poller", "BatchWriter"),
        ("Feed poller", "Archive files"),
        ("Stages, each with its own queue and workers", "BatchWriter"),
        ("Periodic jobs", "BatchWriter"),
        ("BatchWriter", "DuckLake"),
        ("DuckLake", "Periodic jobs"),
        ("DuckLake", "Read model"),
        ("Read model", "Panels"),
        ("Read model", "SSE hub, /events"),
        ("Panels", "Browser"),
        ("Browser", "Panels"),
        ("SSE hub, /events", "Browser"),
    ):
        assert edge in arrows, edge
    # The web process reads the lake and never writes to it.
    assert ("Read model", "DuckLake") not in arrows
    assert ("Panels", "DuckLake") not in arrows


def test_process_diagram_keeps_the_single_writer_rule():
    arrows = diagram_arrows("processes")
    for edge in (
        ("monitor command", "data/control/"),
        ("data/control/", "pipeline process"),
        ("monitor command", "data/writer.lock"),
        ("pipeline process", "data/status.json"),
        ("pipeline process", "data/archive/"),
        ("pipeline process", "DuckLake"),
        ("DuckLake", "web process"),
        ("DuckLake", "child process"),
        ("data/status.json", "web process"),
        ("data/archive/", "child process"),
        ("web process", "child process"),
    ):
        assert edge in arrows, edge
    assert ("web process", "DuckLake") not in arrows
    assert ("child process", "DuckLake") not in arrows
