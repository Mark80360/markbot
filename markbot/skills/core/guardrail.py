"""Skill execution guardrail for post-execution validation.

Provides a validation layer that checks agent behavior against
skill-defined constraints after skill scripts are executed.
This acts as a fourth defense layer to ensure skills are followed
as hard constraints rather than suggestions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from markbot.types.skill import SkillDefinition
from markbot.utils.constants import MEMORY_FILENAME, USER_FILENAME


@dataclass
class GuardrailViolation:
    """A single constraint violation detected by the guardrail."""

    severity: str  # "warning" | "error" | "critical"
    message: str
    tool_name: Optional[str] = None
    step_name: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"[{self.severity.upper()}] {self.message}"]
        if self.tool_name:
            parts.append(f" (tool: {self.tool_name})")
        if self.step_name:
            parts.append(f" (step: {self.step_name})")
        return "".join(parts)


@dataclass
class GuardrailResult:
    """Result of a guardrail check pass."""

    passed: bool
    violations: list[GuardrailViolation] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return any(v.severity == "warning" for v in self.violations)

    @property
    def has_errors(self) -> bool:
        return any(v.severity in ("error", "critical") for v in self.violations)

    def format_report(self) -> str:
        """Format violations as a human-readable report."""
        if not self.violations:
            return "Guardrail check PASSED — no violations."

        lines = [f"Guardrail check {'FAILED' if not self.passed else 'PASSED WITH WARNINGS'}:"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


class SkillGuardrail:
    """Validates that agent behavior follows skill constraints.

    This guardrail can be used to check tool calls and actions
    taken after a skill script execution to ensure they align
    with the skill's defined workflow.

    Supports two execution contexts:
    - "main": Full agent with all tools available
    - "subagent": Read-only agent with restricted tools

    Usage:
        guardrail = SkillGuardrail(skill, context="main")
        result = guardrail.check_tool_call("write_file", {"path": "..."})
        if not result.passed:
            logger.warning("Violation: {}", result.format_report())
    """

    MAIN_AGENT_ALLOWED_TOOLS = {
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "list_dir",
        "glob",
        "grep",
        "exec",
        "run_code",
        "web_search",
        "web_fetch",
        "web_extract",
        "think",
        "todo",
        "message",
        "ask_user_question",
        "memory_search",
        "explore",
        "explore_context_catalog",
        "search_context",
        "load_context",
        "spawn",
        "cron",
        "check_subagent",
        "list_subagents",
    }

    SUBAGENT_ALLOWED_TOOLS = {
        "read_file",
        "list_dir",
        "glob",
        "grep",
        "web_search",
        "web_fetch",
        "web_extract",
        "think",
        "todo",
        "ask_user_question",
        "memory_search",
        "explore",
        "explore_context_catalog",
        "search_context",
        "load_context",
    }

    SUBAGENT_FORBIDDEN_TOOLS = {
        "exec": "Subagents cannot execute shell commands",
        "write_file": "Subagents cannot write files",
        "edit_file": "Subagents cannot edit files",
        "delete_file": "Subagents cannot delete files",
        "spawn": "Subagents cannot spawn other subagents",
        "cron": "Subagents cannot create cron jobs",
        "message": "Subagents cannot send messages to users",
    }

    def __init__(
        self,
        skill: SkillDefinition,
        context: str = "main",
        available_tools: set[str] | None = None,
    ):
        self.skill = skill
        self._context = context
        self._completed_steps: set[str] = set()
        self._called_tools: list[tuple[str, dict]] = []
        self._violations: list[GuardrailViolation] = []
        self._active = False

        if available_tools is not None:
            self._allowed_tools = available_tools
        elif context == "subagent":
            self._allowed_tools = self.SUBAGENT_ALLOWED_TOOLS
        else:
            self._allowed_tools = self.MAIN_AGENT_ALLOWED_TOOLS

    def activate(self) -> None:
        """Activate the guardrail for monitoring."""
        self._active = True
        self._completed_steps.clear()
        self._called_tools.clear()
        self._violations.clear()

    def deactivate(self) -> None:
        """Deactivate the guardrail."""
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    def mark_step_completed(self, step_description: str) -> None:
        """Mark a skill step as completed.

        Args:
            step_description: Description of the completed step.
        """
        self._completed_steps.add(step_description)
        logger.debug("Skill '{}': step marked complete: {}", self.skill.name, step_description)

    def check_tool_call(self, tool_name: str, params: dict[str, Any]) -> GuardrailResult:
        """Check if a tool call violates skill constraints.

        Validates that the agent is using appropriate tools during
        skill execution and not going off-script.

        Args:
            tool_name: Name of the tool being called.
            params: Parameters being passed to the tool.

        Returns:
            GuardrailResult with any detected violations.
        """
        if not self._active:
            return GuardrailResult(passed=True)

        self._called_tools.append((tool_name, params))
        violations: list[GuardrailViolation] = []

        if self._context == "subagent" and tool_name in self.SUBAGENT_FORBIDDEN_TOOLS:
            violations.append(GuardrailViolation(
                severity="error",
                message=(
                    f"FORBIDDEN in subagent context: {self.SUBAGENT_FORBIDDEN_TOOLS[tool_name]}. "
                    f"Tool '{tool_name}' is not available to subagents."
                ),
                tool_name=tool_name,
            ))
            return GuardrailResult(
                passed=not any(v.severity in ("error", "critical") for v in violations),
                violations=violations,
            )

        allowed_skill_tools = {f"{self.skill.name}.{s.name}" for s in self.skill.scripts}
        all_allowed = self._allowed_tools | allowed_skill_tools

        if tool_name not in all_allowed:
            violations.append(GuardrailViolation(
                severity="warning",
                message=(
                    f"Tool '{tool_name}' may not be specified in skill '{self.skill.name}'. "
                    f"Consider using only tools defined in the skill or basic read/explore tools."
                ),
                tool_name=tool_name,
            ))

        write_dangerous_patterns = [
            ("SOUL.md", "Modifying SOUL.md during skill execution"),
            ("AGENTS.md", "Modifying AGENTS.md during skill execution"),
            ("USER.md", "Modifying USER.md during skill execution"),
            (USER_FILENAME, f"Modifying {USER_FILENAME} during skill execution"),
            (MEMORY_FILENAME, f"Directly modifying {MEMORY_FILENAME}"),
        ]

        if tool_name in ("write_file", "edit_file"):
            file_path = params.get("path") or params.get("file_path", "")
            for pattern, warning_msg in write_dangerous_patterns:
                if pattern.lower() in str(file_path).lower():
                    violations.append(GuardrailViolation(
                        severity="error",
                        message=f"{warning_msg} — this should be done outside skill context.",
                        tool_name=tool_name,
                    ))

        self._violations.extend(violations)

        return GuardrailResult(
            passed=not any(v.severity in ("error", "critical") for v in violations),
            violations=violations,
        )

    def get_violations(self) -> list[GuardrailViolation]:
        return list(self._violations)

    def get_execution_summary(self) -> dict[str, Any]:
        """Get a summary of the guarded execution.

        Returns:
            Dict with steps completed, tools called, and violation count.
        """
        return {
            "skill": self.skill.name,
            "active": self._active,
            "completed_steps": sorted(self._completed_steps),
            "tools_called": [t[0] for t in self._called_tools],
            "total_tool_calls": len(self._called_tools),
        }


class SkillGuardrailManager:
    """Manages multiple active skill guardrails.

    Allows tracking which skills are currently being executed
    and validates tool calls against all active guardrails.
    """

    def __init__(self, context: str = "main", available_tools: set[str] | None = None):
        self._guardrails: dict[str, SkillGuardrail] = {}
        self._context = context
        self._available_tools = available_tools

    def set_available_tools(self, tool_names: set[str]) -> None:
        self._available_tools = tool_names

    def start_guarding(self, skill: SkillDefinition) -> SkillGuardrail:
        guardrail = SkillGuardrail(skill, context=self._context, available_tools=self._available_tools)
        guardrail.activate()
        self._guardrails[skill.name] = guardrail
        logger.info("Skill guardrail activated for: {} (context={})", skill.name, self._context)
        return guardrail

    def stop_guarding(self, skill_name: str) -> Optional[GuardrailResult]:
        """Stop guarding for a specific skill and return final status.

        Args:
            skill_name: Name of the skill to stop guarding.

        Returns:
            Final guardrail result with any accumulated warnings.
        """
        guardrail = self._guardrails.pop(skill_name, None)
        if guardrail:
            guardrail.deactivate()
            violations = guardrail.get_violations()
            passed = not any(v.severity in ("error", "critical") for v in violations)
            return GuardrailResult(passed=passed, violations=violations)
        return None

    def check_all(self, tool_name: str, params: dict[str, Any]) -> list[GuardrailResult]:
        """Check a tool call against all active guardrails.

        Args:
            tool_name: Name of the tool being called.
            params: Tool parameters.

        Returns:
            List of results from each active guardrail.
        """
        results = []
        for name, guardrail in list(self._guardrails.items()):
            if guardrail.is_active:
                result = guardrail.check_tool_call(tool_name, params)
                if result.violations:
                    results.append(result)
        return results

    @property
    def active_skills(self) -> list[str]:
        """List names of currently guarded (active) skills."""
        return [name for name, gr in self._guardrails.items() if gr.is_active]
