"""Code execution tool — write and run Python scripts on the fly.

Provides a single-call interface for the agent to generate and execute
ad-hoc Python code when existing tools cannot satisfy the requirement.
Security is enforced by:
  1. Static code scanning (SecurityScanner.scan_code)
  2. Sandbox execution (Sandbox with resource limits)
  3. Ephemeral temp files with automatic cleanup
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from markbot.config.paths import get_code_run_dir
from markbot.tools.base import Tool


def _make_temp_script(purpose: str, suffix: str = ".py") -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid4().hex[:4]
    safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in purpose[:32])
    return get_code_run_dir() / f"{safe}_{ts}_{uid}{suffix}"


class CodeExecutionTool(Tool):
    _is_destructive = True

    _MAX_CODE_CHARS = 50_000
    _MAX_OUTPUT_CHARS = 15_000
    _DEFAULT_TIMEOUT = 60
    _MAX_TIMEOUT = 300

    @property
    def name(self) -> str:
        return "run_code"

    @property
    def description(self) -> str:
        return (
            "Execute Python code in a sandboxed environment and return the output. "
            "Use this when existing tools cannot accomplish the task — for example: "
            "complex data processing, chart generation, file format conversion, "
            "algorithm implementation, or batch operations. "
            "Code runs in a temporary file that is cleaned up after execution. "
            "Optionally specify pip dependencies to install before running."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Write complete, self-contained scripts. Print output to stdout.",
                },
                "purpose": {
                    "type": "string",
                    "description": "Short description of what the code does (used for temp file naming).",
                },
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional pip packages to install before execution. "
                        "Prefer pinned versions, e.g. ['pandas==2.2.0', 'numpy==1.26.0']. "
                        "Standard library modules need not be listed."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Execution timeout in seconds (default {self._DEFAULT_TIMEOUT}, max {self._MAX_TIMEOUT}).",
                    "minimum": 1,
                    "maximum": self._MAX_TIMEOUT,
                },
                "save_artifacts": {
                    "type": "boolean",
                    "description": (
                        "If true, the temp script file is NOT deleted after execution. "
                        "Use this when the code generates files you want to inspect later."
                    ),
                    "default": False,
                },
            },
            "required": ["code"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        code = kwargs.get("code", "")
        purpose = kwargs.get("purpose", "script")
        dependencies = kwargs.get("dependencies") or []
        timeout = min(kwargs.get("timeout") or self._DEFAULT_TIMEOUT, self._MAX_TIMEOUT)
        save_artifacts = kwargs.get("save_artifacts", False)

        if not code.strip():
            return "Error: No code provided."

        if len(code) > self._MAX_CODE_CHARS:
            return f"Error: Code exceeds {self._MAX_CODE_CHARS:,} character limit."

        scan_error = self._scan_code(code)
        if scan_error:
            return scan_error

        script_path = _make_temp_script(purpose)
        try:
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(code, encoding="utf-8")

            if dependencies:
                dep_result = await self._install_dependencies(dependencies, timeout)
                if dep_result is not None:
                    return dep_result

            from markbot.skills.core.sandbox import Sandbox, SandboxConfig

            sandbox_config = SandboxConfig(
                timeout=timeout,
                max_memory_mb=256,
                max_cpu_time=timeout,
                network=False,
            )

            sandbox = Sandbox(sandbox_config)

            workspace = kwargs.get("_tool_context")
            cwd = None
            if workspace and hasattr(workspace, "workspace"):
                cwd = Path(workspace.workspace)

            # Stream each stdout line to the bus via ToolContext.report_progress,
            # so long-running scripts (pip install, training loops) surface
            # live progress instead of blocking silently until completion.
            report_progress = getattr(workspace, "report_progress", None)

            async def _on_output_line(line: str) -> None:
                if report_progress is not None:
                    try:
                        report_progress(line, None)
                    except Exception:
                        pass

            result = await sandbox.run(
                script=script_path,
                language="python",
                args={},
                cwd=cwd,
                on_output_line=_on_output_line if report_progress is not None else None,
            )

            output = self._format_result(result)

            if len(output) > self._MAX_OUTPUT_CHARS:
                half = self._MAX_OUTPUT_CHARS // 2
                output = (
                    output[:half]
                    + f"\n\n... ({len(output) - self._MAX_OUTPUT_CHARS:,} chars truncated) ...\n\n"
                    + output[-half:]
                )

            return output

        except Exception as e:
            logger.error("CodeExecutionTool execution failed: {}", str(e))
            return f"Error executing code: {e}"

        finally:
            if not save_artifacts and script_path.exists():
                try:
                    script_path.unlink()
                except OSError:
                    pass

    def _scan_code(self, code: str) -> str | None:
        try:
            from markbot.skills.core.scanner import BLOCKING_SEVERITIES, SecurityScanner

            scanner = SecurityScanner()
            scan_result = scanner.scan_code(code, language="python", trust_level="agent-created")

            if not scan_result.is_safe:
                blocking = [
                    f"  - [{f.severity}] {f.message} (pattern: {f.pattern})"
                    for f in scan_result.findings
                    if f.severity in BLOCKING_SEVERITIES
                ]
                if blocking:
                    return (
                        "Error: Code blocked by security scan.\n"
                        + "\n".join(blocking)
                        + "\nRefactor the code to avoid these patterns."
                    )

            if scan_result.findings:
                warnings = [
                    f"  - [{f.severity}] {f.message}"
                    for f in scan_result.findings
                    if f.severity not in BLOCKING_SEVERITIES
                ]
                if warnings:
                    logger.debug("Code scan warnings: {}", warnings)

            return None

        except Exception as e:
            logger.warning("Security scan failed (allowing execution): {}", e)
            return None

    async def _install_dependencies(self, dependencies: list[str], timeout: int) -> str | None:
        safe_deps = []
        for dep in dependencies:
            if not dep.strip():
                continue
            if any(c in dep for c in (";", "&", "|", "`", "$", ">", "<")):
                return f"Error: Invalid dependency specifier: '{dep}'"
            safe_deps.append(dep.strip())

        if not safe_deps:
            return None

        install_cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + safe_deps

        try:
            import asyncio

            process = await asyncio.create_subprocess_exec(
                *install_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=min(timeout, 120),
                )
            except asyncio.TimeoutError:
                process.kill()
                return "Error: Dependency installation timed out."

            if process.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                return f"Error: Failed to install dependencies.\n{err_msg}"

            installed = ", ".join(safe_deps)
            logger.info("CodeExecutionTool installed dependencies: {}", installed)
            return None

        except Exception as e:
            return f"Error: Failed to install dependencies: {e}"

    @staticmethod
    def _format_result(result: Any) -> str:
        parts = []

        if result.stdout and result.stdout.strip():
            parts.append(result.stdout.strip())

        if result.stderr and result.stderr.strip():
            parts.append(f"[stderr]:\n{result.stderr.strip()}")

        if not result.success:
            parts.append(f"\n[exit code: {result.exit_code}]")
        elif not parts:
            parts.append("(no output)")

        # Close the loop on missing-package failures: the sandbox preamble
        # already prints a hint to stderr when ImportError is raised, but a
        # bare `import foo` with a missing-but-not-installed module sometimes
        # surfaces as a ModuleNotFoundError only. Surface a short reminder
        # pointing the model at the `dependencies` parameter so it does not
        # guess or retry blindly.
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        if "ModuleNotFoundError" in combined or "ImportError" in combined:
            parts.append(
                "\n[markbot hint] Detected an import failure. If a third-party "
                "package is missing, declare it via the `dependencies` parameter "
                "on the next run_code call, e.g. dependencies=[\"requests\"]."
            )

        if result.execution_time_ms > 1000:
            parts.append(f"\n[executed in {result.execution_time_ms / 1000:.1f}s]")

        return "\n".join(parts)
