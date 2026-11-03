"""The formulas in the Methods page: their rendering, and their agreement.

A case compares the formula with the function the dashboard calls. Where the
TeX writes a constant, as in the velocity, attention, t, and cost formulas, the
case reads it from the document. The share, title-hash, and merge formulas
write no constant, so those cases compute the formula in the test.
"""

import inspect
import math
import re
from pathlib import Path

import duckdb
import numpy as np
import pytest
import xxhash
from conftest import clone_lake
from test_under import app_client

from monitor import analytics
from monitor.llm.budget import estimate_usd
from monitor.parsers import dedupe, storylines
from monitor.settings import Settings
from monitor.web.methods_doc import (
    hold_math,
    render,
    section_text,
    sections_html,
)
from monitor.web.panels import Panels, load_sql

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "docs" / "methods.md").read_text(encoding="utf-8")


def number_after(pattern, text=TEXT):
    """The number a pattern captures in the document's TeX."""
    match = re.search(pattern, text)
    assert match, pattern
    return float(match.group(1))


# Rendering.


def test_markdown_does_not_touch_math():
    html = render("Text $a_1 * b_2 * c$ and more *real emphasis*.\n").html
    assert r"\(a_1 * b_2 * c\)" in html
    assert "<em>real emphasis</em>" in html
    assert "<em>" not in html.split("and more")[0]


def test_display_math_stands_alone_and_is_escaped():
    html = render("Before.\n\n$$\nx_{i} * y_{j} < *z*\n$$\n\nAfter.\n").html
    assert (
        r'<div class="math-display">\[x_{i} * y_{j} &lt; *z*\]</div>' in html
    )
    assert "<em>" not in html
    assert "<p><div" not in html


def test_math_cannot_inject_markup():
    html = render("$<script>alert(1)</script>$\n").html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "text",
    [
        "A price of $5 and another of $10 today.\n",
        "It costs $1.20/$3 to run.\n",
        "`$not math$` stays code.\n",
        "```\n$not math$\n$$also not$$\n```\n",
    ],
)
def test_text_that_only_looks_like_math_is_left_alone(text):
    _, held = hold_math(text)
    assert held == {}
    assert not render(text).has_math


def test_a_document_with_no_math_says_so():
    assert not render("Plain text.\n").has_math


def test_the_methods_document_holds_math_and_leaves_no_token():
    document = render(TEXT)
    assert document.has_math
    assert "MATHHOLD" not in document.html
    assert document.html.count('class="math-display"') >= 10
    assert "<em>" not in "".join(
        re.findall(r"\\\[.*?\\\]", document.html, flags=re.S)
    )


def test_a_section_runs_to_its_next_peer_heading():
    section = section_text(TEXT, "velocity-and-rising")
    assert section.startswith("### Velocity and rising")
    assert "### State attention index" not in section
    assert "$o$" in section
    assert section_text(TEXT, "no-such-heading") == ""


def test_a_rendered_section_keeps_its_math():
    document = sections_html(TEXT, ["state-attention-index"])
    assert document.has_math
    assert r"\frac{n_s + 5}{E_s + 5}" in document.html


# Where KaTeX loads, and that it loads from here.


@pytest.fixture(scope="module")
def client(lake_template, tmp_path_factory):
    settings = clone_lake(lake_template, tmp_path_factory.mktemp("math"))
    with app_client(settings) as test_client:
        yield test_client


KATEX = "/static/vendor/katex/katex.min.js"


@pytest.mark.parametrize(
    "path",
    ["/methods", "/under/panels/map", "/under/panels/tone"],
)
def test_a_page_with_math_loads_katex_from_this_server(client, path):
    page = client.get(path).text
    assert KATEX in page
    assert "/static/vendor/katex/auto-render.min.js" in page
    assert "/static/js/math.js" in page
    assert "cdn" not in page.lower().split("</head>")[0]


@pytest.mark.parametrize(
    "path",
    ["/", "/under", "/under/lake", "/under/panels", "/under/panels/calls"],
)
def test_a_page_with_no_math_does_not_load_katex(client, path):
    assert KATEX not in client.get(path).text


def test_katex_is_vendored_with_its_fonts():
    vendor = ROOT / "src/monitor/web/static/vendor/katex"
    for name in ("katex.min.js", "katex.min.css", "auto-render.min.js"):
        assert (vendor / name).is_file()
    assert (vendor / "fonts" / "KaTeX_Main-Regular.woff2").is_file()


# Velocity and rising.


@pytest.mark.parametrize(
    ("observed", "trailing"), [(0, 0), (3, 0), (10, 40), (5, 80), (20, 120)]
)
def test_the_velocity_formula_is_what_the_function_computes(
    observed, trailing
):
    section = section_text(TEXT, "velocity-and-rising")
    short = number_after(r"e = t \\cdot \\frac\{(\d+)\}", section)
    long = number_after(r"e = t \\cdot \\frac\{\d+\}\{(\d+)\}", section)
    extra = number_after(r"\\sqrt\{e \+ (\d+)\}", section)
    expected = trailing * short / long
    from_formula = (observed - expected) / math.sqrt(expected + extra)
    assert analytics.velocity_score(observed, trailing) == pytest.approx(
        from_formula
    )
    assert (short, long) == (
        analytics.SHORT_WINDOW_MINUTES,
        analytics.TRAILING_WINDOW_MINUTES,
    )


@pytest.mark.parametrize(
    ("observed", "trailing"), [(0, 0), (3, 0), (10, 40), (5, 80), (20, 120)]
)
def test_the_score_in_the_rising_query_is_the_function(observed, trailing):
    sql = load_sql("rising")
    expression = re.search(
        r"(\(c\.observed - .*?\+ 1\)) AS score", sql, flags=re.S
    ).group(1)
    score = (
        duckdb.connect()
        .execute(
            f"SELECT {expression} FROM (SELECT {observed} AS observed, "
            f"{trailing} AS trailing) c"
        )
        .fetchone()[0]
    )
    assert analytics.velocity_score(observed, trailing) == pytest.approx(score)


def test_the_rising_rule_matches_the_prose():
    section = " ".join(section_text(TEXT, "velocity-and-rising").split())
    score = number_after(r"score is at least (\d+)", section)
    least = number_after(r"\$o\$ is at least (\d+)", section)
    assert score == analytics.RISING_SCORE
    assert least == analytics.RISING_MIN_OBSERVED
    assert not analytics.is_rising(least - 1, 0)
    assert analytics.is_rising(100, 0)


# The attention index.


@pytest.mark.parametrize(
    ("window", "total", "baseline", "baseline_total"),
    [(10, 100, 5, 100), (0, 50, 1, 50), (40, 200, 40, 1000), (3, 9, 0, 90)],
)
def test_the_attention_formula_is_what_the_function_computes(
    window, total, baseline, baseline_total
):
    section = section_text(TEXT, "state-attention-index")
    pseudo = number_after(r"A_s = \\frac\{n_s \+ (\d+)\}", section)
    assert f"{{E_s + {pseudo:.0f}}}" in section
    expected = baseline / baseline_total * total
    assert analytics.attention_index(
        window, total, baseline, baseline_total
    ) == pytest.approx((window + pseudo) / (expected + pseudo))


def test_the_attention_index_is_one_with_no_baseline():
    assert "with no baseline items at all the index is 1" in " ".join(
        section_text(TEXT, "state-attention-index").split()
    )
    assert analytics.attention_index(10, 100, 0, 0) == 1.0


# Tone.


def t_from_the_document(degrees):
    """The expansion, with coefficients read from the document's TeX."""
    section = section_text(TEXT, "tone-aggregates")
    z = number_after(r"quantile \$z = ([\d.]+)\$", section)
    terms = re.findall(r"\\frac\{([^{}]*z[^{}]*)\}\{(\d+)\\nu", section)
    assert len(terms) == 4
    total = z
    for power, (top, bottom) in enumerate(terms, start=1):
        value = 0.0
        for sign, coefficient, exponent in re.findall(
            r"([+-]?)\s*(\d*)z(?:\^(\d))?", top
        ):
            amount = float(coefficient or 1) * z ** int(exponent or 1)
            value += -amount if sign == "-" else amount
        total += value / (float(bottom) * degrees**power)
    return total


@pytest.mark.parametrize("degrees", [4, 5, 13, 29, 99, 500])
def test_the_t_expansion_is_what_the_function_computes(degrees):
    assert analytics.t_975(degrees) == pytest.approx(
        t_from_the_document(degrees)
    )


@pytest.mark.parametrize(
    "values",
    [
        [1.0, -1.0, 0.5, 0.25, -0.25],
        [2.0] * 6,
        [-2.0, -1.9, -2.0, -1.8, -2.0, -1.95, -2.0],
        list(np.linspace(-1.5, 1.5, 30)),
    ],
)
def test_the_tone_formula_is_what_the_function_computes(values):
    section = section_text(TEXT, "tone-aggregates")
    low = -number_after(r"\\sqrt\{n\}\},\\ -(\d+)\\right", section)
    high = number_after(r"\\sqrt\{n\}\},\\ (\d+)\\right", section)
    n = len(values)
    mean = sum(values) / n
    variance = max(sum(v * v for v in values) / n - mean**2, 0.0)
    variance *= n / (n - 1)
    margin = t_from_the_document(n - 1) * math.sqrt(variance / n)
    aggregate = analytics.tone_aggregate(
        sum(values), sum(v * v for v in values), n
    )
    assert aggregate.mean == pytest.approx(mean)
    assert aggregate.ci_low == pytest.approx(max(mean - margin, low))
    assert aggregate.ci_high == pytest.approx(min(mean + margin, high))


def test_the_tone_cutoffs_match_the_prose():
    section = " ".join(section_text(TEXT, "tone-aggregates").split())
    hidden = number_after(r"Hidden when \$n\$ is under (\d+)", section)
    low = number_after(r'"low n" when \$n\$ is under (\d+)', section)
    assert analytics.tone_aggregate(0, 0, int(hidden) - 1).hidden
    assert not analytics.tone_aggregate(0, 0, int(hidden)).hidden
    assert analytics.tone_aggregate(0, 0, int(low) - 1).low_n
    assert not analytics.tone_aggregate(0, 0, int(low)).low_n


# Coverage.


def test_the_share_formula_is_what_the_function_computes():
    assert r"\frac{\text{items}(s, b)}" in section_text(
        TEXT, "coverage-by-lean"
    )
    assert analytics.coverage_share(3, 12) == pytest.approx(3 / 12)


def test_a_bucket_with_no_stories_has_no_share_and_no_part_in_divergence():
    assert analytics.coverage_share(0, 0) is None
    shares = [0.5, None, 0.125]
    assert analytics.divergence(shares) == pytest.approx(0.5 - 0.125)
    assert analytics.divergence([None, None]) == 0.0


# What a formula leaves out.


def flat(section):
    return " ".join(section_text(TEXT, section).split())


def test_the_rising_section_states_the_conditions_beside_its_formula():
    section = flat("velocity-and-rising")
    limit = inspect.signature(Panels.rising).parameters["limit"].default
    assert "a storyline's status was active at the time shown" in section
    assert "its label is not empty" in section
    assert f"it keeps the top {limit} by score" in section


def test_the_attention_section_says_the_window_can_overlap_the_baseline():
    assert (
        "A window that starts before November 3 12:00 ET overlaps the "
        "baseline, so $b_s$ includes the items in the overlap"
        in flat("state-attention-index")
    )


def test_the_coverage_section_states_what_its_formula_counts():
    section = flat("coverage-by-lean")
    assert "pass the Focus, Level, and Language controls" in section
    assert "The Unrated bucket has no column in the matrix" in section


# Cost.


def test_the_cost_formula_is_what_the_function_computes():
    section = section_text(TEXT, "cost")
    read = number_after(r"([0-9.]+)\\,\\text\{cache read\}", section)
    write = number_after(r"([0-9.]+)\\,\\text\{cache write\}", section)
    settings = Settings()
    model, (input_price, output_price) = next(
        iter(settings.llm.prices_per_mtok.items())
    )
    wanted = (
        100 + read * 20 + write * 30
    ) * input_price / 1_000_000 + 40 * output_price / 1_000_000
    assert estimate_usd(
        settings,
        model,
        input_tokens=100,
        output_tokens=40,
        cache_read_tokens=20,
        cache_write_tokens=30,
    ) == pytest.approx(wanted)


# Dedupe.


def title_bits_from_the_document(title):
    """Formula (1): each token votes on each bit. A positive sum sets it."""
    tokens = dedupe.title_tokens(dedupe.normalize_title(title))
    value = 0
    for bit in range(64):
        votes = sum(
            2 * ((xxhash.xxh64(token.encode()).intdigest() >> bit) & 1) - 1
            for token in tokens
        )
        if votes > 0:
            value |= 1 << bit
    return value


def test_the_title_hash_formula_is_what_the_function_computes():
    title = "Georgia Senate race tightens as voters head to the polls"
    assert dedupe.title_hash_of(title) == title_bits_from_the_document(title)


def test_the_dedupe_thresholds_are_the_settings():
    section = section_text(TEXT, "dedupe")
    nlp = Settings().nlp
    assert nlp.dedupe_simhash_distance == number_after(
        r"d\(H_a, H_b\) \\le (\d+)", section
    )
    assert nlp.dedupe_cosine == number_after(
        r"\\lVert v \\rVert\} \\ge ([\d.]+)", section
    )
    assert dedupe.hamming(0b1011, 0b0001) == 2


def test_a_match_turns_on_at_the_stated_cosine():
    settings = Settings()
    index = dedupe.DedupeIndex(settings)
    threshold = number_after(
        r"\\lVert v \\rVert\} \\ge ([\d.]+)", section_text(TEXT, "dedupe")
    )
    from datetime import UTC, datetime

    now = datetime(2026, 11, 3, tzinfo=UTC)
    first = np.zeros(384, dtype=np.float32)
    first[0] = 1.0
    index.add(None, first, now, "2026-11-03", "group", "a")

    def at(cosine):
        vector = np.zeros(384, dtype=np.float32)
        vector[0] = cosine
        vector[1] = math.sqrt(1 - cosine**2)
        return index.find(None, vector, now, "2026-11-03", "b")

    assert at(threshold + 0.001) is not None
    assert at(threshold - 0.001) is None


# Storylines.


def unit(values):
    vector = np.zeros(384, dtype=np.float32)
    vector[: len(values)] = values
    return vector / np.linalg.norm(vector)


def test_the_join_formula_is_what_the_function_computes():
    section = section_text(TEXT, "storylines")
    alpha = number_after(r"c' = \\frac\{\(1 - ([\d.]+)\)", section)
    assert alpha == storylines.EMA_ALPHA
    centroid, vector = unit([1.0, 0.0]), unit([0.6, 0.8])
    blended = (1 - alpha) * centroid + alpha * vector
    assert storylines.ema_update(centroid, vector) == pytest.approx(
        blended / np.linalg.norm(blended), abs=1e-5
    )


def test_the_join_and_merge_thresholds_are_the_settings():
    section = section_text(TEXT, "storylines")
    nlp = Settings().nlp
    assert nlp.storyline_join == number_after(
        r"join when \} \\cos\(v, c\) \\ge ([\d.]+)", section
    )
    assert nlp.storyline_merge == number_after(
        r"merge when \} \\cos\(c_a, c_b\) \\ge ([\d.]+)", section
    )


def test_the_merge_formula_is_what_the_engine_computes():
    engine = storylines.StorylineEngine(Settings())
    from datetime import UTC, datetime

    now = datetime(2026, 11, 3, tzinfo=UTC)
    older = storylines.Storyline("a", unit([1.0, 0.0]), now, now, n_items=3)
    newer = storylines.Storyline("b", unit([1.0, 0.2]), now, now, n_items=1)
    blended = 3 * older.centroid + 1 * newer.centroid
    wanted = blended / np.linalg.norm(blended)
    engine._absorb(older, newer)
    assert older.centroid == pytest.approx(wanted, abs=1e-5)
