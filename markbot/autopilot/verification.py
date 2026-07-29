"""Verification gate — run configured verification commands and report results."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from markbot.autopilot.types import (
    VerificationCommand,
    VerificationPolicy,
    VerificationStep,
)

_SHELL_METACHARS = frozenset(";&|`$<>\n\r")


def parse_verification_entry(entry: Any) -> VerificationCommand:
    if isinstance(entry, dict):
        raw = str(entry.get("command", "")).strip()
        if not raw:
            return VerificationCommand(raw=str(entry), error="empty command")
        if bool(entry.get("shell", False)):
            return VerificationCommand(raw=raw, shell=True)
    elif isinstance(entry, str):
        raw = entry.strip()
        if not raw:
            return VerificationCommand(raw=entry, error="empty command")
    else:
        return VerificationCommand(
            raw=str(entry),
            error="entry must be a string or a mapping with a 'command' key",
        )

    if any(ch in _SHELL_METACHARS for ch in raw):
        return VerificationCommand(
            raw=raw,
            error=(
                "command contains shell metacharacters; use the mapping form "
                "{command: '...', shell: true} to opt in"
            ),
        )
    try:
        argv = tuple(shlex.split(raw))
    except ValueError as exc:
        return VerificationCommand(raw=raw, error=f"could not tokenize command: {exc}")
    if not argv:
        return VerificationCommand(raw=raw, error="empty command")
    return VerificationCommand(raw=raw, argv=argv, shell=False)


def _looks_available(command: str, cwd: Path) -> bool:
    lowered = command.lower()
    if lowered.startswith("uv "):
        return (cwd / "pyproject.toml").exists()
    if "ruff check" in lowered:
        return (cwd / "pyproject.toml").exists()
    if "pytest" in lowered:
        return (cwd / "tests").exists() or (cwd / "pyproject.toml").exists()
    if "npm" in lowered or "tsc" in lowered:
        return (cwd / "package.json").exists()
    return True


def build_verification_commands(
    policy: VerificationPolicy,
    cwd: Path,
) -> list[VerificationCommand]:
    configured = policy.commands
    parsed = [parse_verification_entry(entry) for entry in configured]
    selected: list[VerificationCommand] = []
    for cmd in parsed:
        if cmd.error is not None:
            selected.append(cmd)
            continue
        if _looks_available(cmd.raw, cwd):
            selected.append(cmd)
    return selected


async def run_verification_steps(
    policy: VerificationPolicy,
    *,
    cwd: Path,
    timeout: int = 1800,
) -> list[VerificationStep]:
    steps: list[VerificationStep] = []
    commands = build_verification_commands(policy, cwd)

    for cmd in commands:
        if cmd.error is not None:
            steps.append(
                VerificationStep(
                    command=cmd.raw,
                    returncode=-1,
                    status="error",
                    stderr=f"verification policy error: {cmd.error}",
                )
            )
            continue

        try:
            step = await _run_single_command(cmd, cwd=cwd, timeout=timeout)
            steps.append(step)
        except Exception as exc:
            steps.append(
                VerificationStep(
                    command=cmd.raw,
                    returncode=-1,
                    status="error",
                    stderr=str(exc),
                )
            )

    return steps


async def _run_single_command(
    cmd: VerificationCommand,
    *,
    cwd: Path,
    timeout: int,
) -> VerificationStep:
    """Execute a single verification command asynchronously."""
    if cmd.shell:
        process = await asyncio.create_subprocess_shell(
            cmd.raw,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *cmd.argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        return VerificationStep(
            command=cmd.raw,
            returncode=-1,
            status="error",
            stderr=f"Timed out after {timeout}s",
        )
    except asyncio.CancelledError:
        # Prevent zombie subprocess when parent task is cancelled
        # (autopilot shutdown, /stop, etc.)
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise
    except Exception:
        # Kill on any other unexpected exception (OSError, etc.) to
        # prevent zombie processes — same rationale as CancelledError.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

    rc = process.returncode if process.returncode is not None else -1
    return VerificationStep(
        command=cmd.raw,
        returncode=rc,
        status="success" if rc == 0 else "failed",
        stdout=stdout[-4000:],
        stderr=stderr[-4000:],
    )


def render_verification_report(
    card_title: str,
    card_id: str,
    steps: list[VerificationStep],
) -> str:
    lines = [
        f"# Verification Report: {card_id}",
        "",
        f"Title: {card_title}",
        "",
    ]
    if not steps:
        lines.append("No verification commands were applicable.")
        return "\n".join(lines).strip() + "\n"
    for step in steps:
        lines.extend(
            [
                f"## {step.status.upper()} :: {step.command}",
                "",
                f"Return code: {step.returncode}",
                "",
            ]
        )
        if step.stdout:
            lines.extend(["### stdout", "```text", step.stdout, "```", ""])
        if step.stderr:
            lines.extend(["### stderr", "```text", step.stderr, "```", ""])
    return "\n".join(lines).strip() + "\n"


def verification_passed(steps: list[VerificationStep]) -> bool:
    if not steps:
        # No verification commands were applicable — nothing was verified.
        # This is NOT a pass; treat it as a failure to prevent "false completion"
        # in unattended autopilot runs.
        return False
    return all(
        step.status in ("success", "skipped")
        for step in steps
    )
