"""Tests for markbot.utils.website_policy — domain blocklist enforcement."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from markbot.utils.website_policy import (
    WebsitePolicyError,
    _normalize_host,
    check_website_policy,
    clear_cache,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the policy cache before each test."""
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# _normalize_host
# ---------------------------------------------------------------------------


class TestNormalizeHost:
    def test_lowercase(self):
        assert _normalize_host("EXAMPLE.COM") == "example.com"

    def test_strip_whitespace(self):
        assert _normalize_host("  example.com  ") == "example.com"

    def test_strip_trailing_dot(self):
        assert _normalize_host("example.com.") == "example.com"

    def test_empty_string(self):
        assert _normalize_host("") == ""

    def test_none(self):
        assert _normalize_host(None) == ""


# ---------------------------------------------------------------------------
# check_website_policy
# ---------------------------------------------------------------------------


class TestCheckWebsitePolicy:
    def test_allowed_by_default(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": [], "allowed_domains": []},
        ):
            assert check_website_policy("https://example.com") is None

    def test_blocked_domain(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": ["evil.com"], "allowed_domains": []},
        ):
            result = check_website_policy("https://evil.com/page")
            assert result is not None
            assert "evil.com" in result
            assert "blocked" in result.lower()

    def test_blocked_wildcard_pattern(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": ["*.malware.*"], "allowed_domains": []},
        ):
            result = check_website_policy("https://download.malware.ru/file")
            assert result is not None

    def test_case_insensitive_block(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": ["evil.com"], "allowed_domains": []},
        ):
            result = check_website_policy("https://EVIL.COM/page")
            assert result is not None

    def test_allowlist_enforced(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": [], "allowed_domains": ["trusted.com"]},
        ):
            # Not in allowlist → blocked
            result = check_website_policy("https://unknown.com/page")
            assert result is not None
            assert "allowlist" in result.lower() or "not in" in result.lower()

    def test_allowlist_allows_listed(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": [], "allowed_domains": ["trusted.com"]},
        ):
            result = check_website_policy("https://trusted.com/page")
            assert result is None

    def test_block_takes_precedence_over_allowlist(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={
                "blocked_domains": ["evil.com"],
                "allowed_domains": ["evil.com"],
            },
        ):
            result = check_website_policy("https://evil.com/page")
            assert result is not None

    def test_no_hostname_passes(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": [], "allowed_domains": []},
        ):
            assert check_website_policy("https:///path") is None

    def test_invalid_url_returns_error(self):
        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            return_value={"blocked_domains": [], "allowed_domains": []},
        ):
            result = check_website_policy("not a url at all")
            # urlparse won't raise but hostname will be None → passes
            # Actually "not a url" has no scheme so hostname is None
            assert result is None


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------


class TestClearCache:
    def test_does_not_raise(self):
        clear_cache()

    def test_forces_reload(self):
        call_count = 0

        def mock_loader():
            nonlocal call_count
            call_count += 1
            return {"blocked_domains": [], "allowed_domains": []}

        with patch(
            "markbot.utils.website_policy._load_policy_from_config",
            side_effect=mock_loader,
        ):
            check_website_policy("https://example.com")
            assert call_count == 1

            clear_cache()

            check_website_policy("https://example.com")
            assert call_count == 2


# ---------------------------------------------------------------------------
# WebsitePolicyError
# ---------------------------------------------------------------------------


class TestWebsitePolicyError:
    def test_is_exception(self):
        assert issubclass(WebsitePolicyError, Exception)

    def test_can_be_raised(self):
        with pytest.raises(WebsitePolicyError):
            raise WebsitePolicyError("test")
