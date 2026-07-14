"""Agent runtime profiles.

Profiles select default permission mode, tool surface, and skill loading
policy for different usage modes without rewriting the agent loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProfileName = Literal["coding", "assistant", "unattended"]


# Tool groups used for enable/disable by profile.
TOOL_GROUPS: dict[str, frozenset[str]] = {
    "core_fs": frozenset({
        "read_file", "write_file", "edit_file", "list_dir", "delete_file",
        "glob", "grep",
    }),
    "core_web": frozenset({"web_search", "web_fetch", "web_extract"}),
    "core_shell": frozenset({"exec", "run_code", "code_execution"}),
    "agent_ops": frozenset({
        "message", "think", "todo", "ask_user_question",
        "spawn", "check_subagent", "list_subagents",
        "explore", "explore_context_catalog", "search_context", "load_context",
    }),
    "memory": frozenset({
        "memory_search", "memory_save", "memory_forget", "memory_list", "memory_dream",
    }),
    "skills": frozenset({"skill_view", "skills_list", "skill_manage"}),
    "schedule": frozenset({"cron"}),
    "autopilot": frozenset({
        "autopilot_intake", "autopilot_score", "autopilot_accept",
        "autopilot_run_next", "autopilot_verify", "autopilot_status",
        "autopilot_reject", "autopilot_requeue",
    }),
    "desktop": frozenset({"computer_use"}),
    "browser": frozenset({
        "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
        "browser_scroll", "browser_press", "browser_back", "browser_forward",
        "browser_wait", "browser_screenshot", "browser_close",
    }),
}


@dataclass(frozen=True)
class Profile:
    """Resolved runtime profile."""

    name: ProfileName
    permission_mode: str
    # When True, register desktop/browser/autopilot tools if config enables them.
    enable_desktop: bool = True
    enable_browser: bool = True
    enable_autopilot: bool = True
    enable_subagents: bool = True
    enable_explore: bool = True
    # Skill index filtering
    min_skill_score: float = 0.0
    hide_stale_skills: bool = False
    # Exec defaults applied when config does not override.
    exec_restrict_to_workspace: bool = True
    exec_require_allowlist: bool = False
    # Soft tool-name denylist applied after registration (exact names).
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    description: str = ""


_PROFILES: dict[str, Profile] = {
    "coding": Profile(
        name="coding",
        permission_mode="auto",
        enable_desktop=True,
        enable_browser=True,
        enable_autopilot=True,
        enable_subagents=True,
        enable_explore=True,
        min_skill_score=0.35,
        hide_stale_skills=True,
        exec_restrict_to_workspace=True,
        exec_require_allowlist=False,
        description="Software development and project work",
    ),
    "assistant": Profile(
        name="assistant",
        permission_mode="default",
        enable_desktop=False,
        enable_browser=True,
        enable_autopilot=False,
        enable_subagents=True,
        enable_explore=False,
        min_skill_score=0.45,
        hide_stale_skills=True,
        exec_restrict_to_workspace=True,
        exec_require_allowlist=True,
        disabled_tools=frozenset({"computer_use"}),
        description="Interactive personal assistant with confirmation for mutations",
    ),
    "unattended": Profile(
        name="unattended",
        permission_mode="auto",
        enable_desktop=False,
        enable_browser=False,
        enable_autopilot=True,
        enable_subagents=True,
        enable_explore=False,
        min_skill_score=0.45,
        hide_stale_skills=True,
        exec_restrict_to_workspace=True,
        exec_require_allowlist=True,
        disabled_tools=frozenset({
            "computer_use",
            # Actual browser tool names registered by markbot.tools.browser
            "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
            "browser_scroll", "browser_press", "browser_back", "browser_vision",
            "browser_console", "browser_get_images",
        }),
        description="Cron/heartbeat/autopilot unattended execution",
    ),
}


def list_profiles() -> list[str]:
    return list(_PROFILES.keys())


def get_profile(name: str | None) -> Profile:
    """Return a known profile, defaulting to coding."""
    if not name:
        return _PROFILES["coding"]
    key = str(name).strip().lower()
    if key not in _PROFILES:
        return _PROFILES["coding"]
    return _PROFILES[key]


def resolve_profile_name(name: str | None) -> ProfileName:
    profile = get_profile(name)
    return profile.name
