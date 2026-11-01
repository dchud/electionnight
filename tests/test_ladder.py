"""The automatic degradation ladder, on a scratch lake."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from monitor.disk import DiskGuard, DiskLevel
from monitor.llm.budget import Budget
from monitor.llm.client import LlmClient
from monitor.parsers.base import Item
from monitor.parsers.classify_a import ClassifyParser
from monitor.pipeline import Pipeline


@pytest.fixture
def pipeline(settings):
    built = Pipeline(settings, no_llm=True)
    built.stages = built.build_stages()
    built.first_stage = built.stages[0]
    yield built
    built.conn.close()


def old_item(minutes=5):
    return Item(
        "a",
        first_seen_at=datetime.now(UTC) - timedelta(minutes=minutes),
    )


async def test_core_lag_sheds_background_work_and_recovers(pipeline):
    await pipeline.ladder_job()
    assert pipeline.shedding is False
    pipeline.stages[1].live.put_nowait(old_item())
    await pipeline.ladder_job()
    assert pipeline.shedding is True
    assert pipeline.embed.throttle == 0.5
    assert all(stage.pause_backfill for stage in pipeline.stages)
    pipeline.stages[1].live.get_nowait()
    await pipeline.ladder_job()
    assert pipeline.shedding is False
    assert pipeline.embed.throttle == 1.0
    assert not any(stage.pause_backfill for stage in pipeline.stages)


async def test_lag_within_hysteresis_keeps_shedding(pipeline):
    pipeline.stages[1].live.put_nowait(old_item(5))
    await pipeline.ladder_job()
    pipeline.stages[1].live.get_nowait()
    pipeline.stages[1].live.put_nowait(old_item(1.5))
    await pipeline.ladder_job()
    assert pipeline.shedding is True


async def test_health_job_records_a_row(pipeline):
    await pipeline.health_job()
    pipeline.writer.flush()
    rows = pipeline.conn.execute(
        "SELECT shedding, llm_state, free_gb > 0 FROM lake.nlp.health"
    ).fetchall()
    assert rows == [(False, "off", True)]


@pytest.mark.parametrize(
    ("free", "expected"),
    [
        (9.0, DiskLevel.WARN),
        (4.0, DiskLevel.DEGRADE),
        (2.0, DiskLevel.AUTOPRUNE),
    ],
)
async def test_disk_levels_reach_the_pipeline(pipeline, free, expected):
    pipeline.guard = DiskGuard(pipeline.settings, free_space=lambda p: free)
    await pipeline.disk_job()
    assert pipeline.guard.level() == expected


def api_error(status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "failed", response=httpx2.Response(status, request=request), body=None
    )


async def test_outage_moves_stage_a_to_keywords_and_recovers(settings):
    settings.llm.degrade_after_seconds = 0
    calls = []

    async def create(**params):
        calls.append(params)
        raise api_error(401)

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm = LlmClient(settings, None, Budget(settings), client=fake)
    parser = ClassifyParser(settings, llm, {})
    item = Item("a", title="Early voting opens in Ohio for midterm election")
    output = await parser.run([item])
    row = output.rows["nlp.classify_a"][0]
    assert row["method"] == "keywords"
    assert row["election_relevant"] is True
    assert llm.degraded is True
    assert len(calls) == 1
    # While degraded, only an occasional probe reaches the API.
    await parser.run([Item("b", title="Midterm election day in Ohio")])
    assert len(calls) == 2
    await parser.run([Item("c", title="Midterm election day in Ohio")])
    assert len(calls) == 2
