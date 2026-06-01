"""PyAutoGUI backend for cross-platform desktop control.

Works on macOS, Linux, and Windows using pyautogui for mouse/keyboard control
and Pillow for screenshots. On Linux, requires python3-tk or python3-Xlib
and a running X11 or Wayland display.

Unlike the cua-driver backend (macOS-only, background control via SkyLight SPIs),
this backend operates in the foreground — mouse and keyboard events go to the
active window under the real cursor. This is the standard approach on Linux/Windows.

Dependencies:
  pip install pyautogui Pillow

Linux additional:
  sudo apt-get install python3-tk python3-xlib scrot
  (or equivalent for your distro)
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import subprocess
import sys
import time as _time
from typing import Any, Dict, List, Optional, Tuple

from markbot.tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)

_PYAUTOGUI_AVAILABLE: Optional[bool] = None


def _probe_xvfb() -> bool:
    if sys.platform != "linux":
        return False
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Xvfb"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_pyautogui(force: bool = False) -> bool:
    global _PYAUTOGUI_AVAILABLE
    if _PYAUTOGUI_AVAILABLE is not None and not force:
        return _PYAUTOGUI_AVAILABLE
    try:
        import pyautogui
        _PYAUTOGUI_AVAILABLE = True
    except Exception:
        _PYAUTOGUI_AVAILABLE = False
    return _PYAUTOGUI_AVAILABLE


def _pyautogui_install_hint() -> str:
    return (
        "pyautogui is not installed. Install with:\n"
        "  pip install pyautogui Pillow\n"
        "On Linux you may also need:\n"
        "  sudo apt-get install python3-tk python3-xlib scrot"
    )


_MODIFIER_NAMES = {
    "cmd", "command", "shift", "option", "alt", "ctrl", "control",
    "fn", "super", "win", "windows", "meta",
}

_PLATFORM_ALIASES: dict[str, str] = {
    "command": "ctrl" if sys.platform != "darwin" else "command",
    "cmd": "ctrl" if sys.platform != "darwin" else "command",
    "option": "alt",
    "control": "ctrl",
    "windows": "win",
    "meta": "win",
}


def _parse_key_combo(key_combo: str) -> Tuple[str, str]:
    """Split 'cmd+a' or 'ctrl+s' into the last key and an optional modifier prefix."""
    parts = key_combo.strip().split()
    if len(parts) == 1:
        p = parts[0]
        idx = p.rfind("+")
        if idx == -1:
            return ("", p)
        modifier = p[:idx].lower()
        key = p[idx + 1:]
        return (modifier, key)
    elif len(parts) == 2:
        return (parts[0].lower(), parts[1])
    else:
        return ("", key_combo)


_KEY_MAP: dict[str, str] = {
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "bs": "backspace",
    "space": "space",
    "tab": "tab",
    "enter": "enter",
    "escape": "escape",
    "backspace": "backspace",
    "delete": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
    "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8",
    "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    "capslock": "capslock",
    "insert": "insert",
    "print": "printscreen",
    "printscreen": "printscreen",
    "scrolllock": "scrolllock",
    "pause": "pause",
    "numlock": "numlock",
}


def _resolve_key(key: str) -> str:
    return _KEY_MAP.get(key.lower(), key)


def _normalize_modifier(mod: str) -> str:
    return _PLATFORM_ALIASES.get(mod.lower(), mod.lower())


def _list_apps_linux() -> List[Dict[str, Any]]:
    apps: List[Dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["wmctrl", "-l", "-p"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = line.split(None, 5)
                if len(parts) >= 5:
                    apps.append({
                        "name": parts[5] if len(parts) > 5 else parts[4],
                        "pid": parts[2],
                    })
    except FileNotFoundError:
        pass

    if not apps:
        try:
            result = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--name", ""],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for wid in result.stdout.strip().splitlines():
                    name_result = subprocess.run(
                        ["xdotool", "getwindowname", wid],
                        capture_output=True, text=True, timeout=2,
                    )
                    pid_result = subprocess.run(
                        ["xdotool", "getwindowpid", wid],
                        capture_output=True, text=True, timeout=2,
                    )
                    if name_result.returncode == 0:
                        apps.append({
                            "name": name_result.stdout.strip(),
                            "pid": pid_result.stdout.strip() if pid_result.returncode == 0 else "0",
                        })
        except FileNotFoundError:
            pass

    return apps


def _list_apps_windows() -> List[Dict[str, Any]]:
    apps: List[Dict[str, Any]] = []
    try:
        result = subprocess.run(
            [
                "powershell", "-Command",
                "Get-Process | Where-Object {$_.MainWindowTitle} | "
                "Select-Object ProcessName, Id, MainWindowTitle | "
                "ConvertTo-Json",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            import json
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]
            for proc in data:
                apps.append({
                    "name": proc.get("MainWindowTitle", proc.get("ProcessName", "")),
                    "pid": str(proc.get("Id", 0)),
                })
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return apps


def _list_apps_macos() -> List[Dict[str, Any]]:
    apps: List[Dict[str, Any]] = []
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process '
             'whose background only is false'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for name in result.stdout.strip().split(", "):
                name = name.strip()
                if name:
                    apps.append({"name": name, "pid": "0"})
    except FileNotFoundError:
        pass
    return apps


class PyAutoGUIBackend(ComputerUseBackend):
    """Cross-platform desktop control via pyautogui + Pillow.

    Works on macOS, Linux (X11), and Windows. Operates in the foreground
    (moves the real cursor, types into the active window). Element-based
    interaction is not supported — use pixel coordinates instead.
    """

    def __init__(self) -> None:
        self._started = False
        self._screen_width = 0
        self._screen_height = 0

    # ── Lifecycle ──────────────────────────────────────────────────

    def is_available(self) -> bool:
        if sys.platform == "linux" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            if _probe_xvfb():
                os.environ["DISPLAY"] = ":99"
            else:
                return False
        return _check_pyautogui(force=_PYAUTOGUI_AVAILABLE is False)

    def start(self) -> None:
        if self._started:
            return
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        self._screen_width, self._screen_height = pyautogui.size()
        self._started = True

    def stop(self) -> None:
        self._started = False

    def _require_started(self) -> None:
        if not self._started:
            self.start()

    # ── Capture ────────────────────────────────────────────────────

    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        self._require_started()
        import pyautogui

        try:
            screenshot = pyautogui.screenshot()
        except Exception as e:
            return CaptureResult(mode=mode, width=0, height=0)

        buf = io.BytesIO()
        screenshot.save(buf, format="PNG")
        png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        width, height = screenshot.size
        png_bytes_len = len(png_b64.encode("ascii"))

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=[],
            png_bytes_len=png_bytes_len,
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
        self._require_started()
        import pyautogui

        if element is not None:
            return ActionResult(ok=False, action="click", message="Element-based click not supported; use x/y coordinates")

        if x is None or y is None:
            return ActionResult(ok=False, action="click", message="click requires x and y coordinates")

        try:
            if modifiers:
                import pyautogui as pag
                for m in modifiers:
                    pag.keyDown(_normalize_modifier(m))
                pyautogui.click(x=x, y=y, button=button, clicks=click_count)
                for m in reversed(modifiers):
                    pag.keyUp(_normalize_modifier(m))
            else:
                pyautogui.click(x=x, y=y, button=button, clicks=click_count)
            return ActionResult(ok=True, action="click", message=f"Clicked at ({x}, {y}) button={button} clicks={click_count}")
        except Exception as e:
            return ActionResult(ok=False, action="click", message=f"Click failed: {e}")

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
        self._require_started()
        import pyautogui

        if from_element is not None or to_element is not None:
            return ActionResult(ok=False, action="drag", message="Element-based drag not supported; use from_xy/to_xy")

        if from_xy is None or to_xy is None:
            return ActionResult(ok=False, action="drag", message="drag requires from_xy and to_xy")

        try:
            pyautogui.moveTo(from_xy[0], from_xy[1])
            pyautogui.drag(to_xy[0] - from_xy[0], to_xy[1] - from_xy[1], duration=0.3, button=button)
            return ActionResult(ok=True, action="drag", message=f"Dragged from {from_xy} to {to_xy}")
        except Exception as e:
            return ActionResult(ok=False, action="drag", message=f"Drag failed: {e}")

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
        self._require_started()
        import pyautogui

        if element is not None:
            return ActionResult(ok=False, action="scroll", message="Element-based scroll not supported; use x/y")

        try:
            if x is not None and y is not None:
                pyautogui.moveTo(x, y)

            direction = direction.lower()
            if direction == "up":
                pyautogui.scroll(amount)
            elif direction == "down":
                pyautogui.scroll(-amount)
            elif direction == "left":
                pyautogui.hscroll(-amount)
            elif direction == "right":
                pyautogui.hscroll(amount)
            return ActionResult(ok=True, action="scroll", message=f"Scrolled {direction} {amount}")
        except Exception as e:
            return ActionResult(ok=False, action="scroll", message=f"Scroll failed: {e}")

    # ── Keyboard ───────────────────────────────────────────────────

    def type_text(self, text: str) -> ActionResult:
        self._require_started()
        import pyautogui

        try:
            pyautogui.write(text, interval=0.02)
            return ActionResult(ok=True, action="type", message=f"Typed {len(text)} characters")
        except Exception as e:
            return ActionResult(ok=False, action="type", message=f"Type failed: {e}")

    def key(self, keys: str) -> ActionResult:
        self._require_started()
        import pyautogui

        modifier_prefix, key_str = _parse_key_combo(keys)

        try:
            if modifier_prefix:
                mod = _normalize_modifier(modifier_prefix)
                resolved = _resolve_key(key_str)
                if mod == "command" and sys.platform == "darwin":
                    pyautogui.hotkey("command", resolved)
                elif mod == "ctrl":
                    pyautogui.hotkey("ctrl", resolved)
                elif mod == "alt":
                    pyautogui.hotkey("alt", resolved)
                elif mod == "shift":
                    pyautogui.hotkey("shift", resolved)
                else:
                    pyautogui.hotkey(mod, resolved)
            else:
                resolved = _resolve_key(key_str)
                pyautogui.press(resolved)
            return ActionResult(ok=True, action="key", message=f"Pressed key: {keys}")
        except Exception as e:
            return ActionResult(ok=False, action="key", message=f"Key press failed: {e}")

    # ── Introspection ──────────────────────────────────────────────

    def list_apps(self) -> List[Dict[str, Any]]:
        if sys.platform == "darwin":
            return _list_apps_macos()
        elif sys.platform == "win32":
            return _list_apps_windows()
        else:
            return _list_apps_linux()

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        try:
            if sys.platform == "darwin":
                subprocess.run(
                    ["osascript", "-e", f'tell application "{app}" to activate'],
                    capture_output=True, text=True, timeout=5,
                )
                return ActionResult(ok=True, action="focus_app", message=f"Focused app: {app}")

            elif sys.platform == "win32":
                subprocess.run(
                    ["powershell", "-Command",
                     f"(New-Object -ComObject WScript.Shell).AppActivate('{app}')"],
                    capture_output=True, text=True, timeout=5,
                )
                return ActionResult(ok=True, action="focus_app", message=f"Focused app: {app}")

            else:
                result = subprocess.run(
                    ["wmctrl", "-a", app],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return ActionResult(ok=True, action="focus_app", message=f"Focused app: {app}")

                result = subprocess.run(
                    ["xdotool", "search", "--name", app, "windowactivate"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return ActionResult(ok=True, action="focus_app", message=f"Focused app: {app}")

                return ActionResult(ok=False, action="focus_app", message=f"Could not focus app: {app}")
        except FileNotFoundError as e:
            return ActionResult(ok=False, action="focus_app", message=f"Focus tool not found: {e}")
        except Exception as e:
            return ActionResult(ok=False, action="focus_app", message=f"Focus failed: {e}")

    # ── Native-value mutation ──────────────────────────────────────

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        return ActionResult(ok=False, action="set_value", message="set_value not supported on this backend (no accessibility tree)")