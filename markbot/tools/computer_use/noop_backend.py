"""Noop backend for testing — returns canned responses without any OS interaction."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from markbot.tools.computer_use.backend import (
    ActionResult,
    BackendCapabilities,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)


class NoopBackend(ComputerUseBackend):
    """Testing stub that returns success without touching the OS."""

    def is_available(self) -> bool:
        return True

    def capabilities(self) -> BackendCapabilities:
        # Report full capability so tests exercise the element-targeting path.
        return BackendCapabilities(
            coordinate_targeting=True,
            element_targeting=True,
            som_overlay=True,
            ax_tree=True,
            set_value=True,
        )

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        elements = [
            UIElement(index=1, role="AXButton", label="Click Me", bounds=(100, 200, 50, 30)),
            UIElement(index=2, role="AXTextField", label="Input Field", bounds=(300, 400, 200, 40)),
            UIElement(index=3, role="AXStaticText", label="Hello World", bounds=(10, 10, 100, 20)),
        ]
        return CaptureResult(
            mode=mode,
            width=1920,
            height=1080,
            elements=elements,
            png_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" if mode != "ax" else None,
            capabilities=self.capabilities(),
        )

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
        return ActionResult(ok=True, action="click", message=f"[noop] click ({x},{y}) el={element} btn={button} cnt={click_count}")

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
        return ActionResult(ok=True, action="drag", message=f"[noop] drag {from_xy}->{to_xy} el={from_element}->{to_element}")

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
        return ActionResult(ok=True, action="scroll", message=f"[noop] scroll {direction} {amount}")

    def type_text(self, text: str) -> ActionResult:
        return ActionResult(ok=True, action="type", message=f"[noop] type: {text[:50]}")

    def key(self, keys: str) -> ActionResult:
        return ActionResult(ok=True, action="key", message=f"[noop] key: {keys}")

    def list_apps(self) -> List[Dict[str, Any]]:
        return [{"name": "Finder", "pid": "1"}]

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        return ActionResult(ok=True, action="focus_app", message=f"[noop] focus_app: {app}")

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        return ActionResult(ok=True, action="set_value", message=f"[noop] set_value el={element} value={value[:50]}")
