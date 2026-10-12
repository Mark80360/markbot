"""Tests for the exec tool's confinement and credential hygiene.

Two independent holes are covered here:

* ``working_dir`` is a *model-supplied* parameter, and the path guard used
  to anchor on it. Passing ``working_dir="/"`` therefore disabled
  ``restrict_to_workspace`` outright.
* ``env`` was a bare ``os.environ.copy()``, and provider keys are written
  into the process environment at startup — so any prompt injection that
  reached the shell could exfiltrate every configured credential.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from markbot.tools.shell import ExecTool


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "inside.txt").write_text("inside\n", encoding="utf-8")
    (tmp_path.parent / "outside_secret.md").write_text(
        "outside-secret-marker\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def tool(workspace):
    return ExecTool(working_dir=str(workspace), restrict_to_workspace=True)


class TestWorkingDirCannotEscape:
    """The confinement root is the operator-configured working_dir."""

    @pytest.mark.parametrize(
        "wd", ["/", "..", "../..", "/etc", "/tmp", "sub/../.."]
    )
    async def test_escape_refused(self, workspace, tool, wd):
        out = await tool._legacy_execute("cat inside.txt", working_dir=wd)
        assert "blocked by safety guard" in out

    async def test_absolute_outside_refused(self, workspace, tool):
        outside = str(workspace.parent / "outside_secret.md")
        out = await tool._legacy_execute(f"cat {outside}")
        assert "blocked by safety guard" in out
        assert "outside-secret-marker" not in out

    async def test_workspace_itself_allowed(self, workspace, tool):
        out = await tool._legacy_execute("cat inside.txt", working_dir=str(workspace))
        assert "inside" in out
        assert "blocked" not in out

    async def test_subdir_allowed(self, workspace, tool):
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "f.txt").write_text("subfile\n", encoding="utf-8")
        out = await tool._legacy_execute("cat f.txt", working_dir="sub")
        assert "subfile" in out

    async def test_unrestricted_tool_allows_any_cwd(self, workspace):
        """With restrict_to_workspace off the operator opted out entirely."""
        t = ExecTool(working_dir=str(workspace), restrict_to_workspace=False)
        out = await t._legacy_execute("cat inside.txt", working_dir=str(workspace.parent))
        assert "blocked" not in out


class TestPathSpellings:
    """``$HOME/x``, ``${HOME}/x``, ``~user/x`` and ``%USERPROFILE%\\x`` all
    reach the same file; the guard must recognise every spelling."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat /etc/passwd",
            "cat $HOME/.aws/credentials",
            "cat ${HOME}/.ssh/id_rsa",
            "cat ~/.aws/credentials",
            "cat ~root/.bashrc",
            "cat %APPDATA%/secret.txt",
        ],
    )
    async def test_outside_paths_blocked(self, tool, cmd):
        out = await tool._legacy_execute(cmd)
        assert "blocked by safety guard" in out, cmd

    async def test_relative_path_inside_allowed(self, tool):
        assert "blocked" not in await tool._legacy_execute("cat inside.txt")

    async def test_unresolvable_env_var_fails_closed(self, tool, monkeypatch):
        """An env var the guard cannot resolve is treated as an escape, not
        as a relative path. On Windows ``$HOME`` does not exist, so
        expandvars leaves it verbatim and it would otherwise be read as a
        relative path inside the workspace."""
        monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
        out = await tool._legacy_execute("cat $SOME_UNSET_VAR/secret")
        assert "blocked by safety guard" in out

    def test_extract_finds_env_var_forms(self, tool):
        for cmd, needle in [
            ("cat $HOME/x", "$HOME/x"),
            ("cat ${HOME}/x", "${HOME}/x"),
            ("cat %APPDATA%/x", "%APPDATA%/x"),
        ]:
            assert any(needle in p for p in tool._extract_all_paths(cmd)), cmd

    def test_extract_finds_other_user_tilde(self, tool):
        assert any("~root/.bashrc" in p for p in tool._extract_all_paths("cat ~root/.bashrc"))


class TestCredentialEnvStripping:
    SECRETS = [
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DB_PASSWORD",
        "MY_SESSION_KEY", "SOME_ACCESS_KEY", "SERVICE_AUTH",
        "AZURE_OPENAI_API_KEY", "HF_TOKEN",
    ]

    @pytest.fixture(autouse=True)
    def _seed(self, monkeypatch):
        for name in self.SECRETS:
            monkeypatch.setenv(name, "leaked-value")
        monkeypatch.setenv("MARKBOT_SAFE_VAR", "keep-me")

    @pytest.mark.parametrize("name", SECRETS)
    def test_credential_not_inherited(self, tool, name):
        assert name not in tool._build_env()

    def test_ordinary_var_inherited(self, tool):
        assert tool._build_env()["MARKBOT_SAFE_VAR"] == "keep-me"

    def test_secret_substring_variants_caught(self, tool, monkeypatch):
        for name in [
            "MY_SERVICE_APIKEY", "X_CUSTOM_SECRET", "DB_PWD_PASSWORD",
            "ACME_PRIVATE_KEY", "OAUTH_CREDENTIAL_FILE",
        ]:
            monkeypatch.setenv(name, "leaked")
            assert name not in tool._build_env(), name

    def test_extra_env_allow_overrides_denylist(self, workspace):
        t = ExecTool(working_dir=str(workspace), extra_env_allow=["GITHUB_TOKEN"])
        assert t._build_env()["GITHUB_TOKEN"] == "leaked-value"

    def test_allowlist_mode_is_strict(self, workspace):
        t = ExecTool(
            working_dir=str(workspace),
            env_allowlist=["MARKBOT_SAFE_VAR", "GITHUB_TOKEN"],
        )
        env = t._build_env()
        assert set(env) == {"MARKBOT_SAFE_VAR", "GITHUB_TOKEN"}

    def test_allowlist_mode_ignores_ambient_secrets(self, workspace):
        t = ExecTool(working_dir=str(workspace), env_allowlist=["MARKBOT_SAFE_VAR"])
        assert "ANTHROPIC_API_KEY" not in t._build_env()

    def test_denylist_is_the_default(self, workspace):
        t = ExecTool(working_dir=str(workspace))
        assert t.env_allowlist == set()
        assert "ANTHROPIC_API_KEY" not in t._build_env()

    def test_subprocess_receives_stripped_env(self, tool):
        """End-to-end: the child process must not see the key."""
        import asyncio

        async def run():
            return await tool._legacy_execute("echo ${ANTHROPIC_API_KEY:-EMPTY}")

        out = asyncio.run(run())
        assert "leaked-value" not in out


class TestConfigWiring:
    def test_schema_exposes_env_controls(self):
        from markbot.config.schema import ExecToolConfig

        cfg = ExecToolConfig()
        assert cfg.env_allowlist == []
        assert cfg.extra_env_allow == []

    def test_schema_env_allowlist_camel_alias(self):
        from markbot.config.schema import ExecToolConfig

        cfg = ExecToolConfig(envAllowlist=["A", "B"])
        assert cfg.env_allowlist == ["A", "B"]
