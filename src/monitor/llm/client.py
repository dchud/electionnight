"""The Claude client: structured calls, ledger, budget, and degradation."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import anthropic
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from monitor.lake import BatchWriter, hash_id, utcnow
from monitor.llm.budget import Budget, estimate_usd
from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

T = TypeVar("T", bound=BaseModel)

# A cache entry lives five minutes from its last use. Requests sent before the
# first response starts cannot read it, so a cold prefix goes out alone first.
# The others wait for it only this long, so a hung API cannot hold them.
CACHE_COLD_SECONDS = 270.0
COLD_WAIT_SECONDS = 15.0
PROBE_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 120.0
# The API refused this request, so sending it again cannot help.
INVALID_STATUSES = {400, 413}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LlmError(Exception):
    """A call that produced no usable result."""


class LlmRefused(LlmError):
    """The model declined, with `stop_reason: refusal`."""


class LlmTruncated(LlmError):
    """The response hit `max_tokens`."""


class LlmInvalid(LlmError):
    """The response failed validation twice, or the API refused the request."""


class LlmUnavailable(LlmError):
    """The API could not be reached or rejected the request."""


class LlmBudgetStop(LlmError):
    """The budget guard stopped this purpose."""


@dataclass
class _CacheGate:
    leader: asyncio.Event | None = None
    last_success: float = 0.0


def build_message_params(
    model: str,
    max_tokens: int,
    effort: str,
    thinking: dict[str, Any],
    schema: dict[str, Any] | None,
    system: str,
    user: str,
) -> dict[str, Any]:
    output_config: dict[str, Any] = {"effort": effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    return {
        "model": model,
        "max_tokens": max_tokens,
        "thinking": thinking,
        "output_config": output_config,
        "system": [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user}],
    }


def first_text(response: Any) -> str:
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


class LlmClient:
    """All Claude calls go through here.

    Stage A and other bulk calls use the bulk model at low effort with
    thinking disabled. Briefs use the brief model with adaptive thinking.
    Sampling parameters and prefill are never sent; current models reject them.
    """

    def __init__(
        self,
        settings: Settings,
        writer: BatchWriter | None,
        budget: Budget,
        client: Any = None,
        network_up: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.network_up = network_up or (lambda: True)
        self.writer = writer
        self.budget = budget
        key = settings.anthropic_api_key
        self.client = client or anthropic.AsyncAnthropic(
            api_key=key.get_secret_value() if key else None,
            max_retries=3,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        llm = settings.llm
        self.semaphores = {
            "classify_a": asyncio.Semaphore(llm.concurrency_a),
            "extract_b": asyncio.Semaphore(llm.concurrency_b),
        }
        self.misc = asyncio.Semaphore(llm.concurrency_misc)
        self.gates: dict[str, _CacheGate] = {}
        self.failing_since: float | None = None
        self.pending: dict[str, float] = {}
        self.last_answer = 0.0
        self.last_probe = 0.0
        self.last_error: str | None = None
        self.seen_prompts: set[str] = set()
        self.counter = itertools.count()

    def _silent_since(self) -> float | None:
        """Since when a request has waited with no answer to any request."""
        if not self.pending:
            return None
        return max(min(self.pending.values()), self.last_answer)

    @property
    def degraded(self) -> bool:
        """True once failures or silence last past the configured window.

        With no network at all, items wait instead of falling to keywords.
        """
        if not self.network_up():
            return False
        moments = (self.failing_since, self._silent_since())
        since = min((m for m in moments if m is not None), default=None)
        if since is None:
            return False
        elapsed = time.monotonic() - since
        return elapsed >= self.settings.llm.degrade_after_seconds

    def should_attempt(self) -> bool:
        """While degraded, only an occasional probe goes to the API."""
        if not self.degraded:
            return True
        now = time.monotonic()
        if now - self.last_probe >= PROBE_SECONDS:
            self.last_probe = now
            return True
        return False

    def status(self) -> dict[str, Any]:
        return {
            "degraded": self.degraded,
            "last_error": self.last_error,
            "budget": self.budget.status(),
        }

    def _note_success(self) -> None:
        if self.failing_since is not None:
            log.info("llm_recovered")
        self.failing_since = None
        self.last_error = None

    def clear_failures(self) -> None:
        self.failing_since = None
        self.last_error = None

    def _note_failure(self, error: str, since: float | None = None) -> None:
        self.last_error = error
        if self.failing_since is None and self.network_up():
            self.failing_since = since or time.monotonic()

    def _ledger(
        self,
        call_id: str,
        purpose: str,
        model: str,
        usage: Any,
        latency_ms: int,
        status: str,
    ) -> None:
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        usd = estimate_usd(
            self.settings,
            model,
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
        )
        self.budget.record(usd)
        if self.writer is not None:
            self.writer.add(
                "nlp.llm_calls",
                [
                    {
                        "call_id": call_id,
                        "purpose": purpose,
                        "model": model,
                        "input_tokens": input_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_write_tokens": cache_write,
                        "output_tokens": output_tokens,
                        "usd_est": usd,
                        "latency_ms": latency_ms,
                        "status": status,
                    }
                ],
            )

    def _trace(
        self,
        call_id: str,
        purpose: str,
        params: dict[str, Any],
        response: Any,
        status: str,
        batch_id: str | None,
        item_ids: list[str] | None,
    ) -> None:
        """Keep the exact request and response, for later inspection."""
        if self.writer is None:
            return
        system = params["system"][0]["text"]
        prompt_hash = hash_id(system)
        if prompt_hash not in self.seen_prompts:
            self.seen_prompts.add(prompt_hash)
            self.writer.add(
                "nlp.llm_prompts",
                [
                    {
                        "prompt_hash": prompt_hash,
                        "purpose": purpose,
                        "text": system,
                    }
                ],
            )
        self.writer.add(
            "nlp.llm_traces",
            [
                {
                    "call_id": call_id,
                    "batch_id": batch_id,
                    "purpose": purpose,
                    "prompt_hash": prompt_hash,
                    "model_requested": params["model"],
                    "model_served": getattr(response, "model", None),
                    "item_ids": item_ids or [],
                    "request_text": params["messages"][-1]["content"],
                    "response_text": (
                        first_text(response) if response is not None else None
                    ),
                    "stop_reason": status,
                }
            ],
        )

    async def _send(
        self, params: dict[str, Any], beta: bool, timeout: float
    ) -> Any:
        """One call, with its SDK retries, ended at a deadline.

        The deadline is never shorter than one attempt's timeout, so a slow
        answer still arrives. Retries of quick failures happen inside it.
        """
        options: dict[str, Any] = {}
        if timeout != REQUEST_TIMEOUT_SECONDS:
            options["timeout"] = timeout
        deadline = max(timeout, self.settings.llm.degrade_after_seconds)
        async with asyncio.timeout(deadline):
            if beta:
                return await self.client.beta.messages.create(
                    **params,
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    **options,
                )
            return await self.client.messages.create(**params, **options)

    async def _send_gated(
        self,
        gate_key: str,
        params: dict[str, Any],
        beta: bool,
        timeout: float,
    ) -> Any:
        gate = self.gates.setdefault(gate_key, _CacheGate())
        if time.monotonic() - gate.last_success > CACHE_COLD_SECONDS:
            if gate.leader is None:
                leader = gate.leader = asyncio.Event()
                try:
                    response = await self._send(params, beta, timeout)
                    gate.last_success = time.monotonic()
                    return response
                finally:
                    leader.set()
                    gate.leader = None
            leading = gate.leader
            try:
                async with asyncio.timeout(COLD_WAIT_SECONDS):
                    await leading.wait()
            except TimeoutError:
                pass
        response = await self._send(params, beta, timeout)
        gate.last_success = time.monotonic()
        return response

    async def _request(
        self,
        purpose: str,
        params: dict[str, Any],
        beta: bool,
        batch_id: str | None,
        item_ids: list[str] | None,
        timeout: float,
    ) -> Any:
        if not self.budget.allows(purpose):
            raise LlmBudgetStop(purpose)
        semaphore = self.semaphores.get(purpose, self.misc)
        started = time.monotonic()
        model = params["model"]
        call_id = hash_id(purpose, utcnow().isoformat(), next(self.counter))

        def failed(status: str, since: float | None = None) -> None:
            self._note_failure(status, since)
            self._ledger(call_id, purpose, model, None, 0, "error")
            self._trace(
                call_id, purpose, params, None, status, batch_id, item_ids
            )

        async with semaphore:
            # A call allowed extra time says nothing about a hung API.
            watched = timeout <= REQUEST_TIMEOUT_SECONDS
            sent = time.monotonic() if watched else None
            if sent is not None:
                self.pending[call_id] = sent
            try:
                response = await self._send_gated(
                    purpose, params, beta, timeout
                )
            except TimeoutError as error:
                failed("deadline", sent)
                raise LlmUnavailable("deadline") from error
            except anthropic.APIStatusError as error:
                status = f"HTTP {error.status_code}"
                self.last_answer = time.monotonic()
                failed(status)
                if error.status_code in INVALID_STATUSES:
                    raise LlmInvalid(status) from error
                raise LlmUnavailable(status) from error
            except anthropic.APIConnectionError as error:
                status = type(error).__name__
                failed(status, sent)
                raise LlmUnavailable(status) from error
            finally:
                self.pending.pop(call_id, None)
        self.last_answer = time.monotonic()
        latency = int((time.monotonic() - started) * 1000)
        self._note_success()
        stop = response.stop_reason
        status = "ok" if stop not in {"refusal", "max_tokens"} else stop
        self._ledger(call_id, purpose, model, response.usage, latency, status)
        self._trace(
            call_id, purpose, params, response, stop, batch_id, item_ids
        )
        if stop == "refusal":
            raise LlmRefused(purpose)
        if stop == "max_tokens":
            raise LlmTruncated(purpose)
        return response

    async def structured(
        self,
        purpose: str,
        schema: type[T],
        system: str,
        user: str,
        max_tokens: int = 4096,
        model: str | None = None,
        effort: str = "low",
        thinking: dict[str, Any] | None = None,
        beta_fallback: bool = False,
        batch_id: str | None = None,
        item_ids: list[str] | None = None,
        request_timeout: float | None = None,
    ) -> T:
        """Call the model for a validated object, retrying once if invalid."""
        params = build_message_params(
            model or self.settings.llm.model_bulk,
            max_tokens,
            effort,
            thinking or {"type": "disabled"},
            transform_schema(schema),
            system,
            user,
        )
        last_error: Exception | None = None
        for _attempt in range(2):
            response = await self._request(
                purpose,
                params,
                beta_fallback,
                batch_id,
                item_ids,
                request_timeout or REQUEST_TIMEOUT_SECONDS,
            )
            try:
                return schema.model_validate_json(first_text(response))
            except ValidationError as error:
                last_error = error
                log.warning("llm_invalid", purpose=purpose)
        raise LlmInvalid(str(last_error)[:300])
