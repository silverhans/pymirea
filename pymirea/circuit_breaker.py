from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class BreakerDecision:
    allowed: bool
    retry_after_s: int | None = None


class CircuitBreaker:
    """
    Minimal async circuit breaker for flaky upstreams.

    Notes:
    - We use this only for *upstream availability* (timeouts/5xx), not for user/session errors.
    - When OPEN, requests fail fast with a suggested retry-after.
    - After the cooldown, the breaker becomes HALF_OPEN and lets a small number of probe calls through.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 6,
        open_cooldown_s: float = 25.0,
        half_open_max_calls: int = 2,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.open_cooldown_s = float(open_cooldown_s)
        self.half_open_max_calls = max(1, int(half_open_max_calls))

        self._lock = asyncio.Lock()
        self._state: str = "closed"  # closed | open | half_open
        self._consecutive_failures = 0
        self._opened_until: float | None = None
        self._half_open_in_flight = 0

        # Timestamps (monotonic seconds)
        self._last_failure_at: float | None = None
        self._last_success_at: float | None = None

        # Counters (lifetime within process)
        self._failures_total = 0
        self._success_total = 0

    @property
    def state(self) -> str:
        return self._state

    def _now(self) -> float:
        return time.monotonic()

    async def allow(self) -> BreakerDecision:
        """
        Decide whether a request should be attempted right now.
        """
        async with self._lock:
            now = self._now()

            if self._state == "open":
                until = float(self._opened_until or 0.0)
                if now < until:
                    retry_after = int(max(1.0, until - now))
                    return BreakerDecision(False, retry_after_s=retry_after)
                # Cooldown elapsed -> probe mode.
                self._state = "half_open"
                self._half_open_in_flight = 0

            if self._state == "half_open":
                if self._half_open_in_flight >= self.half_open_max_calls:
                    # Too many probes already; ask callers to retry shortly.
                    return BreakerDecision(False, retry_after_s=1)
                self._half_open_in_flight += 1
                return BreakerDecision(True)

            # closed
            return BreakerDecision(True)

    async def record_success(self) -> None:
        async with self._lock:
            self._success_total += 1
            self._last_success_at = self._now()
            self._consecutive_failures = 0
            if self._state == "half_open" and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1
            if self._state in {"open", "half_open"}:
                self._state = "closed"
                self._opened_until = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures_total += 1
            self._last_failure_at = self._now()

            if self._state == "half_open" and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = "open"
                self._opened_until = self._now() + self.open_cooldown_s

    async def snapshot(self) -> dict:
        async with self._lock:
            now = self._now()
            retry_after_s = None
            if self._state == "open" and self._opened_until:
                retry_after_s = int(max(0.0, float(self._opened_until) - now))
            return {
                "name": self.name,
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "failures_total": self._failures_total,
                "success_total": self._success_total,
                "retry_after_s": retry_after_s,
                "last_failure_s_ago": None if self._last_failure_at is None else round(now - self._last_failure_at, 3),
                "last_success_s_ago": None if self._last_success_at is None else round(now - self._last_success_at, 3),
            }

