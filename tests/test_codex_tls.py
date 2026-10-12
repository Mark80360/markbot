"""Tests for the OpenAI Codex TLS downgrade.

The provider caught ``CERTIFICATE_VERIFY_FAILED`` and silently retried the
same request with ``verify=False``. That accepted any MITM certificate
without telling anyone, on a channel carrying the full conversation plus the
OAuth credential.

The downgrade is now opt-in and, when off, refuses rather than proceeding.
"""

from __future__ import annotations

import ast
import io

import pytest


def _call_codex_body() -> str:
    """Parse the module without importing it.

    ``oauth_cli_kit`` is an optional dependency, so the source is inspected
    statically rather than executed.
    """
    src = io.open(
        "markbot/providers/openai_codex.py", encoding="utf-8"
    ).read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call_codex"
    )
    return ast.unparse(fn)


def _init_body() -> str:
    src = io.open("markbot/providers/openai_codex.py", encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    return ast.unparse(fn)


class TestDowngradeIsGated:
    def test_gate_present(self):
        assert "if not self.insecure_skip_verify" in _call_codex_body()

    def test_refusal_is_explicit(self):
        body = _call_codex_body()
        assert "Refusing to retry without" in body
        assert "MITM" in body or "man-in-the-middle" in body.lower() or "interception" in body

    def test_downgrade_mentions_the_opt_in(self):
        """The retry must be explained, not silent."""
        body = _call_codex_body()
        assert "insecureSkipVerify is enabled" in body

    def test_other_errors_still_raise(self):
        """Only certificate failures take this path."""
        body = _call_codex_body()
        assert "CERTIFICATE_VERIFY_FAILED" in body
        # The re-raise guard: anything else propagates immediately.
        assert "not in str(e)" in body and "raise" in body


class TestDefaultIsSecure:
    def test_default_reads_from_spec(self):
        body = _init_body()
        assert "self.insecure_skip_verify" in body
        assert "insecure_skip_verify" in body
        # Defaults to False via getattr.
        assert "getattr(spec, 'insecure_skip_verify', False)" in body

    def test_config_default_is_false(self):
        from markbot.config.schema import ProviderConfig

        assert ProviderConfig().insecure_skip_verify is False

    def test_config_opt_in(self):
        from markbot.config.schema import ProviderConfig

        assert ProviderConfig(insecureSkipVerify=True).insecure_skip_verify is True

    def test_camel_alias_roundtrip(self):
        from markbot.config.schema import ProviderConfig

        cfg = ProviderConfig(insecureSkipVerify=True)
        assert cfg.model_dump(by_alias=True)["insecureSkipVerify"] is True


class TestSpecField:
    def test_spec_has_field(self):
        import dataclasses

        from markbot.providers.registry import ProviderSpec

        names = {f.name for f in dataclasses.fields(ProviderSpec)}
        assert "insecure_skip_verify" in names

    def test_spec_default_false(self):
        import dataclasses

        from markbot.providers.registry import ProviderSpec

        field = next(
            f for f in dataclasses.fields(ProviderSpec)
            if f.name == "insecure_skip_verify"
        )
        assert field.default is False


class TestFallbackAppliesConfig:
    def test_manager_applies_the_flag(self):
        """The setting lives in config, not on the static spec, so the
        manager must copy it onto the provider."""
        import inspect

        from markbot.providers.fallback import FallbackManager

        src = inspect.getsource(FallbackManager._get_or_create_provider)
        assert "insecure_skip_verify" in src


class TestNoOtherSilentDowngrade:
    def test_repo_has_no_other_verify_false_fallback(self):
        """Guard against the pattern reappearing elsewhere."""
        import glob

        offenders = []
        for path in glob.glob("markbot/providers/*.py"):
            src = io.open(path, encoding="utf-8").read()
            if "verify=False" not in src:
                continue
            if "insecure_skip_verify" in src:
                continue  # the documented, gated case
            offenders.append(path)
        assert not offenders, f"ungated TLS downgrade in: {offenders}"
