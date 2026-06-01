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

import json
import logging
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from markbot.tools.base import BaseTool
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext, ToolDefinition, ToolParameter

from markbot.tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)
from markbot.tools.computer_use.schema import get_computer_use_schema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safety: blocked patterns
# ---------------------------------------------------------------------------

_SAFE_ACTIONS = frozenset({"capture", "wait", "list_apps"})

_DESTRUCTIVE_ACTIONS = frozenset({
    "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "focus_app",
})

_BLOCKED_KEY_COMBOS = {
    frozenset({"cmd", "shift", "backspace"}),
    frozenset({"cmd", "option", "backspace"}),
    frozenset({"cmd", "ctrl", "q"}),
    frozenset({"cmd", "shift", "q"}),
    frozenset({"cmd", "option", "shift", "q"}),
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


def _get_backend() -> ComputerUseBackend:
    global _backend
    with _backend_lock:
        if _backend is None:
            backend_name = os.environ.get("MARKBOT_COMPUTER_USE_BACKEND", "").lower().strip()

            if backend_name == "noop":
                from markbot.tools.computer_use.noop_backend import NoopBackend
                _backend = NoopBackend()
            elif backend_name == "pyautogui":
                from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
                _backend = PyAutoGUIBackend()
            elif backend_name in {"cua", "cua-driver", ""}:
                if sys.platform == "darwin":
                    from markbot.tools.computer_use.cua_backend import CuaDriverBackend, cua_driver_binary_available
                    if cua_driver_binary_available():
                        _backend = CuaDriverBackend()
                    else:
                        from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
                        _backend = PyAutoGUIBackend()
                else:
                    from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
                    _backend = PyAutoGUIBackend()
            else:
                raise RuntimeError(f"Unknown MARKBOT_COMPUTER_USE_BACKEND={backend_name!r}")
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
                pass
        _backend = None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:
    capture_after = bool(args.get("capture_after"))

    if action == "capture":
        mode = str(args.get("mode", "som"))
        if mode not in {"som", "vision", "ax"}:
            return json.dumps({"error": f"bad mode {mode!r}; use som|vision|ax"})
        cap = backend.capture(mode=mode, app=args.get("app"))
        return _capture_response(cap, max_elements=_coerce_max_elements(args.get("max_elements")))

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


_DEFAULT_MAX_ELEMENTS = 100
_MAX_ALLOWED_MAX_ELEMENTS = 1000


def _coerce_max_elements(value: Any) -> int:
    if value is None:
        return _DEFAULT_MAX_ELEMENTS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ELEMENTS
    if n < 1:
        return _DEFAULT_MAX_ELEMENTS
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
        f"{total_elements} interactable element(s):",
    ]
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

class ComputerUseTool(BaseTool):
    """Desktop control tool — screenshots, mouse, keyboard, scroll, drag.

    Automatically selects the best backend for the current platform:
      - macOS + cua-driver:  cua-driver (background, no cursor steal)
      - Linux/Windows:       pyautogui (foreground, real cursor)
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config

    @property
    def definition(self) -> ToolDefinition:
        schema = get_computer_use_schema()
        params_schema = schema.get("parameters", {})
        properties = params_schema.get("properties", {})
        required = params_schema.get("required", [])

        tool_params = []
        for name, prop in properties.items():
            param = ToolParameter(
                name=name,
                type=prop.get("type", "string"),
                description=prop.get("description", ""),
                required=name in required,
            )
            if "enum" in prop:
                param.enum = prop["enum"]
            if "default" in prop:
                param.default = prop["default"]
            tool_params.append(param)

        return ToolDefinition(
            name=schema["name"],
            description=schema["description"],
            parameters=tool_params,
            is_read_only=False,
            is_destructive=False,
        )

    @property
    def is_enabled(self) -> bool:
        try:
            backend = _get_backend()
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
            pat = _is_blocked_type(text)
            if pat:
                return PermissionDecision(
                    behavior="deny",
                    reason=f"blocked pattern in type text: {pat!r}",
                )

        if action == "key":
            keys = params.get("keys", "")
            combo = _canon_key_combo(keys)
            for blocked in _BLOCKED_KEY_COMBOS:
                if blocked.issubset(combo) and len(blocked) <= len(combo):
                    return PermissionDecision(
                        behavior="deny",
                        reason=f"blocked key combo: {sorted(blocked)}",
                    )

        return await super().check_permission(params, context)

    async def execute(self, params: Dict[str, Any], context: ToolContext) -> Any:
        action = (params.get("action") or "").strip().lower()
        if not action:
            return json.dumps({"error": "missing `action`"})

        if action == "type":
            text = params.get("text", "")
            pat = _is_blocked_type(text)
            if pat:
                return json.dumps({
                    "error": f"blocked pattern in type text: {pat!r}",
                    "hint": "Dangerous shell patterns cannot be typed via computer_use.",
                })

        if action == "key":
            keys = params.get("keys", "")
            combo = _canon_key_combo(keys)
            for blocked in _BLOCKED_KEY_COMBOS:
                if blocked.issubset(combo) and len(blocked) <= len(combo):
                    return json.dumps({
                        "error": f"blocked key combo: {sorted(blocked)}",
                        "hint": "Destructive system shortcuts are hard-blocked.",
                    })

        try:
            backend = _get_backend()
        except Exception as e:
            return json.dumps({
                "error": f"computer_use backend unavailable: {e}",
                "hint": "No suitable backend found for this platform.",
            })

        try:
            return _dispatch(backend, action, params)
        except Exception as e:
            logger.exception("computer_use %s failed", action)
            return json.dumps({"error": f"{action} failed: {e}"})


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def check_computer_use_requirements() -> bool:
    try:
        backend = _get_backend()
        return backend.is_available()
    except Exception:
        return False