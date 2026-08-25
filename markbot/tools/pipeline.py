"""Five-stage tool execution pipeline.

Stages, in order::

    resolve → guards → pre-execute(+approval) → execute(timeout) → post-execute → finalize

Design rules borrowed from dsh's tool registry:

* **Guards are monotonic** — they may only deny, never allow. Listener
  ordering therefore cannot turn a denial back into a permission.
* **Pre-execute decisions exclude parameter rewriting.** By the time a
  decision is rendered the args have been logged/presented; rewriting
  them here would desynchronise what was approved from what runs.
  (Permission handlers that populate ``updated_input`` remain supported
  for back-compat but new code should prefer post-execute replace.)
* **Timeouts are declarative.** ``ToolDefinition.timeout_ms`` states the
  budget; this pipeline enforces it. The declaration never reaches the
  model.
* **Cancellation distinguishes before-dispatch from mid-flight**, so
  telemetry can tell "never started" from "started then cancelled".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

from loguru import logger

from markbot.errors import (
    ErrorCode,
    MarkbotError,
    ToolCancelledBeforeDispatchError,
    ToolTimeoutError,
    tool_result_error,
)
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext


class PreblockedResult(str):
    """Synthetic tool result returned when a call was denied before execution.

    Subclasses ``str`` so it remains a valid tool-result payload for the LLM,
    while remaining distinguishable from a real executor failure so the
    failure-window / blacklist are not inflated by re-observation.
    """

    __slots__ = ()


def is_preblocked_result(result: Any) -> bool:
    return isinstance(result, PreblockedResult)


@dataclass(frozen=True)
class PreToolDecision:
    """Outcome of the pre-execute stage."""

    outcome: Literal["allow", "deny", "ask"]
    reason: Optional[str] = None
    code: ErrorCode = ErrorCode.TOOL_PERMISSION_DENIED
    updated_input: Optional[dict[str, Any]] = None

    @staticmethod
    def allow() -> "PreToolDecision":
        return _ALLOW

    @staticmethod
    def deny(reason: str, code: ErrorCode = ErrorCode.TOOL_PERMISSION_DENIED) -> "PreToolDecision":
        return PreToolDecision("deny", reason=reason, code=code)

    @staticmethod
    def ask(reason: str | None = None) -> "PreToolDecision":
        return PreToolDecision("ask", reason=reason)


_ALLOW = PreToolDecision("allow")

#: Monotonic guard: abstain with None / empty message; deny with a reason.
#: Returning a truthy value is ALWAYS a denial — there is no allow channel.
GuardResult = Union[str, None]
AsyncGuard = Callable[[str, dict[str, Any], ToolContext], Union[GuardResult, Awaitable[GuardResult]]]

#: Pre-execute hook: full decision vocabulary.
AsyncPreHook = Callable[
    [str, dict[str, Any], ToolContext],
    Union[PreToolDecision, Awaitable[PreToolDecision]],
]

#: Post-execute hook: observe or replace the result.
@dataclass(frozen=True)
class PostToolDecision:
    action: Literal["accept", "replace"] = "accept"
    content: Any = None

    @staticmethod
    def replace(content: Any) -> "PostToolDecision":
        return PostToolDecision("replace", content)


AsyncPostHook = Callable[
    [str, dict[str, Any], ToolContext, Any],
    Union[PostToolDecision, None, Awaitable[Union[PostToolDecision, None]]],
]

#: Audit sink: receives ``(event_type, payload)`` for durable events.
EventSink = Callable[[str, dict[str, Any]], Union[None, Awaitable[None]]]


@dataclass
class _StageHooks:
    guards: list[AsyncGuard] = field(default_factory=list)
    pre_hooks: list[AsyncPreHook] = field(default_factory=list)
    post_hooks: list[AsyncPostHook] = field(default_factory=list)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class ToolPipeline:
    """Owns the guarded execution stages; :class:`ToolRegistry` delegates."""

    def __init__(self) -> None:
        self._hooks = _StageHooks()
        self._event_sink: EventSink | None = None

    # -- registration (each returns a disposer) ------------------------

    def add_guard(self, guard: AsyncGuard) -> Callable[[], None]:
        """Register a deny-only guard. Disposal unregisters it."""
        self._hooks.guards.append(guard)

        def dispose() -> None:
            try:
                self._hooks.guards.remove(guard)
            except ValueError:
                pass

        return dispose

    def add_pre_execute_hook(self, hook: AsyncPreHook) -> Callable[[], None]:
        self._hooks.pre_hooks.append(hook)

        def dispose() -> None:
            try:
                self._hooks.pre_hooks.remove(hook)
            except ValueError:
                pass

        return dispose

    def add_post_execute_hook(self, hook: AsyncPostHook) -> Callable[[], None]:
        self._hooks.post_hooks.append(hook)

        def dispose() -> None:
            try:
                self._hooks.post_hooks.remove(hook)
            except ValueError:
                pass

        return dispose

    def set_event_sink(self, sink: EventSink | None) -> None:
        self._event_sink = sink

    # -- audit ----------------------------------------------------------

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            await _maybe_await(sink(event_type, payload))
        except Exception as exc:  # noqa: BLE001 — audit must never break execution
            logger.debug("tool event sink failed for {}: {}", event_type, exc)

    # -- stages ----------------------------------------------------------

    async def stage_guards(
        self,
        name: str,
        params: dict[str, Any],
        context: ToolContext,
        *,
        as_preblocked: bool = False,
        extra_guards: list[AsyncGuard] | None = None,
    ) -> str | None:
        """Run monotonic guards. Returns the denial text or None.

        Installed guards run first, then *extra_guards* — call-scoped
        guards supplied by the caller (e.g. a turn runner's loop-cap /
        failure-blacklist checks). First denial wins and short-circuits.

        Denial wrapping: installed-guard denials are plain strings unless
        *as_preblocked*; extra-guard denials are always wrapped in
        :class:`PreblockedResult` since callers that supply turn-scoped
        guards rely on failure-window skip semantics.

        Fail-closed: a guard that raises is treated as a denial, never as
        an abstention — a broken guard must not silently disable the
        restriction it exists to enforce.
        """
        for guard in list(self._hooks.guards):
            try:
                verdict = await _maybe_await(guard(name, params, context))
            except Exception as exc:  # noqa: BLE001
                logger.error("Guard raised for tool {} — failing closed: {}", name, exc)
                return await self._guard_denial(
                    name,
                    f"Guard failure while checking '{name}'; denied (fail-closed).",
                    context,
                    as_preblocked,
                )
            if verdict:
                return await self._guard_denial(
                    name, str(verdict), context, as_preblocked
                )
        for guard in list(extra_guards or []):
            try:
                verdict = await _maybe_await(guard(name, params, context))
            except Exception as exc:  # noqa: BLE001
                logger.error("Call guard raised for tool {} — failing closed: {}", name, exc)
                return await self._guard_denial(
                    name,
                    f"Guard failure while checking '{name}'; denied (fail-closed).",
                    context,
                    as_preblocked=True,
                )
            if verdict:
                return await self._guard_denial(
                    name, str(verdict), context, as_preblocked=True
                )
        return None

    async def _guard_denial(
        self, name: str, denial: str, context: ToolContext, as_preblocked: bool
    ) -> str:
        await self._emit(
            "guard.denied",
            {"tool": name, "reason": denial[:200], "session_id": context.session_id},
        )
        return PreblockedResult(denial) if as_preblocked else denial

    async def stage_pre_execute(
        self,
        name: str,
        params: dict[str, Any],
        context: ToolContext,
        *,
        base_decision: PermissionDecision,
        approver: Callable | None,
    ) -> tuple[Any, dict[str, Any]]:
        """Combine permission decision with pre-hooks; run approval on ask.

        Returns ``(effective_params, denial_text_or_None)``.

        Failure semantics differ from :meth:`stage_guards` by design:
        hooks are middleware whose decisions layer ON TOP of the base
        permission check, so a raising hook is skipped (logged) and the
        base decision still applies. Guards are the security monotonic
        layer and fail closed.
        """
        effective = params
        decision = base_decision

        for hook in list(self._hooks.pre_hooks):
            try:
                hook_decision = await _maybe_await(hook(name, effective, context))
            except Exception as exc:  # noqa: BLE001
                logger.error("Pre-execute hook failed for {}: {}", name, exc)
                continue
            if not isinstance(hook_decision, PreToolDecision):
                continue
            if hook_decision.outcome == "deny":
                return effective, tool_result_error(
                    hook_decision.code, f"Tool '{name}' denied: {hook_decision.reason or 'policy'}"
                )
            if hook_decision.outcome == "ask" and decision.behavior == "allow":
                decision = PermissionDecision(behavior="ask", reason=hook_decision.reason)
            if hook_decision.updated_input:
                # Legacy escape hatch — see module docstring caveat.
                effective = hook_decision.updated_input

        if decision.behavior == "deny":
            return effective, tool_result_error(
                ErrorCode.TOOL_PERMISSION_DENIED, f"Tool '{name}' execution denied."
            )

        if decision.behavior == "ask":
            reason = decision.reason or "This tool requires confirmation before running."
            if approver is None:
                if context.is_non_interactive:
                    return effective, tool_result_error(
                        ErrorCode.APPROVAL_UNAVAILABLE,
                        f"Tool '{name}' requires confirmation but the session "
                        f"is non-interactive. Reason: {reason}",
                    )
                return effective, tool_result_error(
                    ErrorCode.APPROVAL_REQUIRED,
                    f"Permission required: Tool '{name}' was not executed. "
                    f"Reason: {reason} No interactive approval channel is configured. "
                    "Use /mode auto for unattended runs, or enable an approver.",
                )
            await self._emit(
                "approval.asked",
                {"tool": name, "reason": reason[:200], "session_id": context.session_id},
            )
            try:
                approved = approver(name, effective, context, reason)
                approved = await _maybe_await(approved)
            except Exception as exc:  # noqa: BLE001
                logger.error("Permission approver failed for {}: {}", name, exc)
                await self._emit("approval.decided", {"tool": name, "approved": False, "error": str(exc)[:120]})
                return effective, tool_result_error(
                    ErrorCode.APPROVAL_FAILED, f"Permission approval failed for '{name}': {exc}"
                )
            await self._emit("approval.decided", {"tool": name, "approved": bool(approved)})
            if not approved:
                return effective, tool_result_error(
                    ErrorCode.APPROVAL_DENIED,
                    f"Tool '{name}' execution denied by user. Reason: {reason}",
                )

        if decision.updated_input:
            effective = decision.updated_input
        return effective, None

    async def stage_execute(
        self,
        tool: Any,
        name: str,
        params: dict[str, Any],
        context: ToolContext,
    ) -> Any:
        """Execute the tool body under its declared timeout budget."""
        timeout_ms = None
        definition = getattr(tool, "definition", None)
        if definition is not None:
            timeout_ms = getattr(definition, "timeout_ms", None)

        coro = tool.execute(params, context)
        if not timeout_ms or timeout_ms <= 0:
            logger.info("Executing tool: {}", name)
            return await coro
        logger.info("Executing tool: {} (timeout={}ms)", name, timeout_ms)
        try:
            return await asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            raise ToolTimeoutError(
                f"Tool '{name}' timed out after {timeout_ms}ms",
                timeout_ms=int(timeout_ms),
            ) from None

    async def stage_post_execute(
        self,
        name: str,
        params: dict[str, Any],
        context: ToolContext,
        result: Any,
    ) -> Any:
        for hook in list(self._hooks.post_hooks):
            try:
                decision = await _maybe_await(hook(name, params, context, result))
            except Exception as exc:  # noqa: BLE001
                logger.error("Post-execute hook failed for {}: {}", name, exc)
                continue
            if isinstance(decision, PostToolDecision) and decision.action == "replace":
                result = decision.content
        return result

    @staticmethod
    def finalize(result: Any) -> Any:
        """Normalise odd return shapes into model-consumable values."""
        if isinstance(result, (str, list)) or result is None:
            return result
        return str(result)

    # -- full run ---------------------------------------------------------

    async def run(
        self,
        tool: Any,
        name: str,
        params: dict[str, Any],
        context: ToolContext,
        *,
        base_decision: PermissionDecision,
        approver: Callable | None,
        guard_denials_preblocked: bool = False,
        extra_guards: list[AsyncGuard] | None = None,
    ) -> Any:
        """Execute all stages for one resolved tool call.

        *extra_guards* are call-scoped monotonic guards (see
        :meth:`stage_guards`); their denials come back wrapped in
        :class:`PreblockedResult`.
        """
        started = time.monotonic()

        # Stage 1 — monotonic guards (deny-only).
        denial = await self.stage_guards(
            name,
            params,
            context,
            as_preblocked=guard_denials_preblocked,
            extra_guards=extra_guards,
        )
        if denial is not None:
            return denial

        # Stage 2 — pre-execute hooks + interactive approval.
        effective_params, denial = await self.stage_pre_execute(
            name, params, context, base_decision=base_decision, approver=approver
        )
        if denial is not None:
            return denial

        # Cancellation before dispatch is distinguishable from mid-flight.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise ToolCancelledBeforeDispatchError(
                f"Tool '{name}' cancelled before dispatch"
            )

        await self._emit("tool.call", {"tool": name, "session_id": context.session_id})

        # Stage 3 — execute under declared budget.
        try:
            result = await self.stage_execute(tool, name, effective_params, context)
        except MarkbotError as exc:
            logger.error("Tool {} failed: [{}] {}", name, exc.code.value, exc.message)
            result = exc.model_text()
        except asyncio.CancelledError:
            logger.warning("Tool {} cancelled mid-execution", name)
            await self._emit(
                "tool.result",
                {
                    "tool": name,
                    "session_id": context.session_id,
                    "cancelled": True,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            # Preserve the asyncio cancellation contract: never swallow.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Tool execution failed: {}", exc)
            result = tool_result_error(ErrorCode.TOOL_EXECUTION_FAILED, f"Error executing {name}: {exc}")

        # Stage 4 — post-execute observation / replacement.
        result = await self.stage_post_execute(name, params, context, result)

        # Stage 5 — finalise.
        return self.finalize(result)


def soft_disabled_error(tool_name: str, available: list[str]) -> str:
    return tool_result_error(
        ErrorCode.TOOL_SOFT_DISABLED,
        f"Tool '{tool_name}' is soft-disabled by the current tool footprint / "
        f"profile and cannot be executed. Available tools: {', '.join(available) or '(none)'}.",
    )


def unknown_tool_error(name: str, available: list[str]) -> str:
    return tool_result_error(
        ErrorCode.TOOL_NOT_FOUND,
        f"Tool '{name}' does not exist. This may be a hallucinated tool name. "
        f"Available tools: {', '.join(sorted(available))}. Use only the tools listed above.",
    )


__all__ = [
    "ErrorCode",
    "MarkbotError",
    "PostToolDecision",
    "PreToolDecision",
    "PreblockedResult",
    "ToolTimeoutError",
    "ToolPipeline",
    "is_preblocked_result",
    "soft_disabled_error",
    "unknown_tool_error",
]
