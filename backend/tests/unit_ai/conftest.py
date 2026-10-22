"""Shared pytest fixtures for the AI engine tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from backend.app.ai.llm import reset_quota_state


@pytest.fixture(autouse=True)
def _fresh_quota_state() -> Iterator[None]:
    """Models marked out-of-quota in one test must not be skipped in the next."""
    reset_quota_state()
    yield
    reset_quota_state()
