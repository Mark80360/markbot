"""Shell command classification for verify-on-stop accounting.

Distinguishes actions that produce side effects (restart/install/pull/kill)
from verifies that read state (status/ps/curl/logs). Conflating them causes
the "false completion" failure mode where ``systemctl restart X`` is treated
as verification when it is actually the thing needing verification.

This module owns the prefix tables and the classifier; the iteration runner
only consumes :func:`classify_shell_command`. Tables are data — extend them
here rather than special-casing in loop code.
"""

from __future__ import annotations

# (first_token, second_token) pairs. Empty second token ("") matches any
# second token. Matching is on the first segment of a chained command
# (before && || ; |) and after stripping env prefixes (VAR=val, sudo).
# Conservative: unrecognized commands default to "neutral".
_SHELL_ACTION_PREFIXES: frozenset[tuple[str, str]] = frozenset({
    ("systemctl", "restart"), ("systemctl", "start"), ("systemctl", "stop"),
    ("systemctl", "reload"), ("systemctl", "try-restart"),
    ("service", "restart"), ("service", "start"), ("service", "stop"),
    ("service", "reload"),
    ("launchctl", "kickstart"), ("launchctl", "load"), ("launchctl", "unload"),
    ("pip", "install"), ("pip3", "install"),
    ("npm", "install"), ("npm", "i"), ("npm", "add"),
    ("pnpm", "add"), ("pnpm", "install"), ("pnpm", "i"),
    ("yarn", "add"), ("yarn", "install"),
    ("bun", "add"), ("bun", "install"),
    ("brew", "install"), ("brew", "remove"), ("brew", "upgrade"),
    ("apt", "install"), ("apt-get", "install"),
    ("apt", "remove"), ("apt-get", "remove"),
    ("yum", "install"), ("dnf", "install"),
    ("git", "pull"), ("git", "push"), ("git", "checkout"),
    ("git", "merge"), ("git", "reset"), ("git", "rebase"),
    ("docker", "restart"), ("docker", "start"), ("docker", "stop"),
    ("docker", "rm"), ("docker", "kill"),
    ("pkill", ""), ("killall", ""),
    ("rm", ""), ("mv", ""), ("chmod", ""), ("chown", ""),
})

_SHELL_VERIFY_PREFIXES: frozenset[tuple[str, str]] = frozenset({
    ("systemctl", "is-active"), ("systemctl", "status"), ("systemctl", "is-enabled"),
    ("service", "status"),
    ("launchctl", "list"), ("launchctl", "print"),
    ("ps", ""), ("pgrep", ""), ("pidof", ""),
    ("ss", ""), ("netstat", ""), ("lsof", ""),
    ("curl", ""), ("wget", ""),
    ("journalctl", ""),
    ("docker", "ps"), ("docker", "logs"), ("docker", "inspect"), ("docker", "stats"),
    ("git", "log"), ("git", "status"), ("git", "diff"), ("git", "show"),
    ("pip", "show"), ("pip3", "show"),
    ("npm", "list"), ("npm", "ls"), ("npm", "info"),
    ("pnpm", "list"), ("pnpm", "ls"),
    ("cat", ""), ("head", ""), ("tail", ""), ("less", ""),
    ("ls", ""), ("find", ""), ("tree", ""),
    ("grep", ""), ("rg", ""),
    ("test", ""),
    ("pytest", ""), ("tox", ""), ("nox", ""),
    ("ruff", ""), ("flake8", ""), ("mypy", ""), ("pyright", ""),
    ("eslint", ""), ("tsc", ""),
    ("make", "test"), ("make", "check"), ("make", "verify"), ("make", "lint"),
    ("cargo", "test"), ("cargo", "check"), ("cargo", "clippy"),
    ("go", "test"), ("go", "vet"),
})

# Verify tools that may appear as a subcommand (e.g. `python -m pytest`,
# `python -m mypy`). When any of these appears as a token, the command is
# classified as verify. This is conservative — false-verify (marking a
# neutral command as verify) only sets verification_done=True, which is
# safe; false-action would be the dangerous case, and this set does not
# affect action classification.
_KNOWN_VERIFY_TOOLS: frozenset[str] = frozenset({
    "pytest", "tox", "nox", "unittest",
    "ruff", "flake8", "mypy", "pyright", "pylint", "isort", "black",
    "eslint", "tsc", "prettier",
    "jest", "vitest", "mocha", "karma",
})


def classify_shell_command(command: str) -> str:
    """Classify a shell command as 'action', 'verify', or 'neutral'.

    Used by verify-on-stop accounting to avoid treating action commands
    (restart/install/pull/kill) as verification. Treating an action as
    verification is the 'false completion' failure mode: the model runs
    ``systemctl restart X`` and the loop marks ``verification_done=True``,
    so the verify-on-stop nudge never fires even though nobody confirmed
    the service actually came up.

    Conservative: unrecognized commands return 'neutral' (neither flag is
    set by the caller's neutral branch, which preserves the existing
    ``verification_done = True`` behavior to avoid regressing the
    file-edit verify path — most exec calls in a coding session ARE
    verification like pytest).
    """
    if not command or not isinstance(command, str):
        return "neutral"
    cmd = command.strip()
    # Classify the first segment only (before && || ; |) — a chained
    # command is classified by its leading action.
    for sep in ("&&", "||", ";", "|"):
        if sep in cmd:
            cmd = cmd.split(sep, 1)[0].strip()
    tokens = cmd.split()
    # Strip leading env assignments: VAR=val cmd ...
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    # Strip command prefixes: sudo / env / time / noglob
    while tokens and tokens[0] in ("sudo", "env", "time", "noglob"):
        tokens = tokens[1:]
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]
    if not tokens:
        return "neutral"
    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""

    # `service` 命令格式是 `service <name> <action>`，action 在第三个
    # token（与 systemctl <action> <name> 相反）。特殊处理。
    if first == "service" and len(tokens) >= 3:
        third = tokens[2]
        if third in ("restart", "start", "stop", "reload", "force-reload"):
            return "action"
        if third in ("status",):
            return "verify"

    if (first, second) in _SHELL_ACTION_PREFIXES or (first, "") in _SHELL_ACTION_PREFIXES:
        return "action"
    if (first, second) in _SHELL_VERIFY_PREFIXES or (first, "") in _SHELL_VERIFY_PREFIXES:
        return "verify"

    # 已知 verify 工具作为子命令出现（如 `python -m pytest`、
    # `python -m mypy`）。保守判定：误判 neutral 为 verify 只会设
    # verification_done=True，是安全的；不会误判为 action。
    if any(t in _KNOWN_VERIFY_TOOLS for t in tokens):
        return "verify"

    return "neutral"
