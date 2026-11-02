from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from monitor.lake import BatchWriter, connect
from monitor.llm.budget import (
    Budget,
    BudgetState,
    estimate_usd,
    load_budget,
)
from monitor.llm.client import (
    LlmBudgetStop,
    LlmClient,
    LlmInvalid,
    LlmRefused,
    LlmTruncated,
    LlmUnavailable,
)


class Answer(BaseModel):
    word: str


def usage(**kw):
    defaults = dict(
        input_tokens=100,
        output_tokens=10,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(**{**defaults, **kw})


def response(text='{"word": "ok"}', stop="end_turn", **kw):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=usage(**kw),
    )


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(create=self.beta_create)
        )

    async def create(self, **params):
        self.calls.append(params)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def beta_create(self, **params):
        return await self.create(**params)


def client_with(settings, *replies):
    return LlmClient(
        settings, None, Budget(settings), client=FakeClient(*replies)
    )


async def test_structured_call_sends_required_settings(settings):
    client = client_with(settings, response())
    result = await client.structured("classify_a", Answer, "sys", "user")
    assert result.word == "ok"
    params = client.client.calls[0]
    assert params["model"] == "claude-haiku-5-5"
    assert params["thinking"] == {"type": "disabled"}
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in params
    assert params["messages"][-1]["role"] == "user"


async def test_refusal_raises(settings):
    client = client_with(settings, response(stop="refusal"))
    with pytest.raises(LlmRefused):
        await client.structured("classify_a", Answer, "s", "u")


async def test_max_tokens_raises(settings):
    client = client_with(settings, response(stop="max_tokens"))
    with pytest.raises(LlmTruncated):
        await client.structured("classify_a", Answer, "s", "u")


async def test_invalid_response_retries_once_then_raises(settings):
    client = client_with(settings, response("not json"), response())
    assert (await client.structured("x", Answer, "s", "u")).word == "ok"
    client = client_with(settings, response("no"), response("still no"))
    with pytest.raises(LlmInvalid):
        await client.structured("x", Answer, "s", "u")


def status_error(code):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "failed",
        response=httpx2.Response(code, request=request),
        body=None,
    )


async def test_api_failure_raises_and_marks_failing(settings):
    client = client_with(settings, status_error(500))
    with pytest.raises(LlmUnavailable):
        await client.structured("x", Answer, "s", "u")
    assert client.failing_since is not None
    assert client.degraded is False


async def test_degraded_after_window_and_recovers_on_success(settings):
    settings.llm.degrade_after_seconds = 0
    client = client_with(settings, status_error(500), response())
    with pytest.raises(LlmUnavailable):
        await client.structured("x", Answer, "s", "u")
    assert client.degraded is True
    assert client.should_attempt() is True
    assert client.should_attempt() is False
    await client.structured("x", Answer, "s", "u")
    assert client.degraded is False


def sdk_client(settings, status, message):
    """The real SDK, answering with an API error from a local transport."""

    def handler(request):
        return httpx2.Response(
            status,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": message},
            },
        )

    sdk = anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(handler)
        ),
    )
    return LlmClient(settings, None, Budget(settings), client=sdk)


CREDIT_MESSAGE = "Your credit balance is too low to access the Anthropic API."


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, CREDIT_MESSAGE),
        (400, CREDIT_MESSAGE.upper()),
        (401, "invalid x-api-key"),
        (402, "payment required"),
        (403, "permission denied"),
    ],
)
async def test_an_account_refusal_is_an_outage(settings, status, message):
    client = sdk_client(settings, status, message)
    with pytest.raises(LlmUnavailable):
        await client.structured("classify_a", Answer, "s", "u")
    assert client.failing_since is not None


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "messages: text content blocks must be non-empty"),
        (413, "request too large"),
    ],
)
async def test_a_refused_request_is_permanent(settings, status, message):
    client = sdk_client(settings, status, message)
    with pytest.raises(LlmInvalid):
        await client.structured("classify_a", Answer, "s", "u")


class HandledClient:
    """Calls a handler for each request."""

    def __init__(self, handler):
        self.messages = SimpleNamespace(create=handler)


def handled_client(settings, handler):
    return LlmClient(
        settings, None, Budget(settings), client=HandledClient(handler)
    )


class Clock:
    """Stands in for `time` in the client, so a test sets the time."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    stand_in = Clock()
    monkeypatch.setattr("monitor.llm.client.time", stand_in)
    return stand_in


WINDOW = 120.0


async def test_one_hung_call_does_not_degrade_a_healthy_api(settings, clock):
    settings.llm.degrade_after_seconds = WINDOW

    async def hangs_while_others_answer(**params):
        clock.now += WINDOW
        client.last_answer = clock.now - 1
        raise TimeoutError

    client = handled_client(settings, hangs_while_others_answer)
    with pytest.raises(LlmUnavailable):
        await client.structured("classify_a", Answer, "s", "u")
    assert client.degraded is False
    assert client.should_attempt() is True
    assert client.should_attempt() is True


async def test_an_api_that_stops_answering_degrades(settings, clock):
    settings.llm.degrade_after_seconds = WINDOW
    replies = [response()]

    async def answers_then_hangs(**params):
        if replies:
            return replies.pop()
        clock.now += WINDOW
        raise TimeoutError

    client = handled_client(settings, answers_then_hangs)
    await client.structured("classify_a", Answer, "s", "u")
    assert client.degraded is False
    clock.now += 10
    with pytest.raises(LlmUnavailable):
        await client.structured("classify_a", Answer, "s", "u")
    assert client.degraded is True
    assert client.should_attempt() is True
    assert client.should_attempt() is False


async def test_silence_counts_from_the_last_answer(settings, clock):
    settings.llm.degrade_after_seconds = WINDOW

    async def outage_begins_during_the_call(**params):
        clock.now += WINDOW
        client.last_answer = clock.now - WINDOW / 2
        raise TimeoutError

    client = handled_client(settings, outage_begins_during_the_call)
    with pytest.raises(LlmUnavailable):
        await client.structured("classify_a", Answer, "s", "u")
    assert client.degraded is False
    clock.now += WINDOW / 2
    assert client.degraded is True


async def test_failed_connections_degrade_after_the_window(settings, clock):
    settings.llm.degrade_after_seconds = WINDOW
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = client_with(
        settings, anthropic.APIConnectionError(request=request)
    )
    with pytest.raises(LlmUnavailable):
        await client.structured("classify_a", Answer, "s", "u")
    assert client.degraded is False
    clock.now += WINDOW
    assert client.degraded is True


async def test_ledger_records_cost_and_budget(settings):
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    budget = Budget(settings)
    client = LlmClient(
        settings,
        writer,
        budget,
        client=FakeClient(response(cache_read_input_tokens=1000)),
    )
    await client.structured("classify_a", Answer, "s", "u")
    writer.flush()
    rows = conn.execute(
        "SELECT purpose, input_tokens, cache_read_tokens, status, usd_est "
        "FROM lake.nlp.llm_calls"
    ).fetchall()
    assert rows[0][:4] == ("classify_a", 100, 1000, "ok")
    assert rows[0][4] == pytest.approx(budget.spent_total)
    assert load_budget(conn, settings).spent_total == pytest.approx(
        budget.spent_total
    )
    conn.close()


def test_estimate_usd_uses_cache_multipliers(settings):
    usd = estimate_usd(
        settings,
        "claude-haiku-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert usd == pytest.approx(0.10 + 0.50 + 0.01 + 0.125)


def test_budget_transitions(settings):
    settings.llm.daily_usd_cap = 40
    settings.llm.hard_usd_cap = 75
    day = datetime(2026, 11, 3, 18, tzinfo=UTC)
    budget = Budget(settings)
    assert budget.state(day) is BudgetState.OK
    budget.record(31.9, day)
    assert budget.state(day) is BudgetState.OK
    budget.record(0.2, day)
    assert budget.state(day) is BudgetState.SOFT
    assert budget.allows("classify_a", day) is True
    budget.record(43.0, day)
    assert budget.state(day) is BudgetState.HARD
    assert budget.allows("classify_a", day) is False
    assert budget.allows("briefs", day) is True


def test_budget_soft_state_resets_at_midnight_eastern(settings):
    budget = Budget(settings)
    evening = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)  # 10 pm ET Nov 3
    morning = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)  # 1 am ET Nov 4
    budget.record(33.0, evening)
    assert budget.state(evening) is BudgetState.SOFT
    assert budget.state(morning) is BudgetState.OK
    assert budget.spent_total == 33.0


async def test_budget_stop_raises_before_calling_api(settings):
    settings.llm.hard_usd_cap = 0
    client = client_with(settings)
    with pytest.raises(LlmBudgetStop):
        await client.structured("classify_a", Answer, "s", "u")
    assert client.client.calls == []
