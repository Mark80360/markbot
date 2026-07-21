"""Pure decision engine for tool-loop guardrails.

Single source of truth for tool-loop control:
  - exact-failure / tool-streak / no-progress / failure-window
  - failed-method blacklist (cross-loop persistence payload)
  - pre-execution block checks

No I/O, no session mutation, no LLM calls. ``IterationRunner`` feeds
observations and applies the returned decisions (warn / block / halt).

Decision lattice (strictest wins when multiple fire):
  allow < warn < block < halt
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_EXACT_FAILURE_WARN = 2
DEFAULT_EXACT_FAILURE_BLOCK = 4
DEFAULT_TOOL_STREAK_WARN = 3
DEFAULT_TOOL_STREAK_BLOCK = 5
DEFAULT_NO_PROGRESS_WARN = 2
DEFAULT_NO_PROGRESS_BLOCK = 4
DEFAULT_WINDOW_SIZE = 6
DEFAULT_WINDOW_FAILURE_THRESHOLD = 4
DEFAULT_MAX_REFLECTIONS = 2
DEFAULT_FAILED_METHODS_CAP = 30

# Read-only / idempotent tools: identical args + identical result ⇒ no progress.
DEFAULT_IDEMPOTENT_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_dir",
    "list_directory",
    "glob",
    "grep",
    "search_files",
    "search_codebase",
    "find",
    "list",
    "web_fetch",
    "web_search",
    "web_extract",
    "memory_search",
    "memory_list",
    "skills_list",
    "skill_view",
    "explore_context_catalog",
    "search_context",
    "load_context",
    "check_subagent",
    "list_subagents",
    "todo",
    "think",
})

# Failure-string keywords (lowercase). Keep specific to avoid false positives
# on normal page / log content.
_FAILURE_KEYWORDS: tuple[str, ...] = (
    "error:",
    "traceback",
    "no module named",
    "modulenotfounderror",
    "filenotfounderror",
    "command not found",
    "no such file or directory",
    "connection refused",
    "connection timed out",
    "timed out",
    "permission denied",
    "access denied",
    "execution denied",
    "exit code: 1",
    "exit code: 2",
    "exit code: 127",
    "failed to",
    "unable to",
    "cannot ",
    "can't ",
    "is not installed",
    "is not available",
    "500 internal server error",
    "fetch failed",
    "readability extraction failed",
    "readability failed",
    "jina reader",
    "forbidden origin",
    '"error":',
    '"errors":',
    '"error_code":',
    '"errcode":',
    '"status_code": 4',
    '"status_code": 5',
    '"status": 4',
    '"status": 5',
)


class GuardrailAction(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass(frozen=True)
class GuardrailConfig:
    """Thresholds for the pure decision engine."""

    exact_failure_warn: int = DEFAULT_EXACT_FAILURE_WARN
    exact_failure_block: int = DEFAULT_EXACT_FAILURE_BLOCK
    tool_streak_warn: int = DEFAULT_TOOL_STREAK_WARN
    tool_streak_block: int = DEFAULT_TOOL_STREAK_BLOCK
    no_progress_warn: int = DEFAULT_NO_PROGRESS_WARN
    no_progress_block: int = DEFAULT_NO_PROGRESS_BLOCK
    window_size: int = DEFAULT_WINDOW_SIZE
    window_failure_threshold: int = DEFAULT_WINDOW_FAILURE_THRESHOLD
    max_reflections: int = DEFAULT_MAX_REFLECTIONS
    idempotent_tools: frozenset[str] = DEFAULT_IDEMPOTENT_TOOLS
    failed_methods_cap: int = DEFAULT_FAILED_METHODS_CAP
    enabled: bool = True

    @classmethod
    def from_settings(cls, settings: Any | None) -> "GuardrailConfig":
        """Build from ``ToolsConfig.guardrails`` or a plain mapping."""
        if settings is None:
            return cls()
        if isinstance(settings, Mapping):
            g = settings
            get = g.get
        else:
            get = lambda k, default=None: getattr(settings, k, default)  # noqa: E731

        idempotent = get("idempotent_tools", None)
        # Empty list / None → engine defaults. Non-empty overrides fully.
        if not idempotent:
            tools = DEFAULT_IDEMPOTENT_TOOLS
        else:
            tools = frozenset(idempotent)

        return cls(
            enabled=bool(get("enabled", True)),
            exact_failure_warn=int(get("exact_failure_warn", DEFAULT_EXACT_FAILURE_WARN)),
            exact_failure_block=int(get("exact_failure_block", DEFAULT_EXACT_FAILURE_BLOCK)),
            tool_streak_warn=int(get("tool_streak_warn", DEFAULT_TOOL_STREAK_WARN)),
            tool_streak_block=int(get("tool_streak_block", DEFAULT_TOOL_STREAK_BLOCK)),
            no_progress_warn=int(get("no_progress_warn", DEFAULT_NO_PROGRESS_WARN)),
            no_progress_block=int(get("no_progress_block", DEFAULT_NO_PROGRESS_BLOCK)),
            window_size=int(get("window_size", DEFAULT_WINDOW_SIZE)),
            window_failure_threshold=int(
                get("window_failure_threshold", DEFAULT_WINDOW_FAILURE_THRESHOLD)
            ),
            max_reflections=int(get("max_reflections", DEFAULT_MAX_REFLECTIONS)),
            idempotent_tools=tools,
            failed_methods_cap=int(get("failed_methods_cap", DEFAULT_FAILED_METHODS_CAP)),
        )


@dataclass(frozen=True)
class GuardrailDecision:
    """Result of evaluating one observation or the failure-window check."""

    action: GuardrailAction
    reason: str = ""
    tool_name: str = ""
    signature: str = ""
    count: int = 0
    message: str = ""

    @property
    def should_halt(self) -> bool:
        return self.action is GuardrailAction.HALT

    @property
    def should_block(self) -> bool:
        return self.action is GuardrailAction.BLOCK

    @property
    def should_warn(self) -> bool:
        return self.action is GuardrailAction.WARN


@dataclass
class GuardrailState:
    """Mutable counters for one agent turn / session."""

    recent_failures: list[bool] = field(default_factory=list)
    exact_failure_counts: dict[str, int] = field(default_factory=dict)
    tool_streaks: dict[str, int] = field(default_factory=dict)
    no_progress: dict[str, tuple[str, int]] = field(default_factory=dict)
    warned_signatures: set[str] = field(default_factory=set)
    blocked_signatures: set[str] = field(default_factory=set)
    blocked_tools: set[str] = field(default_factory=set)
    tools_warned: set[str] = field(default_factory=set)
    # Cross-loop blacklist: "tool_name: short reason"
    failed_methods: list[str] = field(default_factory=list)
    reflection_count: int = 0
    forced_stop_count: int = 0


def args_signature(arguments: Any, max_len: int = 200) -> str:
    """Stable, compact signature of tool arguments for equality checks."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        text = arguments
    elif isinstance(arguments, Mapping):
        try:
            text = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            parts: list[str] = []
            for k in sorted(arguments.keys(), key=lambda x: str(x)):
                v = str(arguments[k])
                if len(v) > 60:
                    v = v[:57] + "..."
                parts.append(f"{k}={v}")
            text = ",".join(parts)
    elif isinstance(arguments, (list, tuple)):
        text = str(list(arguments))
    else:
        text = str(arguments)
    return text[:max_len]


def result_signature(result: Any, max_len: int = 256) -> str:
    """Compact equality signature for tool results (no-progress detection)."""
    if isinstance(result, BaseException):
        return f"exc:{type(result).__name__}"
    text = result if isinstance(result, str) else str(result)
    return text[:max_len]


def _dict_has_error(obj: Mapping[str, Any]) -> bool:
    for k in ("error", "errors", "error_code", "errcode"):
        if obj.get(k):
            return True
    for k in ("status", "status_code", "http_status", "code"):
        v = obj.get(k)
        if isinstance(v, int) and v >= 400:
            return True
    if obj.get("ok") is False or obj.get("success") is False:
        return True
    return False


class PreblockedResult(str):
    """Synthetic tool result returned when a call was denied before execution.

    Subclasses ``str`` so it remains a valid tool-result payload for the LLM,
    while remaining distinguishable from a real executor failure so the
    failure-window / blacklist are not inflated by re-observation.
    """

    __slots__ = ()


def is_preblocked_result(result: Any) -> bool:
    return isinstance(result, PreblockedResult)


def is_failure_result(result: Any) -> bool:
    """Single failure classifier used by the iteration runner and this engine.

    Pre-blocked denials still classify as failures for metrics / TOOL_COMPLETED
    (they look like Error strings to the model). The iteration runner skips
    ``observe()`` for :class:`PreblockedResult` so the failure window is not
    re-inflated after a signature/tool is already blocked.
    """
    if isinstance(result, BaseException):
        return True
    if result is None:
        return False
    if isinstance(result, dict):
        return _dict_has_error(result)
    if not isinstance(result, str):
        return False

    text = result
    if text.startswith("Error:") or text.lstrip().lower().startswith("error "):
        return True

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and _dict_has_error(parsed):
            return True

    lower = text.lower()
    return any(kw in lower for kw in _FAILURE_KEYWORDS)


def summarize_failed_method(tool_name: str, result: Any, max_detail: int = 80) -> str:
    """Build ``tool_name: short reason`` for the failure blacklist."""
    if isinstance(result, BaseException):
        detail = f"{type(result).__name__}: {result}"
    elif isinstance(result, dict):
        err = result.get("error") or result.get("errors") or result.get("error_code")
        detail = str(err) if err else str(result)
    elif isinstance(result, str):
        stripped = result.lstrip()
        detail = result
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                err = obj.get("error") or obj.get("errors") or obj.get("error_code")
                if err:
                    detail = str(err)
    else:
        detail = str(result)
    if len(detail) > max_detail:
        detail = detail[: max_detail - 3] + "..."
    return f"{tool_name}: {detail}"


def _strictest(*decisions: GuardrailDecision) -> GuardrailDecision:
    order = {
        GuardrailAction.ALLOW: 0,
        GuardrailAction.WARN: 1,
        GuardrailAction.BLOCK: 2,
        GuardrailAction.HALT: 3,
    }
    best = GuardrailDecision(action=GuardrailAction.ALLOW)
    for d in decisions:
        if order[d.action] > order[best.action]:
            best = d
    return best


class ToolCallGuardrail:
    """Stateful pure decision engine for one agent turn / session."""

    def __init__(
        self,
        config: GuardrailConfig | None = None,
        state: GuardrailState | None = None,
    ) -> None:
        self.config = config or GuardrailConfig()
        self.state = state or GuardrailState()

    def reset_turn_local(self) -> None:
        """Clear per-turn counters that should not cross user turns.

        Cross-loop fields (reflection / forced_stop / failed_methods) stay.
        """
        self.state.recent_failures.clear()
        self.state.exact_failure_counts.clear()
        self.state.tool_streaks.clear()
        self.state.no_progress.clear()
        self.state.warned_signatures.clear()
        self.state.blocked_signatures.clear()
        self.state.blocked_tools.clear()
        self.state.tools_warned.clear()

    def observe(
        self,
        tool_name: str,
        arguments: Any,
        result: Any,
        *,
        is_failure: bool | None = None,
    ) -> GuardrailDecision:
        """Ingest one tool observation; return strictest per-call decision.

        Does **not** evaluate the failure window — call
        :meth:`evaluate_failure_window` after a tool batch so reflection /
        halt policy stays turn-scoped rather than spamming every call.
        """
        if not self.config.enabled:
            return GuardrailDecision(action=GuardrailAction.ALLOW)

        failed = is_failure_result(result) if is_failure is None else bool(is_failure)
        self.state.recent_failures.append(failed)
        if len(self.state.recent_failures) > self.config.window_size:
            self.state.recent_failures = self.state.recent_failures[-self.config.window_size :]

        if failed and tool_name:
            self.note_failed_method(tool_name, result)

        decisions = [
            self._observe_exact_failure(tool_name, arguments, failed),
            self._observe_tool_streak(tool_name, failed),
        ]
        if not failed:
            decisions.append(self._observe_no_progress(tool_name, arguments, result))
        return _strictest(*decisions)

    def evaluate_failure_window(self) -> GuardrailDecision:
        """Check rolling failure rate for reflection warn / forced halt."""
        if not self.config.enabled:
            return GuardrailDecision(action=GuardrailAction.ALLOW)

        cfg = self.config
        st = self.state
        if len(st.recent_failures) < cfg.window_failure_threshold:
            return GuardrailDecision(action=GuardrailAction.ALLOW)

        failures = sum(1 for f in st.recent_failures if f)
        if failures < cfg.window_failure_threshold:
            return GuardrailDecision(action=GuardrailAction.ALLOW)

        if st.forced_stop_count > 0 or st.reflection_count >= cfg.max_reflections:
            return GuardrailDecision(
                action=GuardrailAction.HALT,
                reason="failure_window_exhausted",
                count=failures,
                message=(
                    f"Consecutive tool failures ({failures}/{len(st.recent_failures)}) "
                    f"exceeded reflection budget "
                    f"({st.reflection_count}/{cfg.max_reflections})."
                ),
            )

        return GuardrailDecision(
            action=GuardrailAction.WARN,
            reason="failure_window",
            count=failures,
            message=(
                f"Recent tool window has {failures} failures out of "
                f"{len(st.recent_failures)} calls. Change strategy or stop."
            ),
        )

    def note_reflection(self) -> int:
        """Record that a reflection prompt was injected; return new count."""
        self.state.reflection_count += 1
        if self.state.recent_failures:
            keep = max(1, self.config.window_size // 2)
            self.state.recent_failures = self.state.recent_failures[-keep:]
        return self.state.reflection_count

    def note_forced_stop(self) -> int:
        self.state.forced_stop_count += 1
        return self.state.forced_stop_count

    def note_failed_method(self, tool_name: str, result: Any) -> str | None:
        """Append a de-duplicated failure blacklist entry. Return entry or None."""
        entry = summarize_failed_method(tool_name, result)
        if not entry or entry in self.state.failed_methods:
            return None
        cap = max(1, self.config.failed_methods_cap)
        if len(self.state.failed_methods) >= cap:
            self.state.failed_methods = self.state.failed_methods[-(cap - 1) :]
        self.state.failed_methods.append(entry)
        return entry

    def is_call_blocked(self, tool_name: str, arguments: Any = None) -> bool:
        """True when this tool (or exact signature) is blocked for the turn."""
        if tool_name in self.state.blocked_tools:
            return True
        if arguments is None:
            return False
        key = f"{tool_name}:{args_signature(arguments)}"
        return key in self.state.blocked_signatures

    def is_signature_blocked(self, tool_name: str, arguments: Any) -> bool:
        """Alias for callers that only care about exact-arg blocks."""
        return self.is_call_blocked(tool_name, arguments)

    def block_message(self, tool_name: str, arguments: Any = None) -> str:
        """Human-readable denial for a pre-blocked tool call."""
        if tool_name in self.state.blocked_tools:
            return (
                f"Error: tool `{tool_name}` is blocked for this turn after "
                "repeated consecutive failures. Use a different tool or approach."
            )
        sig = args_signature(arguments)
        return (
            f"Error: tool `{tool_name}` with args [{sig}] is blocked after "
            "repeated identical failures / no-progress loops. "
            "Change arguments or method."
        )

    def to_persisted(self) -> dict[str, Any]:
        """Serialize cross-loop fields for session failure-state storage."""
        return {
            "reflection_injected_count": self.state.reflection_count,
            "forced_stop_count": self.state.forced_stop_count,
            "failed_methods": list(self.state.failed_methods),
        }

    def load_persisted(self, data: Mapping[str, Any] | None) -> None:
        if not data:
            return
        self.state.reflection_count = int(data.get("reflection_injected_count", 0) or 0)
        self.state.forced_stop_count = int(data.get("forced_stop_count", 0) or 0)
        methods = data.get("failed_methods") or []
        if isinstance(methods, list):
            self.state.failed_methods = [str(m) for m in methods]

    # ------------------------------------------------------------------
    # Internal observers
    # ------------------------------------------------------------------

    def _observe_exact_failure(
        self, tool_name: str, arguments: Any, failed: bool
    ) -> GuardrailDecision:
        sig = args_signature(arguments)
        if not tool_name or not sig:
            return GuardrailDecision(action=GuardrailAction.ALLOW)
        key = f"{tool_name}:{sig}"
        if not failed:
            for k in [
                k for k in self.state.exact_failure_counts if k.startswith(f"{tool_name}:")
            ]:
                del self.state.exact_failure_counts[k]
            return GuardrailDecision(action=GuardrailAction.ALLOW)

        count = self.state.exact_failure_counts.get(key, 0) + 1
        self.state.exact_failure_counts[key] = count
        cfg = self.config

        if count >= cfg.exact_failure_block:
            self.state.blocked_signatures.add(key)
            return GuardrailDecision(
                action=GuardrailAction.BLOCK,
                reason="exact_failure_block",
                tool_name=tool_name,
                signature=sig,
                count=count,
                message=(
                    f"Tool `{tool_name}` with args [{sig}] failed {count} times. "
                    "Identical retries are blocked — change arguments or method."
                ),
            )
        if count >= cfg.exact_failure_warn and key not in self.state.warned_signatures:
            self.state.warned_signatures.add(key)
            return GuardrailDecision(
                action=GuardrailAction.WARN,
                reason="exact_failure",
                tool_name=tool_name,
                signature=sig,
                count=count,
                message=(
                    f"Tool `{tool_name}` with args [{sig}] failed {count} times. "
                    "Do not retry the same call; inspect paths or switch approach."
                ),
            )
        return GuardrailDecision(action=GuardrailAction.ALLOW)

    def _observe_tool_streak(self, tool_name: str, failed: bool) -> GuardrailDecision:
        if not tool_name:
            return GuardrailDecision(action=GuardrailAction.ALLOW)
        if not failed:
            self.state.tool_streaks[tool_name] = 0
            return GuardrailDecision(action=GuardrailAction.ALLOW)

        streak = self.state.tool_streaks.get(tool_name, 0) + 1
        self.state.tool_streaks[tool_name] = streak
        cfg = self.config

        if streak >= cfg.tool_streak_block:
            self.state.blocked_tools.add(tool_name)
            return GuardrailDecision(
                action=GuardrailAction.BLOCK,
                reason="tool_streak_block",
                tool_name=tool_name,
                count=streak,
                message=(
                    f"Tool `{tool_name}` failed {streak} times in a row and is "
                    "blocked for this turn. Use a different tool."
                ),
            )
        if streak >= cfg.tool_streak_warn and tool_name not in self.state.tools_warned:
            self.state.tools_warned.add(tool_name)
            return GuardrailDecision(
                action=GuardrailAction.WARN,
                reason="tool_streak",
                tool_name=tool_name,
                count=streak,
                message=(
                    f"Tool `{tool_name}` failed {streak} times in a row. "
                    "Stop using it and switch methods."
                ),
            )
        return GuardrailDecision(action=GuardrailAction.ALLOW)

    def _observe_no_progress(
        self, tool_name: str, arguments: Any, result: Any
    ) -> GuardrailDecision:
        if tool_name not in self.config.idempotent_tools:
            return GuardrailDecision(action=GuardrailAction.ALLOW)
        sig = args_signature(arguments)
        if not sig:
            return GuardrailDecision(action=GuardrailAction.ALLOW)
        key = f"{tool_name}:{sig}"
        rsig = result_signature(result)
        prev = self.state.no_progress.get(key)
        if prev is None or prev[0] != rsig:
            self.state.no_progress[key] = (rsig, 1)
            return GuardrailDecision(action=GuardrailAction.ALLOW)

        count = prev[1] + 1
        self.state.no_progress[key] = (rsig, count)
        cfg = self.config

        if count >= cfg.no_progress_block:
            self.state.blocked_signatures.add(key)
            return GuardrailDecision(
                action=GuardrailAction.BLOCK,
                reason="no_progress_block",
                tool_name=tool_name,
                signature=sig,
                count=count,
                message=(
                    f"Idempotent tool `{tool_name}` with args [{sig}] returned "
                    f"identical output {count} times. Further identical calls are blocked."
                ),
            )
        if count >= cfg.no_progress_warn and key not in self.state.warned_signatures:
            self.state.warned_signatures.add(key)
            return GuardrailDecision(
                action=GuardrailAction.WARN,
                reason="no_progress",
                tool_name=tool_name,
                signature=sig,
                count=count,
                message=(
                    f"Idempotent tool `{tool_name}` with args [{sig}] returned "
                    f"identical output {count} times. Act on known data instead of re-reading."
                ),
            )
        return GuardrailDecision(action=GuardrailAction.ALLOW)
