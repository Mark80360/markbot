"""Daemon / pid-file helpers for the gateway and agent services.

Extracted from ``markbot.cli.commands``. Owns:

- pid-file location, read/write/cleanup
- cross-platform process liveness + termination
- foreground-to-daemon handoff (used by ``markbot gateway start``)
- restart sentinel coordination (agent self-restart support)

This module is public API surface (importable from other modules),
but its primary consumer is the ``markbot gateway`` group command.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer

from markbot.cli.ui import console, make_section_helpers

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
    from markbot.cli.groups.gateway import run_gateway_foreground

    ensure_pid_dir()
    paths = _gateway_paths()
    log_file_path = paths["log_file"]

    section, kv, divider = make_section_helpers()

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
            run_gateway_foreground(port, workspace, config_path, verbose)
        except Exception as e:
            print(f"Gateway failed: {e}", file=sys.stderr)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Restart sentinel — coordination for agent self-restart
# ---------------------------------------------------------------------------
#
# Why a sentinel file rather than letting the agent's exec tool run
# ``markbot gateway restart`` directly:
#
#   When the agent invokes ``markbot gateway restart`` via the exec tool,
#   the restart process becomes a child of the gateway process.  On
#   Windows, ``TerminateProcess`` on the gateway drags the restart
#   child down with it (shared console / pipe), so the "start" leg of
#   the restart never executes — the gateway dies but nothing brings it
#   back.
#
# The sentinel decouples the two phases:
#   1. Agent writes a sentinel file containing the restarter params.
#   2. The gateway's foreground loop polls for the sentinel; on sight
#      it spawns a *detached* restarter process (independent process
#      group / new session) and then exits gracefully through its
#      normal finally block (channels drained, handoff saved, etc).
#   3. The detached restarter waits for the old PID to disappear,
#      then calls ``markbot gateway start`` to bring the gateway back.


RESTART_SENTINEL_FILENAME = "restart.sentinel"
GATEWAY_PARAMS_FILENAME = "gateway.params.json"
RESTART_MARKER_FILENAME = "restart.marker"
RESTART_POLL_INTERVAL_S = 1.0
RESTART_RESTARTER_WAIT_S = 2.0
RESTART_RESTARTER_MAX_WAIT_S = 30.0
# If a restart marker is younger than this many seconds, the bootstrap
# layer treats the next session as a "post-restart resume" and injects
# a system hint nudging the agent to confirm continuation of the
# interrupted task.
RESTART_MARKER_FRESH_S = 600.0  # 10 minutes


def restart_marker_path() -> Path:
    paths = _gateway_paths()
    return paths["pid_dir"] / RESTART_MARKER_FILENAME


def write_restart_marker(reason: str = "") -> Path:
    """Drop a marker so the next gateway start knows it followed a restart.

    The marker carries a timestamp; the bootstrap layer reads it on
    startup and, if fresh, injects a "you were restarted mid-task"
    system hint into the next session's context.
    """
    ensure_pid_dir()
    path = restart_marker_path()
    path.write_text(
        json.dumps({"reason": reason, "written_at": time.time()}),
        encoding="utf-8",
    )
    return path


def consume_restart_marker() -> dict | None:
    """Read and delete the restart marker, returning its payload (or None)."""
    path = restart_marker_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = None
    try:
        path.unlink()
    except OSError:
        pass
    return payload


def sentinel_path() -> Path:
    paths = _gateway_paths()
    return paths["pid_dir"] / RESTART_SENTINEL_FILENAME


def gateway_params_path() -> Path:
    paths = _gateway_paths()
    return paths["pid_dir"] / GATEWAY_PARAMS_FILENAME


def write_gateway_params(
    *,
    port: int,
    workspace: str | None,
    config: str | None,
    verbose: bool,
) -> Path:
    """Persist the gateway's startup parameters.

    The detached restarter reads this file to know how to relaunch the
    gateway with the same arguments as the original invocation.  Written
    once at gateway start, never modified during the run.
    """
    ensure_pid_dir()
    path = gateway_params_path()
    payload = {
        "port": port,
        "workspace": workspace,
        "config": config,
        "verbose": verbose,
        "written_at": time.time(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def read_gateway_params() -> dict | None:
    """Return the persisted gateway startup parameters, or None."""
    path = gateway_params_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_restart_sentinel(
    *,
    reason: str = "",
    mode: str = "restart",
) -> Path:
    """Create a sentinel file instructing the gateway to self-restart.

    The sentinel only carries a reason/mode/timestamp; the actual restart
    parameters are read from ``gateway.params.json`` (written once at
    gateway start).  This keeps the writer side trivial — e.g. the
    exec tool interceptor doesn't need to know the gateway's port or
    workspace.

    ``mode`` is either ``"restart"`` (default — a detached restarter
    will bring the gateway back) or ``"stop"`` (gateway stays down
    after graceful shutdown).
    """
    ensure_pid_dir()
    path = sentinel_path()
    payload = {
        "mode": mode,
        "reason": reason,
        "written_at": time.time(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def read_restart_sentinel() -> dict | None:
    """Return the sentinel payload, or None if no sentinel is present."""
    path = sentinel_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def consume_restart_sentinel() -> dict | None:
    """Atomically read and delete the sentinel, returning its payload."""
    path = sentinel_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = None
    try:
        path.unlink()
    except OSError:
        pass
    return payload


def spawn_detached_restarter(payload: dict | None, old_pid: int | None) -> int | None:
    """Spawn a detached process that restarts the gateway after old_pid exits.

    The restarter is detached so it survives the gateway's death.  Its
    job is simple: poll old_pid, wait for it to disappear, then invoke
    ``markbot gateway start`` with the original parameters (read from
    ``gateway.params.json`` written at gateway start; the optional
    ``payload`` argument overrides those params if provided).

    Returns the restarter PID on success, None on failure.
    """
    # Prefer explicit override payload; fall back to the persisted params.
    if payload and "port" in payload:
        params = payload
    else:
        params = read_gateway_params() or {}
    port = params.get("port", 18790)
    workspace = params.get("workspace")
    config = params.get("config")
    verbose = bool(params.get("verbose", False))

    # Build a small inline restarter script.  We keep it portable
    # between Windows and POSIX by writing it as a Python one-liner
    # invoked via ``python -c``.
    restarter_code = (
        "import sys, time, subprocess, os\n"
        f"old_pid = {old_pid!r}\n"
        f"port = {port!r}\n"
        f"workspace = {workspace!r}\n"
        f"config = {config!r}\n"
        f"verbose = {verbose!r}\n"
        f"wait_s = {RESTART_RESTARTER_WAIT_S!r}\n"
        f"max_wait_s = {RESTART_RESTARTER_MAX_WAIT_S!r}\n"
        "deadline = time.time() + max_wait_s\n"
        "def alive(pid):\n"
        "    if not pid: return False\n"
        "    if sys.platform == 'win32':\n"
        "        import ctypes\n"
        "        from ctypes import wintypes\n"
        "        k = ctypes.windll.kernel32\n"
        "        h = k.OpenProcess(0x1000, False, pid)\n"
        "        if not h: return False\n"
        "        ec = wintypes.DWORD()\n"
        "        got = k.GetExitCodeProcess(h, ctypes.byref(ec))\n"
        "        k.CloseHandle(h)\n"
        "        return bool(got) and ec.value == 259\n"
        "    try:\n"
        "        os.kill(pid, 0)\n"
        "        return True\n"
        "    except OSError:\n"
        "        return False\n"
        "while alive(old_pid) and time.time() < deadline:\n"
        "    time.sleep(0.5)\n"
        "time.sleep(wait_s)\n"
        "cmd = [sys.executable, '-m', 'markbot', 'gateway', 'start',\n"
        "       '--port', str(port)]\n"
        "if workspace: cmd.extend(['--workspace', workspace])\n"
        "if config: cmd.extend(['--config', config])\n"
        "if verbose: cmd.append('--verbose')\n"
        "subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    )

    cmd = [sys.executable, "-c", restarter_code]

    try:
        if sys.platform == "win32":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        return proc.pid
    except Exception:
        return None
