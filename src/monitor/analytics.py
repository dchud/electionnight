"""Metric definitions from the Methods page, as pure functions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

SHORT_WINDOW_MINUTES = 15
TRAILING_WINDOW_MINUTES = 120
RISING_SCORE = 2.0
RISING_MIN_OBSERVED = 3
NEW_WINDOW = timedelta(minutes=30)
ATTENTION_PSEUDO_COUNT = 5.0
TONE_PSEUDO_OBSERVATIONS = 5
TONE_HIDE_BELOW = 5
TONE_LOW_N_BELOW = 15
Z_95 = 1.96


def velocity_score(observed: float, trailing: float) -> float:
    """Observed in the last 15 min against the rate of the prior 120 min."""
    expected = trailing * SHORT_WINDOW_MINUTES / TRAILING_WINDOW_MINUTES
    return (observed - expected) / math.sqrt(expected + 1)


def is_rising(observed: float, trailing: float) -> bool:
    return (
        velocity_score(observed, trailing) >= RISING_SCORE
        and observed >= RISING_MIN_OBSERVED
    )


def is_new(first_seen_at: datetime, as_of: datetime) -> bool:
    return as_of - first_seen_at <= NEW_WINDOW


def attention_index(
    state_window: float,
    total_window: float,
    state_baseline: float,
    total_baseline: float,
) -> float:
    """A state's share of coverage in the window over its baseline share.

    Pseudo-counts shrink the ratio toward 1, so small counts stay near normal.
    """
    if total_baseline <= 0:
        return 1.0
    baseline_share = state_baseline / total_baseline
    expected = baseline_share * total_window
    return (state_window + ATTENTION_PSEUDO_COUNT) / (
        expected + ATTENTION_PSEUDO_COUNT
    )


@dataclass(frozen=True)
class ToneAggregate:
    mean: float | None
    ci_low: float | None
    ci_high: float | None
    n: int
    hidden: bool
    low_n: bool


def tone_aggregate(
    tone_sum: float, tone_sq_sum: float, n: int
) -> ToneAggregate:
    """Mean tone shrunk toward 0, with a 95% confidence interval.

    Hidden below 5 observations and flagged as low n below 15.
    """
    if n < TONE_HIDE_BELOW:
        return ToneAggregate(None, None, None, n, hidden=True, low_n=True)
    shrunk = tone_sum / (n + TONE_PSEUDO_OBSERVATIONS)
    raw_mean = tone_sum / n
    variance = max(tone_sq_sum / n - raw_mean**2, 0.0)
    if n > 1:
        variance *= n / (n - 1)
    margin = Z_95 * math.sqrt(variance / n)
    return ToneAggregate(
        mean=shrunk,
        ci_low=shrunk - margin,
        ci_high=shrunk + margin,
        n=n,
        hidden=False,
        low_n=n < TONE_LOW_N_BELOW,
    )


def coverage_share(
    storyline_in_bucket: float, relevant_in_bucket: float
) -> float:
    """Share of a lean bucket's election coverage devoted to a storyline."""
    if relevant_in_bucket <= 0:
        return 0.0
    return storyline_in_bucket / relevant_in_bucket


def divergence(shares: list[float]) -> float:
    """Spread across lean buckets, used to sort the coverage matrix."""
    return max(shares) - min(shares) if shares else 0.0
