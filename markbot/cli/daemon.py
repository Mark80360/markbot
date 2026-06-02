"""Daemon / pid-file helpers for the gateway and agent services.

Extracted from ``markbot.cli.commands``. Owns:

- pid-file location, read/write/cleanup
- cross-platform process liveness + termination
- foreground-to-daemon handoff (used by ``markbot gateway start``)

This module is public API surface (importable from other modules),
but its primary consumer is the ``markbot gateway`` group command.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer

from markbot.cli.ui import console

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROCESS_STOP_RETRIES = 10
PROCESS_STOP_WAIT_INTERVAL = 0.5
DAEMON_STARTUP_WAIT = 0.5

# ---------------------------------------------------------------------------
# Gateway / agent log + pid file locations
# ---------------------------------------------------------------------------
#
# Resolved lazily inside the helpers so the ``markbot.config.paths``
# import side-effects (creating the directories) only fire when needed.

def _gateway_paths():
    from markbot.config.paths import get_gateway_dir, get_logs_dir

    pid_dir = get_gateway_dir()
    return {
        "pid_dir": pid_dir,
        "pid_file": pid_dir / "gateway.pid",
        "log_file": pid_dir / "gateway.log",
        "agent_log_dir": get_logs_dir(),
        "agent_log_file": get_logs_dir() / "agent.log",
    }


# ---------------------------------------------------------------------------
# Pid file helpers
# ---------------------------------------------------------------------------


def ensure_pid_dir() -> None:
    paths = _gateway_paths()
    paths["pid_dir"].mkdir(parents=True, exist_ok=True)


def read_pid() -> int | None:
    paths = _gateway_paths()
    if not paths["pid_file"].exists():
        return None
    try:
        return int(paths["pid_file"].read_text().strip())
    except (ValueError, OSError):
        return None


def write_pid(pid: int) -> None:
    ensure_pid_dir()
    paths = _gateway_paths()
    paths["pid_file"].write_text(str(pid))


def remove_pid() -> None:
    paths = _gateway_paths()
    if paths["pid_file"].exists():
        paths["pid_file"].unlink()


def is_process_running(pid: int) -> bool:
    """Check if a process is running (cross-platform)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle == 0:
                return False

            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                kernel32.CloseHandle(handle)
                return exit_code.value == 259  # STILL_ACTIVE

            kernel32.CloseHandle(handle)
            return True
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False


def terminate_process(pid: int, force: bool = False) -> bool:
    """Terminate a process by PID (cross-platform).

    Args:
        pid: Process ID to terminate.
        force: If True, use SIGKILL (Unix) or force terminate (Windows).
              If False, use SIGTERM (Unix) or graceful terminate (Windows).

    Returns:
        True if process was terminated successfully, False otherwise.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)

            if handle == 0:
                return False

            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                if exit_code.value != 259:  # Not STILL_ACTIVE
                    kernel32.CloseHandle(handle)
                    return True

            kernel32.TerminateProcess(handle, 1 if force else 0)
            kernel32.CloseHandle(handle)
        else:
            if force:
                os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)

        for _ in range(PROCESS_STOP_RETRIES):
            time.sleep(PROCESS_STOP_WAIT_INTERVAL)
            if not is_process_running(pid):
                return True

        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Daemon start (foreground -> background handoff)
# ---------------------------------------------------------------------------


def start_daemon(port: int, workspace: str | None, config_path: str | None, verbose: bool) -> None:
    """Start the gateway as a daemon process (cross-platform)."""
    from rich.text import Text

    from markbot.cli.commands import _run_gateway_foreground

    ensure_pid_dir()
    paths = _gateway_paths()
    log_file_path = paths["log_file"]

    W = 72  # total width

    def section(title: str, color: str = "cyan") -> None:
        title_text = f"  {title}  "
        pad = W - len(title_text) - 2
        line = Text.from_markup(f"[{color}]{title_text}[/][dim]{'─' * pad}[/]")
        console.print(line)

    def kv(key: str, value: str, key_w: int = 14) -> None:
        line = Text.from_markup(f"  [cyan]{key:<{key_w}}[/cyan] {value}")
        console.print(line)

    def divider() -> None:
        line = Text.from_markup(f"[dim]{'─' * (W - 2)}[/]")
        console.print(line)

    console.print()

    if sys.platform == "win32":
        # Windows: use subprocess
        cmd = [
            sys.executable, "-m", "markbot",
            "gateway", "start",
            "--port", str(port),
            "--foreground",
        ]

        if workspace:
            cmd.extend(["--workspace", workspace])
        if config_path:
            cmd.extend(["--config", config_path])
        if verbose:
            cmd.append("--verbose")

        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )

        with open(log_file_path, "w") as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                cwd=str(Path.cwd()),
            )

        time.sleep(DAEMON_STARTUP_WAIT)

        if process.poll() is not None:
            section("Status", "red")
            console.print("  [red]✗[/red] Failed to start gateway daemon")
            console.print()
            raise typer.Exit(1)

        write_pid(process.pid)
        section("Status", "green")
        kv("State", "[bold green]● RUNNING[/bold green]")
        kv("PID", str(process.pid))
        divider()
        console.print()

    else:
        # Linux/Unix: use os.fork()
        pid = os.fork()
        if pid > 0:
            write_pid(pid)
            section("Status", "green")
            kv("State", "[bold green]● RUNNING[/bold green]")
            kv("PID", str(pid))
            kv("Log File", str(log_file_path))
            divider()
            console.print()
            return

        os.setsid()
        sys.stdin.close()
        ensure_pid_dir()
        with open(log_file_path, "w") as log_file:
            os.dup2(log_file.fileno(), sys.stdout.fileno())
            os.dup2(log_file.fileno(), sys.stderr.fileno())
        try:
            _run_gateway_foreground(port, workspace, config_path, verbose)
        except Exception as e:
            print(f"Gateway failed: {e}", file=sys.stderr)
            sys.exit(1)
