"""Shell execution tool with enhanced security."""

import asyncio
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.tools.base import Tool
from markbot.utils.constants import DANGEROUS_COMMAND_PATTERNS
from markbot.utils.helpers import strip_ansi


class ExecTool(Tool):
    """Tool to execute shell commands."""

    _is_destructive = True

    # Rate limiting: max commands per session in a sliding window
    _RATE_WINDOW_S = 60
    _MAX_COMMANDS_PER_WINDOW = 30

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        allowed_internal_ips: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or DANGEROUS_COMMAND_PATTERNS
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.allowed_internal_ips = allowed_internal_ips or []
        self._cmd_timestamps: defaultdict[str, list[float]] = defaultdict(list)

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def _legacy_execute(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, **kwargs: Any,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        rate_error = self._check_rate_limit()
        if rate_error:
            return rate_error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                finally:
                    if sys.platform != "win32":
                        try:
                            os.waitpid(process.pid, os.WNOHANG)
                        except (ProcessLookupError, ChildProcessError) as e:
                            logger.debug("Process already reaped or not found: {}", e)
                return f"Error: Command timed out after {effective_timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(strip_ansi(stdout.decode("utf-8", errors="replace")))

            if stderr:
                stderr_text = strip_ansi(stderr.decode("utf-8", errors="replace"))
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Head + tail truncation to preserve both start and end of output
            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except Exception as e:
            # Log full error details but only return generic message in production
            logger.error("Shell command execution failed: {}", str(e))
            error_msg = str(e)
            if os.environ.get("MARKBOT_PRODUCTION", "").lower() in ("1", "true", "yes"):
                return "Error: Command execution failed. Please check the command and try again."
            return f"Error executing command: {error_msg}"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        curl_wget_error = self._validate_curl_wget_urls(cmd)
        if curl_wget_error:
            return curl_wget_error

        if self.restrict_to_workspace:
            cwd_path = Path(cwd).resolve()

            for raw in self._extract_all_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser()
                    if not p.is_absolute():
                        p = cwd_path / p
                    p = p.resolve()
                except Exception:
                    continue
                if cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    def _check_rate_limit(self) -> str | None:
        """Rate-limit shell commands to prevent abuse."""
        now = time.time()
        session_key = getattr(self, "_session_key", "default")
        timestamps = self._cmd_timestamps[session_key]

        # Prune expired entries
        cutoff = now - self._RATE_WINDOW_S
        self._cmd_timestamps[session_key] = [t for t in timestamps if t > cutoff]
        timestamps = self._cmd_timestamps[session_key]

        if len(timestamps) >= self._MAX_COMMANDS_PER_WINDOW:
            return (
                f"Error: Rate limit exceeded ({self._MAX_COMMANDS_PER_WINDOW} commands "
                f"per {self._RATE_WINDOW_S}s). Please wait and retry."
            )

        timestamps.append(now)
        return None

    def _validate_curl_wget_urls(self, command: str) -> str | None:
        """Validate URLs in curl/wget commands target only external hosts."""
        from markbot.utils.network import contains_internal_url

        lower = command.lower()
        if not (lower.startswith("curl") or lower.startswith("wget")):
            return None

        if contains_internal_url(command, self.allowed_internal_ips):
            return "Error: Command blocked by safety guard (internal/private URL in curl/wget)"

        return None

    @staticmethod
    def _looks_like_path(s: str) -> bool:
        s = s.strip()
        if not s:
            return False
        if re.match(r'[A-Za-z]:[\\/]', s):
            return True
        if s.startswith('/'):
            return True
        if s.startswith('~'):
            return True
        if re.search(r'(?:^|[/\\])\.\.(?:$|[/\\])', s):
            return True
        return False

    @staticmethod
    def _extract_all_paths(command: str) -> list[str]:
        paths = []
        quoted_re = re.compile(r'''["']([^"']+)["']''')

        for m in quoted_re.finditer(command):
            content = m.group(1).strip()
            if ExecTool._looks_like_path(content):
                paths.append(content)

        remaining = quoted_re.sub(' ', command)

        win_paths = re.findall(r'[A-Za-z]:[\\/][^\s\"\'|><;]+', remaining)
        posix_paths = re.findall(r'(?:^|[\s|>])(/[^\s\"\'|><;]+)', remaining)
        home_paths = re.findall(r'(?:^|[\s|>])(~/[^\s\"\'|><;]*)', remaining)
        rel_traversal = re.findall(
            r'(?:^|[\s|>])([^\s\"\'|><;]*[/\\]\.\.(?:[/\\][^\s\"\'|><;]*)*)',
            remaining,
        )

        paths.extend(win_paths + posix_paths + home_paths + rel_traversal)
        return paths
