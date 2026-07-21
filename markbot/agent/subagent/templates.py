"""Capability templates — allocate subagent manpower by task type.

Parent agents pick a template name (or pass a full capability object).
Templates are hardened by DelegationPolicy.blocked_tools at spawn time.
"""

from __future__ import annotations

from typing import Any, Mapping

from markbot.agent.subagent.capability import CapabilityToken


_TEMPLATES: dict[str, CapabilityToken] = {
    "research": CapabilityToken(
        allowed_tools=(
            "read_file", "glob", "grep", "list_dir",
            "web_search", "web_fetch", "web_extract",
        ),
        forbidden_tools=(
            "exec", "write_file", "edit_file", "delete_file",
            "message", "spawn", "ask_user_question", "cron",
            "skill_manage", "computer_use",
        ),
        max_iterations=12,
        max_budget_usd=0.4,
        timeout_seconds=240,
        description="Read-only research / code exploration",
        metadata={"template": "research"},
    ),
    "code_edit": CapabilityToken(
        allowed_tools=(
            "read_file", "write_file", "edit_file", "list_dir",
            "glob", "grep", "exec",
        ),
        forbidden_tools=(
            "message", "spawn", "ask_user_question", "cron",
            "skill_manage", "computer_use", "web_search", "web_fetch",
        ),
        max_iterations=20,
        max_budget_usd=1.0,
        timeout_seconds=600,
        description="Focused code edit + local verify via exec",
        metadata={"template": "code_edit"},
    ),
    "verify": CapabilityToken(
        allowed_tools=(
            "read_file", "glob", "grep", "list_dir", "exec",
        ),
        forbidden_tools=(
            "write_file", "edit_file", "delete_file",
            "message", "spawn", "ask_user_question", "cron",
            "skill_manage", "computer_use",
        ),
        max_iterations=10,
        max_budget_usd=0.3,
        timeout_seconds=300,
        description="Run tests / checks and report evidence",
        metadata={"template": "verify"},
    ),
    "browse": CapabilityToken(
        allowed_tools=(
            "web_search", "web_fetch", "web_extract",
            "browser_navigate", "browser_snapshot", "browser_click",
            "browser_type", "browser_scroll", "browser_press",
            "browser_back", "browser_forward", "browser_wait",
            "browser_screenshot", "browser_close",
        ),
        forbidden_tools=(
            "exec", "write_file", "edit_file", "delete_file",
            "message", "spawn", "ask_user_question", "cron",
            "skill_manage", "computer_use",
        ),
        max_iterations=15,
        max_budget_usd=0.6,
        timeout_seconds=360,
        description="Web research / browser navigation",
        metadata={"template": "browse"},
    ),
    "read_only": CapabilityToken.read_only("Read-only research"),
}


def list_templates() -> list[str]:
    return sorted(_TEMPLATES.keys())


def get_template(name: str) -> CapabilityToken | None:
    if not name:
        return None
    return _TEMPLATES.get(str(name).strip().lower())


def resolve_capability(
    capability: Any = None,
    *,
    template: str | None = None,
) -> CapabilityToken:
    """Resolve LLM-facing capability args into a CapabilityToken.

    Priority:
      1. explicit capability mapping / token
      2. template name
      3. read_only default
    """
    if isinstance(capability, CapabilityToken):
        return capability
    if isinstance(capability, Mapping):
        tmpl = capability.get("template") or capability.get("profile")
        if tmpl and not (
            capability.get("allowed_tools")
            or capability.get("allowedTools")
            or capability.get("forbidden_tools")
            or capability.get("forbiddenTools")
        ):
            got = get_template(str(tmpl))
            if got is not None:
                return got
        return CapabilityToken.from_dict(capability)
    if template:
        got = get_template(template)
        if got is not None:
            return got
    return CapabilityToken.read_only()


def format_result_payload(
    *,
    status: str,
    task_id: str,
    label: str,
    task: str,
    result: str,
    artifacts: list[str] | None = None,
    evidence: list[str] | None = None,
    residual_risk: str = "",
    progress: Any = None,
) -> str:
    """Structured subagent result for the parent agent (still human-readable)."""
    status_text = {
        "ok": "completed successfully",
        "cancelled": "was cancelled",
        "error": "failed",
    }.get(status, "failed")

    parts = [
        f"[Subagent '{label}' {status_text}]",
        "",
        f"Task: {task}",
        f"task_id: {task_id}",
        f"status: {status}",
    ]
    if progress is not None:
        try:
            parts.append(
                "metrics: "
                f"duration={getattr(progress, 'duration_seconds', 0):.1f}s "
                f"tokens={getattr(progress, 'total_tokens', 0)} "
                f"tools={getattr(progress, 'tool_use_count', 0)}"
            )
        except Exception:
            pass
    if artifacts:
        parts.append("artifacts:")
        parts.extend(f"- {a}" for a in artifacts[:20])
    if evidence:
        parts.append("evidence:")
        parts.extend(f"- {e}" for e in evidence[:20])
    if residual_risk:
        parts.append(f"residual_risk: {residual_risk}")
    parts.extend([
        "",
        "Result:",
        result,
        "",
        "Summarize this naturally for the user. Keep it brief (1-2 sentences). "
        "Mention residual risk only if non-empty.",
    ])
    return "\n".join(parts)
