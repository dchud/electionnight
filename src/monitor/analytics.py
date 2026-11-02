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
TONE_HIDE_BELOW = 5
TONE_LOW_N_BELOW = 15
TONE_MIN = -2.0
TONE_MAX = 2.0
Z_975 = 1.959964


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


def t_975(df: int) -> float:
    """The 97.5th percentile of Student's t, within 0.001 for df of 4 or more.

    This is the Cornish-Fisher expansion around the normal quantile.
    """
    z = Z_975
    return (
        z
        + (z**3 + z) / (4 * df)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3)
        + (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z)
        / (92160 * df**4)
    )


def tone_aggregate(
    tone_sum: float, tone_sq_sum: float, n: int
) -> ToneAggregate:
    """The mean tone with its 95% t-interval, cut off at the ends of the scale.

    Hidden below 5 observations and flagged as low n below 15.
    """
    if n < TONE_HIDE_BELOW:
        return ToneAggregate(None, None, None, n, hidden=True, low_n=True)
    mean = tone_sum / n
    variance = max(tone_sq_sum / n - mean**2, 0.0) * n / (n - 1)
    margin = t_975(n - 1) * math.sqrt(variance / n)
    return ToneAggregate(
        mean=mean,
        ci_low=max(mean - margin, TONE_MIN),
        ci_high=min(mean + margin, TONE_MAX),
        n=n,
        hidden=False,
        low_n=n < TONE_LOW_N_BELOW,
    )


def coverage_share(
    storyline_in_bucket: float, relevant_in_bucket: float
) -> float | None:
    """Share of a lean bucket's election coverage devoted to a storyline.

    None when the bucket has no coverage at all, which is no data, not 0%.
    """
    if relevant_in_bucket <= 0:
        return None
    return storyline_in_bucket / relevant_in_bucket


def divergence(shares: list[float | None]) -> float:
    """Spread across lean buckets with coverage, to sort the matrix."""
    known = [share for share in shares if share is not None]
    return max(known) - min(known) if known else 0.0
