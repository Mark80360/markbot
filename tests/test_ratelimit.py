"""Tests for markbot.utils.ratelimit — Token Bucket and Sliding Window."""

import time

from markbot.utils.ratelimit import (
    CompositeRateLimiter,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
)


class TestTokenBucket:
    def test_allows_within_capacity(self):
        limiter = TokenBucketRateLimiter(rate=10.0, capacity=5.0)
        for _ in range(5):
            assert limiter.allow("user1") is True

    def test_rejects_over_capacity(self):
        limiter = TokenBucketRateLimiter(rate=1.0, capacity=3.0)
        for _ in range(3):
            assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False

    def test_tokens_refill_over_time(self):
        limiter = TokenBucketRateLimiter(rate=100.0, capacity=1.0)
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False
        time.sleep(0.02)
        assert limiter.allow("user1") is True

    def test_per_key_isolation(self):
        limiter = TokenBucketRateLimiter(rate=1.0, capacity=1.0)
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False
        assert limiter.allow("user2") is True

    def test_retry_after(self):
        limiter = TokenBucketRateLimiter(rate=10.0, capacity=1.0)
        limiter.allow("user1")
        ra = limiter.retry_after("user1")
        assert ra > 0

    def test_retry_after_when_available(self):
        limiter = TokenBucketRateLimiter(rate=10.0, capacity=10.0)
        ra = limiter.retry_after("user1")
        assert ra == 0.0

    def test_reset(self):
        limiter = TokenBucketRateLimiter(rate=1.0, capacity=1.0)
        limiter.allow("user1")
        assert limiter.allow("user1") is False
        limiter.reset("user1")
        assert limiter.allow("user1") is True

    def test_stats(self):
        limiter = TokenBucketRateLimiter(rate=10.0, capacity=5.0)
        limiter.allow("user1")
        stats = limiter.stats
        assert "user1" in stats
        assert stats["user1"]["capacity"] == 5.0


class TestSlidingWindow:
    def test_allows_within_limit(self):
        limiter = SlidingWindowRateLimiter(max_requests=5, window_s=60.0)
        for _ in range(5):
            assert limiter.allow("user1") is True

    def test_rejects_over_limit(self):
        limiter = SlidingWindowRateLimiter(max_requests=3, window_s=60.0)
        for _ in range(3):
            assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False

    def test_per_key_isolation(self):
        limiter = SlidingWindowRateLimiter(max_requests=1, window_s=60.0)
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False
        assert limiter.allow("user2") is True

    def test_retry_after(self):
        limiter = SlidingWindowRateLimiter(max_requests=1, window_s=60.0)
        limiter.allow("user1")
        ra = limiter.retry_after("user1")
        assert ra > 0

    def test_reset(self):
        limiter = SlidingWindowRateLimiter(max_requests=1, window_s=60.0)
        limiter.allow("user1")
        assert limiter.allow("user1") is False
        limiter.reset("user1")
        assert limiter.allow("user1") is True

    def test_stats(self):
        limiter = SlidingWindowRateLimiter(max_requests=5, window_s=60.0)
        limiter.allow("user1")
        stats = limiter.stats
        assert "user1" in stats
        assert stats["user1"]["requests_in_window"] == 1


class TestCompositeRateLimiter:
    def test_all_must_pass(self):
        tb = TokenBucketRateLimiter(rate=1.0, capacity=1.0)
        sw = SlidingWindowRateLimiter(max_requests=1, window_s=60.0)
        composite = CompositeRateLimiter(tb, sw)

        assert composite.allow("user1") is True
        assert composite.allow("user1") is False

    def test_retry_after_returns_max(self):
        tb = TokenBucketRateLimiter(rate=1.0, capacity=1.0)
        sw = SlidingWindowRateLimiter(max_requests=1, window_s=60.0)
        composite = CompositeRateLimiter(tb, sw)

        composite.allow("user1")
        ra = composite.retry_after("user1")
        assert ra > 0
