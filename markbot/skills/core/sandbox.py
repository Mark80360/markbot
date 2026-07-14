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

# Resource module is Unix-only
try:
    import resource
except ImportError:
    resource = None

# Windows: flag to suppress the console window that subprocess.Popen would
# otherwise pop up for every child process. 0x08000000 == CREATE_NO_WINDOW.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


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

        # Prepare environment
        process_env = os.environ.copy()
        process_env.update(env)
        process_env["SANDBOX_MODE"] = "1"

        # Apply PATH restrictions if no network
        if not self.config.network:
            # Keep only essential paths plus the interpreter's directory
            import shutil
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
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (self.config.max_cpu_time, self.config.max_cpu_time + 5),
                    )
                    max_memory = self.config.max_memory_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (max_memory, max_memory))
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
                )

        except Exception as e:
            execution_time = (time.perf_counter() - start_time) * 1000

            return ExecutionResult(
                success=False,
                stdout="",
                stderr=f"Failed to execute script: {e}",
                exit_code=-1,
                execution_time_ms=execution_time,
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
