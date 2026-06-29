"""Cua-driver backend for macOS desktop control.

Communicates with cua-driver via MCP stdio transport using the ``mcp`` SDK.
Requires cua-driver to be installed: https://github.com/trycua/cua

The private SkyLight SPIs cua-driver uses are macOS-only and not Apple-public.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from markbot.tools.computer_use.backend import (
    ActionResult,
    BackendCapabilities,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)

PINNED_CUA_DRIVER_VERSION = os.environ.get("MARKBOT_CUA_DRIVER_VERSION", "0.5.0")
_CUA_DRIVER_CMD = os.environ.get("MARKBOT_CUA_DRIVER_CMD", "cua-driver")
_CUA_DRIVER_ARGS = ["mcp"]

_WINDOW_LINE_RE = re.compile(
    r'^-\s+(.+?)\s+\(pid\s+(\d+)\)\s+.*\[window_id:\s+(\d+)\]',
    re.MULTILINE,
)

_ELEMENT_LINE_RE = re.compile(
    r'^\s*(?:-\s+)?\[(\d+)\]\s+(\w+)(?:\s+"([^"]*)"|(?:\s+\(\d+\))?\s+id=([^\s\[\]]*))?',
    re.MULTILINE,
)


def _is_macos() -> bool:
    return sys.platform == "darwin"


def cua_driver_binary_available() -> bool:
    return bool(shutil.which(_CUA_DRIVER_CMD))


def cua_driver_install_hint() -> str:
    return (
        "cua-driver is not installed. Install with:\n"
        '  /bin/bash -c "$(curl -fsSL '
        'https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"\n'
        "Or set MARKBOT_CUA_DRIVER_CMD to the full path."
    )


def _parse_windows_from_text(text: str) -> list[dict[str, Any]]:
    windows = []
    for m in _WINDOW_LINE_RE.finditer(text):
        windows.append({
            "app_name": m.group(1).strip(),
            "pid": int(m.group(2)),
            "window_id": int(m.group(3)),
            "off_screen": "[off-screen]" in m.group(0),
        })
    return windows


def _split_tree_text(full_text: str) -> tuple[str, str]:
    lines = full_text.split("\n", 1)
    summary = lines[0]
    tree = lines[1] if len(lines) > 1 else ""
    return summary, tree


class _AsyncBridge:
    """Run one asyncio loop on a daemon thread for MCP communication."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            try:
                self._loop.run_forever()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    logger.debug("Failed to close cua-driver asyncio loop", exc_info=True)

        self._thread = threading.Thread(target=_run, daemon=True, name="cua-driver-loop")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("cua-driver asyncio bridge failed to start")

    def run(self, coro, timeout: float = 30.0) -> Any:
        if not self._loop or not self._thread or not self._thread.is_alive():
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("cua-driver bridge not started")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


class _CuaDriverSession:
    """Holds the MCP ClientSession, spawned lazily."""

    def __init__(self, bridge: _AsyncBridge) -> None:
        self._bridge = bridge
        self._session: Any = None
        self._exit_stack: Any = None
        self._lock = threading.Lock()
        self._started = False

    async def _aenter(self) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not cua_driver_binary_available():
            raise RuntimeError(cua_driver_install_hint())

        params = StdioServerParameters(
            command=_CUA_DRIVER_CMD,
            args=_CUA_DRIVER_ARGS,
            env={**os.environ},
        )
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._exit_stack = stack
        self._session = session

    async def _aexit(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("cua-driver shutdown error: %s", e)
        self._exit_stack = None
        self._session = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._bridge.start()
            self._bridge.run(self._aenter(), timeout=15.0)
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                self._bridge.run(self._aexit(), timeout=5.0)
            finally:
                self._started = False

    async def _call_tool_async(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.call_tool(name, args)
        return _extract_tool_result(result)

    def call_tool(self, name: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        if not self._started:
            raise RuntimeError("cua-driver session not started")
        return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)


def _extract_tool_result(mcp_result: Any) -> dict[str, Any]:
    """Convert an mcp CallToolResult into a plain dict."""
    data: Any = None
    images: list[str] = []
    is_error = bool(getattr(mcp_result, "isError", False))
    structured: Optional[dict] = getattr(mcp_result, "structuredContent", None) or None
    text_chunks: list[str] = []

    for part in getattr(mcp_result, "content", []) or []:
        ptype = getattr(part, "type", None)
        if ptype == "text":
            text_chunks.append(getattr(part, "text", "") or "")
        elif ptype == "image":
            b64 = getattr(part, "data", None)
            if b64:
                images.append(b64)

    if text_chunks:
        joined = "\n".join(t for t in text_chunks if t)
        try:
            data = json.loads(joined) if joined.strip().startswith(("{", "[")) else joined
        except json.JSONDecodeError:
            data = joined

    return {"data": data, "images": images, "structuredContent": structured, "isError": is_error}


class CuaDriverBackend(ComputerUseBackend):
    """macOS desktop control via cua-driver MCP."""

    def __init__(self) -> None:
        self._bridge = _AsyncBridge()
        self._session = _CuaDriverSession(self._bridge)
        self._active_pid: Optional[int] = None
        self._active_window_id: Optional[int] = None
        self._last_app: Optional[str] = None
        self._started = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def is_available(self) -> bool:
        if not _is_macos():
            return False
        return cua_driver_binary_available()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            coordinate_targeting=True,
            element_targeting=True,
            som_overlay=True,
            ax_tree=True,
            set_value=True,
        )

    def start(self) -> None:
        if not self._started:
            self._session.start()
            self._started = True

    def stop(self) -> None:
        if self._started:
            try:
                self._session.stop()
            finally:
                self._bridge.stop()
                self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    def _get_image_dimensions(self, b64_data: str) -> tuple[int, int]:
        """Extract width and height from a base64-encoded JPEG/PNG image."""
        try:
            import base64 as b64mod
            raw = b64mod.b64decode(b64_data)
            # JPEG: SOF0 marker (0xFFC0) contains dimensions
            if raw[:2] == b'\xff\xd8':
                i = 2
                while i < len(raw) - 9:
                    if raw[i] != 0xFF:
                        break
                    marker = raw[i + 1]
                    if marker in (0xC0, 0xC1, 0xC2):
                        h = int.from_bytes(raw[i + 5:i + 7], 'big')
                        w = int.from_bytes(raw[i + 7:i + 9], 'big')
                        return w, h
                    length = int.from_bytes(raw[i + 2:i + 4], 'big')
                    i += 2 + length
            # PNG: IHDR chunk contains dimensions
            elif raw[:8] == b'\x89PNG\r\n\x1a\n':
                w = int.from_bytes(raw[16:20], 'big')
                h = int.from_bytes(raw[20:24], 'big')
                return w, h
        except Exception:
            logger.debug("Failed to parse image dimensions from base64 capture", exc_info=True)
        return 0, 0

    def _action(self, name: str, args: dict[str, Any]) -> ActionResult:
        self._ensure_started()
        try:
            out = self._session.call_tool(name, args)
        except Exception as e:
            return ActionResult(ok=False, action=name, message=f"cua-driver error: {e}")

        is_error = out.get("isError", False)
        data = out.get("data", "")
        message = data if isinstance(data, str) else json.dumps(data)
        return ActionResult(ok=not is_error, action=name, message=message)

    # ── Capture ────────────────────────────────────────────────────

    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        self._ensure_started()

        lw_out = self._session.call_tool("list_windows", {"on_screen_only": True})

        sc = lw_out.get("structuredContent") or {}
        raw_windows = sc.get("windows") if sc else None
        if raw_windows:
            windows = [
                {
                    "app_name": w.get("app_name", ""),
                    "pid": int(w["pid"]),
                    "window_id": int(w["window_id"]),
                    "off_screen": not w.get("is_on_screen", True),
                }
                for w in raw_windows
            ]
        else:
            raw_text = lw_out["data"] if isinstance(lw_out["data"], str) else ""
            windows = _parse_windows_from_text(raw_text)

        if not windows:
            return CaptureResult(mode=mode, width=0, height=0, capabilities=self.capabilities())

        if app:
            matches = [w for w in windows if app.lower() in w["app_name"].lower()]
            target = matches[0] if matches else next((w for w in windows if not w["off_screen"]), windows[0])
        else:
            target = next((w for w in windows if not w["off_screen"]), windows[0])

        self._active_pid = target["pid"]
        self._active_window_id = target["window_id"]
        app_name = target["app_name"]
        self._last_app = app_name

        png_b64: Optional[str] = None
        elements: List[UIElement] = []
        width, height = 0, 0
        png_bytes_len = 0

        if mode == "vision":
            sc_out = self._session.call_tool(
                "screenshot",
                {"window_id": self._active_window_id, "format": "jpeg", "quality": 85},
            )
            if sc_out["images"]:
                png_b64 = sc_out["images"][0]
                width, height = self._get_image_dimensions(png_b64)
        else:
            gws_out = self._session.call_tool(
                "get_window_state",
                {"pid": self._active_pid, "window_id": self._active_window_id},
            )
            text = gws_out["data"] if isinstance(gws_out["data"], str) else ""
            _summary, tree = _split_tree_text(text)

            idx = 1
            for m in _ELEMENT_LINE_RE.finditer(tree):
                label = m.group(3) or m.group(4) or ""
                elements.append(UIElement(
                    index=idx,
                    role=m.group(2),
                    label=label,
                    app=app_name,
                ))
                idx += 1

            if gws_out["images"]:
                png_b64 = gws_out["images"][0]
                width, height = self._get_image_dimensions(png_b64)

        if png_b64:
            png_bytes_len = len(png_b64.encode("ascii"))

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements,
            app=app_name,
            png_bytes_len=png_bytes_len,
            capabilities=self.capabilities(),
        )

    # ── Pointer actions ────────────────────────────────────────────

    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        if self._active_pid is None:
            return ActionResult(ok=False, action="click", message="No active window — call capture() first")

        if click_count == 2:
            tool = "double_click"
        elif button == "right":
            tool = "right_click"
        else:
            tool = "click"

        args: dict[str, Any] = {"pid": self._active_pid}
        if element is not None:
            if self._active_window_id is None:
                return ActionResult(ok=False, action="click", message="No active window_id for element click")
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        else:
            return ActionResult(ok=False, action="click", message="click requires element= or x/y")

        if modifiers:
            args["modifiers"] = modifiers

        return self._action(tool, args)

    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        if self._active_pid is None:
            return ActionResult(ok=False, action="drag", message="No active window — call capture() first")

        args: dict[str, Any] = {"pid": self._active_pid}
        if from_element is not None and to_element is not None:
            args["from_element_index"] = from_element
            args["to_element_index"] = to_element
            args["window_id"] = self._active_window_id
        elif from_xy is not None and to_xy is not None:
            args["from_x"] = from_xy[0]
            args["from_y"] = from_xy[1]
            args["to_x"] = to_xy[0]
            args["to_y"] = to_xy[1]
        else:
            return ActionResult(ok=False, action="drag", message="drag requires from_xy/to_xy or from_element/to_element")

        return self._action("drag", args)

    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        if self._active_pid is None:
            return ActionResult(ok=False, action="scroll", message="No active window — call capture() first")

        delta_map = {"up": -amount, "down": amount, "left": -amount, "right": amount}
        dx = delta_map.get(direction, 0) if direction in {"left", "right"} else 0
        dy = delta_map.get(direction, 0) if direction in {"up", "down"} else 0

        args: dict[str, Any] = {"pid": self._active_pid, "delta_x": dx, "delta_y": dy}
        if x is not None:
            args["x"] = x
        if y is not None:
            args["y"] = y
        if modifiers:
            args["modifiers"] = modifiers

        return self._action("scroll", args)

    # ── Keyboard ───────────────────────────────────────────────────

    def type_text(self, text: str) -> ActionResult:
        if self._active_pid is None:
            return ActionResult(ok=False, action="type", message="No active window — call capture() first")
        return self._action("type_text", {"pid": self._active_pid, "text": text})

    def key(self, keys: str) -> ActionResult:
        if self._active_pid is None:
            return ActionResult(ok=False, action="key", message="No active window — call capture() first")
        return self._action("key", {"pid": self._active_pid, "keys": keys})

    # ── Introspection ──────────────────────────────────────────────

    def list_apps(self) -> List[Dict[str, Any]]:
        self._ensure_started()
        try:
            out = self._session.call_tool("list_apps", {})
        except Exception as e:
            return [{"name": f"Error: {e}", "pid": "0"}]

        data = out.get("data", "")
        if isinstance(data, list):
            return [{"name": a.get("app_name", a.get("name", "")), "pid": str(a.get("pid", 0))} for a in data]
        elif isinstance(data, str):
            return [{"name": line.strip().lstrip("- "), "pid": "0"} for line in data.split("\n") if line.strip() and line.strip().startswith("-")]
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        self._ensure_started()
        self._last_app = app

        try:
            out = self._session.call_tool("focus_app", {"app_name": app})
        except Exception as e:
            return ActionResult(ok=False, action="focus_app", message=f"cua-driver error: {e}")

        is_error = out.get("isError", False)
        data = out.get("data", "")
        message = data if isinstance(data, str) else json.dumps(data)
        return ActionResult(ok=not is_error, action="focus_app", message=message)

    # ── Native-value mutation ──────────────────────────────────────

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        if self._active_pid is None:
            return ActionResult(ok=False, action="set_value", message="No active window — call capture() first")
        if self._active_window_id is None:
            return ActionResult(ok=False, action="set_value", message="No active window_id for set_value")

        return self._action("set_value", {
            "pid": self._active_pid,
            "window_id": self._active_window_id,
            "element_index": element,
            "value": value,
        })
