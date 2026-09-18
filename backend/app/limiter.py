"""
Shared rate-limiter instance — imported by main.py (attaches to app.state)
and by routers that apply @limiter.limit() decorators.

Two layers (TW-078):

  1. slowapi key_func keyed on client IP ONLY. The previous implementation
     read the JWT "sub" claim WITHOUT verifying the signature, so an
     attacker could mint a fresh unsigned "sub" per request and get a fresh
     bucket every time — no limit ever triggered. Never derive a rate-limit
     bucket from attacker-controlled input.

  2. enforce_user_limit(): a FastAPI dependency that runs AFTER get_auth and
     enforces a per-user limit keyed on the VERIFIED auth.user_id. Apply it
     to the costly endpoints (LLM calls, etc.) so one authenticated user
     cannot burn shared resources even from a single IP.

Both layers are per-process in-memory (same as slowapi's default storage).
A multi-worker deployment should move to shared storage (e.g. Redis).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from .auth import AuthContext, get_auth


def _rate_limit_key(request: Request) -> str:
    # IP only — never trust unverified JWT claims for bucket keys (TW-078).
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)


_PERIOD_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


def _parse_limit(limit: str) -> tuple[int, int]:
    """Parse a slowapi-style limit string like "20/hour" into (calls, window_seconds)."""
    calls, _, period = limit.partition("/")
    period = period.strip().lower().rstrip("s")  # "hours" -> "hour"
    if period not in _PERIOD_SECONDS:
        raise ValueError(f"Unsupported rate-limit period in {limit!r}")
    return int(calls), _PERIOD_SECONDS[period]


class _SlidingWindowLimiter:
    """Per-process in-memory sliding-window limiter.

    Mirrors slowapi's default (per-process) storage semantics so behavior is
    consistent with the @limiter.limit decorators already in use.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, max_calls: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= max_calls:
                return False
            hits.append(now)
            if len(self._hits) > 20000:
                # Opportunistic prune of idle keys so the map cannot grow forever.
                for stale in [k for k, v in self._hits.items() if not v]:
                    del self._hits[stale]
            return True

    def reset(self) -> None:
        """Clear all buckets (tests)."""
        with self._lock:
            self._hits.clear()


_user_limiter = _SlidingWindowLimiter()


def enforce_user_limit(limit: str) -> Callable:
    """
    Dependency factory: enforce a per-VERIFIED-user rate limit.

    The returned dependency takes get_auth as its own dependency, so the
    user_id is always signature-verified — it cannot be spoofed the way the
    old unverified-claim key could (TW-078). Raises 429 when the user
    exceeds `limit` (slowapi-style, e.g. "20/hour").

    Usage:
        @limiter.limit("20/hour")
        def endpoint(
            request: Request,
            auth: Annotated[AuthContext, Depends(get_auth)],
            _ul: Annotated[None, Depends(enforce_user_limit("20/hour"))],
        ): ...
    """
    max_calls, window_seconds = _parse_limit(limit)

    async def _enforce(auth: Annotated[AuthContext, Depends(get_auth)]) -> None:
        if not _user_limiter.allow(auth.user_id, max_calls, window_seconds):
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded for your account. Please try again later.",
            )

    return _enforce
