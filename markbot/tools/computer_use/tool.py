"""Entry point for the ``computer_use`` tool.

Universal cross-platform desktop control via pluggable backends
(cua-driver, pyautogui). The schema is standard OpenAI function-calling
so every tool-capable model can drive it.

Return contract
---------------
For text-only results (wait, key, list_apps, focus_app, failures, etc.):
  JSON string.

For captures / actions with ``capture_after=True``:
  A dict wrapped as the OpenAI-style multi-part tool-message content:

      {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "<human-readable summary + SOM index>"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,<b64>"}},
        ],
        "text_summary": "<text used for fallback string content>",
      }

Vision routing policy
---------------------
When the active model cannot process images in tool results, the multimodal
envelope is downgraded to text-only via ``vision_routing.should_route_to_text_only``.
Non-vision providers (DeepSeek, Groq, etc.) never receive base64 images inside
tool-result blocks — they see the text summary only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional

from markbot.tools.base import Tool
from markbot.tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)
from markbot.tools.computer_use.schema import get_computer_use_schema
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safety: blocked patterns
# ---------------------------------------------------------------------------

_SAFE_ACTIONS = frozenset({"capture", "wait", "list_apps"})

_BLOCKED_KEY_COMBOS = {
    # macOS
    frozenset({"cmd", "shift", "backspace"}),
    frozenset({"cmd", "option", "backspace"}),
    frozenset({"cmd", "ctrl", "q"}),
    frozenset({"cmd", "shift", "q"}),
    frozenset({"cmd", "option", "shift", "q"}),
    # Windows/Linux
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "shift", "esc"}),
    frozenset({"ctrl", "alt", "del"}),
}

_KEY_ALIASES = {"command": "cmd", "control": "ctrl", "alt": "option", "\u2318": "cmd", "\u2325": "option"}


def _canon_key_combo(keys: str) -> frozenset:
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys) if p.strip()]
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    return frozenset(parts)


_BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\s+-[rf]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}", re.IGNORECASE),
]


def _is_blocked_type(text: str) -> Optional[str]:
    for pat in _BLOCKED_TYPE_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# ---------------------------------------------------------------------------
# Backend selection — env-swappable, auto-detect platform
# ---------------------------------------------------------------------------

_backend_lock = threading.Lock()
_backend: Optional[ComputerUseBackend] = None


def _select_backend(config: Any = None) -> ComputerUseBackend:
    """Instantiate a backend WITHOUT starting it.

    Used by ``is_enabled`` to probe availability without spawning subprocesses
    (cua-driver MCP server) or stealing the cursor. The caller is responsible
    for calling ``start()`` if it intends to actually use the backend.
    """
    env_name = os.environ.get("MARKBOT_COMPUTER_USE_BACKEND", "").lower().strip()
    cfg_name = ""
    if config is not None:
        cfg_name = (getattr(config, "backend", "") or "").lower().strip()
    # Env var overrides config (useful for CI / debugging overrides).
    backend_name = env_name or cfg_name

    if backend_name == "noop":
        from markbot.tools.computer_use.noop_backend import NoopBackend
        return NoopBackend()
    if backend_name == "pyautogui":
        from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
        return PyAutoGUIBackend()
    if backend_name in {"cua", "cua-driver", "atspi", ""}:
        if sys.platform == "darwin":
            from markbot.tools.computer_use.cua_backend import (
                CuaDriverBackend,
                cua_driver_binary_available,
            )
            if cua_driver_binary_available():
                return CuaDriverBackend()
            from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
            return PyAutoGUIBackend()
        if sys.platform == "linux":
            # Linux: prefer AT-SPI (element targeting with real bounds),
            # fall back to pyautogui if pyatspi / at-spi2 registry is
            # unavailable. Explicit atspi + unavailability = hard error.
            from markbot.tools.computer_use.atspi_backend import _probe_pyatspi
            atspi_ok = _probe_pyatspi()
            if atspi_ok:
                from markbot.tools.computer_use.atspi_backend import AtspiBackend
                return AtspiBackend()
            if backend_name == "atspi":
                raise RuntimeError(
                    "MARKBOT_COMPUTER_USE_BACKEND=atspi but pyatspi or the "
                    "at-spi2 registry is unavailable (install python3-pyatspi "
                    "and ensure an at-spi session bus is running)"
                )
            from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
            return PyAutoGUIBackend()
        from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
        return PyAutoGUIBackend()
    raise RuntimeError(f"Unknown MARKBOT_COMPUTER_USE_BACKEND={backend_name!r}")


def _get_backend(config: Any = None) -> ComputerUseBackend:
    """Return the cached backend, instantiating and starting it on first call."""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = _select_backend(config)
            if _backend.is_available():
                _backend.start()
        return _backend


def reset_backend_for_tests() -> None:
    global _backend
    with _backend_lock:
        if _backend is not None:
            try:
                _backend.stop()
            except Exception:
                logger.debug("Failed to stop computer_use backend during test reset", exc_info=True)
        _backend = None


_DEFAULT_MAX_ELEMENTS = 100
_MAX_ALLOWED_MAX_ELEMENTS = 1000


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dispatch(
    backend: ComputerUseBackend,
    action: str,
    args: Dict[str, Any],
    capture_after_default: bool = True,
    max_elements_default: int = _DEFAULT_MAX_ELEMENTS,
) -> Any:
    # ``capture_after`` defaults to the tool-level config value when the model
    # doesn't explicitly request or suppress it.
    ca_raw = args.get("capture_after")
    capture_after = bool(ca_raw) if ca_raw is not None else capture_after_default

    if action == "capture":
        mode = str(args.get("mode", "som"))
        if mode not in {"som", "vision", "ax"}:
            return json.dumps({"error": f"bad mode {mode!r}; use som|vision|ax"})
        cap = backend.capture(mode=mode, app=args.get("app"))
        return _capture_response(cap, max_elements=_coerce_max_elements(args.get("max_elements"), max_elements_default))

    if action == "wait":
        seconds = float(args.get("seconds", 1.0))
        res = backend.wait(seconds)
        return _text_response(res)

    if action == "list_apps":
        apps = backend.list_apps()
        return json.dumps({"apps": apps, "count": len(apps)})

    if action == "focus_app":
        app = args.get("app")
        if not app:
            return json.dumps({"error": "focus_app requires `app`"})
        res = backend.focus_app(app, raise_window=bool(args.get("raise_window")))
        return _maybe_follow_capture(backend, res, capture_after)

    if action in {"click", "double_click", "right_click", "middle_click"}:
        button = args.get("button")
        click_count = 1
        if action == "double_click":
            click_count = 2
        elif action == "right_click":
            button = "right"
        elif action == "middle_click":
            button = "middle"
        else:
            button = button or "left"
        element = args.get("element")
        coord = args.get("coordinate") or (None, None)
        x, y = (coord[0], coord[1]) if coord and coord[0] is not None else (None, None)
        res = backend.click(
            element=element if element is not None else None,
            x=x, y=y, button=button or "left", click_count=click_count,
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "drag":
        has_elements = args.get("from_element") is not None and args.get("to_element") is not None
        has_coords = args.get("from_coordinate") and args.get("to_coordinate")
        if not has_elements and not has_coords:
            return json.dumps({
                "error": "drag requires from_coordinate/to_coordinate or from_element/to_element",
            })
        res = backend.drag(
            from_element=args.get("from_element"),
            to_element=args.get("to_element"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            button=args.get("button", "left"),
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "scroll":
        coord = args.get("coordinate") or (None, None)
        res = backend.scroll(
            direction=args.get("direction", "down"),
            amount=int(args.get("amount", 3)),
            element=args.get("element"),
            x=coord[0] if coord and coord[0] is not None else None,
            y=coord[1] if coord and coord[1] is not None else None,
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "type":
        res = backend.type_text(args.get("text", ""))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "key":
        res = backend.key(args.get("keys", ""))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "set_value":
        value = args.get("value")
        if value is None:
            return json.dumps({"error": "set_value requires `value`"})
        res = backend.set_value(value=str(value), element=args.get("element"))
        return _maybe_follow_capture(backend, res, capture_after)

    return json.dumps({"error": f"unknown action {action!r}"})


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

def _text_response(res: ActionResult) -> str:
    payload: Dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    if res.meta:
        payload["meta"] = res.meta
    return json.dumps(payload)


def _coerce_max_elements(value: Any, default: int = _DEFAULT_MAX_ELEMENTS) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    if n > _MAX_ALLOWED_MAX_ELEMENTS:
        return _MAX_ALLOWED_MAX_ELEMENTS
    return n


def _capture_response(cap: CaptureResult, max_elements: int = _DEFAULT_MAX_ELEMENTS) -> Any:
    total_elements = len(cap.elements)
    visible_elements = cap.elements[:max_elements]
    truncated_elements = max(0, total_elements - len(visible_elements))

    element_index = _format_elements(visible_elements)
    summary_lines = [
        f"capture mode={cap.mode} {cap.width}x{cap.height}"
        + (f" app={cap.app}" if cap.app else "")
        + (f" window={cap.window_title!r}" if cap.window_title else ""),
    ]
    # Tell the model which targeting mode this backend supports. The summary
    # text is the only channel that reliably reaches the model (add_tool_result
    # keeps content/text_summary and drops other top-level keys), so the
    # capability hint MUST live here.
    if cap.capabilities is not None:
        summary_lines.append(cap.capabilities.short_label())
    summary_lines.append(f"{total_elements} interactable element(s):")
    if element_index:
        summary_lines.extend(element_index)
    summary = "\n".join(summary_lines)

    if cap.png_b64 and cap.mode != "ax":
        _b64_prefix = cap.png_b64[:8]
        _mime = "image/jpeg" if _b64_prefix.startswith("/9j/") else "image/png"

        if _should_route_to_text_only():
            return _maybe_route_to_text_only(cap, summary)

        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url",
                 "image_url": {"url": f"data:{_mime};base64,{cap.png_b64}"}},
            ],
            "text_summary": summary,
            "meta": {"mode": cap.mode, "width": cap.width, "height": cap.height,
                     "elements": total_elements, "png_bytes": cap.png_bytes_len},
        }

    if truncated_elements:
        summary_lines.append(
            f"  (response truncated to {len(visible_elements)} of {total_elements} elements; "
            f"raise max_elements or pass app= to narrow)"
        )
    summary = "\n".join(summary_lines)
    payload: Dict[str, Any] = {
        "mode": cap.mode,
        "width": cap.width,
        "height": cap.height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in visible_elements],
        "total_elements": total_elements,
        "summary": summary,
    }
    if truncated_elements:
        payload["truncated_elements"] = truncated_elements
    return json.dumps(payload)


def _should_route_to_text_only() -> bool:
    try:
        from markbot.tools.computer_use.vision_routing import should_route_to_text_only
        return bool(should_route_to_text_only())
    except Exception:
        return False


def _maybe_route_to_text_only(cap: CaptureResult, summary: str) -> Any:
    payload: Dict[str, Any] = {
        "mode": cap.mode,
        "width": cap.width,
        "height": cap.height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in cap.elements],
        "total_elements": len(cap.elements),
        "summary": summary,
        "vision_routed_to_text_only": True,
    }
    return json.dumps(payload)


def _maybe_follow_capture(
    backend: ComputerUseBackend, res: ActionResult, do_capture: bool,
) -> Any:
    if not do_capture:
        return _text_response(res)
    if not res.ok:
        return _text_response(res)
    try:
        last_app = getattr(backend, "_last_app", None)
        cap = backend.capture(mode="som", app=last_app)
    except Exception as e:
        logger.warning("follow-up capture failed: %s", e)
        return _text_response(res)
    resp = _capture_response(cap)
    if isinstance(resp, dict) and resp.get("_multimodal"):
        prefix = f"[{res.action}] ok={res.ok}" + (f" — {res.message}" if res.message else "")
        resp["content"][0]["text"] = prefix + "\n\n" + resp["content"][0]["text"]
        resp["text_summary"] = prefix + "\n\n" + resp["text_summary"]
        return resp
    try:
        data = json.loads(resp)
    except (TypeError, json.JSONDecodeError):
        data = {"capture": resp}
    data["action"] = res.action
    data["ok"] = res.ok
    if res.message:
        data["message"] = res.message
    return json.dumps(data)


def _format_elements(elements: List[UIElement], max_lines: int = 40) -> List[str]:
    out: List[str] = []
    for e in elements[:max_lines]:
        label = e.label.replace("\n", " ")[:60]
        out.append(f"  #{e.index} {e.role} {label!r} @ {e.bounds}"
                   + (f" [{e.app}]" if e.app else ""))
    if len(elements) > max_lines:
        out.append(f"  ... +{len(elements) - max_lines} more (call capture with app= to narrow)")
    return out


def _element_to_dict(e: UIElement) -> Dict[str, Any]:
    return {
        "index": e.index,
        "role": e.role,
        "label": e.label,
        "bounds": list(e.bounds),
        "app": e.app,
    }


def _summarize_action(action: str, args: Dict[str, Any]) -> str:
    if action in {"click", "double_click", "right_click", "middle_click"}:
        if args.get("element") is not None:
            return f"{action} element #{args['element']}"
        coord = args.get("coordinate")
        if coord:
            return f"{action} at {tuple(coord)}"
        return action
    if action == "drag":
        src = args.get("from_element") or args.get("from_coordinate")
        dst = args.get("to_element") or args.get("to_coordinate")
        return f"drag {src} \u2192 {dst}"
    if action == "scroll":
        return f"scroll {args.get('direction', '?')} x{args.get('amount', 3)}"
    if action == "type":
        text = args.get("text", "")
        return f"type {text[:60]!r}" + ("..." if len(text) > 60 else "")
    if action == "key":
        return f"key {args.get('keys', '')!r}"
    if action == "focus_app":
        return f"focus {args.get('app', '')!r}" + (" (raise)" if args.get("raise_window") else "")
    return action


# ---------------------------------------------------------------------------
# ComputerUseTool — markbot BaseTool integration
# ---------------------------------------------------------------------------

class ComputerUseTool(Tool):
    """Desktop control tool — screenshots, mouse, keyboard, scroll, drag.

    Automatically selects the best backend for the current platform:
      - macOS + cua-driver:  cua-driver (background, no cursor steal)
      - Linux/Windows:       pyautogui (foreground, real cursor)
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config

        # Merge module-level safety defaults with config-provided extras so
        # users can extend (but never shrink) the blocked lists via YAML.
        self._blocked_key_combos: set = set(_BLOCKED_KEY_COMBOS)
        self._blocked_type_patterns: list = list(_BLOCKED_TYPE_PATTERNS)
        self._max_elements_default: int = _DEFAULT_MAX_ELEMENTS
        self._capture_after_default: bool = True

        if config is not None:
            for combo_str in getattr(config, "blocked_key_combos", None) or []:
                self._blocked_key_combos.add(_canon_key_combo(combo_str))
            for pat_str in getattr(config, "blocked_type_patterns", None) or []:
                try:
                    self._blocked_type_patterns.append(re.compile(pat_str, re.IGNORECASE))
                except re.error:
                    logger.warning("invalid blocked_type_patterns regex: %r", pat_str)
            cfg_max = getattr(config, "max_elements", None)
            if cfg_max is not None:
                self._max_elements_default = max(10, min(_MAX_ALLOWED_MAX_ELEMENTS, int(cfg_max)))
            self._capture_after_default = bool(getattr(config, "capture_after_actions", True))

    # ── Instance-level safety checks (config-aware) ──────────────────

    def _is_blocked_type(self, text: str) -> Optional[str]:
        for pat in self._blocked_type_patterns:
            if pat.search(text):
                return pat.pattern
        return None

    def _is_blocked_key_combo(self, keys: str) -> Optional[list]:
        combo = _canon_key_combo(keys)
        for blocked in self._blocked_key_combos:
            if blocked.issubset(combo):
                return sorted(blocked)
        return None

    @property
    def name(self) -> str:
        return "computer_use"

    @property
    def description(self) -> str:
        return get_computer_use_schema()["description"]

    @property
    def parameters(self) -> dict[str, Any]:
        return get_computer_use_schema()["parameters"]

    @property
    def is_enabled(self) -> bool:
        """Probe backend availability WITHOUT starting it.

        ``_select_backend`` only instantiates the backend object; it does not
        call ``start()``, so no cua-driver subprocess is spawned and no cursor
        is grabbed just because the tool registry asked ``is_enabled``.
        """
        try:
            backend = _select_backend(self._config)
            return backend.is_available()
        except Exception:
            return False

    def is_read_only(self, params: Dict[str, Any]) -> bool:
        return (params.get("action", "") or "").strip().lower() in _SAFE_ACTIONS

    def is_destructive(self, params: Dict[str, Any]) -> bool:
        return False

    async def check_permission(
        self, params: Dict[str, Any], context: ToolContext
    ) -> PermissionDecision:
        action = (params.get("action", "") or "").strip().lower()
        if action in _SAFE_ACTIONS:
            return PermissionDecision(behavior="allow")

        if action == "type":
            text = params.get("text", "")
            pat = self._is_blocked_type(text)
            if pat:
                return PermissionDecision(
                    behavior="deny",
                    reason=f"blocked pattern in type text: {pat!r}",
                )

        if action == "key":
            keys = params.get("keys", "")
            blocked = self._is_blocked_key_combo(keys)
            if blocked:
                return PermissionDecision(
                    behavior="deny",
                    reason=f"blocked key combo: {blocked}",
                )

        return await super().check_permission(params, context)

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        params = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        action = (params.get("action") or "").strip().lower()
        if not action:
            return json.dumps({"error": "missing `action`"})

        if action == "type":
            text = params.get("text", "")
            pat = self._is_blocked_type(text)
            if pat:
                return json.dumps({
                    "error": f"blocked pattern in type text: {pat!r}",
                    "hint": "Dangerous shell patterns cannot be typed via computer_use.",
                })

        if action == "key":
            keys = params.get("keys", "")
            blocked = self._is_blocked_key_combo(keys)
            if blocked:
                return json.dumps({
                    "error": f"blocked key combo: {blocked}",
                    "hint": "Destructive system shortcuts are hard-blocked.",
                })

        try:
            backend = _get_backend(self._config)
        except Exception as e:
            return json.dumps({
                "error": f"computer_use backend unavailable: {e}",
                "hint": "No suitable backend found for this platform.",
            })

        try:
            # Backends are synchronous (cua-driver's _AsyncBridge.run blocks up
            # to 30s). Run dispatch in a worker thread so the agent event loop
            # is not frozen during a single capture/action.
            return await asyncio.to_thread(
                _dispatch, backend, action, params,
                self._capture_after_default, self._max_elements_default,
            )
        except Exception as e:
            logger.exception("computer_use %s failed", action)
            return json.dumps({"error": f"{action} failed: {e}"})


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def check_computer_use_requirements() -> bool:
    """Probe whether a computer_use backend is available (without starting it)."""
    try:
        backend = _select_backend()
        return backend.is_available()
    except Exception:
        return False
