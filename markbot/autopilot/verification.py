"""Verification gate — run configured verification commands and report results."""

from __future__ import annotations

import shlex
import subprocess
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


def run_verification_steps(
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

        target: str | list[str] = cmd.raw if cmd.shell else list(cmd.argv)
        try:
            completed = subprocess.run(
                target,
                cwd=cwd,
                shell=cmd.shell,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            steps.append(
                VerificationStep(
                    command=cmd.raw,
                    returncode=completed.returncode,
                    status="success" if completed.returncode == 0 else "failed",
                    stdout=(completed.stdout or "")[-4000:],
                    stderr=(completed.stderr or "")[-4000:],
                )
            )
        except FileNotFoundError as exc:
            steps.append(
                VerificationStep(
                    command=cmd.raw,
                    returncode=-1,
                    status="error",
                    stderr=f"executable not found: {exc}",
                )
            )
        except subprocess.TimeoutExpired as exc:
            steps.append(
                VerificationStep(
                    command=cmd.raw,
                    returncode=-1,
                    status="error",
                    stdout=str(getattr(exc, "stdout", ""))[-4000:],
                    stderr=f"Timed out after {exc.timeout}s",
                )
            )
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
        return True
    return all(
        step.status in ("success", "skipped")
        for step in steps
    )
