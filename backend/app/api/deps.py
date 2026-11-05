"""Write guards: ``X-Demo-Key`` (A.6) and the in-memory rate limiter."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

from fastapi import Request

from ..config import get_settings
from .errors import ApiError

WRITES_PER_MINUTE = 30
EXTRACTIONS_PER_HOUR = 10
_WRITE_LIMITS: dict[str, deque[float]] = defaultdict(deque)
_EXTRACTION_LIMITS: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_demo_key(request: Request) -> None:
    """Dependency for every write route except ``/webhooks/paypal``."""
    provided = request.headers.get("X-Demo-Key", "")
    expected = get_settings().demo_key.get_secret_value()
    if not hmac.compare_digest(provided, expected):
        raise ApiError(401, "DEMO_KEY_REQUIRED", "a valid X-Demo-Key header is required")


def limit_writes(request: Request) -> None:
    """30 writes per minute per IP (A.6)."""
    _bump(_WRITE_LIMITS, client_ip(request), WRITES_PER_MINUTE, 60.0)


def limit_extractions(request: Request) -> None:
    """10 contract extractions per hour per IP, so AI quota cannot be drained."""
    _bump(_EXTRACTION_LIMITS, client_ip(request), EXTRACTIONS_PER_HOUR, 3600.0)


def _bump(bucket: dict[str, deque[float]], ip: str, maximum: int, window: float) -> None:
    now = time.monotonic()
    hits = bucket[ip]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= maximum:
        raise ApiError(429, "TOO_MANY_REQUESTS", "rate limit exceeded; retry later")
    hits.append(now)
