"""Tests for exec-tool environment construction.

Two defects found in review of the credential-stripping change:

1. In ``env_allowlist`` mode ``PATH`` was filtered out, so ``path_append``
   concatenated onto an empty string and produced ``";/opt/bin"`` — every
   shell command would fail to resolve anything. ``path_append`` now
   guarantees a real ``PATH``; a plain allowlist that omits ``PATH`` stays
   strict, because that is the operator's stated intent.
2. Env-var matching had to be case-insensitive: Windows normalises
   environment variable names, so a user writing the usual ``Path`` (or
   ``MyToken``) must not be silently ignored.
"""

from __future__ import annotations

import os

import pytest

from markbot.tools.shell import ExecTool


def _tool(**kw) -> ExecTool:
    kw.setdefault("working_dir", os.getcwd())
    return ExecTool(**kw)


class TestPathHandling:
    def test_allowlist_without_path_stays_strict(self):
        """An allowlist omitting PATH means "no search path", not "helpfully
        add PATH". The existing strict test in test_shell_confinement.py
        pins this, so widening it here would be a second bug."""
        env = _tool(env_allowlist=["SOME_VAR"])._build_env()
        assert "PATH" not in {k.upper() for k in env}

    def test_allowlist_keeps_explicitly_listed_path(self, monkeypatch):
        monkeypatch.setenv("PATH", os.defpath)
        env = _tool(env_allowlist=["SOME_VAR", "Path"])._build_env()
        assert "PATH" in {k.upper() for k in env}

    def test_allowlist_mode_path_append_is_sane(self):
        """The original bug: PATH stripped, so path_append produced a bogus
        path and every command failed."""
        t = _tool(env_allowlist=["SOME_VAR"], path_append="/opt/bin")
        env = t._build_env()
        env["PATH"] = env.get("PATH", "") + os.pathsep + t.path_append
        assert env["PATH"]
        assert not env["PATH"].startswith(os.pathsep)
        assert "/opt/bin" in env["PATH"]

    def test_path_falls_back_when_absent(self, monkeypatch):
        monkeypatch.delenv("PATH", raising=False)
        env = _tool(env_allowlist=["SOME_VAR"], path_append="/opt/bin")._build_env()
        assert env.get("PATH")

    def test_denylist_mode_keeps_path(self, monkeypatch):
        monkeypatch.setenv("PATH", os.defpath)
        assert "PATH" in _tool()._build_env()


class TestCaseInsensitivity:
    @pytest.mark.parametrize("spelling", ["MyToken", "MYTOKEN", "mytoken"])
    def test_extra_env_allow_any_case(self, monkeypatch, spelling):
        monkeypatch.setenv("MYTOKEN", "kept")
        env = _tool(extra_env_allow=[spelling])._build_env()
        # Windows upper-cases os.environ keys; match on the normalised form.
        assert "MYTOKEN" in env or "MyToken" in env
        assert env.get("MYTOKEN", env.get("MyToken")) == "kept"

    def test_allowlist_any_case(self, monkeypatch):
        monkeypatch.setenv("MYVAR", "v")
        env = _tool(env_allowlist=["MyVar"])._build_env()
        assert "MYVAR" in env


class TestCredentialStripping:
    @pytest.mark.parametrize(
        "name",
        [
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "MY_SERVICE_APIKEY",
            "ACME_PRIVATE_KEY", "SOME_SESSION_KEY",
        ],
    )
    def test_stripped_in_denylist_mode(self, monkeypatch, name):
        monkeypatch.setenv(name, "leaked")
        assert name not in _tool()._build_env()

    @pytest.mark.parametrize(
        "name", ["ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD"]
    )
    def test_stripped_in_allowlist_mode(self, monkeypatch, name):
        monkeypatch.setenv(name, "leaked")
        monkeypatch.setenv("KEEPME", "ok")
        env = _tool(env_allowlist=["KEEPME"])._build_env()
        assert name not in env
        assert "KEEPME" in env

    def test_ordinary_vars_survive(self, monkeypatch):
        monkeypatch.setenv("PROJECT_NAME", "demo")
        assert "PROJECT_NAME" in _tool()._build_env()

    def test_extra_env_allow_overrides_denylist(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ci-token")
        env = _tool(extra_env_allow=["GITHUB_TOKEN"])._build_env()
        assert env.get("GITHUB_TOKEN") == "ci-token"

    def test_extra_env_allow_also_applies_in_allowlist_mode(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ci-token")
        env = _tool(
            env_allowlist=["SAFE"], extra_env_allow=["GITHUB_TOKEN"]
        )._build_env()
        assert env.get("GITHUB_TOKEN") == "ci-token"
