"""Tests for verify-on-stop: command classification, nudge, and verifier footer.

Covers the action/verify/neutral classification of shell commands and the
verify-on-stop nudge / footer logic that prevents the "false completion"
failure mode where the model runs a side-effecting command (restart/install/
pull/kill) and declares done without verifying the post-condition.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from markbot.agent.iteration import (
    _ACTION_COMPLETION_CLAIM_PATTERNS,
    _classify_shell_command,
    LoopState,
    TurnExitReason,
)
from markbot.agent.iteration import IterationRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(channel: str = "cli") -> IterationRunner:
    """Build an IterationRunner bypassing __init__ for unit tests."""
    runner = IterationRunner.__new__(IterationRunner)
    runner.channel = channel
    runner.loop = MagicMock()
    # resolve_sanitised_name returns the input by default.
    runner.loop.tools.resolve_sanitised_name = lambda name: name
    return runner


def _make_state(**kwargs) -> LoopState:
    """Build a LoopState with defaults, overriding via kwargs."""
    return LoopState(messages=[], initial_count=0, **kwargs)


def _tool_call(name: str, command: str = "") -> SimpleNamespace:
    """Build a minimal tool-call-like object."""
    args = {"command": command} if command else {}
    return SimpleNamespace(name=name, arguments=args)


# ---------------------------------------------------------------------------
# _classify_shell_command
# ---------------------------------------------------------------------------

class TestClassifyShellCommand:
    """Command classification: action / verify / neutral."""

    @pytest.mark.parametrize("cmd", [
        "systemctl restart gateway",
        "systemctl start nginx",
        "systemctl stop docker",
        "systemctl reload nginx",
        "service nginx restart",
        "pip install requests",
        "pip3 install -e .",
        "npm install",
        "npm i lodash",
        "npm add react",
        "pnpm add vue",
        "yarn add express",
        "brew install jq",
        "apt install -y curl",
        "apt-get install -y git",
        "git pull",
        "git push origin main",
        "git checkout feature-branch",
        "docker restart redis",
        "docker stop postgres",
        "pkill -f gateway",
        "killall node",
        "rm -rf /tmp/junk",
        "mv old.txt new.txt",
        "chmod +x script.sh",
    ])
    def test_action_commands(self, cmd: str):
        assert _classify_shell_command(cmd) == "action"

    @pytest.mark.parametrize("cmd", [
        "systemctl is-active gateway",
        "systemctl status nginx",
        "systemctl is-enabled docker",
        "service nginx status",
        "launchctl list | grep gateway",
        "ps aux",
        "pgrep -f gateway",
        "pidof python",
        "ss -tlnp",
        "netstat -tlnp",
        "lsof -i :8080",
        "curl -s http://localhost:8080/health",
        "docker ps",
        "docker logs redis",
        "git log -1 --oneline",
        "git status",
        "git diff",
        "pip show requests",
        "npm list",
        "cat /etc/hostname",
        "head -n 10 file.txt",
        "tail -f /var/log/syslog",
        "ls -la",
        "find . -name '*.py'",
        "grep -r 'pattern' .",
        "rg 'pattern'",
        "pytest",
        "python -m pytest",
        "ruff check .",
        "mypy .",
        "make test",
        "make check",
    ])
    def test_verify_commands(self, cmd: str):
        assert _classify_shell_command(cmd) == "verify"

    @pytest.mark.parametrize("cmd", [
        "echo hello",
        "python script.py",
        "node app.js",
        "bash deploy.sh",
        "ansible-playbook site.yml",
        "terraform apply",
        "",  # empty
    ])
    def test_neutral_commands(self, cmd: str):
        assert _classify_shell_command(cmd) == "neutral"

    def test_chained_command_classified_by_first_segment(self):
        # `systemctl restart X && systemctl is-active X` — first segment
        # is the action, so the whole command is "action".
        assert _classify_shell_command(
            "systemctl restart gateway && systemctl is-active gateway"
        ) == "action"

    def test_sudo_prefix_stripped(self):
        assert _classify_shell_command("sudo systemctl restart nginx") == "action"
        assert _classify_shell_command("sudo systemctl status nginx") == "verify"

    def test_env_prefix_stripped(self):
        assert _classify_shell_command("FOO=bar systemctl restart nginx") == "action"
        assert _classify_shell_command(
            "env GATEWAY_HOME=/tmp systemctl status nginx"
        ) == "verify"

    def test_non_string_returns_neutral(self):
        assert _classify_shell_command(None) == "neutral"  # type: ignore[arg-type]
        assert _classify_shell_command(123) == "neutral"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _record_verification_call
# ---------------------------------------------------------------------------

class TestRecordVerificationCall:
    """action/verify distinction in _record_verification_call."""

    def test_action_command_sets_side_effect_pending_not_verification_done(self):
        runner = _make_runner()
        state = _make_state()
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl restart gateway"), ""
        )
        assert state.side_effect_pending is True
        assert state.verification_done is False

    def test_verify_command_sets_verification_done_clears_side_effect(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True)
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl is-active gateway"), ""
        )
        assert state.verification_done is True
        assert state.side_effect_pending is False

    def test_neutral_command_preserves_existing_behavior(self):
        runner = _make_runner()
        state = _make_state()
        runner._record_verification_call(
            state, _tool_call("exec", "echo hello"), ""
        )
        # Neutral defaults to verification_done=True to avoid regressing
        # the file-edit verify path (most exec calls ARE verification).
        assert state.verification_done is True

    def test_neutral_command_does_not_clear_side_effect_pending(self):
        # Critical: a neutral command (echo/python/node — unrecognised
        # commands that aren't explicitly verify) run AFTER an action
        # must NOT clear side_effect_pending — only a true verify command
        # (status/is-active/ps/logs) should. _should_inject_verify_nudge
        # checks side_effect_pending BEFORE the verification_done short-
        # circuit, so this keeps the action-verify gate strict.
        runner = _make_runner()
        state = _make_state(side_effect_pending=True)
        runner._record_verification_call(
            state, _tool_call("exec", "echo hello"), ""
        )
        assert state.verification_done is True
        assert state.side_effect_pending is True  # NOT cleared by neutral

    def test_code_execution_always_counts_as_verification(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True)
        runner._record_verification_call(
            state, _tool_call("code_execution", ""), "result"
        )
        assert state.verification_done is True
        assert state.side_effect_pending is False

    def test_non_verification_tool_ignored(self):
        runner = _make_runner()
        state = _make_state()
        runner._record_verification_call(
            state, _tool_call("read_file", ""), "content"
        )
        assert state.verification_done is False
        assert state.side_effect_pending is False

    def test_action_then_verify_clears_pending(self):
        runner = _make_runner()
        state = _make_state()
        # Action: restart
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl restart gateway"), ""
        )
        assert state.side_effect_pending is True
        assert state.verification_done is False
        # Verify: check status
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl is-active gateway"), ""
        )
        assert state.side_effect_pending is False
        assert state.verification_done is True


# ---------------------------------------------------------------------------
# _should_inject_verify_nudge
# ---------------------------------------------------------------------------

class TestShouldInjectVerifyNudge:
    """Nudge trigger conditions for both file edits and side-effect actions."""

    def test_side_effect_pending_triggers_nudge(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True)
        assert runner._should_inject_verify_nudge(state) is True

    def test_side_effect_pending_with_neutral_command_still_triggers_nudge(self):
        # After action then neutral command: verification_done=True but
        # side_effect_pending still True (neutral doesn't clear it).
        # Nudge MUST still fire — only a true verify command clears the gate.
        runner = _make_runner()
        state = _make_state(side_effect_pending=True, verification_done=True)
        assert runner._should_inject_verify_nudge(state) is True

    def test_side_effect_cleared_by_verification_no_nudge(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=False, verification_done=True)
        assert runner._should_inject_verify_nudge(state) is False

    def test_file_mutation_triggers_nudge(self):
        runner = _make_runner()
        state = _make_state(file_mutations=[{"tool": "write_file", "path": "a.py", "ok": True}])
        assert runner._should_inject_verify_nudge(state) is True

    def test_doc_only_mutation_no_nudge(self):
        runner = _make_runner()
        state = _make_state(file_mutations=[{"tool": "write_file", "path": "README.md", "ok": True}])
        assert runner._should_inject_verify_nudge(state) is False

    def test_messaging_channel_no_nudge(self):
        runner = _make_runner(channel="feishu")
        state = _make_state(side_effect_pending=True)
        assert runner._should_inject_verify_nudge(state) is False

    def test_max_nudges_reached_no_more(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True, verify_nudges=99)
        assert runner._should_inject_verify_nudge(state) is False


# ---------------------------------------------------------------------------
# _build_verify_nudge
# ---------------------------------------------------------------------------

class TestBuildVerifyNudge:
    """Nudge text adapts to file edits vs side-effect actions."""

    def test_side_effect_nudge_mentions_post_condition(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True)
        text = runner._build_verify_nudge(state)
        assert "side-effecting" in text
        assert "post-condition" in text
        assert "systemctl is-active" in text
        assert state.verify_nudges == 1

    def test_file_edit_nudge_mentions_verify_command(self):
        runner = _make_runner()
        state = _make_state(
            file_mutations=[{"tool": "write_file", "path": "a.py", "ok": True}]
        )
        text = runner._build_verify_nudge(state)
        assert "pytest" in text
        assert "a.py" in text


# ---------------------------------------------------------------------------
# _maybe_inject_mutation_verifier_footer
# ---------------------------------------------------------------------------

class TestActionVerifierFooter:
    """Footer injection for unverified action-completion claims."""

    def test_action_claim_without_verification_injects_footer(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True, verification_done=False)
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "Gateway 已重启，服务正常运行。"
        )
        assert footer is not None
        assert "action-verification" in footer

    def test_action_claim_with_verification_done_no_footer(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=False, verification_done=True)
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "Gateway 已重启，服务正常运行。"
        )
        assert footer is None

    def test_action_claim_with_neutral_command_still_injects_footer(self):
        # After action then neutral command: verification_done=True but
        # side_effect_pending still True (neutral doesn't clear it).
        # Footer MUST still fire — only a true verify command clears the gate.
        runner = _make_runner()
        state = _make_state(side_effect_pending=True, verification_done=True)
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "Gateway 已重启，服务正常运行。"
        )
        assert footer is not None
        assert "action-verification" in footer

    def test_action_claim_without_side_effect_no_footer(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=False, verification_done=False)
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "Gateway 已重启，服务正常运行。"
        )
        assert footer is None

    def test_english_action_claim_injects_footer(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True, verification_done=False)
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "I've restarted the service successfully."
        )
        assert footer is not None

    def test_file_claim_without_mutation_still_injects_footer(self):
        runner = _make_runner()
        state = _make_state()
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "I've updated the file as requested."
        )
        assert footer is not None
        assert "file-mutation" in footer

    def test_file_claim_with_mutation_no_footer(self):
        runner = _make_runner()
        state = _make_state(
            file_mutations=[{"tool": "write_file", "path": "a.py", "ok": True}]
        )
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "I've updated the file as requested."
        )
        assert footer is None

    def test_no_claim_no_footer(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True)
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "The weather is nice today."
        )
        assert footer is None

    def test_empty_content_no_footer(self):
        runner = _make_runner()
        state = _make_state(side_effect_pending=True)
        assert runner._maybe_inject_mutation_verifier_footer(state, "") is None
        assert runner._maybe_inject_mutation_verifier_footer(state, None) is None


# ---------------------------------------------------------------------------
# Integration: action → nudge → verify → no nudge
# ---------------------------------------------------------------------------

class TestActionVerifyFlow:
    """End-to-end: action sets pending, verify clears it, nudge respects both."""

    def test_restart_then_is_active_clears_nudge_gate(self):
        runner = _make_runner()
        state = _make_state()

        # 1. Model runs `systemctl restart gateway` — action, not verify.
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl restart gateway"), ""
        )
        assert state.side_effect_pending is True
        assert state.verification_done is False
        assert runner._should_inject_verify_nudge(state) is True
        
        # 2. Nudge injected, model heeds it and runs `systemctl is-active gateway`.
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl is-active gateway"), ""
        )
        assert state.side_effect_pending is False
        assert state.verification_done is True
        assert runner._should_inject_verify_nudge(state) is False

    def test_restart_then_declare_done_triggers_footer(self):
        runner = _make_runner()
        state = _make_state()

        # Model runs restart but then declares done without verifying.
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl restart gateway"), ""
        )
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "Gateway 已重启完成。"
        )
        assert footer is not None
        assert "action-verification" in footer

    def test_restart_then_neutral_command_does_not_clear_gate(self):
        # Regression: a neutral command (cat/ls/echo) run after an action
        # must NOT satisfy the action-verify gate. Only a true verify
        # command (status/is-active/ps/logs) clears side_effect_pending.
        runner = _make_runner()
        state = _make_state()

        # 1. Action: restart
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl restart gateway"), ""
        )
        assert state.side_effect_pending is True
        assert state.verification_done is False
        assert runner._should_inject_verify_nudge(state) is True

        # 2. Neutral command: echo. Sets verification_done (file-edit
        #    path lenient default) but does NOT clear side_effect_pending.
        runner._record_verification_call(
            state, _tool_call("exec", "echo hello"), ""
        )
        assert state.verification_done is True
        assert state.side_effect_pending is True  # still pending

        # 3. Nudge still fires (side_effect_pending checked before
        #    verification_done short-circuit).
        assert runner._should_inject_verify_nudge(state) is True

        # 4. If model declares done anyway, footer still fires.
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "Gateway 已重启完成。"
        )
        assert footer is not None
        assert "action-verification" in footer

        # 5. True verify command finally clears the gate.
        runner._record_verification_call(
            state, _tool_call("exec", "systemctl is-active gateway"), ""
        )
        assert state.side_effect_pending is False
        assert state.verification_done is True
        assert runner._should_inject_verify_nudge(state) is False
        footer = runner._maybe_inject_mutation_verifier_footer(
            state, "Gateway 已重启完成。"
        )
        assert footer is None
