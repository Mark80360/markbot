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

    def __init__(self, config: Any = None, skill_registry: Any = None) -> None:
        """Initialise the tool with an optional ``CodeExecutionConfig``.

        When *config* is provided its ``timeout``/``max_memory_mb`` override
        the class defaults and ``allowed_dependencies`` is enforced as a
        pip-install whitelist (empty list = allow all, matching the schema
        semantics). When None, defaults are used and all packages are
        allowed (preserving prior behaviour for callers that don't pass
        a config).

        *skill_registry* enables the ``persist_as`` parameter: a successfully
        validated script can be saved as a reusable skill tool and hot-loaded
        in the same session, so the agent can extend its own toolset.
        """
        self._config = config
        self._skill_registry = skill_registry
        if config is not None:
            self._default_timeout = int(getattr(config, "timeout", self._DEFAULT_TIMEOUT))
            self._max_memory_mb = int(getattr(config, "max_memory_mb", 256))
            # Normalise the whitelist once: lowercase package names, strip
            # version specifiers so ``numpy>=1.20`` matches ``numpy``.
            raw_allowed = getattr(config, "allowed_dependencies", None) or []
            self._allowed_deps = {self._dep_name(d) for d in raw_allowed}
        else:
            self._default_timeout = self._DEFAULT_TIMEOUT
            self._max_memory_mb = 256
            self._allowed_deps = set()

    @staticmethod
    def _dep_name(spec: str) -> str:
        """Extract the bare package name from a pip specifier.

        ``numpy>=1.20`` → ``numpy``; ``requests[socks]==2.31`` → ``requests``.
        Used for whitelist matching.
        """
        name = spec.strip()
        for sep in (">", "<", "=", "!", "~", ";", "["):
            idx = name.find(sep)
            if idx != -1:
                name = name[:idx]
        return name.strip().lower()

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
                    "description": f"Execution timeout in seconds (default {self._default_timeout}, max {self._MAX_TIMEOUT}).",
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
                "output_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Paths (relative to the working directory or absolute) of files "
                        "the script is expected to produce, e.g. ['chart.png', 'out/result.json']. "
                        "After execution each existing file is confirmed and its absolute path "
                        "returned so you can send it to the user via the message tool. "
                        "Files that were not created are reported as missing."
                    ),
                },
                "persist_as": {
                    "type": "string",
                    "description": (
                        "If set to a skill name (lowercase, underscores/hyphens only), a "
                        "successfully executed script is saved as a reusable skill tool under "
                        "workspace/skills/<persist_as>/ and immediately registered. The new tool "
                        "is named '<persist_as>.run' and takes no parameters — use this to turn a "
                        "validated one-off script into a permanent capability you can call again. "
                        "Only persist scripts that ran without errors."
                    ),
                },
            },
            "required": ["code"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        code = kwargs.get("code", "")
        purpose = kwargs.get("purpose", "script")
        dependencies = kwargs.get("dependencies") or []
        timeout = min(kwargs.get("timeout") or self._default_timeout, self._MAX_TIMEOUT)
        save_artifacts = kwargs.get("save_artifacts", False)
        output_files = kwargs.get("output_files") or []
        persist_as = (kwargs.get("persist_as") or "").strip()

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
                max_memory_mb=self._max_memory_mb,
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

            # Persist a successful script as a reusable skill tool so the
            # agent can extend its own toolset. Only persist when the run
            # succeeded and a skill_registry is wired up.
            if persist_as and getattr(result, "success", False) and self._skill_registry is not None:
                persist_msg = self._persist_as_skill(
                    persist_as, code, purpose, dependencies, workspace
                )
                if persist_msg:
                    output += f"\n\n--- Persist as skill ---\n{persist_msg}"

            # Confirm declared output files so the agent gets absolute paths
            # it can pass to the message tool. Files are resolved relative to
            # the script's cwd when available, else the temp dir.
            if output_files:
                base = Path(cwd) if cwd else script_path.parent
                produced: list[str] = []
                missing: list[str] = []
                for rel in output_files:
                    candidate = Path(rel)
                    resolved = candidate if candidate.is_absolute() else (base / candidate)
                    if resolved.exists():
                        produced.append(str(resolved))
                    else:
                        missing.append(rel)
                if produced or missing:
                    output += "\n\n--- Output files ---"
                    if produced:
                        output += "\nCreated:\n" + "\n".join(f"  {p}" for p in produced)
                    if missing:
                        output += "\nMissing (not created by script):\n" + "\n".join(
                            f"  {m}" for m in missing
                        )

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

    @staticmethod
    def _build_dep_shim(dependencies: list[str]) -> str:
        """Build a self-install preamble for a persisted script.

        The shim tries to import each package; on failure it pip-installs
        the original specifier (which may include a version pin) and
        retries the import. This makes the persisted tool self-contained
        since ``SkillTool`` does not run ``pip install`` itself.
        """
        # Map bare import name → original specifier for install.
        # Heuristic: import name is the specifier up to the first
        # version/operator character, lowercased. Most packages share
        # import and distribution name; for the common exceptions
        # (PIL/Pillow, cv2/opencv-python, sklearn/scikit-learn) the
        # agent should edit run.py after persisting.
        pairs = []
        for spec in dependencies:
            spec = spec.strip()
            if not spec:
                continue
            import_name = CodeExecutionTool._dep_name(spec).replace("-", "_")
            pairs.append((import_name, spec))
        if not pairs:
            return ""

        lines = [
            "# Auto-generated dependency shim — ensures this persisted",
            "# tool can install its own requirements on first run.",
            "import importlib",
            "import subprocess",
            "import sys",
            "",
            "_DEPS = " + repr(pairs),
            "",
            "for _import_name, _spec in _DEPS:",
            "    try:",
            "        importlib.import_module(_import_name)",
            "    except ImportError:",
            "        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', _spec])",
            "        importlib.import_module(_import_name)",
            "",
        ]
        return "\n".join(lines)

    def _persist_as_skill(
        self,
        name: str,
        code: str,
        purpose: str,
        dependencies: list[str],
        workspace: Any,
    ) -> str | None:
        """Save a validated script as a reusable skill and hot-load it.

        Creates ``workspace/skills/<name>/`` with a ``SKILL.md`` (frontmatter
        declaring one parameterless script) and a ``run.py`` containing the
        code. The skill is then hot-loaded via the registry so it is callable
        as ``<name>.run`` in the current session. Returns a short status
        message, or None if the workspace path could not be determined.
        """
        import re

        # Validate the skill name: lowercase, alnum, underscores, hyphens.
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            return (
                f"Error: Invalid skill name '{name}'. Use lowercase letters, "
                "digits, underscores or hyphens (must start with a letter/digit)."
            )

        ws_root = None
        if workspace is not None:
            ws_root = getattr(workspace, "workspace", None)
        if ws_root is None:
            return "Error: Cannot persist skill — workspace path not available in tool context."
        ws_root = Path(ws_root)

        skill_dir = ws_root / "skills" / name
        script_file = skill_dir / "run.py"
        skill_file = skill_dir / "SKILL.md"

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)

            # Prepend a self-install shim so the persisted tool is
            # self-contained: SkillTool does not install dependencies, so
            # the script must do it itself. The shim is no-op if the
            # packages are already importable.
            script_body = code
            if dependencies:
                shim = self._build_dep_shim(dependencies)
                script_body = shim + "\n" + code
            script_file.write_text(script_body, encoding="utf-8")

            # Build SKILL.md frontmatter. The script takes no parameters so
            # the agent can call it as a zero-arg tool. Dependencies are
            # handled by the self-install shim prepended to run.py, so they
            # are only mentioned in the description for human reference.
            deps_line = ", ".join(dependencies) if dependencies else "none"
            frontmatter = (
                "---\n"
                f"name: {name}\n"
                f"description: Auto-persisted tool - {purpose or 'ad-hoc script'}. "
                f"Dependencies: {deps_line}.\n"
                "when_to_use: Run this when the same ad-hoc computation is needed again.\n"
                "scripts:\n"
                "  run:\n"
                "    entry: run.py\n"
                "    description: Execute the persisted script.\n"
                "---\n"
                f"# {name}\n\n"
                "Auto-persisted from a successful `run_code` call. Edit "
                "`SKILL.md` or `run.py` to refine behaviour.\n"
            )
            skill_file.write_text(frontmatter, encoding="utf-8")
        except OSError as e:
            return f"Error: Could not write skill files: {e}"

        # Hot-load so the new tool is available immediately.
        try:
            loaded = self._skill_registry.load_skill(name)
        except Exception as e:
            logger.warning("persist_as: hot-load failed for '{}': {}", name, e)
            return (
                f"Skill files written to {skill_dir} but hot-load failed: {e}. "
                "The tool will be available after a restart."
            )

        if loaded:
            return (
                f"Saved and registered skill '{name}'. You can now call the "
                f"tool '{name}.run' (no parameters) to re-run this script."
            )
        return (
            f"Skill files written to {skill_dir} but registration was rejected "
            "(security scan or requirements). Review the skill and retry, or "
            "edit it via skill_manage."
        )

    async def _install_dependencies(self, dependencies: list[str], timeout: int) -> str | None:
        safe_deps = []
        for dep in dependencies:
            if not dep.strip():
                continue
            if any(c in dep for c in (";", "&", "|", "`", "$", ">", "<")):
                return f"Error: Invalid dependency specifier: '{dep}'"
            # Enforce the configured whitelist. An empty whitelist means
            # "allow all" (per CodeExecutionConfig semantics); a non-empty
            # set restricts installs to the listed package names.
            if self._allowed_deps:
                pkg = self._dep_name(dep)
                if pkg not in self._allowed_deps:
                    return (
                        f"Error: Dependency '{dep}' is not in the allowed list "
                        f"(allowed: {', '.join(sorted(self._allowed_deps))})."
                    )
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
