"""Shared Gemini client, transient-error policy and failover loop.

Used by both the contract extractor and the compliance auditor. Only ``GEMINI_API_KEY``
is needed; models are tried in order, each with one quick retry on a transient error,
all inside the caller's time budget. Callers always have a deterministic fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

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


def is_transient_error(exc: BaseException) -> bool:
    """True for provider errors worth one quick retry (rate limit, overload, 5xx).

    Timeouts are excluded (they already spent their slot), as are exhausted quotas,
    which a retry cannot fix.
    """
    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or status not in _TRANSIENT_STATUS:
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
    next provider; transient errors get one retry first if the budget allows it.
    """
    deadline = time.monotonic() + budget_seconds
    for provider in providers:
        name = getattr(provider, "name", type(provider).__name__)
        for try_number in (1, 2):
            remaining = deadline - time.monotonic()
            if remaining < min_slot:
                logger.warning("%s: budget exhausted before %s", purpose, name)
                break
            timeout = min(provider_timeout, remaining)
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
                retry_fits = deadline - time.monotonic() >= retry_delay + min_slot
                if try_number == 1 and is_transient_error(exc) and retry_fits:
                    await asyncio.sleep(retry_delay)
                    continue
                break
    return None
