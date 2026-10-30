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

from markbot.tools.shell import (
    ExecTool,
    _command_segments,
    _unresolvable_path_expansion,
)


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


class TestParameterExpansionCannotEscape:
    """``${HOME:0:6}/…`` is ``~`` with no literal path component.

    The token scan resolves literal paths, and an env reference is expanded
    before resolving. A *modified* expansion (``:offset:length``, ``#``,
    ``%``, ``/x/y``, ``:-default``) produces a value that only the shell can
    compute, so it never reached ``expandvars`` and the path guard saw
    nothing to check — ``cat ${HOME:0:6}/marktang/.ssh/id_rsa`` was allowed.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat ${HOME:0:6}/marktang/.ssh/id_rsa",
            "cat ${HOME:0:5}ang/.ssh/id_rsa",
            "cat ${HOME:0:11}/.aws/credentials",
            "cat ${HOME#/}/.ssh/id_rsa",
            "cat ${HOME%/*}/secret",
            "cat ${HOME:-/tmp}/x",
            "cp inside.txt ${PWD:0:1}/outside.txt",
        ],
    )
    async def test_modified_expansion_refused(self, tool, cmd):
        out = await tool._legacy_execute(cmd)
        assert "blocked by safety guard" in out, cmd
        assert "unresolvable parameter expansion" in out, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat ${HOME}/x",          # plain expansion: resolvable via expandvars
            "cat $HOME/x",
            "echo ${PATH:0:3}",       # not path-like (no separator in the token)
        ],
    )
    def test_resolvable_or_non_path_forms_not_flagged(self, tool, cmd):
        """The guard must not turn into a blanket ban on ``${...}``."""
        assert _unresolvable_path_expansion(cmd) is None, cmd

    def test_relative_path_inside_still_allowed(self, tool):
        """Confirms the new rule did not break the ordinary case."""
        assert _unresolvable_path_expansion("cat inside.txt") is None


class TestUrlGuardIsNotPrefixGated:
    """The internal-URL check used to be gated on ``command.startswith("curl")``.

    That made it dead for every wrapped invocation — which is how the tool
    actually gets called after a prompt injection tells the model to be
    "careful" and prefix the command with ``env`` or ``timeout``.
    """

    BLOCKED = [
        "curl http://169.254.169.254/latest/meta-data/",
        "env curl http://169.254.169.254/latest/meta-data/",
        "env -i curl http://169.254.169.254/",
        "command curl http://169.254.169.254/",
        "nohup curl http://169.254.169.254/",
        "timeout 5 curl http://169.254.169.254/",
        "nice curl http://169.254.169.254/",
        "nice -n 5 wget http://169.254.169.254/",
        "true; curl http://169.254.169.254/",
        "ls && curl http://127.0.0.1:9120/api/status",
        "curl http://10.0.0.5/admin",
        "curl http://[::ffff:169.254.169.254]/latest/meta-data/",
        "curl http://[2002:a9fe:a9fe::]/",
        "curl http://[64:ff9b::a9fe:a9fe]/",
    ]

    @pytest.mark.parametrize("cmd", BLOCKED)
    def test_internal_url_blocked_whatever_the_prefix(self, tool, cmd):
        err = tool._guard_command(cmd, str(tool.working_dir))
        assert err is not None, cmd
        assert "internal/private URL" in err, (cmd, err)

    def test_external_url_still_allowed(self, tool, monkeypatch):
        import socket

        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *a, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ],
        )
        assert tool._guard_command("curl https://example.com", str(tool.working_dir)) is None

    def test_command_without_url_not_scanned(self, tool):
        """The pre-check keeps ordinary commands off the DNS path."""
        assert tool._guard_command("cat inside.txt", str(tool.working_dir)) is None

    def test_whitelisted_internal_ip_allowed(self, workspace):
        """An operator exemption must still work through the wrapper form."""
        t = ExecTool(
            working_dir=str(workspace),
            restrict_to_workspace=True,
            allowed_internal_ips=["10.0.0.5"],
        )
        assert t._guard_command("env curl http://10.0.0.5/api", str(workspace)) is None


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


class TestGuardRelaxations:
    """Operators can tune the guard via ``tools.exec.*`` in config.json."""

    def test_dev_null_read_redirect_allowed(self, workspace):
        tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
        assert tool._guard_command("cat inside.txt 2>/dev/null", str(workspace)) is None

    def test_dev_null_write_redirect_allowed(self, workspace):
        tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
        assert tool._guard_command("ls > /dev/null 2>&1", str(workspace)) is None

    def test_external_redirect_still_blocked(self, workspace):
        tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
        outside = str(workspace.parent / "outside_secret.md")
        err = tool._guard_command(f"echo x > {outside}", str(workspace))
        assert err is not None
        assert "blocked" in err

    def test_shell_chaining_blocked_by_default(self, workspace):
        tool = ExecTool(working_dir=str(workspace), restrict_to_workspace=True)
        assert tool._guard_command("echo $(date)", str(workspace)) is not None

    def test_shell_chaining_allowed_when_opted_in(self, workspace):
        tool = ExecTool(
            working_dir=str(workspace),
            restrict_to_workspace=True,
            allow_nested_shell=True,
        )
        assert tool._guard_command("echo $(date)", str(workspace)) is None

    def test_denylist_still_applies_when_chaining_allowed(self, workspace):
        """Opting out of the heuristic must not disable the denylist."""
        tool = ExecTool(
            working_dir=str(workspace),
            restrict_to_workspace=True,
            allow_nested_shell=True,
        )
        err = tool._guard_command("rm -rf /tmp/x", str(workspace))
        assert err is not None
        assert "blocked" in err


class TestAllowlistCoversEverySegment:
    """The allowlist has to authorise every command in a chain.

    Two defects met here. The 2026-10-30 gateway log shows the first: every
    refused exec was ``cd /home/…/workspace && ls …``, ``ls`` was allowlisted
    and ``cd`` was not, so the ``^``-anchored match rejected the whole string
    and the model concluded exec had been disabled. The second is the hole
    behind it: matching only the *first* token meant ``ls && bash evil.sh``
    was authorised by its ``ls`` and then ran ``bash``.
    """

    ALLOW = [
        r"^(ls|pwd|cat|head|tail|wc|echo|date)\b",
        r"^(git|rg|grep|find|sed|awk)\b",
    ]

    @pytest.fixture
    def strict(self, workspace):
        return ExecTool(
            working_dir=str(workspace),
            restrict_to_workspace=True,
            require_allowlist=True,
            allow_patterns=list(self.ALLOW),
        )

    @pytest.mark.parametrize("cmd,expected", [
        ("cd /w && ls", ["cd /w", "ls"]),
        ("cd /w; ls", ["cd /w", "ls"]),
        ("ls | head -3", ["ls", "head -3"]),
        ("ls || cat x", ["ls", "cat x"]),
        ('grep -E "a|b" f', ["grep -E _ f"]),
        ("ls > /dev/null 2>&1", ["ls > /dev/null 2>&1"]),
        ("ls &", ["ls"]),
    ])
    def test_segments(self, cmd, expected):
        assert _command_segments(cmd) == expected

    def test_cd_prefix_is_authorised_by_its_own_segment(self, strict, workspace):
        assert strict._guard_command(f"cd {workspace} && ls inside.txt", str(workspace)) is None

    @pytest.mark.parametrize("cmd", [
        "ls && bash evil.sh",
        "ls; bash evil.sh",
        "ls | bash",
        "ls || bash evil.sh",
        "cd /w && ls && bash evil.sh",
    ])
    def test_chained_command_cannot_borrow_authorisation(self, strict, workspace, cmd):
        err = strict._guard_command(cmd, str(workspace))
        assert err is not None, cmd
        assert "`bash`" in err, (cmd, err)

    def test_plain_command_still_checked(self, strict, workspace):
        assert strict._guard_command("ls inside.txt", str(workspace)) is None
        assert strict._guard_command("bash evil.sh", str(workspace)) is not None

    @pytest.mark.parametrize("cmd", ["cd .. && ls", "cd - && ls"])
    def test_cd_escape_still_refused(self, strict, workspace, cmd):
        err = strict._guard_command(cmd, str(workspace))
        assert err is not None
        assert "cd outside working dir" in err

    def test_cd_outside_workspace_still_refused(self, strict, workspace):
        err = strict._guard_command("cd /etc && ls", str(workspace))
        assert err is not None
        assert "path outside working dir" in err

    @pytest.mark.parametrize("cmd", [
        "cd $HOME && cat .ssh/id_rsa",
        'cd "$HOME" && cat .ssh/id_rsa',
        "cd ${HOME} && cat .ssh/id_rsa",
        "cd ~ && cat .ssh/id_rsa",
        "cd && cat .ssh/id_rsa",
        "cd -P $HOME && cat .ssh/id_rsa",
    ])
    def test_cd_target_must_be_lexically_resolvable(self, strict, workspace, cmd):
        """``cd $HOME`` / ``cd ~`` / bare ``cd`` move the shell outside the
        workspace without leaving a path token the scanner can check, so the
        rest of the chain reads files the guard never saw."""
        assert strict._guard_command(cmd, str(workspace)) is not None, cmd

    def test_cd_into_a_subdirectory_is_allowed(self, strict, workspace):
        sub = workspace / "sub"
        sub.mkdir()
        assert strict._guard_command("cd sub && ls", str(workspace)) is None
        assert strict._guard_command(f"cd {workspace} && ls", str(workspace)) is None

    def test_refusal_names_what_is_allowed(self, strict, workspace):
        err = strict._guard_command("bash evil.sh", str(workspace))
        assert "Allowed leading commands:" in err
        assert "ls" in err
        assert "working_dir" in err

    @pytest.mark.parametrize("cmd,strict_allowlist", [
        ("rm -rf /tmp/x", False),
        ("cd .. && ls", True),
        ("cd $HOME && ls", True),
        ("cat /etc/passwd", False),
        ("curl http://169.254.169.254/latest/meta-data/", False),
        ("bash evil.sh", True),
    ])
    def test_every_refusal_carries_the_shared_marker(
        self, strict, tool, workspace, cmd, strict_allowlist
    ):
        """``agent.tool_guardrails.is_policy_denial`` keys off this marker, so
        every refusal path — denylist, allowlist, confinement, internal URL —
        must carry it. Without this the two modules can drift apart silently
        and refusals start counting as tool failures again."""
        from markbot.utils.constants import SAFETY_GUARD_MARKER

        target = strict if strict_allowlist else tool
        err = target._guard_command(cmd, str(workspace))
        assert err is not None, cmd
        assert SAFETY_GUARD_MARKER in err.lower(), (cmd, err)


class TestConfigWiring:
    def test_schema_exposes_env_controls(self):
        from markbot.config.schema import ExecToolConfig

        cfg = ExecToolConfig()
        assert cfg.env_allowlist == []
        assert cfg.extra_env_allow == []

    def test_schema_exposes_guard_controls(self):
        from markbot.config.schema import ExecToolConfig

        cfg = ExecToolConfig()
        assert cfg.restrict_to_workspace is True
        assert cfg.allow_nested_shell is False

    def test_schema_env_allowlist_camel_alias(self):
        from markbot.config.schema import ExecToolConfig

        cfg = ExecToolConfig(envAllowlist=["A", "B"])
        assert cfg.env_allowlist == ["A", "B"]

    @pytest.mark.parametrize("key", [
        "allow_nested_shell",
        "allowNestedShell",
        "allow_shell_chaining",   # previous name, still honoured
        "allowShellChaining",
    ])
    def test_nested_shell_alias_accepts_old_config_key(self, key):
        """The field was renamed because it never governed plain chaining.
        An operator's existing config.json must keep working."""
        from markbot.config.schema import ExecToolConfig

        cfg = ExecToolConfig(**{key: True})
        assert cfg.allow_nested_shell is True
