"""Sandbox execution environment for skill scripts.

Provides a restricted execution environment with resource limits,
file access control, and timeout management.
"""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from loguru import logger

# Resource module is Unix-only
try:
    import resource
except ImportError:
    resource = None

# Windows: flag to suppress the console window that subprocess.Popen would
# otherwise pop up for every child process. 0x08000000 == CREATE_NO_WINDOW.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Environment variables considered safe to inherit into sandboxed children.
# Secrets (API keys, tokens, passwords) are intentionally excluded — skills
# must declare any needed env via SandboxConfig.environment.
_SAFE_INHERITED_ENV_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TMPDIR", "TEMP", "TMP", "SHELL", "TZ", "PWD",
    "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",  # macOS dyld
    "LD_LIBRARY_PATH",  # Linux ld.so
})

# Substrings that mark an env var as sensitive — never inherited even if it
# appears in the safe set (belt-and-suspenders with the allowlist above).
_SENSITIVE_ENV_SUBSTRINGS: tuple[str, ...] = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
    "CREDENTIAL", "API_KEY", "ACCESS_KEY",
    "PRIVATE_KEY", "BEARER",
)


def _decode_output(data: bytes | None) -> str:
    """Decode subprocess bytes with a utf-8 → locale fallback chain.

    ``decode("utf-8", errors="replace")`` silently replaces every
    non-ASCII byte with U+FFFD, which on Chinese-Windows child processes
    (GBK / CP936 locales) turns meaningful output into rows of ``.
    We instead try utf-8 strictly, then the two common Windows East-Asian
    codepages, then latin-1 (always succeeds), and only fall back to
    lossy ``replace`` as a last resort. ``None`` / empty → ``""``.
    """
    if not data:
        return ""
    if isinstance(data, str):
        return data
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# Type alias for an async per-line callback used by streaming execution.
OutputLineCallback = Callable[[str], Awaitable[None]]


@dataclass
class SandboxConfig:
    """Configuration for sandbox execution."""

    allowed_paths: list[str] = field(default_factory=list)
    network: bool = False
    timeout: int = 30
    max_cpu_time: int = 10
    max_memory_mb: int = 256
    max_files: int = 100
    environment: dict[str, str] = field(default_factory=dict)
    # When True, the python preamble (encoding patch + CREATE_NO_WINDOW
    # recursion + ImportError hint) is prepended to every python script
    # executed via run(). Defaults to True so run_code gets the patches
    # out of the box; SkillTool may opt out by setting it False.
    inject_preamble: bool = True
    # Grace period (seconds) for the stderr drain task to finish after
    # stdout streaming ends. A chatty child (e.g. ``pip install``) may
    # keep emitting on stderr for several seconds after the last stdout
    # line; the previous hard-coded 5s killed the process too eagerly on
    # slow networks. Exposed here so operators can tune it per workload.
    stderr_drain_timeout_s: float = 15.0
    # When True (default), attempt OS-level isolation before running the
    # child: macOS ``sandbox-exec`` denies network and restricts file
    # writes; Linux ``bwrap`` unshares the network namespace and bind-mounts
    # the filesystem read-only except for cwd/tmp. If the platform tool is
    # unavailable, the run degrades to PATH restriction (the legacy
    # ``network`` flag) and ``ExecutionResult.isolation_level`` reflects
    # ``"degraded"`` so callers know true isolation was not applied.
    isolate: bool = True

    def resolve_workspace_path(self, workspace: Path) -> None:
        """Resolve {workspace} placeholder in allowed_paths."""
        self.allowed_paths = [
            p.replace("{workspace}", str(workspace)) for p in self.allowed_paths
        ]


@dataclass
class ExecutionResult:
    """Result of sandboxed script execution."""

    success: bool
    stdout: str
    stderr: str
    exit_code: Optional[int]
    execution_time_ms: float
    # "os" = OS-level isolation (sandbox-exec / bwrap) was applied.
    # "degraded" = OS tool unavailable; fell back to PATH restriction only.
    # "none" = isolation disabled (isolate=False).
    isolation_level: str = "none"


class Sandbox:
    """Sandbox environment for executing skill scripts.

    Provides:
    - Resource limits (CPU, memory, files)
    - File access restrictions
    - Network isolation
    - Timeout enforcement

    Example:
        config = SandboxConfig(
            allowed_paths=["/tmp", "/workspace"],
            network=False,
            timeout=60
        )
        sandbox = Sandbox(config)
        result = await sandbox.run(
            script=Path("scripts/analyze.py"),
            language="python",
            args={"target": "src/"}
        )
    """

    SUPPORTED_LANGUAGES = {
        "python": "python",
        "py": "python",
        "shell": "bash",
        "sh": "bash",
        "bash": "bash",
        "node": "node",
        "nodejs": "node",
        "js": "node",
    }

    # Cache for OS isolation tool availability: tool_name -> resolved path
    # or "" (checked and unavailable). Absent key means unchecked.
    _isolation_tool_cache: dict[str, str] = {}

    def __init__(self, config: Optional[Union[SandboxConfig, dict]] = None):
        """Initialize sandbox with configuration."""
        if config is None:
            self.config = SandboxConfig()
        elif isinstance(config, dict):
            self.config = SandboxConfig(**config)
        else:
            self.config = config

    def _get_interpreter(self, language: str) -> str:
        """Get interpreter command for a language."""
        lang = self.SUPPORTED_LANGUAGES.get(language.lower(), language.lower())

        if lang == "python":
            return sys.executable
        elif lang == "bash":
            if os.name == "nt":
                import shutil
                bash_path = shutil.which("bash")
                if bash_path:
                    return bash_path
                raise ValueError(
                    "Shell scripts are not supported on Windows without Git Bash or WSL. "
                    "Install Git for Windows (git-scm.com) to enable bash script execution."
                )
            return "bash"
        elif lang == "node":
            return "node"
        else:
            raise ValueError(f"Unsupported language: {language}")

    def _build_command(
        self, script: Path, language: str, args: dict
    ) -> tuple[list[str], dict[str, str]]:
        """Build execution command and environment."""
        interpreter = self._get_interpreter(language)

        if language.lower() in ("shell", "sh", "bash"):
            # For shell scripts, pass args as environment variables
            cmd = [interpreter, str(script)]
            env = {f"SCRIPT_ARG_{k.upper()}": str(v) for k, v in args.items()}
        else:
            # For other languages, pass args as JSON
            import json

            cmd = [interpreter, str(script)]
            if args:
                cmd.append(json.dumps(args))
            env = {}

        # Merge with config environment
        env.update(self.config.environment)

        return cmd, env

    def _resolve_language(self, language: str) -> str:
        """Map any accepted language alias to its canonical interpreter key."""
        return self.SUPPORTED_LANGUAGES.get(language.lower(), language.lower())

    async def run(
        self,
        script: Path,
        language: str,
        args: dict,
        cwd: Optional[Path] = None,
        on_output_line: Optional[OutputLineCallback] = None,
    ) -> ExecutionResult:
        """Execute a script in the sandbox.

        Args:
            script: Path to the script file.
            language: Script language (python, shell, node).
            args: Arguments to pass to the script.
            cwd: Working directory for execution.
            on_output_line: Optional async callback invoked once per line
                of stdout as soon as it is produced. Enables live progress
                reporting for long-running scripts (pip install, training).
                When None, behaves like the historical blocking
                ``communicate()`` call.

        Returns:
            ExecutionResult with output and status.

        Implementation notes:
          * Python scripts are executed with the sandbox preamble
            (``SandboxConfig.inject_preamble``) prepended via a throwaway
            temp file, so the user's original ``script`` file on disk is
            never mutated and security scanning (which runs against the
            original) is unaffected.
          * Child processes on Windows are created with CREATE_NO_WINDOW
            so console popups do not steal focus.
          * Output bytes are decoded via :func:`_decode_output` instead
            of a lossy ``utf-8/replace``.
        """
        import time

        start_time = time.perf_counter()

        if not script.exists():
            return ExecutionResult(
                success=False,
                stdout="",
                stderr=f"Script not found: {script}",
                exit_code=-1,
                execution_time_ms=0,
            )

        # If injecting the preamble, build a temp script = preamble + user code.
        # The original script path is still what scanning/access-checks used.
        effective_script = script
        preamble_tmp: Optional[Path] = None
        canonical_lang = self._resolve_language(language)
        if canonical_lang == "python" and self.config.inject_preamble:
            try:
                from markbot.skills.core.preamble import PYTHON_PREAMBLE

                user_code = script.read_text(encoding="utf-8", errors="replace")
                combined = PYTHON_PREAMBLE + "\n" + user_code
                import tempfile

                fd, tmp_name = tempfile.mkstemp(
                    prefix="mb_preamble_", suffix=".py", dir=str(script.parent)
                )
                os.close(fd)
                preamble_tmp = Path(tmp_name)
                preamble_tmp.write_text(combined, encoding="utf-8")
                effective_script = preamble_tmp
            except Exception:
                # Preamble injection is best-effort; fall back to the
                # original script if anything goes wrong.
                preamble_tmp = None
                effective_script = script

        # Build command using the (possibly preamble-wrapped) script path.
        cmd, env = self._build_command(effective_script, language, args)
        cwd = cwd or script.parent

        # Prepare environment — inherit only safe, non-sensitive variables.
        # Secrets (API keys, tokens) are NOT forwarded so a compromised skill
        # script cannot exfiltrate them via os.environ. Skills that need a
        # specific env var must declare it via SandboxConfig.environment.
        process_env = {
            k: v for k, v in os.environ.items()
            if k in _SAFE_INHERITED_ENV_KEYS
            and not any(s in k.upper() for s in _SENSITIVE_ENV_SUBSTRINGS)
        }
        process_env.update(env)
        process_env["SANDBOX_MODE"] = "1"

        # Attempt OS-level isolation (macOS sandbox-exec / Linux bwrap).
        # When applied, this denies network access and restricts file
        # writes at the kernel level — far stronger than PATH restriction.
        cmd, isolation_level = self._build_isolated_command(
            cmd, cwd, effective_script
        )
        if isolation_level == "degraded":
            logger.warning(
                "OS isolation requested but tool unavailable; "
                "falling back to PATH restriction only. "
                "Install sandbox-exec (macOS, built-in) or bwrap (Linux) "
                "for true network/filesystem isolation."
            )

        # Apply PATH restrictions only when OS isolation was NOT applied.
        # OS isolation already denies network at the kernel level, so PATH
        # clamping is redundant there and can break interpreter resolution
        # inside the sandbox namespace.
        if isolation_level != "os" and not self.config.network:
            interpreter_dir = str(Path(cmd[0]).parent.resolve()) if os.path.isfile(cmd[0]) else ""

            if os.name == "nt":
                sys_dir = r"C:\Windows\System32"
                win_dir = r"C:\Windows"
                paths = [p for p in [sys_dir, win_dir, interpreter_dir] if p]
                process_env["PATH"] = ";".join(paths)
            else:
                essential = ["/usr/bin", "/usr/local/bin", "/bin", "/sbin"]
                if interpreter_dir:
                    essential.insert(0, interpreter_dir)
                process_env["PATH"] = ":".join(essential)

        try:
            def _set_child_limits() -> None:
                if os.name == "nt" or resource is None or not hasattr(resource, "setrlimit"):
                    return
                try:
                    # CPU time (soft, hard) — hard is soft+5s grace for SIGXCPU.
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (self.config.max_cpu_time, self.config.max_cpu_time + 5),
                    )
                    # Memory. RLIMIT_AS on macOS is unreliable (breaks dyld
                    # and malloc), so use RLIMIT_RSS (advisory) there; Linux
                    # keeps the hard virtual-address cap via RLIMIT_AS.
                    max_memory = self.config.max_memory_mb * 1024 * 1024
                    if sys.platform == "darwin":
                        rlimit_mem = getattr(resource, "RLIMIT_RSS", None)
                        if rlimit_mem is not None:
                            resource.setrlimit(rlimit_mem, (max_memory, max_memory))
                    else:
                        resource.setrlimit(resource.RLIMIT_AS, (max_memory, max_memory))
                    # Open file descriptors — previously a dead config field.
                    rlimit_nofile = getattr(resource, "RLIMIT_NOFILE", None)
                    if rlimit_nofile is not None and self.config.max_files > 0:
                        try:
                            _, cur_hard = resource.getrlimit(rlimit_nofile)
                        except (ValueError, OSError):
                            cur_hard = self.config.max_files
                        target = min(self.config.max_files, cur_hard)
                        resource.setrlimit(rlimit_nofile, (target, cur_hard))
                except (ValueError, OSError):
                    pass

            # CREATE_NO_WINDOW keeps child consoles from popping up on Windows.
            creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0

            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                creationflags=creationflags,
                preexec_fn=_set_child_limits if os.name != "nt" else None,
            )

            # Wait with timeout. When a streaming callback is provided we read
            # stdout line-by-line so progress can flow to the bus / UI; stderr
            # is drained concurrently to avoid pipe deadlock on chatty programs.
            try:
                stdout_data, stderr_data = await self._read_with_optional_stream(
                    process, on_output_line, self.config.timeout, self.config.stderr_drain_timeout_s
                )

                execution_time = (time.perf_counter() - start_time) * 1000

                return ExecutionResult(
                    success=process.returncode == 0,
                    stdout=_decode_output(stdout_data),
                    stderr=_decode_output(stderr_data),
                    exit_code=process.returncode,
                    execution_time_ms=execution_time,
                    isolation_level=isolation_level,
                )

            except asyncio.TimeoutError:
                # Kill the process if timeout
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass

                execution_time = (time.perf_counter() - start_time) * 1000

                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr=f"Script execution timed out after {self.config.timeout}s",
                    exit_code=-1,
                    execution_time_ms=execution_time,
                    isolation_level=isolation_level,
                )

        except Exception as e:
            execution_time = (time.perf_counter() - start_time) * 1000

            return ExecutionResult(
                success=False,
                stdout="",
                stderr=f"Failed to execute script: {e}",
                exit_code=-1,
                execution_time_ms=execution_time,
                isolation_level=isolation_level,
            )
        finally:
            # Clean up the preamble temp file if we created one.
            if preamble_tmp is not None:
                try:
                    preamble_tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    async def _read_with_optional_stream(
        process: asyncio.subprocess.Process,
        on_output_line: Optional[OutputLineCallback],
        timeout: int,
        stderr_drain_timeout_s: float = 15.0,
    ) -> tuple[bytes, bytes]:
        """Collect process output, optionally streaming stdout line-by-line.

        Without a callback this collapses to ``communicate()`` semantics.
        With a callback, stdout is read incrementally so each line can be
        emitted as it is produced (live progress); stderr is read in full
        by a sibling task to prevent the pipe buffer from deadlocking the
        child. A single ``wait_for`` wraps both so the configured timeout
        still governs the whole read.

        ``stderr_drain_timeout_s`` is the grace period given to the stderr
        drain task *after* stdout streaming has ended, so a child that
        keeps writing to stderr (e.g. ``pip install`` progress bars) is
        not killed prematurely.
        """
        if on_output_line is None:
            return await asyncio.wait_for(process.communicate(), timeout=timeout)

        async def _drain_stderr() -> bytes:
            # communicate() would also read stdout, which we're doing ourselves,
            # so read stderr directly. Empty/None-safe via _decode_output later.
            try:
                data = await process.stderr.read()
            except Exception:
                data = b""
            return data or b""

        async def _stream_stdout() -> bytes:
            assert process.stdout is not None
            buf = bytearray()
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                buf.extend(line)
                try:
                    await on_output_line(_decode_output(line).rstrip("\r\n"))
                except Exception:
                    # A callback failure must never abort the run.
                    pass
            return bytes(buf)

        stderr_task = asyncio.ensure_future(_drain_stderr())
        stdout_bytes = await asyncio.wait_for(_stream_stdout(), timeout=timeout)
        # Process has finished producing stdout; await stderr + exit code.
        try:
            stderr_bytes = await asyncio.wait_for(stderr_task, timeout=stderr_drain_timeout_s)
        except asyncio.TimeoutError:
            stderr_bytes = b""
        try:
            await asyncio.wait_for(process.wait(), timeout=stderr_drain_timeout_s)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        return stdout_bytes, stderr_bytes

    def validate_script_access(self, script: Path) -> tuple[bool, str]:
        """Validate that a script is within allowed paths."""
        if not self.config.allowed_paths:
            return True, ""

        script_resolved = script.resolve()

        for allowed in self.config.allowed_paths:
            allowed_path = Path(allowed).resolve()
            try:
                script_resolved.relative_to(allowed_path)
                return True, ""
            except ValueError:
                continue

        return False, f"Script {script} is outside allowed paths: {self.config.allowed_paths}"

    # ------------------------------------------------------------------
    # OS-level isolation (macOS sandbox-exec / Linux bwrap)
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_isolation_tool(cls, tool: str) -> str | None:
        """Resolve *tool* to its path, with a one-time dry-run check.

        Returns the executable path if the tool is available and functional,
        otherwise ``None``. The result is cached per-tool for the process
        lifetime so the dry-run cost is paid only once.

        ``sandbox-exec`` on macOS may exist but fail at ``sandbox_apply``
        due to SIP or missing entitlements; ``bwrap`` on Linux may fail if
        unprivileged user namespaces are disabled. A no-op invocation
        catches both cases so we degrade gracefully instead of crashing.
        """
        cached = cls._isolation_tool_cache.get(tool)
        if cached:
            return cached
        if cached == "":
            return None

        import shutil
        import subprocess

        path = shutil.which(tool)
        available = False
        if path is not None:
            try:
                if tool == "sandbox-exec":
                    result = subprocess.run(
                        [path, "-p", "(version 1)\n(allow default)"],
                        capture_output=True, timeout=3,
                    )
                    available = result.returncode == 0
                elif tool == "bwrap":
                    result = subprocess.run(
                        [path, "--unshare-net", "--ro-bind", "/", "/", "true"],
                        capture_output=True, timeout=3,
                    )
                    available = result.returncode == 0
            except Exception:
                available = False

        if available and path:
            cls._isolation_tool_cache[tool] = path
            return path

        cls._isolation_tool_cache[tool] = ""
        if path is not None:
            logger.warning(
                "{} found at {} but failed dry-run availability check; "
                "OS isolation disabled for this process. "
                "Falling back to PATH restriction.",
                tool, path,
            )
        else:
            logger.info(
                "{} not installed; OS isolation unavailable. "
                "Install {} for true network/filesystem isolation.",
                tool,
                "bubblewrap (apt install bubblewrap)" if tool == "bwrap"
                else "sandbox-exec (macOS built-in)",
            )
        return None

    def _build_isolated_command(
        self, cmd: list[str], cwd: Path, script_path: Path
    ) -> tuple[list[str], str]:
        """Wrap *cmd* with OS-level isolation if configured and available.

        Returns ``(wrapped_cmd, isolation_level)`` where *isolation_level*
        is:

        * ``"os"`` — isolation applied (sandbox-exec or bwrap available).
        * ``"degraded"`` — tool unavailable; caller should fall back to
          PATH restriction via the legacy ``network`` flag.
        * ``"none"`` — ``isolate=False`` or Windows (no isolation attempted).
        """
        if not self.config.isolate or os.name == "nt":
            return cmd, "none"

        writable = self._writable_paths_for(cwd, script_path)

        if sys.platform == "darwin":
            sandbox_exec = self._resolve_isolation_tool("sandbox-exec")
            if sandbox_exec is None:
                return cmd, "degraded"
            profile = self._build_sandbox_exec_profile(writable)
            return [sandbox_exec, "-p", profile] + cmd, "os"

        if sys.platform.startswith("linux"):
            bwrap = self._resolve_isolation_tool("bwrap")
            if bwrap is None:
                return cmd, "degraded"
            args = self._build_bwrap_args(writable)
            return [bwrap] + args + cmd, "os"

        return cmd, "degraded"

    @staticmethod
    def _writable_paths_for(cwd: Path, script_path: Path) -> list[str]:
        """Collect paths the child must be able to write to.

        Includes the working directory, the script's parent (for preamble
        temp files), and system temp directories (python ``tempfile``,
        pip cache, etc.).
        """
        import tempfile

        paths: set[str] = set()
        try:
            paths.add(str(cwd.resolve()))
        except (OSError, RuntimeError):
            paths.add(str(cwd))
        try:
            script_parent = script_path.parent.resolve()
            paths.add(str(script_parent))
        except (OSError, RuntimeError):
            pass
        try:
            paths.add(tempfile.gettempdir())
        except Exception:
            pass
        for t in ("/tmp", "/private/tmp", "/var/tmp"):
            if Path(t).exists():
                paths.add(t)
        return sorted(paths)

    @staticmethod
    def _build_sandbox_exec_profile(writable_paths: list[str]) -> str:
        """Build a macOS ``sandbox-exec`` profile.

        * Denies **all** network access.
        * Allows file reads everywhere (interpreter deps, stdlib, etc.).
        * Denies file writes by default, then re-allows only the working
          directory, script directory, and system temp dirs.
        """
        lines = [
            "(version 1)",
            "(deny network*)",
            "(allow file-read*)",
            "(deny file-write*)",
            "(allow process*)",
            "(allow sysctl*)",
            "(allow signal)",
            "(allow ipc-posix*)",
        ]
        for p in writable_paths:
            # Escape backslashes and double quotes for the profile string.
            safe = p.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'(allow file-write* (subpath "{safe}"))')
        return "\n".join(lines)

    @staticmethod
    def _build_bwrap_args(writable_paths: list[str]) -> list[str]:
        """Build ``bubblewrap`` args for Linux.

        * ``--unshare-net`` — isolated network namespace (no network).
        * ``--ro-bind / /`` — root filesystem read-only.
        * ``--bind <p> <p>`` — specific writable bind-mounts (cwd, tmp).
        * ``--dev`` / ``--proc`` / ``--tmpfs /run`` — standard mounts.
        """
        args = [
            "--unshare-net",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/run",
        ]
        for p in writable_paths:
            if Path(p).exists():
                args.extend(["--bind", p, p])
        return args
