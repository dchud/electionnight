"""The Methods document: its navigation, its values, and its diagrams."""

import importlib
import inspect
import re
import xml.etree.ElementTree as ElementTree
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import clone_lake
from fastapi.testclient import TestClient

from monitor import analytics, maintenance, orchestrator, pipeline, rollups
from monitor.ingest import poller
from monitor.lake import NO_INLINE
from monitor.llm import budget
from monitor.llm import client as llm_client
from monitor.parsers import (
    briefs,
    dedupe,
    embed,
    entities,
    extract_b,
    fetch_text,
    lang,
    storylines,
)
from monitor.parsers.base import Item
from monitor.parsers.classify_a import ClassifyParser
from monitor.parsers.dedupe import DedupeIndex, DedupeParser
from monitor.parsers.embed import EmbedParser
from monitor.parsers.entities import RESOLUTION_SYSTEM
from monitor.parsers.extract_b import ExtractParser
from monitor.parsers.fetch_text import FetchTextParser
from monitor.parsers.lang import LangParser
from monitor.parsers.storylines import NAMING_SYSTEM, StorylineParser
from monitor.settings import Settings
from monitor.web import panels, readmodel, under, under_worker
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
WALKTHROUGH = (ROOT / "docs" / "walkthrough.md").read_text(encoding="utf-8")
NAMES = ("feed-to-screen", "processes", "ducklake")
SVG = "{http://www.w3.org/2000/svg}"
STAGES_AREA = "Stages, each with its own queue and workers"


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


def test_the_page_loads_the_navigation_script(client):
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


def spaced(text):
    return re.sub(r"\s+", " ", text)


def source_of(module):
    return Path(inspect.getfile(module)).read_text(encoding="utf-8")


def hours(delta):
    return int(delta / timedelta(hours=1))


def minutes(delta):
    return int(delta / timedelta(minutes=1))


# A value written as a bare literal in code is read from that code here.
CONTROL_SECONDS = int(
    re.search(r'self\.every\((\d+), "control"', source_of(pipeline)).group(1)
)
RETRY_JITTER_PCT = round(
    (
        float(
            re.search(
                r"delay \* random\.uniform\([\d.]+, ([\d.]+)\)",
                source_of(orchestrator),
            ).group(1)
        )
        - 1
    )
    * 100
)
RETRY_SECONDS = int(
    inspect.signature(orchestrator.Stage).parameters["retry_delay"].default
)
SHED_THROTTLE = float(
    re.search(
        r"self\.embed\.throttle = ([\d.]+) if", source_of(pipeline)
    ).group(1)
)
TRUNCATION_FACTOR = int(
    re.search(r"max_tokens \*= (\d+)", source_of(extract_b)).group(1)
)
INLINE_LIMIT = Settings().lake.data_inlining_row_limit


def stated_phrases():
    """Each sentence fragment in the document that states a value."""
    settings = Settings()
    nlp = settings.nlp
    poll = settings.poll
    commit = settings.commit
    llm = settings.llm
    lake = settings.lake
    tiers = poll.tier_seconds
    tone = extract_b.TONE_LIMIT
    pseudo = analytics.ATTENTION_PSEUDO_COUNT
    short = analytics.SHORT_WINDOW_MINUTES
    trailing = analytics.TRAILING_WINDOW_MINUTES
    start = panels.BASELINE_START
    end = panels.BASELINE_END
    paywall = sorted(fetch_text.PAYWALL_STATUSES)
    return [
        # Collection
        f"polling tier of {tiers[0]}, {tiers[1]}, or {tiers[2]} s",
        f"{poll.jitter_pct}% jitter",
        f"At most {poll.per_domain_concurrency} requests run per domain",
        f"each with a {poll.timeout_seconds:.0f} s timeout",
        f"up to {poller.MAX_BACKOFF_MULTIPLIER} times the tier and never over "
        f"{poll.circuit_retry_seconds} s",
        f"After {poll.circuit_failures} failures in a row",
        f"tried every {poll.circuit_retry_seconds} s",
        f"Each outlet is polled every {tiers[0]}, {tiers[1]}, or {tiers[2]} "
        "seconds",
        # Feed to screen and processes
        f"commits a batch to DuckLake every {commit.batch_seconds} s",
        f"polls the catalog every {settings.web.snapshot_poll_seconds:.0f} s",
        f"the pipeline reads it within {CONTROL_SECONDS} s",
        f"`data/status.json` every {pipeline.STATUS_SECONDS} s",
        # Stages
        f"at least {nlp.relevance_threshold}",
        f"after {RETRY_SECONDS} s and again after {RETRY_SECONDS * 2} s",
        f"each with up to {RETRY_JITTER_PCT}% jitter",
        f"After {orchestrator.MAX_ATTEMPTS} attempts",
        f"has waited over {pipeline.LAG_HIGH_SECONDS:.0f} s",
        f"It resumes under {pipeline.LAG_LOW_SECONDS:.0f} s",
        f"first {lang_text_chars():,} characters of the title and summary",
        f"confidence of at least {lang.TRUST_DETECTION}",
        f"at least {lang.MIN_LETTERS_TO_OVERRIDE} letters",
        f"counts as {lang.WIDE_LETTER_WEIGHT} letters",
        f"bulk model {llm.batch_a} at a time",
        f"At {budget.SOFT_FRACTION:.0%} of the daily cap",
        f"`{nlp.embed_model}` runs on the CPU",
        f"with {embed.ONNX_THREADS} threads, in batches of "
        f"{nlp.embed_batch_size}",
        f"It uses {nlp.embed_duty_cycle:.0%} of the time",
        f"`{embed.PASSAGE_PREFIX}` followed by",
        f"uses the `{embed.QUERY_PREFIX}` prefix",
        f"Each vector has {embed.EMBED_DIM} values",
        f"`FLOAT[{embed.EMBED_DIM}]`",
        f"seen within {nlp.dedupe_window_hours} hours",
        f"A title of under {dedupe.MIN_TITLE_TOKENS} tokens",
        f"Hamming distance of {nlp.dedupe_simhash_distance} or less",
        f"an item matches at a cosine of at least {nlp.dedupe_cosine}",
        f"whose centroid has a cosine of at least {nlp.storyline_join}",
        f"in the last {hours(storylines.POOL_WINDOW)} hours",
        f"$(1 - {storylines.EMA_ALPHA}) c + {storylines.EMA_ALPHA} v$",
        f"at {nlp.storyline_promote_items} items from "
        f"{nlp.storyline_promote_sources} outlets within "
        f"{hours(storylines.PROMOTION_WINDOW)} hours",
        f"{storylines.LABEL_GROWTH} times the items",
        f"from {storylines.HEADLINES_FOR_LABEL} headlines",
        f"A pass every {pipeline.STORYLINE_PASS_SECONDS} s merges active "
        f"storylines at a cosine of at least {nlp.storyline_merge}",
        f"no item for {hours(storylines.DORMANT_AFTER)} hours",
        f"join threshold of {nlp.storyline_join} and a merge threshold of "
        f"{nlp.storyline_merge}",
        f"waits {nlp.fetch_domain_interval_seconds:.0f} s between requests",
        f"a {nlp.fetch_timeout_seconds:.0f} s timeout and a "
        f"{nlp.fetch_deadline_seconds:.0f} s deadline",
        f"up to {fetch_text.MAX_TEXT_CHARS:,} characters",
        f"A status of {paywall[0]}, {paywall[1]}, or {paywall[2]}",
        f"at most {extract_b.MAX_INPUT_TOKENS:,} tokens of text",
        f"returns at most {extract_b.MAX_OUTPUT_TOKENS:,} tokens",
        f"clamped to -{tone:.0f} to +{tone:.0f}",
        f"fuzzy match of at least {nlp.entity_fuzzy_score}",
        f"Every {pipeline.ENTITY_PASS_SECONDS} s, a pass asks the bulk model "
        f"about up to {entities.UNRESOLVED_PER_PASS} unresolved names",
        f"Every {pipeline.ROLLUP_SECONDS} s, rollups recompute the last "
        f"{hours(rollups.RECENT_WINDOW)} hours of "
        f"{rollups.BIN_SECONDS // 60}-minute",
        f"at most {briefs.MAX_ITEMS} items, with at most "
        f"{briefs.MAX_ITEMS_PER_SOURCE} from one outlet",
        f"Each outlet supplies at most {briefs.MAX_ITEMS_PER_SOURCE} items",
        # Models
        f"`{llm.model_bulk}`",
        f"`{llm.model_brief}`",
        f"{llm.batch_a} items per call",
        f"at most {extract_b.MAX_INPUT_TOKENS:,} input tokens",
        f"{embed.EMBED_DIM} dimensions",
        # Prompts
        f"a quote of {extract_b.EVIDENCE_MIN_WORDS} to "
        f"{extract_b.EVIDENCE_MAX_WORDS} words",
        f"within {extract_b.NAME_WINDOW_CHARS} characters of it",
        # Metrics
        f"the count in the last {short} minutes",
        f"the count in the {trailing} minutes before that, times "
        f"{short}/{trailing}",
        f"score is at least {analytics.RISING_SCORE:.0f} and $o$ is at least "
        f"{analytics.RISING_MIN_OBSERVED}",
        f"within {minutes(analytics.NEW_WINDOW)} minutes",
        f"{start:%B} {start.day} {start:%H:%M} to {end:%B} {end.day} "
        f"{end:%H:%M} ET",
        f"shrinks toward 1 with {pseudo:.0f} pseudo-counts",
        f"\\frac{{n_s + {pseudo:.0f}}}{{E_s + {pseudo:.0f}}}",
        f"The attention index of the last {short} minutes, with the "
        f"{trailing} minutes before them",
        f"ends of the scale, {analytics.TONE_MIN:.0f} and "
        f"+{analytics.TONE_MAX:.0f}",
        f"Hidden when $n$ is under {analytics.TONE_HIDE_BELOW}",
        f'"low n" when $n$ is under {analytics.TONE_LOW_N_BELOW}',
        f"covers the last {panels.KPI_RECENT_MINUTES} minutes",
        f"clipped to {under_worker.CONSOLE_MAX_CELL_CHARS:,} characters per "
        "cell",
        # DuckLake
        f"An insert of up to {INLINE_LIMIT:,} rows",
        f"zstd at level {lake.parquet_compression_level}",
        f"target size of {lake.target_file_size}",
        f"Every {commit.maintenance_minutes} minutes, maintenance",
        f"{hours(maintenance.CLEANUP_AGE)} hours after it was replaced",
        f"backups run every {commit.backup_minutes} minutes",
        f"The newest {commit.backups_keep_recent} are kept",
        # Read model
        f"Every {settings.web.snapshot_poll_seconds:.0f} s, the hub",
        f"less {minutes(readmodel.WATERMARK_OVERLAP)} minutes",
        f"following at most {readmodel.MERGE_HOPS} merges",
        # Cost
        f"{llm.cache_read_multiplier}\\,\\text{{cache read}}",
        f"{llm.cache_write_multiplier}\\,\\text{{cache write}}",
        f"no success for {llm_client.CACHE_COLD_SECONDS:.0f} s",
        f"for at most {llm_client.COLD_WAIT_SECONDS:.0f} s",
    ]


def lang_text_chars():
    """How much text the language stage reads, from what it passes along."""
    seen = []

    class Detector:
        def compute_language_confidence_values(self, text):
            seen.append(text)
            return []

    parser = LangParser.__new__(LangParser)
    parser._detector = Detector()
    parser._run([Item(item_id="a", title="t" * 5000, summary="s" * 5000)])
    return len(seen[0])


@pytest.mark.parametrize("phrase", stated_phrases())
def test_prose_states_the_value_in_the_code(phrase):
    assert phrase in spaced(TEXT), phrase


def test_a_prose_value_that_drifts_fails_the_comparison():
    """The list is built from the code, so a changed constant changes it."""
    phrases = stated_phrases()
    assert any(
        f"After {orchestrator.MAX_ATTEMPTS} attempts" in p for p in phrases
    )
    assert "After 99 attempts" not in spaced(TEXT)


def test_core_stages_are_the_ones_named_for_load_shedding():
    section = spaced(TEXT)
    named = re.search(
        r"waiting at `(\w+)`, `(\w+)`, `(\w+)`, `(\w+)`, or `(\w+)`", section
    ).groups()
    assert set(named) == pipeline.CORE_STAGES
    assert "halves the embedding rate" in section
    assert SHED_THROTTLE == 0.5


def test_a_truncated_answer_gets_twice_the_limit():
    assert "with twice the output limit" in spaced(TEXT)
    assert TRUNCATION_FACTOR == 2


def test_rising_sql_uses_the_stated_windows():
    sql = panels.load_sql("rising")
    short = analytics.SHORT_WINDOW_MINUTES
    trailing = analytics.TRAILING_WINDOW_MINUTES
    assert f"INTERVAL {short + trailing} MINUTE" in sql
    assert f"INTERVAL {short} MINUTE" in sql
    assert f"c.trailing * {short}.0 / {trailing}" in sql


def test_time_axis_sql_uses_the_stated_spans():
    sql = readmodel.FACTS_SELECT
    section = spaced(TEXT)
    backlog = re.search(r"INTERVAL (\d+) HOUR", sql).group(1)
    after = re.search(r"g\.gap_end \+ INTERVAL (\d+) MINUTE", sql).group(1)
    before = re.search(r"g\.gap_start - INTERVAL (\d+) MINUTE", sql).group(1)
    assert f"published more than {backlog} hours before" in section
    assert f"up to {before} minutes before it" in section
    assert f"first seen within {after} minutes after it" in section


def test_walkthrough_states_the_limits_in_the_code():
    text = spaced(WALKTHROUGH)
    for phrase in (
        f"stopped at {under.CHILD_LIMIT_SECONDS:.0f} seconds",
        f"keyword comparison gets {under.AGREEMENT_LIMIT_SECONDS:.0f} seconds",
        f"archive read gets {under.ARCHIVE_LIMIT_SECONDS:.0f}",
        f"at most {under.CONSOLE_MAX_ROWS:,} rows",
        f"kept for {under.SCAN_TTL_SECONDS // 60:.0f} minutes, or "
        f"{under.SLOW_SCAN_TTL_SECONDS // 60:.0f} minutes",
        f"after {Settings().poll.circuit_failures} failures in a row",
    ):
        assert phrase in text, phrase


@pytest.mark.parametrize(
    ("quote", "prompt"),
    [
        (
            "You classify news items for a nonpartisan monitor of the "
            "November 3, 2026 US midterm elections.",
            "classify_a.md",
        ),
        (
            "Extract only what the text states. Do not infer outcomes and "
            "do not add outside knowledge.",
            "extract_b.md",
        ),
        (
            "This is about the portrayal, not whether the news is good for "
            "them",
            "extract_b.md",
        ),
        ("Name the event or subject, not its significance.", NAMING_SYSTEM),
        (
            "Null if the surface is too ambiguous to identify from the "
            "context given. Do not guess.",
            RESOLUTION_SYSTEM,
        ),
        ("Every sentence must rest on the supplied items.", briefs.SYSTEM),
        (
            "Results are claims reported by news outlets, not official "
            "results. Attribute them",
            briefs.SYSTEM,
        ),
    ],
)
def test_a_quoted_prompt_is_in_the_prompt(quote, prompt):
    if prompt.endswith(".md"):
        path = ROOT / "src/monitor/llm/prompts" / prompt
        prompt = path.read_text(encoding="utf-8")
    assert quote in prompt
    assert quote in spaced(TEXT)


def t_interval_coverage(n):
    """Simulated coverage of the interval the dashboard draws."""
    rng = np.random.default_rng(n)
    draws = rng.normal(-1.0, 0.5, (40_000, n))
    margin = analytics.t_975(n - 1) * draws.std(axis=1, ddof=1) / n**0.5
    return np.mean(np.abs(draws.mean(axis=1) + 1.0) <= margin)


def shrunk_interval_coverage(n):
    """Simulated coverage of the interval the specification gives."""
    rng = np.random.default_rng(n)
    draws = np.clip(rng.normal(-1.0, 0.5, (40_000, n)), -2.0, 2.0)
    shrunk = draws.mean(axis=1) * n / (n + 5)
    margin = analytics.Z_975 * draws.std(axis=1, ddof=1) / n**0.5
    return np.mean(np.abs(shrunk + 1.0) <= margin)


@pytest.mark.parametrize("n", [6, 14, 30, 100])
def test_the_stated_interval_covers_95_percent(n):
    assert "$n$ of 6, 14, 30, and 100" in spaced(TEXT)
    assert t_interval_coverage(n) == pytest.approx(0.95, abs=0.01)


@pytest.mark.parametrize(("n", "percent"), [(6, 32), (100, 83)])
def test_the_stated_shrunk_coverage_matches_a_simulation(n, percent):
    assert f"{percent}% at $n$ of {n}" in spaced(TEXT)
    assert shrunk_interval_coverage(n) == pytest.approx(
        percent / 100, abs=0.015
    )


# What the prose claims about how the system works


def test_every_row_goes_through_one_batch_writer():
    section = spaced(TEXT)
    assert "Every row the pipeline writes goes through one `BatchWriter`" in (
        section
    )
    assert "Maintenance runs flush and merge directly on the same" in section
    assert "every row the pipeline writes goes through one batch writer" in (
        spaced(WALKTHROUGH)
    )
    assert "every writer goes through" not in spaced(WALKTHROUGH)
    # The one place that inserts rows. `verify.py` proves a reader cannot.
    inserting = {
        path.name
        for path in (ROOT / "src/monitor").rglob("*.py")
        if "INSERT INTO lake" in path.read_text(encoding="utf-8")
    }
    assert inserting == {"lake.py", "verify.py"}


def test_a_monitor_command_can_write_with_no_pipeline():
    source = source_of(importlib.import_module("monitor.control"))
    assert "prune_standalone" in source
    assert "rollups_standalone" in source
    section = spaced(TEXT)
    assert "the command holds the lock and writes directly" in section
    assert "a result file comes back there" in section
    assert "One process, `pipeline`, writes to the lake" not in section


def test_cleanup_says_when_the_clock_starts():
    section = spaced(TEXT)
    assert (
        "The clock starts when a merge or flush replaces the file" in section
    )
    assert "they are 2 hours old" not in section
    assert "they are 2 hours old" not in diagram_words("ducklake")


def test_velocity_is_for_storylines_only():
    section = spaced(TEXT)
    assert "For a storyline, $o$" in section
    assert "storyline or entity" not in section
    assert "Entities get no velocity score" in section
    uses = [
        path.name
        for path in (ROOT / "src/monitor/web").rglob("*.py")
        if "velocity_score" in path.read_text(encoding="utf-8")
    ]
    # The cards page shows this function by name, and does not call it.
    assert set(uses) <= {"under_panels.py"}


def test_every_text_basis_in_the_code_is_stated():
    values = set(
        re.findall(
            r'"(summary\+text|summary|full_text)"$', source_of(extract_b), re.M
        )
    )
    assert values == {"summary", "summary+text", "full_text"}
    for value in values:
        assert f"`{value}`" in TEXT


def test_every_dedupe_method_is_stated():
    assert '"title_hash"' in source_of(dedupe)
    assert '"embedding"' in source_of(dedupe)
    section = spaced(TEXT)
    for method in ("title_hash", "embedding", "none", "url"):
        assert f"`{method}`" in section


def test_the_place_check_is_as_narrow_as_stated():
    assert (
        "Two items fail that check only when both name states and share none"
        in spaced(TEXT)
    )
    index = SimpleNamespace(states=[frozenset({"GA"}), frozenset()])
    same_place = DedupeIndex._same_place
    assert same_place(index, 0, frozenset({"GA", "FL"})) is True
    assert same_place(index, 0, frozenset({"FL"})) is False
    assert same_place(index, 0, frozenset()) is True
    assert same_place(index, 1, frozenset({"FL"})) is True


def test_scans_and_the_console_have_separate_gates():
    source = source_of(under)
    assert "self._gate = threading.Semaphore(1)" in source
    assert "self._console_gate = threading.Semaphore(1)" in source
    section = spaced(TEXT)
    assert "a console query can run beside a scan" in section
    assert "one at a time, at a lowered priority" not in section
    assert "One scan at a time" in diagram_words("processes")


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


def diagram_words(name):
    """A diagram's text in document order, as one string."""
    root = ElementTree.fromstring(diagram_text(name))
    return " ".join(t.text or "" for t in root.iter(f"{SVG}text"))


def diagram_phrases():
    settings = Settings()
    tiers = settings.poll.tier_seconds
    commit = settings.commit
    lake = settings.lake
    return [
        ("feed-to-screen", f"every {tiers[0]}, {tiers[1]}, or"),
        ("feed-to-screen", f"{tiers[2]} s by tier"),
        ("feed-to-screen", f"every {commit.batch_seconds} s"),
        (
            "feed-to-screen",
            f"polls every {settings.web.snapshot_poll_seconds:.0f} s",
        ),
        ("feed-to-screen", f"rollups every {pipeline.ROLLUP_SECONDS} s"),
        (
            "feed-to-screen",
            f"briefs checked every {pipeline.BRIEF_CHECK_SECONDS} s",
        ),
        ("feed-to-screen", f"entity pass {pipeline.ENTITY_PASS_SECONDS} s"),
        (
            "feed-to-screen",
            f"storyline pass {pipeline.STORYLINE_PASS_SECONDS} s",
        ),
        ("feed-to-screen", f"every {commit.maintenance_minutes} minutes"),
        ("feed-to-screen", f"{embed.EMBED_DIM} floats"),
        ("processes", f"written every {pipeline.STATUS_SECONDS} s"),
        ("processes", f"read every {CONTROL_SECONDS} s"),
        ("ducklake", f"up to {INLINE_LIMIT:,} rows"),
        ("ducklake", f"zstd level {lake.parquet_compression_level}"),
        ("ducklake", lake.target_file_size),
        ("ducklake", f"every {commit.maintenance_minutes} minutes"),
        (
            "ducklake",
            f"is deleted {hours(maintenance.CLEANUP_AGE)} hours",
        ),
    ]


@pytest.mark.parametrize(("name", "phrase"), diagram_phrases())
def test_a_diagram_states_the_value_in_the_code(name, phrase):
    assert phrase in diagram_words(name), phrase


def test_the_diagram_names_the_tables_that_never_inline():
    assert len(NO_INLINE) == 4
    words = diagram_words("ducklake")
    for word in ("embeddings", "prompts", "traces", "article text"):
        assert word in words


def shapes(text):
    """Every rect as (x, y, w, h, label, class), from the SVG's geometry."""
    root = ElementTree.fromstring(text)
    texts = [
        (float(t.get("x")), float(t.get("y")), t.text, t.get("class", ""))
        for t in root.iter(f"{SVG}text")
    ]
    found = []
    for rect in root.iter(f"{SVG}rect"):
        css = rect.get("class", "")
        if "bg" in css:
            continue
        x, y = float(rect.get("x")), float(rect.get("y"))
        w, h = float(rect.get("width")), float(rect.get("height"))
        inside = [
            t for t in texts if x <= t[0] <= x + w and y <= t[1] <= y + h
        ]
        if "area" in css:
            inside = [t for t in inside if "area-name" in t[3]]
        label = min(inside, key=lambda t: (t[1], t[0]))[2] if inside else ""
        found.append((x, y, w, h, label, css))
    return found


def arrows_in(text):
    """Each arrow as (start box, end box). A two-way arrow counts both ways."""
    root = ElementTree.fromstring(text)
    boxes = shapes(text)

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
        start_marked = path.get("marker-start")
        end_marked = path.get("marker-end")
        if not start_marked and not end_marked:
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
        first, last = box_at(*points[0]), box_at(*points[-1])
        if end_marked:
            arrows.add((first, last))
        if start_marked:
            arrows.add((last, first))
    return arrows


def diagram_arrows(name):
    return arrows_in(diagram_text(name))


def members_of(name, area):
    """Labels of the boxes whose center lies inside a dashed area."""
    boxes = shapes(diagram_text(name))
    outer = next(b for b in boxes if "area" in b[5] and b[4] == area)
    return {
        b[4]
        for b in boxes
        if b is not outer
        and "area" not in b[5]
        and outer[0] <= b[0] + b[2] / 2 <= outer[0] + outer[2]
        and outer[1] <= b[1] + b[3] / 2 <= outer[1] + outer[3]
    }


def test_a_two_way_arrow_counts_in_both_directions():
    text = diagram_text("processes")
    old = 'marker-end="url(#pr-arrow)" d="M626 250 H680"'
    assert old in text
    two_way = text.replace(old, f'marker-start="url(#pr-arrow)" {old}')
    assert ("web process", "DuckLake") not in arrows_in(text)
    assert ("web process", "DuckLake") in arrows_in(two_way)


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
        ("Outlet sites", "Feed poller"),
        ("Outlet sites", "fetch_text"),
        ("Feed poller", "lang"),
        ("Feed poller", "BatchWriter"),
        ("Feed poller", "Archive files"),
        ("fetch_text", "Archive files"),
        (STAGES_AREA, "BatchWriter"),
        ("Periodic jobs", "BatchWriter"),
        ("BatchWriter", "DuckLake"),
        ("Maintenance", "DuckLake"),
        ("DuckLake", "Periodic jobs"),
        ("DuckLake", STAGES_AREA),
        ("DuckLake", "Feed poller"),
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
    # Maintenance does not go through the batch writer.
    assert ("Maintenance", "BatchWriter") not in arrows


def test_a_box_inside_a_process_area_is_in_that_process():
    assert members_of("feed-to-screen", "pipeline process") == {
        "Feed poller",
        "lang",
        "classify_a",
        "embed",
        "dedupe",
        "storylines",
        "fetch_text",
        "extract_b",
        "BatchWriter",
        "Periodic jobs",
        "Maintenance",
    }
    assert members_of("feed-to-screen", "web process") == {
        "Read model",
        "Panels",
        "SSE hub, /events",
    }
    assert members_of("feed-to-screen", STAGES_AREA) == {
        "lang",
        "classify_a",
        "embed",
        "dedupe",
        "storylines",
        "fetch_text",
        "extract_b",
    }


def test_no_file_box_sits_inside_a_process_box():
    boxes = shapes(diagram_text("processes"))
    for file_box in (b for b in boxes if "file" in b[5]):
        center_x = file_box[0] + file_box[2] / 2
        center_y = file_box[1] + file_box[3] / 2
        for process in (b for b in boxes if "file" not in b[5]):
            assert not (
                process[0] <= center_x <= process[0] + process[2]
                and process[1] <= center_y <= process[1] + process[3]
            ), (file_box[4], process[4])


def test_process_diagram_keeps_the_single_writer_rule():
    arrows = diagram_arrows("processes")
    for edge in (
        ("monitor command", "data/control/"),
        ("data/control/", "monitor command"),
        ("data/control/", "pipeline process"),
        ("pipeline process", "data/control/"),
        ("monitor command", "data/writer.lock"),
        ("monitor command", "DuckLake"),
        ("pipeline process", "data/writer.lock"),
        ("pipeline process", "data/status.json"),
        ("pipeline process", "data/archive/"),
        ("pipeline process", "DuckLake"),
        ("DuckLake", "pipeline process"),
        ("DuckLake", "web process"),
        ("DuckLake", "child process"),
        ("data/status.json", "web process"),
        ("data/archive/", "child process"),
        ("web process", "child process"),
    ):
        assert edge in arrows, edge
    assert ("web process", "DuckLake") not in arrows
    assert ("child process", "DuckLake") not in arrows


def test_lake_diagram_draws_the_pipeline_both_ways():
    arrows = diagram_arrows("ducklake")
    engine = "DuckDB, one in each process"
    assert ("pipeline", engine) in arrows
    assert (engine, "pipeline") in arrows
    assert "INSERT and SELECT" in diagram_words("ducklake")
