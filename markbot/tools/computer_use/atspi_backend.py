"""AT-SPI2 backend for Linux desktop control.

Uses ``pyatspi`` to read the accessibility tree with real element bounds, and
``pyautogui`` for the actual pointer/keyboard/clipboard side effects. This is
the first backend that fills ``UIElement.bounds`` with non-zero values and the
first to draw SOM overlays in pure Python (via Pillow).

Element targeting model:
  * ``capture(mode='ax')``  → accessibility tree only (no image), elements with bounds
  * ``capture(mode='som')`` → screenshot with numbered overlays + elements
  * ``capture(mode='vision')`` → plain screenshot, no elements

Coordinate systems (subtle but critical):
  * pyatspi ``getExtents(DESKTOP_COORDS)`` returns **logical** points.
  * pyautogui ``click(x, y)`` takes **logical** points.
  * pyautogui ``screenshot()`` returns **physical** pixels (scaled) on HiDPI.
  So:
  * Element-based clicks use ``UIElement.center()`` (logical) → pyautogui directly.
  * Pixel-based clicks (model-supplied x/y from the screenshot) must downscale
    via ``_device_scale`` to logical before pyautogui sees them.
  * SOM overlays are drawn ON the screenshot (physical pixels), so element
    bounds (logical) must be MULTIPLIED by ``_device_scale`` before drawing.

Availability:
  ``is_available()`` requires Linux + importable pyatspi + a reachable registry
  (``getDesktop(0)`` does not raise). On failure, ``_get_backend`` falls back
  to ``PyAutoGUIBackend`` so markbot still works without an a11y stack.
"""

from __future__ import annotations

import base64
import io
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from markbot.tools.computer_use.backend import (
    ActionResult,
    BackendCapabilities,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

# ─── pyatspi availability probe (cached) ────────────────────────────────────
# pyatspi import is the real gate; the registry probe adds runtime confidence.
# The result is cached so repeated is_available() calls stay cheap.

_pyatspi_checked = False
_pyatspi_ok = False


def _probe_pyatspi() -> bool:
    """Return True iff pyatspi imports AND a desktop registry is reachable."""
    global _pyatspi_checked, _pyatspi_ok
    if _pyatspi_checked:
        return _pyatspi_ok
    _pyatspi_checked = True
    if sys.platform != "linux":
        return False
    try:
        import pyatspi  # noqa: F401
        # Reachability: getDesktop(0) talks to the at-spi registry daemon over
        # the session bus. If no a11y bus / registry is running this raises.
        pyatspi.Registry.getDesktop(0)
        _pyatspi_ok = True
    except Exception as e:  # ImportError, GLib.Error, RuntimeError, ...
        logger.debug("pyatspi unavailable, AT-SPI backend disabled: %s", e)
        _pyatspi_ok = False
    return _pyatspi_ok


# ─── Element collection limit (defence against giant trees) ─────────────────
_MAX_ELEMENTS = 500
_MAX_TREE_DEPTH = 15


class AtspiBackend(ComputerUseBackend):
    """Linux desktop control via AT-SPI2 (read tree + bounds) + pyautogui (act)."""

    def __init__(self) -> None:
        self._started = False
        self._device_scale = 1.0
        self._screen_width = 0
        self._screen_height = 0
        self._desktop = None
        # Element-index → UIElement map, refreshed on every capture(). Actions
        # resolve element indices against this. Unlike cua we do NOT cache
        # pid/window-id across calls: bounds live locally, no server session.
        self._last_elements: List[UIElement] = []

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return _probe_pyatspi()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            coordinate_targeting=True,
            element_targeting=True,
            som_overlay=True,
            ax_tree=True,
            set_value=True,
        )

    def start(self) -> None:
        if self._started:
            return
        import pyatspi
        self._desktop = pyatspi.Registry.getDesktop(0)

        # Probe the device pixel scale (same idea as pyautogui_backend): on
        # HiDPI, screenshot() returns physical pixels while AT-SPI bounds and
        # pyautogui.click use logical points.
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.05
            logical_w, logical_h = pyautogui.size()
            try:
                probe = pyautogui.screenshot()
                scale = probe.width / logical_w if logical_w else 1.0
            except Exception:
                scale = 1.0
            if abs(scale - round(scale)) > 0.01:
                scale = 1.0
            self._device_scale = max(1.0, round(scale))
            self._screen_width, self._screen_height = logical_w, logical_h
        except Exception as e:
            logger.warning("AT-SPI backend: pyautogui probe failed, scale=1.0: %s", e)
            self._device_scale = 1.0
        self._started = True

    def stop(self) -> None:
        # We never pump the pyatspi event loop, so there's nothing to tear
        # down. Do NOT call Registry.shutdown() — it kills the at-spi bridge
        # for the whole session and other apps' a11y stops working.
        self._started = False

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("AtspiBackend.start() was not called")

    # ── Coordinate helpers ─────────────────────────────────────────────────

    def _to_logical(self, x: Optional[int], y: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
        """Downscale model-supplied physical (screenshot) pixels to logical points."""
        if self._device_scale == 1.0:
            return x, y
        sx = None if x is None else int(round(x / self._device_scale))
        sy = None if y is None else int(round(y / self._device_scale))
        return sx, sy

    # ── Capture ────────────────────────────────────────────────────────────

    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        self._require_started()
        import pyautogui

        # 1) Screenshot (every mode can carry one except pure 'ax').
        png_b64: Optional[str] = None
        width = 0
        height = 0
        png_bytes_len = 0
        if mode != "ax":
            try:
                shot = pyautogui.screenshot()
            except Exception as e:
                logger.warning("Screenshot failed: %s", e)
                return CaptureResult(
                    mode=mode, width=0, height=0, app=str(e),
                    capabilities=self.capabilities(),
                )
            width, height = shot.size
            png_b64 = _encode_png(shot)
            png_bytes_len = len(png_b64.encode("ascii"))

        # 2) Walk the AT-SPI tree for elements (ax + som; vision skips it).
        elements: List[UIElement] = []
        app_name = app or ""
        if mode != "vision":
            try:
                elements = self._walk_tree(app_filter=app)
            except Exception as e:
                logger.warning("AT-SPI tree walk failed: %s", e)
                elements = []
            self._last_elements = elements

        # 3) SOM overlay: draw numbered boxes on the screenshot.
        if mode == "som" and png_b64:
            try:
                png_b64 = self._draw_overlay(png_b64, elements)
                png_bytes_len = len(png_b64.encode("ascii"))
            except Exception as e:
                logger.warning("SOM overlay draw failed: %s", e)

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements if mode != "vision" else [],
            app=app_name,
            png_bytes_len=png_bytes_len,
            capabilities=self.capabilities(),
        )

    def _walk_tree(self, app_filter: Optional[str] = None) -> List[UIElement]:
        """Depth-first walk of the AT-SPI tree collecting interactable elements.

        Each result element has real bounds (logical points). Elements whose
        component/extents are unavailable are skipped rather than emitting
        (0,0,0,0) placeholders — the model can actually trust these bounds.
        """
        out: List[UIElement] = []
        if self._desktop is None:
            return out

        roots: List[Any] = []
        try:
            n_apps = self._desktop.childCount
            for i in range(n_apps):
                try:
                    roots.append(self._desktop[i])
                except Exception:
                    continue
        except Exception:
            return out

        flt = app_filter.lower() if app_filter else None
        idx = 1

        def matches_app(node: Any) -> bool:
            if not flt:
                return True
            name = _safe_attr(node, "name", "").lower()
            if flt in name:
                return True
            # Fall back to the application's bundle/name via getApplication().
            try:
                app_obj = node.getApplication()
                aname = _safe_attr(app_obj, "name", "").lower()
                if flt in aname:
                    return True
            except Exception:
                pass
            return False

        def visit(node: Any, depth: int) -> None:
            nonlocal idx
            if idx > _MAX_ELEMENTS or depth > _MAX_TREE_DEPTH:
                return

            bounds = _get_bounds(node)
            role = _safe_role(node)
            name = _safe_attr(node, "name", "")
            # Only collect elements that have a real on-screen rectangle.
            if bounds and bounds[2] > 0 and bounds[3] > 0 and _is_interactable(role, node):
                out.append(UIElement(
                    index=idx,
                    role=role,
                    label=name[:80],
                    bounds=bounds,
                ))
                idx += 1

            try:
                n = node.childCount
            except Exception:
                return
            for i in range(n):
                if idx > _MAX_ELEMENTS:
                    break
                try:
                    child = node[i]
                except Exception:
                    continue
                if child is None:
                    continue
                # At depth 0, skip children (windows) that don't belong to the
                # matching app. Deeper levels always descend — we already
                # filtered at the app root level above.
                if flt and depth == 0 and not matches_app(child):
                    continue
                visit(child, depth + 1)

        for app_root in roots:
            if flt and not matches_app(app_root):
                continue
            visit(app_root, 0)

        return out

    def _draw_overlay(self, png_b64: str, elements: List[UIElement]) -> str:
        """Draw numbered red boxes for each element on the screenshot.

        The screenshot is in PHYSICAL pixels but bounds are LOGICAL points, so
        we multiply bounds by ``_device_scale`` before drawing.
        """
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(io.BytesIO(base64.b64decode(png_b64)))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 16)
        except Exception:
            try:
                font = ImageFont.load_default(size=18)
            except Exception:
                font = ImageFont.load_default()

        scale = self._device_scale
        # Element indices in the visible page (capped by tool layer later).
        shown = elements[:_MAX_ELEMENTS]
        for el in shown:
            x, y, w, h = el.bounds
            if w <= 0 or h <= 0:
                continue
            # logical → physical for drawing on the screenshot
            px, py = int(x * scale), int(y * scale)
            pw, ph = int(w * scale), int(h * scale)
            draw.rectangle([px, py, px + pw, py + ph], outline="red", width=3)
            label = str(el.index)
            # Small filled box behind the index for legibility.
            tx, ty = px + 2, py + 2
            try:
                tw, th = draw.textbbox((tx, ty), label, font=font)[2:4]
                draw.rectangle([tx - 1, ty - 1, tx + tw + 2, ty + th + 2], fill="red")
                draw.text((tx, ty), label, fill="white", font=font)
            except Exception:
                draw.text((tx, ty), label, fill="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ── Element resolution ─────────────────────────────────────────────────

    def _resolve_element(self, index: int) -> Tuple[Optional[UIElement], str]:
        """Resolve a 1-based element index → (UIElement, '') or (None, reason)."""
        if not self._last_elements:
            return None, "No elements available — call capture(mode='ax'|'som') first"
        if index < 1 or index > len(self._last_elements):
            return None, f"element {index} out of range (1..{len(self._last_elements)})"
        return self._last_elements[index - 1], ""

    # ── Pointer actions ────────────────────────────────────────────────────

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

        # Resolve target coordinates.
        if element is not None:
            el, reason = self._resolve_element(element)
            if el is None:
                return ActionResult(ok=False, action="click", message=reason)
            tx, ty = el.center()  # logical coords
            via = "element"
        elif x is not None and y is not None:
            # Model-supplied pixels are in screenshot (physical) space.
            tx, ty = self._to_logical(x, y)
            via = "coordinate"
        else:
            return ActionResult(ok=False, action="click", message="click requires element= or x/y")

        # Native AT-SPI action first (no focus steal); fall back to pyautogui.
        did_native = False
        if element is not None:
            did_native = self._try_do_action(element, preferred={"click", "press", "jump"})

        try:
            if not did_native:
                mod_keys = [_normalize_modifier(m) for m in modifiers] if modifiers else []
                for m in mod_keys:
                    pyautogui.keyDown(m)
                try:
                    pyautogui.click(x=tx, y=ty, button=button, clicks=click_count)
                finally:
                    for m in reversed(mod_keys):
                        try:
                            pyautogui.keyUp(m)
                        except Exception:
                            pass
            return ActionResult(
                ok=True, action="click",
                message=f"Clicked {via} at ({tx},{ty}) button={button} clicks={click_count}"
                + (" [native AT-SPI]" if did_native else ""),
            )
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

        # Resolve source.
        if from_element is not None:
            el, reason = self._resolve_element(from_element)
            if el is None:
                return ActionResult(ok=False, action="drag", message=reason)
            fx, fy = el.center()
        elif from_xy is not None:
            fx, fy = self._to_logical(from_xy[0], from_xy[1])
        else:
            return ActionResult(ok=False, action="drag", message="drag needs from_element or from_xy")

        # Resolve target.
        if to_element is not None:
            el, reason = self._resolve_element(to_element)
            if el is None:
                return ActionResult(ok=False, action="drag", message=reason)
            tx, ty = el.center()
        elif to_xy is not None:
            tx, ty = self._to_logical(to_xy[0], to_xy[1])
        else:
            return ActionResult(ok=False, action="drag", message="drag needs to_element or to_xy")

        try:
            pyautogui.moveTo(fx, fy)
            pyautogui.drag(tx - fx, ty - fy, duration=0.3, button=button)
            return ActionResult(ok=True, action="drag", message=f"Dragged ({fx},{fy})->({tx},{ty})")
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

        # Resolve position.
        if element is not None:
            el, reason = self._resolve_element(element)
            if el is None:
                return ActionResult(ok=False, action="scroll", message=reason)
            tx, ty = el.center()
        elif x is not None and y is not None:
            tx, ty = self._to_logical(x, y)
        else:
            tx, ty = None, None

        try:
            if tx is not None and ty is not None:
                pyautogui.moveTo(tx, ty)
            mod_keys = [_normalize_modifier(m) for m in modifiers] if modifiers else []
            for m in mod_keys:
                pyautogui.keyDown(m)
            try:
                direction = direction.lower()
                if direction == "up":
                    pyautogui.scroll(amount)
                elif direction == "down":
                    pyautogui.scroll(-amount)
                elif direction == "left":
                    pyautogui.hscroll(-amount)
                elif direction == "right":
                    pyautogui.hscroll(amount)
            finally:
                for m in reversed(mod_keys):
                    try:
                        pyautogui.keyUp(m)
                    except Exception:
                        pass
            return ActionResult(ok=True, action="scroll", message=f"Scrolled {direction} {amount}")
        except Exception as e:
            return ActionResult(ok=False, action="scroll", message=f"Scroll failed: {e}")

    # ── Keyboard actions ───────────────────────────────────────────────────

    def type_text(self, text: str) -> ActionResult:
        self._require_started()
        import pyautogui
        try:
            pyautogui.write(text, interval=0.02)
            return ActionResult(ok=True, action="type", message=f"Typed {len(text)} chars")
        except Exception as e:
            return ActionResult(ok=False, action="type", message=f"Type failed: {e}")

    def key(self, keys: str) -> ActionResult:
        self._require_started()
        import pyautogui
        modifiers, key_str = _parse_key_combo_list(keys)
        resolved_mods = [_normalize_modifier(m) for m in modifiers]
        resolved_final = _resolve_key(key_str)
        try:
            if resolved_mods:
                pyautogui.hotkey(*resolved_mods, resolved_final)
            else:
                pyautogui.press(resolved_final)
            return ActionResult(ok=True, action="key", message=f"Pressed key: {keys}")
        except Exception as e:
            return ActionResult(ok=False, action="key", message=f"Key press failed: {e}")

    # ── Value mutation ─────────────────────────────────────────────────────

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        """Set a value on an element via AT-SPI.

        Text fields → EditableText.setTextContents; selectable lists → iterate
        children and pick the option matching ``value``. Falls back to a clear
        error if the element exposes neither interface.
        """
        self._require_started()
        if element is None:
            return ActionResult(
                ok=False, action="set_value",
                message="set_value requires an element index on this backend",
            )
        el, reason = self._resolve_element(element)
        if el is None:
            return ActionResult(ok=False, action="set_value", message=reason)

        node = self._lookup_node(element)
        if node is None:
            return ActionResult(ok=False, action="set_value",
                                message=f"element {element} no longer in tree")

        # Try EditableText first (entries, text areas).
        try:
            et = node.queryEditableText()
            et.setTextContents(value)
            return ActionResult(ok=True, action="set_value",
                                message=f"Set text of element {element} to {value!r}")
        except Exception:
            pass

        # Try Selection (combo boxes / lists).
        try:
            sel = node.querySelection()
            n = sel.nSelectedChildren
            # Deselect current.
            for i in range(n):
                sel.deselectChild(i)
            # Find matching child by name and select it.
            count = node.childCount
            for i in range(count):
                child = node[i]
                cname = _safe_attr(child, "name", "")
                if cname.strip().lower() == value.strip().lower():
                    sel.selectChild(i)
                    return ActionResult(ok=True, action="set_value",
                                        message=f"Selected {cname!r} on element {element}")
            return ActionResult(ok=False, action="set_value",
                                message=f"No option matching {value!r} on element {element}")
        except Exception as e:
            return ActionResult(ok=False, action="set_value",
                                message=f"set_value failed (no EditableText/Selection): {e}")

    def _lookup_node(self, index: int) -> Optional[Any]:
        """Re-fetch the AT-SPI node for an element index by re-walking.

        We store only UIElement snapshots (bounds/role/label) — not live AT-SPI
        objects — because holding AT-SPI references across calls is unsafe.
        For set_value we re-walk to find the node at the same position. This is
        a best-effort match by role+label; returns None if not found.
        """
        if not (1 <= index <= len(self._last_elements)):
            return None
        target = self._last_elements[index - 1]
        found: List[Any] = []
        try:
            self._walk_tree_collect_node(self._desktop, target, found)
        except Exception:
            pass
        return found[0] if found else None

    def _walk_tree_collect_node(
        self, node: Any, target: UIElement, out: List[Any], depth: int = 0,
    ) -> None:
        if out or depth > _MAX_TREE_DEPTH:
            return
        if (_safe_role(node) == target.role
                and _safe_attr(node, "name", "")[:80] == target.label):
            out.append(node)
            return
        try:
            n = node.childCount
        except Exception:
            return
        for i in range(n):
            try:
                child = node[i]
            except Exception:
                continue
            if child is None:
                continue
            self._walk_tree_collect_node(child, target, out, depth + 1)
            if out:
                return

    # ── Native AT-SPI action (doAction) ────────────────────────────────────

    def _try_do_action(self, element: int, preferred: Tuple[str, ...]) -> bool:
        """Attempt a native AT-SPI action (click/press) on the element.

        Returns True if an action was successfully invoked. Many apps do not
        implement doAction, so callers should treat False as 'fall back to
        pyautogui coordinates' rather than an error.
        """
        node = self._lookup_node(element)
        if node is None:
            return False
        try:
            action = node.queryAction()
            n_actions = action.nActions
            best = -1
            for i in range(n_actions):
                name = (action.getName(i) or "").lower()
                for p in preferred:
                    if p in name:
                        best = i
                        break
                if best >= 0:
                    break
            if best < 0:
                # No named click/press — fall back to action 0 if any.
                best = 0 if n_actions > 0 else -1
            if best < 0:
                return False
            action.doAction(best)
            return True
        except Exception as e:
            logger.debug("doAction failed on element %s: %s", element, e)
            return False

    # ── App/window listing (reuse pyautogui's Linux helpers) ───────────────

    def list_apps(self) -> List[Dict[str, Any]]:
        from markbot.tools.computer_use.pyautogui_backend import _list_apps_linux
        if sys.platform == "linux":
            return _list_apps_linux()
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        # Same wmctrl/xdotool logic as pyautogui_backend — reuse it verbatim.
        from markbot.tools.computer_use.pyautogui_backend import PyAutoGUIBackend
        helper = PyAutoGUIBackend.__new__(PyAutoGUIBackend)
        helper._started = True
        helper._device_scale = self._device_scale
        helper._screen_width = self._screen_width
        helper._screen_height = self._screen_height
        return helper.focus_app(app, raise_window=raise_window)


# ─── pyatspi accessors — all defensive (the tree can mutate under us) ────────

def _safe_attr(node: Any, attr: str, default: Any = "") -> Any:
    try:
        v = getattr(node, attr, default)
        return v if v is not None else default
    except Exception:
        return default


def _safe_role(node: Any) -> str:
    try:
        return node.getRoleName() or "unknown"
    except Exception:
        return "unknown"


def _get_bounds(node: Any) -> Optional[Tuple[int, int, int, int]]:
    """Return (x, y, w, h) in logical desktop coords, or None if unavailable."""
    try:
        comp = node.queryComponent()
    except Exception:
        return None
    try:
        import pyatspi
        ext = comp.getExtents(pyatspi.DESKTOP_COORDS)
    except Exception:
        return None
    try:
        # pyatspi Rect-like: .x/.y/.width/.height (older API used (x,y,w,h) tuple)
        if hasattr(ext, "width"):
            w, h = ext.width, ext.height
            x, y = ext.x, ext.y
        else:
            x, y, w, h = ext  # legacy tuple form
        if w <= 0 or h <= 0:
            return None
        return (int(x), int(y), int(w), int(h))
    except Exception:
        return None


# Roles worth surfacing as interactable elements. AT-SPI role names are
# lowercased human strings ("push button", "entry", "check box", ...).
_INTERACTABLE_ROLE_HINTS = (
    "button", "entry", "text", "combo", "check", "radio", "menu item",
    "list item", "tab", "link", "toggle", "slider", "spin", "page tab",
    "tree item", "icon",
)


def _is_interactable(role: str, node: Any) -> bool:
    role_l = role.lower()
    for hint in _INTERACTABLE_ROLE_HINTS:
        if hint in role_l:
            return True
    # Fallback: an element exposing an Action interface is actionable.
    try:
        node.queryAction()
        return True
    except Exception:
        return False


def _is_application(node: Any) -> bool:
    try:
        return "application" in (node.getRoleName() or "").lower()
    except Exception:
        return False


def _encode_png(image: Any) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ─── Re-exported pyautogui helpers (avoid duplicating key/modifier tables) ──
# Imported lazily-free: these are module-level functions in pyautogui_backend.

def _parse_key_combo_list(combo: str) -> Tuple[List[str], str]:
    from markbot.tools.computer_use.pyautogui_backend import _parse_key_combo_list as _p
    return _p(combo)


def _normalize_modifier(mod: str) -> str:
    from markbot.tools.computer_use.pyautogui_backend import _normalize_modifier as _n
    return _n(mod)


def _resolve_key(key: str) -> str:
    from markbot.tools.computer_use.pyautogui_backend import _resolve_key as _r
    return _r(key)
