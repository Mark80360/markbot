"""SkillScript tool implementation.

Wraps skill executable scripts as proper Tool instances that can be
registered with the agent's ToolRegistry.
"""

import logging
from pathlib import Path
from typing import Any, Optional

from markbot.agent.tools.base import Tool
from .scanner import SecurityScanner, ScanResult
from .sandbox import Sandbox, SandboxConfig

logger = logging.getLogger(__name__)


class SkillScript(Tool):
    """A Tool wrapper for skill executable scripts.

    This class transforms a script defined in SKILL.md metadata into
    a proper Tool that can be registered and executed by the agent.

    Attributes:
        name: Tool name (format: "skill_name.script_name")
        _script_name: Original script name from metadata
        description: Script description
        _entry: Path to the script entry point
        _language: Script language (python, shell, node)
        _parameters: JSON Schema for parameters
        _sandbox: Sandbox instance for execution
        _skill_path: Path to the skill directory
        _scanner: Security scanner instance
    """

    def __init__(
        self,
        name: str,
        script_name: str,
        description: str,
        entry: Path,
        language: str,
        parameters: dict[str, Any],
        sandbox_config: Optional[dict[str, Any]],
        skill_path: Path,
    ):
        """Initialize a SkillScript tool.

        Args:
            name: Full tool name (skill_name.script_name)
            script_name: Original script name from metadata
            description: Script description
            entry: Path to script entry file
            language: Script language
            parameters: JSON Schema for parameters
            sandbox_config: Sandbox configuration dict
            skill_path: Path to skill directory
        """
        self._name = name
        self._script_name = script_name
        self._description = description
        self._entry = Path(entry)
        self._language = language.lower()
        self._parameters = parameters or {"type": "object", "properties": {}}
        self._skill_path = skill_path

        # Initialize sandbox with config
        if sandbox_config:
            sandbox_config["environment"] = sandbox_config.get("environment", {})
            sandbox_config["environment"]["SKILL_NAME"] = name.split(".")[0]
            sandbox_config["environment"]["SCRIPT_NAME"] = script_name
            # Store original working directory for path resolution
            sandbox_config["environment"]["WORKSPACE_ROOT"] = str(Path.cwd().resolve())
            self._sandbox = Sandbox(sandbox_config)
        else:
            config = {"environment": {
                "SKILL_NAME": name.split(".")[0],
                "SCRIPT_NAME": script_name,
                "WORKSPACE_ROOT": str(Path.cwd().resolve())
            }}
            self._sandbox = Sandbox(config)

        # Initialize security scanner
        self._scanner = SecurityScanner()

    @property
    def name(self) -> str:
        """Tool name used in function calls."""
        return self._name

    @property
    def description(self) -> str:
        """Tool description."""
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        return self._parameters

    @property
    def script_name(self) -> str:
        """Original script name from metadata."""
        return self._script_name

    @property
    def entry_path(self) -> Path:
        """Path to script entry point."""
        return self._entry

    @property
    def language(self) -> str:
        """Script language."""
        return self._language

    def scan_security(self) -> ScanResult:
        """Perform security scan on the script.

        Returns:
            ScanResult with findings.
        """
        return self._scanner.scan(self._entry, self._language)

    async def execute(self, **kwargs: Any) -> str:
        """Execute the script with given parameters.

        Process:
        1. Security scan
        2. Parameter validation
        3. Path validation
        4. Sandbox execution

        Returns:
            Script output as string.
        """
        # Step 1: Security scan
        scan_result = self.scan_security()
        if not scan_result.is_safe:
            findings_str = "\n".join(
                f"  Line {f.line}: [{f.severity}] {f.message}"
                for f in scan_result.findings
                if f.severity in ("high", "critical")
            )
            return f"Error: Script failed security check\n{findings_str}"

        # Step 2: Parameter validation (handled by Tool base)
        errors = self.validate_params(kwargs)
        if errors:
            return f"Error: Invalid parameters: {'; '.join(errors)}"

        # Step 3: Path validation
        if self._sandbox.config.allowed_paths:
            allowed, msg = self._sandbox.validate_script_access(self._entry)
            if not allowed:
                return f"Error: {msg}"

        # Step 4: Execute in sandbox
        try:
            logger.info(f"Executing skill script: {self._name} with args: {kwargs}")

            result = await self._sandbox.run(
                script=self._entry.resolve(),
                language=self._language,
                args=kwargs,
                cwd=self._skill_path,
            )

            if result.success:
                output = result.stdout
                if result.stderr:
                    output += f"\n\n[stderr]:\n{result.stderr}"
                return output
            else:
                error_msg = f"Script execution failed (exit code: {result.exit_code})"
                if result.stderr:
                    error_msg += f"\n{result.stderr}"
                return f"Error: {error_msg}"

        except Exception as e:
            logger.exception(f"Error executing skill script {self._name}")
            return f"Error: Failed to execute script: {e}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self._name,
            "script_name": self._script_name,
            "description": self._description,
            "entry": str(self._entry),
            "language": self._language,
            "parameters": self._parameters,
            "skill_path": str(self._skill_path),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillScript":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            script_name=data["script_name"],
            description=data["description"],
            entry=Path(data["entry"]),
            language=data["language"],
            parameters=data["parameters"],
            sandbox_config=None,
            skill_path=Path(data["skill_path"]),
        )
