"""Vega-Lite specs, built once with Altair. Data is streamed in by the browser.

Colors, fonts, and sizes come from the theme config set in the browser, so one
spec serves the light, dark, and projector themes.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Any

import altair as alt

PARTY_DOMAIN = ["Democratic", "Republican", "Other"]
LEAN_ORDER = ["L", "LL", "C", "LR", "R", "All"]
MATRIX_ORDER = ["L", "LL", "C", "LR", "R", "Non-US"]
LEVEL_ORDER = ["National", "State", "Local", "Unclassified"]
LEVEL_COLORS = ["#d95926", "#199e70", "#c98500", "#8f8f88"]
DISTRIBUTION_ORDER = ["L", "LL", "C", "LR", "R", "Non-US", "Unrated"]
TIME_AXIS = alt.Axis(
    format="%-I:%M %p", title=None, grid=False, labelFlush=False
)
RULE_LABEL_ROWS = 5
RULE_LABEL_ROW_PX = 20
# The drawn dots are small; a larger clear dot takes the hover and click.
BUMP_HIT_AREA = 220


def _named(name: str = "table") -> alt.Chart:
    return alt.Chart(alt.NamedData(name))


def _poll_rule_layers() -> list[alt.Chart]:
    """A dashed rule per moment, with labels taking turns across rows."""
    detail = alt.Tooltip("detail:N")
    rule = (
        _named("rules")
        .mark_rule(strokeDash=[3, 3], opacity=0.6)
        .encode(x=alt.X("time:T"), tooltip=detail)
    )
    labels = [
        _named("rules")
        .transform_window(turn="row_number()")
        .transform_filter(f"(datum.turn - 1) % {RULE_LABEL_ROWS} === {row}")
        .mark_text(align="left", dx=3, dy=2, baseline="top", angle=0)
        .encode(
            x=alt.X("time:T"),
            y=alt.value(row * RULE_LABEL_ROW_PX),
            text="label:N",
            tooltip=detail,
        )
        for row in range(RULE_LABEL_ROWS)
    ]
    return [rule, *labels]


def stream(stack: Any) -> dict[str, Any]:
    highlight = alt.selection_point(
        name="focus",
        fields=["storyline_id"],
        on="pointerover",
        clear="pointerout",
    )
    area = (
        _named()
        .mark_area(interpolate="monotone", stroke="transparent")
        .encode(
            x=alt.X("bin_start:T", axis=TIME_AXIS),
            y=alt.Y(
                "sum(n):Q",
                stack=stack,
                axis=None
                if stack == "center"
                else alt.Axis(format="%", title=None),
            ),
            color=alt.Color(
                "label:N",
                sort=alt.EncodingSortField("total", order="descending"),
                legend=alt.Legend(
                    title=None,
                    orient="bottom",
                    columns=3,
                    labelLimit=320,
                    symbolType="square",
                ),
            ),
            detail="storyline_id:N",
            order=alt.Order("total:Q", sort="descending"),
            opacity=alt.condition(highlight, alt.value(0.95), alt.value(0.35)),
            tooltip=[
                alt.Tooltip("label:N", title="Storyline"),
                alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
                alt.Tooltip("sum(n):Q", title="Stories"),
            ],
            href=alt.value(None),
        )
        .add_params(highlight)
    )
    chart = alt.layer(area, *_poll_rule_layers()).properties(
        width="container", height="container"
    )
    return chart.to_dict()


def topic_heat() -> dict[str, Any]:
    chart = (
        _named()
        .mark_rect(cornerRadius=2, stroke=None)
        .encode(
            x=alt.X("bin_start:T", axis=TIME_AXIS),
            x2="bin_end:T",
            y=alt.Y(
                "topic:N",
                title=None,
                sort=alt.EncodingSortField("total", order="descending"),
                axis=alt.Axis(labelLimit=170, labelOverlap=False),
            ),
            color=alt.Color(
                "n:Q",
                legend=alt.Legend(title="Stories", orient="bottom"),
            ),
            tooltip=[
                alt.Tooltip("topic:N", title="Topic"),
                alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
                alt.Tooltip("n:Q", title="Stories"),
            ],
        )
        .properties(width="container", height="container")
    )
    return chart.to_dict()


def bump() -> dict[str, Any]:
    color = alt.Color(
        "party:N",
        scale=alt.Scale(
            domain=PARTY_DOMAIN,
            range=[
                {"expr": "partyDem"},
                {"expr": "partyRep"},
                {"expr": "partyOther"},
            ],
        ),
        legend=alt.Legend(title=None, orient="bottom"),
    )
    base = _named().encode(
        x=alt.X("bin_start:T", axis=TIME_AXIS),
        y=alt.Y(
            "rank:Q",
            scale=alt.Scale(reverse=True, domain=[1, 12]),
            axis=alt.Axis(title="Rank", tickMinStep=1),
        ),
        color=color,
        detail="entity_id:N",
        tooltip=[
            alt.Tooltip("entity:N", title="Entity"),
            alt.Tooltip("rank:Q", title="Rank"),
            alt.Tooltip("n:Q", title="Mentions"),
            alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
        ],
    )
    lines = base.mark_line(interpolate="linear", opacity=0.85)
    points = base.mark_circle(opacity=1)
    hits = base.encode(color=alt.value("transparent")).mark_circle(
        size=BUMP_HIT_AREA
    )
    labels = (
        _named()
        .transform_window(
            newest="row_number()",
            groupby=["entity_id"],
            sort=[alt.SortField("bin_start", order="descending")],
        )
        .transform_filter("datum.newest === 1")
        # Lines can end at one rank in different bins; the newer one is named.
        .transform_window(
            newest_at_rank="row_number()",
            groupby=["rank"],
            sort=[alt.SortField("bin_start", order="descending")],
        )
        .transform_filter("datum.newest_at_rank === 1")
        .mark_text(align="left", dx=8)
        .encode(
            x=alt.X("bin_start:T"),
            y=alt.Y("rank:Q", scale=alt.Scale(reverse=True, domain=[1, 12])),
            text="entity:N",
        )
    )
    chart = alt.layer(lines, points, hits, labels).properties(
        width="container",
        height="container",
        padding={"left": 5, "top": 5, "bottom": 5, "right": 140},
    )
    return chart.to_dict()


def tone_by_lean() -> dict[str, Any]:
    """Lean is told apart by shape and position, never by color."""
    tone_scale = alt.Scale(domain=[-2, 2])
    ink = alt.Scale(
        domain=LEAN_ORDER,
        range=[{"expr": "ink"}] * 5 + [{"expr": "toneAccent"}],
    )
    base = _named().encode(
        y=alt.Y("entity:N", title=None),
        yOffset=alt.YOffset("bucket:N", sort=LEAN_ORDER),
    )
    bars = base.mark_rule(strokeWidth=2, opacity=0.55).encode(
        x=alt.X("ci_low:Q", scale=tone_scale, title="Mean tone"),
        x2="ci_high:Q",
        color=alt.Color("bucket:N", scale=ink, legend=None),
    )
    dots = base.mark_point(size=80, strokeWidth=2, opacity=1).encode(
        x=alt.X("mean:Q", scale=tone_scale),
        color=alt.Color("bucket:N", scale=ink, legend=None),
        shape=alt.Shape(
            "bucket:N",
            scale=alt.Scale(
                domain=LEAN_ORDER,
                range=[
                    "circle",
                    "square",
                    "diamond",
                    "triangle-up",
                    "triangle-down",
                    "cross",
                ],
            ),
            legend=alt.Legend(title=None, orient="bottom", columns=6),
        ),
        fill=alt.condition(
            "datum.low_n",
            alt.value("transparent"),
            alt.Color("bucket:N", scale=ink, legend=None),
        ),
        tooltip=[
            alt.Tooltip("entity:N", title="Entity"),
            alt.Tooltip("bucket:N", title="Bucket"),
            alt.Tooltip("mean:Q", title="Mean tone", format=".2f"),
            alt.Tooltip("ci_low:Q", title="95% low", format=".2f"),
            alt.Tooltip("ci_high:Q", title="95% high", format=".2f"),
            alt.Tooltip("n:Q", title="n"),
        ],
    )
    zero = (
        alt.Chart(alt.Data(values=[{"x": 0}]))
        .mark_rule(opacity=0.5)
        .encode(x="x:Q")
    )
    chart = alt.layer(zero, bars, dots).properties(
        width="container", height="container"
    )
    return chart.to_dict()


def coverage_matrix() -> dict[str, Any]:
    chart = (
        _named()
        .mark_rect(cornerRadius=2)
        .encode(
            x=alt.X(
                "bucket:N",
                sort=MATRIX_ORDER,
                title=None,
                axis=alt.Axis(orient="top", labelAngle=0),
            ),
            y=alt.Y(
                "label:N",
                title=None,
                sort=alt.EncodingSortField("divergence", order="descending"),
                axis=alt.Axis(labelLimit=360),
            ),
            color=alt.Color(
                "share:Q",
                legend=alt.Legend(
                    title="Share of bucket", orient="bottom", format=".0%"
                ),
            ),
            tooltip=[
                alt.Tooltip("label:N", title="Storyline"),
                alt.Tooltip("bucket:N", title="Bucket"),
                alt.Tooltip("share:Q", title="Share", format=".1%"),
            ],
        )
        .properties(width="container", height="container")
    )
    return chart.to_dict()


def global_press() -> dict[str, Any]:
    chart = (
        _named()
        .mark_area(interpolate="monotone", opacity=0.85)
        .encode(
            x=alt.X("bin_start:T", axis=TIME_AXIS),
            y=alt.Y(
                "share:Q", axis=alt.Axis(format="%", title=None, tickCount=2)
            ),
            row=alt.Row("language:N", title=None),
            tooltip=[
                alt.Tooltip("language:N", title="Language"),
                alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
                alt.Tooltip("share:Q", title="Share", format=".1%"),
                alt.Tooltip("n:Q", title="Stories"),
            ],
        )
        .properties(width=170, height=44)
    )
    return chart.to_dict()


def _full_domain() -> alt.Chart:
    """An invisible layer holding every bin, so empty bins keep the axis."""
    return (
        _named()
        .mark_point(opacity=0)
        .encode(x=alt.X("bin_start:T", axis=TIME_AXIS))
    )


def story_volume() -> dict[str, Any]:
    """Stacked rectangles. A bar with `x2` would be read as horizontal."""
    bars = (
        _named()
        .transform_filter("datum.n > 0")
        .transform_stack(
            stack="n",
            as_=["y0", "y1"],
            groupby=["bin_start"],
            sort=[alt.SortField("order", order="ascending")],
        )
        .mark_rect()
        .encode(
            x=alt.X("bin_start:T", axis=TIME_AXIS),
            x2="bin_end:T",
            y=alt.Y(
                "y0:Q",
                axis=alt.Axis(title="Stories", tickMinStep=1),
            ),
            y2="y1:Q",
            color=alt.Color(
                "level_name:N",
                scale=alt.Scale(domain=LEVEL_ORDER, range=LEVEL_COLORS),
                legend=alt.Legend(title=None, orient="bottom"),
            ),
            tooltip=[
                alt.Tooltip("level_name:N", title="Level"),
                alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
                alt.Tooltip("n:Q", title="Stories"),
            ],
        )
    )
    chart = alt.layer(_full_domain(), bars, *_poll_rule_layers()).properties(
        width="container", height="container"
    )
    return chart.to_dict()


def entity_mentions() -> dict[str, Any]:
    bars = (
        _named()
        .transform_filter("datum.n > 0")
        .mark_rect(fill=alt.expr("toneAccent"))
        .encode(
            x=alt.X("bin_start:T", axis=TIME_AXIS),
            x2="bin_end:T",
            y=alt.Y("n:Q", axis=alt.Axis(title="Mentions", tickMinStep=1)),
            y2=alt.datum(0),
            tooltip=[
                alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
                alt.Tooltip("n:Q", title="Mentions"),
            ],
        )
    )
    chart = alt.layer(_full_domain(), bars, *_poll_rule_layers()).properties(
        width="container", height="container"
    )
    return chart.to_dict()


def entity_tone() -> dict[str, Any]:
    tone_scale = alt.Scale(domain=[-2, 2])
    tone_axis = alt.Axis(title="Mean tone", tickCount=5)
    tooltip = [
        alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
        alt.Tooltip("mean:Q", title="Mean tone", format=".2f"),
        alt.Tooltip("ci_low:Q", title="95% low", format=".2f"),
        alt.Tooltip("ci_high:Q", title="95% high", format=".2f"),
        alt.Tooltip("n:Q", title="n"),
    ]
    time = alt.X("bin_start:T", axis=TIME_AXIS)
    band = (
        _named()
        .mark_area(color=alt.expr("toneAccent"), opacity=0.2)
        .encode(
            x=time,
            y=alt.Y("ci_low:Q", scale=tone_scale, axis=tone_axis),
            y2="ci_high:Q",
        )
    )
    line = (
        _named()
        .mark_line(color=alt.expr("toneAccent"))
        .encode(x=time, y=alt.Y("mean:Q", scale=tone_scale))
    )
    solid = (
        _named()
        .transform_filter("!datum.low_n")
        .mark_point(color=alt.expr("toneAccent"), filled=True, size=70)
        .encode(x=time, y=alt.Y("mean:Q", scale=tone_scale), tooltip=tooltip)
    )
    hollow = (
        _named()
        .transform_filter("datum.low_n")
        .mark_point(color=alt.expr("toneAccent"), filled=False, size=70)
        .encode(x=time, y=alt.Y("mean:Q", scale=tone_scale), tooltip=tooltip)
    )
    zero = (
        alt.Chart(alt.Data(values=[{"y": 0}]))
        .mark_rule(opacity=0.5)
        .encode(y="y:Q")
    )
    span = (
        _named("span")
        .mark_point(opacity=0)
        .encode(x=alt.X("bin_start:T", axis=TIME_AXIS))
    )
    chart = alt.layer(
        span, zero, band, line, solid, hollow, *_poll_rule_layers()
    ).properties(width="container", height="container")
    return chart.to_dict()


def entity_tone_lean() -> dict[str, Any]:
    tone_scale = alt.Scale(domain=[-2, 2])
    bucket = alt.Y("bucket:N", sort=LEAN_ORDER, title=None)
    tooltip = [
        alt.Tooltip("bucket:N", title="Bucket"),
        alt.Tooltip("mean:Q", title="Mean tone", format=".2f"),
        alt.Tooltip("ci_low:Q", title="95% low", format=".2f"),
        alt.Tooltip("ci_high:Q", title="95% high", format=".2f"),
        alt.Tooltip("n:Q", title="n"),
    ]
    interval = (
        _named()
        .mark_rule(strokeWidth=2)
        .encode(
            y=bucket,
            x=alt.X(
                "ci_low:Q",
                scale=tone_scale,
                axis=alt.Axis(title="Mean tone", tickCount=5),
            ),
            x2="ci_high:Q",
        )
    )
    solid = (
        _named()
        .transform_filter("!datum.low_n")
        .mark_point(filled=True, size=80, color=alt.expr("textSoft"))
        .encode(y=bucket, x=alt.X("mean:Q", scale=tone_scale), tooltip=tooltip)
    )
    hollow = (
        _named()
        .transform_filter("datum.low_n")
        .mark_point(filled=False, size=80, color=alt.expr("textSoft"))
        .encode(y=bucket, x=alt.X("mean:Q", scale=tone_scale), tooltip=tooltip)
    )
    zero = (
        alt.Chart(alt.Data(values=[{"x": 0}]))
        .mark_rule(opacity=0.5)
        .encode(x="x:Q")
    )
    chart = alt.layer(zero, interval, solid, hollow).properties(
        width="container", height="container"
    )
    return chart.to_dict()


def state_attention() -> dict[str, Any]:
    time = alt.X("bin_start:T", axis=TIME_AXIS)
    line = (
        _named()
        .mark_line(color=alt.expr("toneAccent"))
        .encode(
            x=time,
            y=alt.Y(
                "attention:Q",
                scale=alt.Scale(zero=False),
                axis=alt.Axis(title="Attention index"),
            ),
        )
    )
    points = (
        _named()
        .mark_point(color=alt.expr("toneAccent"), filled=True, size=50)
        .encode(
            x=time,
            y=alt.Y("attention:Q"),
            tooltip=[
                alt.Tooltip("bin_start:T", title="Bin", format="%-I:%M %p"),
                alt.Tooltip("attention:Q", title="Attention", format=".2f"),
                alt.Tooltip("n:Q", title="State stories"),
                alt.Tooltip("total:Q", title="All stories"),
            ],
        )
    )
    normal = (
        alt.Chart(alt.Data(values=[{"y": 1}]))
        .mark_rule(opacity=0.5, strokeDash=[2, 2])
        .encode(y="y:Q")
    )
    chart = alt.layer(normal, line, points, *_poll_rule_layers()).properties(
        width="container", height="container"
    )
    return chart.to_dict()


def lean_distribution() -> dict[str, Any]:
    chart = (
        _named()
        .mark_bar()
        .encode(
            x=alt.X(
                "bucket:N",
                sort=DISTRIBUTION_ORDER,
                title=None,
                axis=alt.Axis(labelAngle=0),
            ),
            y=alt.Y("n:Q", axis=alt.Axis(title="Outlets", tickMinStep=1)),
            tooltip=[
                alt.Tooltip("bucket:N", title="Lean"),
                alt.Tooltip("n:Q", title="Outlets"),
            ],
        )
        .properties(width="container", height="container")
    )
    return chart.to_dict()


SPECS: dict[str, Callable[[], dict[str, Any]]] = {
    "stream": lambda: stream("center"),
    "stream:normalize": lambda: stream("normalize"),
    "topic_heat": topic_heat,
    "bump": bump,
    "tone_by_lean": tone_by_lean,
    "coverage_matrix": coverage_matrix,
    "global_press": global_press,
    "story_volume": story_volume,
    "entity_mentions": entity_mentions,
    "entity_tone": entity_tone,
    "entity_tone_lean": entity_tone_lean,
    "state_attention": state_attention,
    "lean_distribution": lean_distribution,
}


@cache
def cached_spec(name: str) -> dict[str, Any]:
    return SPECS[name]()
