"""Structural tests for the P1-1 cli split.

Asserts the layout invariants introduced by the refactor:
- ``markbot.cli.commands:app`` is importable and is a ``typer.Typer``
- The new public surface modules expose the expected names
- Every ``cli/groups/*.py`` module exposes a ``typer.Typer`` app
- Subprocess invocations of ``markbot --help`` and key sub-commands
  succeed and mention the expected sub-groups
"""
from __future__ import annotations

import subprocess
import sys

import pytest
import typer

import markbot.cli.ui as ui
import markbot.cli.runtime as runtime
import markbot.cli.daemon as daemon
from markbot.cli.commands import app as top_app
from markbot.cli.groups import (
    agent,
    channels,
    config,
    gateway,
    onboard,
    plugins,
    provider,
    status,
    web,
)


def test_top_app_is_typer():
    assert isinstance(top_app, typer.Typer)


@pytest.mark.parametrize("name", [
    "markbot_banner", "flush_pending_tty_input", "restore_terminal",
    "init_prompt_session", "make_console", "render_interactive_ansi",
    "print_agent_response", "response_renderable",
    "print_cli_progress_line", "is_exit_command",
])
def test_ui_public_api(name):
    assert callable(getattr(ui, name)), f"ui.{name} not callable"


@pytest.mark.parametrize("name", [
    "load_runtime_config", "make_provider",
    "merge_missing_defaults", "warn_deprecated_config_keys",
])
def test_runtime_public_api(name):
    assert callable(getattr(runtime, name)), f"runtime.{name} not callable"


@pytest.mark.parametrize("name", [
    "ensure_pid_dir", "read_pid", "write_pid", "remove_pid",
    "is_process_running", "terminate_process", "start_daemon",
])
def test_daemon_public_api(name):
    assert callable(getattr(daemon, name)), f"daemon.{name} not callable"


@pytest.mark.parametrize("mod", [
    agent, gateway, channels, provider, config, onboard, status, plugins, web,
])
def test_groups_have_typer_app(mod):
    assert isinstance(mod.app, typer.Typer)
    # Sub-typer can use either registered_commands (sub-cmd style) or
    # a registered callback (single-cmd style with invoke_without_command).
    # Either form is acceptable; the contract is "loads as a typer.Typer".


def test_commands_py_is_small():
    """The 8-file split keeps commands.py to <= 120 lines."""
    with open("markbot/cli/commands.py", encoding="utf-8") as f:
        n_lines = sum(1 for _ in f)
    assert n_lines <= 120, f"commands.py is {n_lines} lines; expected <= 120"


# Files created or modified by the P1-1 split. doctor.py is a pre-existing
# 1400-line file outside the scope of P1-1 and is intentionally not listed.
_P1_1_FILES = [
    "markbot/cli/commands.py",
    "markbot/cli/ui.py",
    "markbot/cli/runtime.py",
    "markbot/cli/daemon.py",
    "markbot/cli/groups/__init__.py",
    "markbot/cli/groups/agent.py",
    "markbot/cli/groups/onboard.py",
    "markbot/cli/groups/gateway.py",
    "markbot/cli/groups/channels.py",
    "markbot/cli/groups/provider.py",
    "markbot/cli/groups/config.py",
    "markbot/cli/groups/plugins.py",
    "markbot/cli/groups/web.py",
    "markbot/cli/groups/status.py",
]


@pytest.mark.parametrize("path", _P1_1_FILES)
def test_p1_1_files_under_800_lines(path):
    """Every file added or rewritten by P1-1 stays under 800 lines."""
    n = sum(1 for _ in open(path, encoding="utf-8"))
    assert n <= 800, f"{path} is {n} lines; expected <= 800"


def _run_markbot(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "markbot", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_markbot_help_lists_groups():
    p = _run_markbot("--help")
    assert p.returncode == 0, p.stderr
    for name in ("agent", "gateway", "channels", "provider", "config",
                 "onboard", "plugins", "status", "web", "skills",
                 "autopilot", "doctor"):
        assert name in (p.stdout or ""), f"group {name!r} missing from --help"


def test_markbot_gateway_help_lists_subcommands():
    p = _run_markbot("gateway", "--help")
    assert p.returncode == 0, p.stderr
    for sub in ("start", "stop", "restart", "status"):
        assert sub in (p.stdout or ""), f"sub-command {sub!r} missing from gateway --help"


def test_markbot_config_help_lists_subcommands():
    p = _run_markbot("config", "--help")
    assert p.returncode == 0, p.stderr
    for sub in ("get", "set", "list", "provider", "channel", "show"):
        assert sub in (p.stdout or ""), f"sub-command {sub!r} missing from config --help"


def test_external_entrypoint_unchanged():
    """`python -m markbot` still works (markbot.cli.commands:app)."""
    p = _run_markbot("--version")
    assert p.returncode == 0, p.stderr
    assert "MarkBot" in (p.stdout or "")


def test_no_private_imports_from_commands():
    """No other module imports the old underscored helpers from commands.py.

    MarkBot code outside the cli package should reach the public
    ``markbot.cli.runtime`` and ``markbot.cli.ui`` modules instead.
    """
    import pathlib
    bad = []
    for p in pathlib.Path("markbot").rglob("*.py"):
        if p.name == "commands.py":
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if "from markbot.cli.commands import" in line and any(
                tok.strip().startswith("_") for tok in line.split("import", 1)[1].split(",")
            ):
                bad.append(f"{p}: {line.strip()}")
    assert not bad, "Private imports from commands.py still exist:\n" + "\n".join(bad)
