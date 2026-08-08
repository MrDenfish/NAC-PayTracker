"""In-process per-IP rate limiting for the unauthenticated auth endpoints.

Fixed-window counters keyed by client IP. This is the in-app backstop
behind the Cloudflare WAF rules — it exists so signup/login/forgot abuse
is bounded even if the edge rules are ever misconfigured or bypassed.

In-memory is sufficient: the app runs as a single uvicorn process, and a
restart resetting the counters is harmless (the window just starts over).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# Keep the key table bounded: prune expired windows once it grows past
# this (protects memory during a large distributed sweep).
_PRUNE_THRESHOLD = 1024


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._hits) > _PRUNE_THRESHOLD:
                self._hits = {
                    k: (start, count)
                    for k, (start, count) in self._hits.items()
                    if now - start < self._window
                }
            start, count = self._hits.get(key, (now, 0))
            if now - start >= self._window:
                start, count = now, 0
            if count >= self._limit:
                self._hits[key] = (start, count)
                return False
            self._hits[key] = (start, count + 1)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# ── Shared limiters for the auth routes ──────────────────────────────
# Generous for humans, tight for bots: a real pilot signs up once and
# resets a password rarely; pre-WAF bots were doing ~35 signups/day.

SIGNUP_LIMITER = RateLimiter(limit=5, window_seconds=3600)
FORGOT_LIMITER = RateLimiter(limit=5, window_seconds=3600)
LOGIN_LIMITER = RateLimiter(limit=10, window_seconds=300)


def reset_rate_limits() -> None:
    """Test helper: clear all shared limiter state."""
    SIGNUP_LIMITER.reset()
    FORGOT_LIMITER.reset()
    LOGIN_LIMITER.reset()
