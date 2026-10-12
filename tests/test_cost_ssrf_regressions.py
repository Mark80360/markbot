"""Regression tests for two defects found reviewing the cost and SSRF fixes.

1. ``cache_miss_tokens`` of 0 read as "zero cache misses", so OpenAI/Azure/
   DeepSeek prompts were billed at *zero* input cost.
2. ``guarded_client(proxy=...)`` registered the guard and the proxy transport
   under the same ``all://`` key in ``httpx`` ``mounts``; the second silently
   replaced the first, disabling SSRF protection on every proxied deployment.
"""

import pytest

from markbot.agent.cost import CostTracker, ModelPricing, PricingTable
from markbot.agent.iteration import normalise_usage
from markbot.utils.ssrf import SSRFRedirectError, guarded_client

PRICING = PricingTable(
    custom={
        "m": ModelPricing(input_per_1k=3.0, output_per_1k=15.0,
                          cache_read_per_1k=0.3),
    }
)


def _billed(usage, provider="openai_compat"):
    c = normalise_usage(provider, usage)
    t = CostTracker(pricing=PRICING)
    t.add_canonical_usage(c, model="m")
    return t


class TestInputTokensAreNotZeroed:
    def test_prompt_only_is_billed_in_full(self):
        """A provider that reports no cache bucket at all must still bill input."""
        t = _billed({"prompt_tokens": 1000, "completion_tokens": 50})
        assert t.get_model_usage("m").input_tokens == 1000
        assert t.total_cost == pytest.approx(3.0 * 1.0 + 15.0 * 0.05)

    def test_synthesised_zero_miss_does_not_zero_the_bill(self):
        """The exact bug: ``prompt_cache_miss_tokens: 0`` meant "free prompt"."""
        t = _billed({"prompt_tokens": 1000, "completion_tokens": 50,
                     "prompt_cache_miss_tokens": 0})
        assert t.get_model_usage("m").input_tokens == 1000, \
            "a missing cache field was treated as zero misses, billing no input"

    def test_cached_prompt_splits_residual_against_cache_rate(self):
        t = _billed({"prompt_tokens": 1000, "completion_tokens": 50,
                     "prompt_cache_hit_tokens": 800})
        u = t.get_model_usage("m")
        assert u.input_tokens == 200
        assert u.cache_read_input_tokens == 800
        assert t.total_cost == pytest.approx(0.3 * 0.8 + 3.0 * 0.2 + 15.0 * 0.05)

    def test_providers_agree_on_the_same_underlying_call(self):
        shapes = {
            "openai_compat": {"prompt_tokens": 1000, "completion_tokens": 50,
                              "prompt_cache_hit_tokens": 800},
            "deepseek": {"prompt_tokens": 1000, "completion_tokens": 50,
                         "prompt_cache_hit_tokens": 800,
                         "prompt_cache_miss_tokens": 200},
            "anthropic": {"input_tokens": 200, "output_tokens": 50,
                          "cache_read_input_tokens": 800,
                          "cache_creation_input_tokens": 0},
        }
        costs = {p: _billed(u, provider=p).total_cost for p, u in shapes.items()}
        assert len(set(round(c, 6) for c in costs.values())) == 1, costs

    def test_cache_never_double_bills(self):
        """1000 total prompt with 800 cached must not cost 1000 + 800."""
        t = _billed({"prompt_tokens": 1000, "completion_tokens": 0,
                     "prompt_cache_hit_tokens": 800})
        assert t.total_cost < 3.0, "cached tokens were charged twice"


class TestProviderEmitsAbsentRatherThanZero:
    def test_openai_compat_omits_miss_when_unreported(self):
        from markbot.providers.openai_compat import OpenAICompatProvider

        out = OpenAICompatProvider._extract_usage(
            {"usage": {"prompt_tokens": 1000, "completion_tokens": 50}}
        )
        assert "prompt_cache_miss_tokens" not in out, \
            "a synthesised 0 is indistinguishable from 'no misses'"

    def test_openai_compat_preserves_reported_miss(self):
        from markbot.providers.openai_compat import OpenAICompatProvider

        out = OpenAICompatProvider._extract_usage(
            {"usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                       "prompt_cache_miss_tokens": 200}}
        )
        assert out["prompt_cache_miss_tokens"] == 200

    def test_azure_omits_miss_when_unreported(self):
        """Azure must not synthesise a zero miss count either."""
        from markbot.providers.azure_openai import AzureOpenAIProvider

        p = AzureOpenAIProvider.__new__(AzureOpenAIProvider)
        out = p._parse_response({
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
        })
        assert "prompt_cache_miss_tokens" not in (out.usage or {}), \
            "azure synthesised a zero miss count, zeroing the input bill"

    def test_azure_preserves_reported_miss(self):
        from markbot.providers.azure_openai import AzureOpenAIProvider

        p = AzureOpenAIProvider.__new__(AzureOpenAIProvider)
        out = p._parse_response({
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                      "prompt_cache_miss_tokens": 200},
        })
        assert out.usage["prompt_cache_miss_tokens"] == 200

    def test_azure_prompt_is_billed_end_to_end(self):
        from markbot.providers.azure_openai import AzureOpenAIProvider

        p = AzureOpenAIProvider.__new__(AzureOpenAIProvider)
        out = p._parse_response({
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
        })
        t = _billed(out.usage, provider="azure_openai")
        assert t.get_model_usage("m").input_tokens == 1000


class TestGuardSurvivesProxy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7890"])
    async def test_guard_transport_in_both_modes(self, proxy):
        client = guarded_client(timeout=2.0, proxy=proxy) if proxy \
            else guarded_client(timeout=2.0)
        assert "Guarded" in type(client._transport).__name__
        with pytest.raises(SSRFRedirectError):
            await client.get("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.asyncio
    async def test_no_duplicate_all_mount(self):
        """Two ``all://`` mounts collide; the guard would lose the lookup."""
        client = guarded_client(timeout=2.0, proxy="http://127.0.0.1:7890")
        patterns = [str(getattr(p, "pattern", p)) for p in client._mounts]
        assert patterns.count("all://") == 0, "guard must be the transport, not a mount"

    @pytest.mark.asyncio
    async def test_public_host_still_reaches_the_guard(self):
        client = guarded_client(timeout=0.01, proxy="http://127.0.0.1:7890")
        # A connect error (not SSRFRedirectError) means the guard allowed it
        # through and the request actually left the process.
        with pytest.raises(Exception) as exc:
            await client.get("http://example.com/")
        assert not isinstance(exc.value, SSRFRedirectError)
