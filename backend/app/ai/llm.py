"""Shared Gemini client, transient-error policy and failover loop.

Used by both the contract extractor and the compliance auditor. Only ``GEMINI_API_KEY``
is needed; models are tried in order, each with one quick retry on a transient error,
all inside the caller's time budget. Callers always have a deterministic fallback.

A model that reports its *daily* quota exhausted (free tier: 20 requests/day/model) is
skipped until the reset time Google returns, so later requests lose no time on it. The
last available model may use the whole remaining budget instead of the per-model cap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# gemini-2.5-flash (the plan's pick) is closed to new API keys; 3.5-flash with thinking off
# measured ~2 s per contract. Override with GEMINI_MODEL / GEMINI_FALLBACK_MODEL ("" disables).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite"
MIN_PROVIDER_SLOT_SECONDS = (
    0.5  # don't start a model call that cannot realistically finish
)
TRANSIENT_RETRY_DELAY_SECONDS = 0.4
DEFAULT_QUOTA_COOLDOWN_SECONDS = 3600.0  # when Google gives no retryDelay
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")

# Provider name -> time.monotonic() when its daily quota resets. Process-local on purpose:
# each backend worker learns it from its own first 429, which costs one fast request.
_quota_exhausted_until: dict[str, float] = {}

M = TypeVar("M", bound=BaseModel)
P = TypeVar("P")
T = TypeVar("T")


class EmptyModelResponse(ValueError):
    """The model returned no content."""


class GeminiModel:
    """One Gemini model returning JSON that validates against a Pydantic schema."""

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL) -> None:
        self._api_key = api_key
        self.model = model
        self.name = f"gemini:{model}"

    async def generate_json(
        self, system_prompt: str, user_prompt: str, schema: type[M]
    ) -> M:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        response = await client.aio.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                # No server deadline: Gemini rejects < 10 s; the caller's wait_for cancels us.
            ),
        )
        if not response.text:
            raise EmptyModelResponse(f"{self.name} returned an empty response")
        return schema.model_validate_json(response.text)


def configured_key(value: str | None) -> str | None:
    if not value or not value.strip() or value.strip().lower().startswith("your_"):
        return None
    return value.strip()


def gemini_models_from_env() -> list[str]:
    """Model names to try, in order, or ``[]`` when no real ``GEMINI_API_KEY`` is set."""
    if not configured_key(os.environ.get("GEMINI_API_KEY")):
        return []
    primary = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    backup = os.environ.get(
        "GEMINI_FALLBACK_MODEL", DEFAULT_GEMINI_FALLBACK_MODEL
    ).strip()
    return [primary] + ([backup] if backup and backup != primary else [])


def gemini_api_key() -> str:
    key = configured_key(os.environ.get("GEMINI_API_KEY"))
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return key


def _status_code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        status = getattr(exc, attr, None)
        if isinstance(status, int):
            return status
    return None


def daily_quota_reset_seconds(exc: BaseException) -> float | None:
    """Seconds until a model's daily quota resets, or ``None`` if ``exc`` is not that.

    Per-minute rate limits are deliberately excluded: those are worth a quick retry.
    """
    if _status_code(exc) != 429:
        return None
    message = str(exc)
    if "PerDay" not in message:
        return None
    match = _RETRY_DELAY.search(message)
    return float(match.group(1)) if match else DEFAULT_QUOTA_COOLDOWN_SECONDS


def mark_quota_exhausted(name: str, seconds: float) -> None:
    _quota_exhausted_until[name] = time.monotonic() + seconds


def is_quota_exhausted(name: str) -> bool:
    until = _quota_exhausted_until.get(name)
    if until is None:
        return False
    if time.monotonic() >= until:
        del _quota_exhausted_until[name]
        return False
    return True


def reset_quota_state() -> None:
    """Forget all exhausted-quota marks (tests, or after changing the API key)."""
    _quota_exhausted_until.clear()


def is_transient_error(exc: BaseException) -> bool:
    """True for provider errors worth one quick retry (rate limit, overload, 5xx).

    Timeouts are excluded (they already spent their slot), as are exhausted quotas
    (daily limit, no credits), which a retry cannot fix.
    """
    status = _status_code(exc)
    if status is None or status not in _TRANSIENT_STATUS:
        return False
    if daily_quota_reset_seconds(exc) is not None:
        return False
    message = str(exc).lower()
    return "insufficient_quota" not in message and "no credits" not in message


async def run_with_failover(
    providers: Sequence[P],
    attempt: Callable[[P, float], Awaitable[T]],
    *,
    purpose: str,
    budget_seconds: float,
    provider_timeout: float,
    min_slot: float = MIN_PROVIDER_SLOT_SECONDS,
    retry_delay: float = TRANSIENT_RETRY_DELAY_SECONDS,
) -> T | None:
    """Return the first successful ``attempt(provider, timeout)``, or ``None``.

    Any exception from an attempt (model error, timeout, validation failure) moves on to the
    next provider; transient errors get one retry first if the budget allows it. Models out
    of daily quota are skipped without a call. Each model is capped at ``provider_timeout``
    unless it is the last one still available, which may use the rest of the budget.
    """
    deadline = time.monotonic() + budget_seconds
    names = [str(getattr(p, "name", type(p).__name__)) for p in providers]
    for index, provider in enumerate(providers):
        name = names[index]
        if is_quota_exhausted(name):
            logger.info("%s: skipping %s (daily quota exhausted)", purpose, name)
            continue
        for try_number in (1, 2):
            remaining = deadline - time.monotonic()
            if remaining < min_slot:
                logger.warning("%s: budget exhausted before %s", purpose, name)
                break
            is_last = all(is_quota_exhausted(n) for n in names[index + 1 :])
            timeout = remaining if is_last else min(provider_timeout, remaining)
            try:
                return await asyncio.wait_for(attempt(provider, timeout), timeout)
            except Exception as exc:  # noqa: BLE001 - every failure must fall through to the next provider
                logger.warning(
                    "%s via %s (attempt %d) rejected: %s: %s",
                    purpose,
                    name,
                    try_number,
                    type(exc).__name__,
                    str(exc)[:200],
                )
                reset_in = daily_quota_reset_seconds(exc)
                if reset_in is not None:
                    mark_quota_exhausted(name, reset_in)
                    logger.warning(
                        "%s: %s is out of daily quota; skipping it for %.0f s",
                        purpose,
                        name,
                        reset_in,
                    )
                    break
                retry_fits = deadline - time.monotonic() >= retry_delay + min_slot
                if try_number == 1 and is_transient_error(exc) and retry_fits:
                    await asyncio.sleep(retry_delay)
                    continue
                break
    return None
