"""Shared failover loop: daily-quota skipping and budget for the last model."""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.app.ai.llm import (
    DEFAULT_QUOTA_COOLDOWN_SECONDS,
    daily_quota_reset_seconds,
    is_quota_exhausted,
    is_transient_error,
    run_with_failover,
)

# Shape of the real Gemini error (abridged from a live 429 on the free tier).
GOOGLE_DAILY_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current "
    "quota ... limit: 20, model: gemini-3.6-flash\\nPlease retry in 8h10m21.0s.', 'details': "
    "[{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': "
    "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaValue': '20'}]}, "
    "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '29421s'}]}}"
)
GOOGLE_MINUTE_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': [{'violations': [{'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}, {'retryDelay': '20s'}]}}"
)


class APIError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class Provider:
    def __init__(
        self,
        name: str,
        *,
        exc: BaseException | None = None,
        delay: float = 0.0,
        result: str = "ok",
    ) -> None:
        self.name = name
        self._exc = exc
        self._delay = delay
        self._result = result
        self.calls = 0
        self.timeouts: list[float] = []

    async def __call__(self, timeout: float) -> str:
        self.calls += 1
        self.timeouts.append(timeout)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self._result


async def run(providers: list[Provider], **kwargs: float) -> str | None:
    async def attempt(p: Provider, timeout: float) -> str:
        return await p(timeout)

    options = {"budget_seconds": 2.0, "provider_timeout": 1.0, "retry_delay": 0.01}
    options.update(kwargs)
    return await run_with_failover(providers, attempt, purpose="test", **options)


# --- classification -------------------------------------------------------------------


def test_daily_quota_error_is_recognised_with_googles_retry_delay() -> None:
    assert daily_quota_reset_seconds(APIError(429, GOOGLE_DAILY_429)) == 29421.0


def test_daily_quota_without_retry_info_uses_default_cooldown() -> None:
    exc = APIError(429, "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert daily_quota_reset_seconds(exc) == DEFAULT_QUOTA_COOLDOWN_SECONDS


@pytest.mark.parametrize(
    "exc",
    [
        APIError(429, GOOGLE_MINUTE_429),
        APIError(503, "high demand"),
        APIError(400, GOOGLE_DAILY_429),
        RuntimeError(GOOGLE_DAILY_429),
    ],
)
def test_other_errors_are_not_daily_quota(exc: BaseException) -> None:
    assert daily_quota_reset_seconds(exc) is None


def test_daily_quota_is_not_retried_but_per_minute_limit_is() -> None:
    assert is_transient_error(APIError(429, GOOGLE_DAILY_429)) is False
    assert is_transient_error(APIError(429, GOOGLE_MINUTE_429)) is True


# --- quota-aware skipping -------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_model_is_tried_once_then_skipped_on_later_requests() -> None:
    out_of_quota = Provider("gemini:primary", exc=APIError(429, GOOGLE_DAILY_429))
    backup = Provider("gemini:backup")

    assert await run([out_of_quota, backup]) == "ok"
    assert (out_of_quota.calls, backup.calls) == (1, 1)  # no retry on daily quota
    assert is_quota_exhausted("gemini:primary")

    assert await run([out_of_quota, backup]) == "ok"
    assert (out_of_quota.calls, backup.calls) == (1, 2)  # skipped entirely


@pytest.mark.asyncio
async def test_skipping_costs_no_time() -> None:
    out_of_quota = Provider("gemini:primary", exc=APIError(429, GOOGLE_DAILY_429))
    await run([out_of_quota, Provider("gemini:backup")])
    start = time.perf_counter()
    await run([out_of_quota, Provider("gemini:backup")])
    assert time.perf_counter() - start < 0.05


@pytest.mark.asyncio
async def test_model_is_tried_again_after_the_reset_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_of_quota = Provider("gemini:primary", exc=APIError(429, GOOGLE_DAILY_429))
    await run([out_of_quota])
    assert out_of_quota.calls == 1

    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 29421 + 1)
    assert not is_quota_exhausted("gemini:primary")
    await run([out_of_quota])
    assert out_of_quota.calls == 2


@pytest.mark.asyncio
async def test_all_models_exhausted_returns_none_immediately() -> None:
    a = Provider("gemini:a", exc=APIError(429, GOOGLE_DAILY_429))
    b = Provider("gemini:b", exc=APIError(429, GOOGLE_DAILY_429))
    assert await run([a, b]) is None
    start = time.perf_counter()
    assert await run([a, b]) is None
    assert time.perf_counter() - start < 0.05
    assert (a.calls, b.calls) == (1, 1)


@pytest.mark.asyncio
async def test_exhaustion_is_tracked_per_model() -> None:
    await run([Provider("gemini:a", exc=APIError(429, GOOGLE_DAILY_429))])
    assert is_quota_exhausted("gemini:a")
    assert not is_quota_exhausted("gemini:b")


# --- budget for the last model --------------------------------------------------------


@pytest.mark.asyncio
async def test_last_model_gets_the_whole_remaining_budget() -> None:
    first = Provider("first", exc=APIError(400, "bad request"))
    slow_last = Provider("last", delay=0.3)
    result = await run([first, slow_last], budget_seconds=1.0, provider_timeout=0.1)
    assert result == "ok"
    assert slow_last.timeouts[0] > 0.5


@pytest.mark.asyncio
async def test_non_last_model_is_still_capped() -> None:
    slow_first = Provider("first", delay=0.3)
    backup = Provider("last")
    result = await run([slow_first, backup], budget_seconds=1.0, provider_timeout=0.1)
    assert result == "ok"
    assert slow_first.timeouts[0] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_model_before_an_exhausted_one_counts_as_last() -> None:
    await run([Provider("gemini:b", exc=APIError(429, GOOGLE_DAILY_429))])
    slow = Provider("gemini:a", delay=0.3)
    result = await run(
        [slow, Provider("gemini:b")], budget_seconds=1.0, provider_timeout=0.1
    )
    assert result == "ok"
    assert slow.timeouts[0] > 0.5


@pytest.mark.asyncio
async def test_last_model_never_exceeds_the_total_budget() -> None:
    hang = Provider("only", delay=5.0)
    start = time.perf_counter()
    assert await run([hang], budget_seconds=0.2, provider_timeout=0.1) is None
    assert time.perf_counter() - start < 0.4
