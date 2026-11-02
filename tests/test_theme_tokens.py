"""The theme values that keep dense panels light and readable."""

from __future__ import annotations

import json
import re

import pytest
from test_sql_format import CSS, ROOT, THEMES, block, contrast

from monitor.web import under_map
from monitor.web.charts import specs

DIAGRAMS = sorted((ROOT / "docs/diagrams").glob("*.svg"))
HEAT = [f"--heat-{step}" for step in range(1, 6)]
PUOR = {"#b35806", "#e08214", "#8073ac", "#b2abd2", "#542788"}


@pytest.mark.parametrize("theme", THEMES)
def test_each_heat_step_stands_further_from_the_panel(theme):
    tokens = THEMES[theme]
    steps = [contrast(tokens[name], tokens["--surface-1"]) for name in HEAT]
    assert steps == sorted(steps)
    assert len(set(steps)) == len(steps)


@pytest.mark.parametrize("theme", THEMES)
def test_the_lowest_heat_color_differs_from_a_blank_cell(theme):
    tokens = THEMES[theme]
    assert contrast(tokens["--heat-1"], tokens["--surface-1"]) >= 1.3


@pytest.mark.parametrize("theme", THEMES)
def test_a_required_line_can_be_seen(theme):
    tokens = THEMES[theme]
    assert contrast(tokens["--line-strong"], tokens["--surface-1"]) >= 3


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("surface", ["--surface-1", "--surface-2"])
@pytest.mark.parametrize("text", ["--text-1", "--text-2", "--text-3"])
def test_text_meets_contrast(theme, surface, text):
    tokens = THEMES[theme]
    assert contrast(tokens[text], tokens[surface]) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
def test_a_selected_control_can_be_seen(theme):
    tokens = THEMES[theme]
    assert contrast(tokens["--selected-text"], tokens["--selected-bg"]) >= 4.5
    assert contrast(tokens["--accent"], tokens["--selected-bg"]) >= 3


@pytest.mark.parametrize("theme", THEMES)
def test_tone_stays_on_the_purple_orange_scale(theme):
    tokens = THEMES[theme]
    assert {tokens["--tone-neg"], tokens["--tone-pos"]} <= PUOR


def test_the_dark_projector_restates_what_the_projector_changes():
    """The light projector block follows the dark block and would win."""
    projector = set(block(':root[data-projector="1"]'))
    dark = set(block(':root[data-theme="dark"]'))
    restated = set(block(':root[data-projector="1"][data-theme="dark"]'))
    assert projector & dark <= restated


def test_every_weight_above_regular_comes_from_the_theme():
    outside_font_faces = re.sub(r"@font-face\s*{[^}]*}", "", CSS)
    fixed = re.findall(r"font-weight:\s*(\d+)", outside_font_faces)
    assert set(fixed) <= {"400"}


@pytest.mark.parametrize(
    "styles",
    [
        under_map.STYLE,
        *[path.read_text(encoding="utf-8") for path in DIAGRAMS],
    ],
)
def test_diagram_text_takes_its_weight_from_the_theme(styles):
    assert set(re.findall(r"font:\s*(\d+)", styles)) <= {"400"}


def test_method_table_headers_use_a_theme_weight():
    rule = CSS[CSS.index(".doc th {") :]
    assert "font-weight: var(--weight-label)" in rule[: rule.index("}")]


@pytest.mark.parametrize("theme", THEMES)
def test_the_other_area_can_be_seen(theme):
    tokens = THEMES[theme]
    assert contrast(tokens["--series-other"], tokens["--surface-1"]) >= 1.4


@pytest.mark.parametrize("build", [specs.topic_heat, specs.coverage_matrix])
def test_heat_charts_leave_their_scale_to_the_theme(build):
    assert '"scheme"' not in json.dumps(build())
