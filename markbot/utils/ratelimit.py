"""Rate limiting — Token Bucket and Sliding Window algorithms.

Provides per-key rate limiting that can be applied to API calls,
tool invocations, or any other rate-sensitive operations.

Two algorithms are available:
- **TokenBucketRateLimiter**: allows burst traffic up to bucket capacity,
  then refills tokens at a steady rate.  Good for API rate limits.
- **SlidingWindowRateLimiter**: tracks request counts in a sliding time
  window.  Good for "N requests per minute" style limits.

Usage::

    from markbot.utils.ratelimit import TokenBucketRateLimiter

    limiter = TokenBucketRateLimiter(rate=10, capacity=20)

    if limiter.allow("user:alice"):
        # process request
    else:
        # reject: rate limit exceeded
        retry_after = limiter.retry_after("user:alice")
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


class RateLimiter(ABC):
    """Abstract rate limiter interface."""

    @abstractmethod
    def allow(self, key: str) -> bool:
        """Check if a request for the given key is allowed."""
        ...

    @abstractmethod
    def retry_after(self, key: str) -> float:
        """Return seconds until the next request would be allowed."""
        ...

    @abstractmethod
    def reset(self, key: str) -> None:
        """Reset rate limit state for a key."""
        ...


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    capacity: float


class TokenBucketRateLimiter(RateLimiter):
    """Token Bucket rate limiter.

    - **rate**: tokens added per second (sustained throughput).
    - **capacity**: maximum burst size (bucket capacity).

    Each ``allow()`` call consumes one token.  When the bucket is
    empty, requests are rejected until tokens refill.

    Thread-safe: uses a per-key lock to avoid race conditions.
    """

    def __init__(self, rate: float = 10.0, capacity: float = 20.0) -> None:
        self._rate = rate
        self._capacity = capacity
        self._buckets: dict[str, _Bucket] = {}
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._global_lock = threading.Lock()

    def _get_bucket(self, key: str) -> _Bucket:
        with self._global_lock:
            if key not in self._buckets:
                self._buckets[key] = _Bucket(
                    tokens=self._capacity,
                    last_refill=time.monotonic(),
                    capacity=self._capacity,
                )
            return self._buckets[key]

    def _refill(self, bucket: _Bucket) -> None:
        now = time.monotonic()
        elapsed = now - bucket.last_refill
        refill = elapsed * self._rate
        bucket.tokens = min(bucket.capacity, bucket.tokens + refill)
        bucket.last_refill = now

    def allow(self, key: str) -> bool:
        lock = self._locks[key]
        with lock:
            bucket = self._get_bucket(key)
            self._refill(bucket)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def retry_after(self, key: str) -> float:
        lock = self._locks[key]
        with lock:
            bucket = self._get_bucket(key)
            self._refill(bucket)
            if bucket.tokens >= 1.0:
                return 0.0
            deficit = 1.0 - bucket.tokens
            return deficit / self._rate

    def reset(self, key: str) -> None:
        with self._global_lock:
            if key in self._buckets:
                self._buckets[key].tokens = self._capacity
                self._buckets[key].last_refill = time.monotonic()

    @property
    def stats(self) -> dict[str, dict[str, Any]]:
        """Return current token counts for all tracked keys."""
        result = {}
        for key, bucket in self._buckets.items():
            self._refill(bucket)
            result[key] = {
                "tokens": round(bucket.tokens, 2),
                "capacity": bucket.capacity,
                "rate": self._rate,
            }
        return result


@dataclass
class _Window:
    timestamps: list[float] = field(default_factory=list)


class SlidingWindowRateLimiter(RateLimiter):
    """Sliding Window rate limiter.

    - **max_requests**: maximum number of requests in the window.
    - **window_s**: window duration in seconds.

    Tracks request timestamps and removes expired entries on each check.
    More memory-intensive than token bucket but provides exact counting.

    Thread-safe.
    """

    def __init__(self, max_requests: int = 60, window_s: float = 60.0) -> None:
        self._max_requests = max_requests
        self._window_s = window_s
        self._windows: dict[str, _Window] = defaultdict(_Window)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    def _prune(self, window: _Window) -> None:
        cutoff = time.monotonic() - self._window_s
        window.timestamps = [ts for ts in window.timestamps if ts > cutoff]

    def allow(self, key: str) -> bool:
        lock = self._locks[key]
        with lock:
            window = self._windows[key]
            self._prune(window)
            if len(window.timestamps) < self._max_requests:
                window.timestamps.append(time.monotonic())
                return True
            return False

    def retry_after(self, key: str) -> float:
        lock = self._locks[key]
        with lock:
            window = self._windows[key]
            self._prune(window)
            if len(window.timestamps) < self._max_requests:
                return 0.0
            oldest = window.timestamps[0]
            elapsed = time.monotonic() - oldest
            return max(0.0, self._window_s - elapsed)

    def reset(self, key: str) -> None:
        lock = self._locks[key]
        with lock:
            if key in self._windows:
                self._windows[key].timestamps.clear()

    @property
    def stats(self) -> dict[str, dict[str, Any]]:
        """Return current request counts for all tracked keys."""
        result = {}
        for key, window in self._windows.items():
            self._prune(window)
            result[key] = {
                "requests_in_window": len(window.timestamps),
                "max_requests": self._max_requests,
                "window_s": self._window_s,
            }
        return result


class CompositeRateLimiter(RateLimiter):
    """Combine multiple rate limiters — a request must pass ALL of them."""

    def __init__(self, *limiters: RateLimiter) -> None:
        self._limiters = list(limiters)

    def allow(self, key: str) -> bool:
        return all(limiter.allow(key) for limiter in self._limiters)

    def retry_after(self, key: str) -> float:
        return max(limiter.retry_after(key) for limiter in self._limiters)

    def reset(self, key: str) -> None:
        for limiter in self._limiters:
            limiter.reset(key)


_global_limiter: RateLimiter | None = None


def get_rate_limiter(
    algorithm: str = "token_bucket",
    *,
    rate: float = 10.0,
    capacity: float = 20.0,
    max_requests: int = 60,
    window_s: float = 60.0,
) -> RateLimiter:
    """Get the global rate limiter (lazy-initialized)."""
    global _global_limiter
    if _global_limiter is not None:
        return _global_limiter

    if algorithm == "sliding_window":
        _global_limiter = SlidingWindowRateLimiter(max_requests=max_requests, window_s=window_s)
    else:
        _global_limiter = TokenBucketRateLimiter(rate=rate, capacity=capacity)

    return _global_limiter


def set_rate_limiter(limiter: RateLimiter) -> None:
    """Override the global rate limiter."""
    global _global_limiter
    _global_limiter = limiter
