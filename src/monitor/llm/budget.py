"""LLM cost estimates and the spending guard.

`LLM__DAILY_USD_CAP` is a per-day threshold; at 80% of it, low-signal sources
use the keyword path. `LLM__HARD_USD_CAP` applies to total spend across all
days; at it, only briefs still call the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from monitor.settings import Settings

ET = ZoneInfo("America/New_York")
SOFT_FRACTION = 0.8
BRIEF_PURPOSE = "briefs"


class BudgetState(StrEnum):
    OK = "ok"
    SOFT = "soft"
    HARD = "hard"


def estimate_usd(
    settings: Settings,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    llm = settings.llm
    prices = llm.prices_per_mtok.get(model)
    if prices is None:
        prices = max(llm.prices_per_mtok.values(), key=lambda p: p[0])
    input_price, output_price = prices
    return (
        input_tokens * input_price
        + cache_read_tokens * input_price * llm.cache_read_multiplier
        + cache_write_tokens * input_price * llm.cache_write_multiplier
        + output_tokens * output_price
    ) / 1_000_000


@dataclass
class Budget:
    settings: Settings
    spent_today: float = 0.0
    spent_total: float = 0.0
    day: str = ""

    def _roll(self, now: datetime | None = None) -> None:
        today = (now or datetime.now(ET)).astimezone(ET).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.spent_today = 0.0

    def record(self, usd: float, now: datetime | None = None) -> None:
        self._roll(now)
        self.spent_today += usd
        self.spent_total += usd

    def state(self, now: datetime | None = None) -> BudgetState:
        self._roll(now)
        llm = self.settings.llm
        if self.spent_total >= llm.hard_usd_cap:
            return BudgetState.HARD
        if self.spent_today >= llm.daily_usd_cap * SOFT_FRACTION:
            return BudgetState.SOFT
        return BudgetState.OK

    def allows(self, purpose: str, now: datetime | None = None) -> bool:
        return (
            self.state(now) is not BudgetState.HARD or purpose == BRIEF_PURPOSE
        )

    def status(self) -> dict[str, float | str]:
        return {
            "state": self.state().value,
            "spent_today": round(self.spent_today, 4),
            "spent_total": round(self.spent_total, 4),
            "daily_cap": self.settings.llm.daily_usd_cap,
            "hard_cap": self.settings.llm.hard_usd_cap,
        }


def load_budget(conn, settings: Settings) -> Budget:
    """Rebuild spend from the ledger, so a restart keeps the totals."""
    budget = Budget(settings)
    budget._roll()
    rows = conn.execute(
        """
        SELECT coalesce(sum(usd_est), 0),
               coalesce(sum(usd_est) FILTER (
                   WHERE (created_at AT TIME ZONE 'America/New_York')::DATE
                       = (now() AT TIME ZONE 'America/New_York')::DATE), 0)
        FROM lake.nlp.llm_calls
        """
    ).fetchall()
    budget.spent_total = float(rows[0][0])
    budget.spent_today = float(rows[0][1])
    return budget


def cost_report(settings: Settings) -> str:
    """Text report for `monitor cost`."""
    from monitor.lake import connect

    conn = connect(settings, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT (created_at AT TIME ZONE 'America/New_York')::DATE AS day,
                   purpose, model, count(*) AS calls,
                   sum(input_tokens), sum(cache_read_tokens),
                   sum(output_tokens), sum(usd_est),
                   count(*) FILTER (WHERE status <> 'ok')
            FROM lake.nlp.llm_calls
            GROUP BY ALL ORDER BY 1, 2, 3
            """
        ).fetchall()
    finally:
        conn.close()
    lines = [
        f"{'day':<12}{'purpose':<14}{'model':<20}{'calls':>7}"
        f"{'input':>11}{'cached':>11}{'output':>10}{'usd':>10}{'failed':>8}"
    ]
    total = 0.0
    for day, purpose, model, calls, tin, tcache, tout, usd, failed in rows:
        total += usd or 0
        lines.append(
            f"{day!s:<12}{purpose:<14}{model:<20}{calls:>7}{tin or 0:>11}"
            f"{tcache or 0:>11}{tout or 0:>10}{usd or 0:>10.4f}{failed:>8}"
        )
    lines.append(f"total ${total:.4f}")
    return "\n".join(lines)
