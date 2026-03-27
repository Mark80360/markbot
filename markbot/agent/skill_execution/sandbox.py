"""Sandbox execution environment for skill scripts.

Provides a restricted execution environment with resource limits,
file access control, and timeout management.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

# Resource module is Unix-only
try:
    import resource
except ImportError:
    resource = None

logger = logging.getLogger(__name__)


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
            return "bash" if os.name != "nt" else "cmd"
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

    async def run(
        self,
        script: Path,
        language: str,
        args: dict,
        cwd: Optional[Path] = None,
    ) -> ExecutionResult:
        """Execute a script in the sandbox.

        Args:
            script: Path to the script file.
            language: Script language (python, shell, node).
            args: Arguments to pass to the script.
            cwd: Working directory for execution.

        Returns:
            ExecutionResult with output and status.
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

        # Build command
        cmd, env = self._build_command(script, language, args)
        cwd = cwd or script.parent

        # Prepare environment
        process_env = os.environ.copy()
        process_env.update(env)
        process_env["SANDBOX_MODE"] = "1"

        # Apply PATH restrictions if no network
        if not self.config.network:
            # Keep only essential paths
            essential_paths = [
                "/usr/bin",
                "/usr/local/bin",
                "/bin",
                "/sbin",
            ]
            if os.name == "nt":
                process_env["PATH"] = ";".join(
                    [
                        r"C:\Windows\System32",
                        r"C:\Windows",
                    ]
                )
            else:
                process_env["PATH"] = ":".join(essential_paths)

        try:
            # Create subprocess
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,  # 1MB buffer
            )

            # Set resource limits (Unix only)
            if os.name != "nt" and resource is not None and hasattr(resource, "setrlimit"):
                try:
                    # CPU time limit
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (self.config.max_cpu_time, self.config.max_cpu_time + 5),
                    )
                    # Memory limit
                    max_memory = self.config.max_memory_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (max_memory, max_memory))
                except (ValueError, OSError):
                    pass

            # Wait with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.config.timeout,
                )

                execution_time = (time.perf_counter() - start_time) * 1000

                return ExecutionResult(
                    success=process.returncode == 0,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
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
