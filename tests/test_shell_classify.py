"""Tests for shell command classification (extracted from iteration.py)."""

from markbot.agent.shell_classify import classify_shell_command


class TestActionClassification:
    def test_service_restart_is_action(self):
        assert classify_shell_command("systemctl restart nginx") == "action"

    def test_install_is_action(self):
        assert classify_shell_command("pip install requests") == "action"
        assert classify_shell_command("npm i") == "action"

    def test_git_mutations_are_action(self):
        assert classify_shell_command("git push origin main") == "action"
        assert classify_shell_command("git reset --hard HEAD~1") == "action"

    def test_kill_and_delete_are_action(self):
        assert classify_shell_command("pkill -f myserver") == "action"
        assert classify_shell_command("rm -rf build/") == "action"

    def test_service_third_token_format(self):
        # `service <name> <action>` — action is the third token
        assert classify_shell_command("service nginx restart") == "action"
        assert classify_shell_command("service nginx status") == "verify"


class TestVerifyClassification:
    def test_status_commands_are_verify(self):
        assert classify_shell_command("systemctl status nginx") == "verify"
        assert classify_shell_command("docker ps") == "verify"
        assert classify_shell_command("git status") == "verify"

    def test_test_runners_are_verify(self):
        assert classify_shell_command("pytest tests/") == "verify"
        assert classify_shell_command("ruff check .") == "verify"
        assert classify_shell_command("make test") == "verify"

    def test_verify_tool_as_subcommand(self):
        assert classify_shell_command("python -m pytest -q") == "verify"
        assert classify_shell_command("python -m mypy src") == "verify"


class TestChainingAndPrefixes:
    def test_chained_command_uses_first_segment(self):
        assert classify_shell_command("systemctl restart x && systemctl status x") == "action"

    def test_env_prefixes_stripped(self):
        assert classify_shell_command("FOO=bar pytest -q") == "verify"

    def test_sudo_stripped(self):
        assert classify_shell_command("sudo systemctl restart nginx") == "action"


class TestNeutralFallback:
    def test_unknown_command_neutral(self):
        assert classify_shell_command("./deploy.sh --force") == "neutral"

    def test_empty_neutral(self):
        assert classify_shell_command("") == "neutral"
        assert classify_shell_command(None) == "neutral"
