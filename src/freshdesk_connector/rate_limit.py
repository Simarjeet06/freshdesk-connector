"""Client-side rate limiting.

Freshdesk enforces a per-account, per-minute request budget shared by *every* integration
on the account (the merchant's other apps, scripts and staff all draw from it). The connector
should never be the thing that exhausts it, so we:

1. Pace requests with a token bucket sized to FRESHDESK_RATE_LIMIT_PER_MIN.
2. Watch X-RateLimit-Remaining on every response and slow down when the shared budget is low,
   even if our own bucket still has tokens.
3. Honour Retry-After on 429 responses (handled in client.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import anyio

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


@dataclass
class RateLimitState:
    """The latest values Freshdesk reported. Exposed through the connector_status tool."""

    total: int | None = None
    remaining: int | None = None
    last_retry_after: float | None = None
    throttled_requests: int = 0

    def update_from_headers(self, headers) -> None:
        def _int(name: str) -> int | None:
            v = headers.get(name)
            try:
                return int(v) if v is not None else None
            except ValueError:
                return None

        total = _int("x-ratelimit-total")
        remaining = _int("x-ratelimit-remaining")
        if total is not None:
            self.total = total
        if remaining is not None:
            self.remaining = remaining


@dataclass
class TokenBucket:
    rate_per_min: int
    sleep: Sleep = anyio.sleep
    clock: Clock = time.monotonic
    # When the account-wide remaining budget drops under this fraction, slow down further.
    low_watermark: float = 0.1
    _tokens: float = field(init=False)
    _updated: float = field(init=False)
    _lock: anyio.Lock = field(init=False, default_factory=anyio.Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.rate_per_min)
        self._updated = self.clock()

    @property
    def _per_second(self) -> float:
        return self.rate_per_min / 60.0

    def _refill(self) -> None:
        now = self.clock()
        self._tokens = min(self.rate_per_min, self._tokens + (now - self._updated) * self._per_second)
        self._updated = now

    async def acquire(self, state: RateLimitState | None = None) -> float:
        """Wait until a request may be sent. Returns the seconds spent waiting."""
        waited = 0.0
        async with self._lock:
            self._refill()
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._per_second
                await self.sleep(wait)
                waited += wait
                self._refill()
            self._tokens = max(0.0, self._tokens - 1)

            # Adaptive back-off: if the account is close to its limit (because of other
            # integrations), spread our remaining requests out instead of bursting.
            if state and state.total and state.remaining is not None:
                if state.remaining <= max(1, int(state.total * self.low_watermark)):
                    pause = 60.0 / max(state.remaining, 1) if state.remaining else 5.0
                    pause = min(pause, 5.0)
                    await self.sleep(pause)
                    waited += pause
        return waited
