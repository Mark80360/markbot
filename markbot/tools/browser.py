"""Browser automation tools for markbot.

Uses Playwright (async API) for local browser control. Provides 8 tools:
navigate, snapshot, click, type, scroll, press, back, vision.

Each browser session is isolated per task_id. Sessions are lazily created
and auto-closed after a timeout.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Optional

from markbot.tools.base import Tool
from markbot.types.permission import PermissionDecision
from markbot.types.tool import ToolContext

logger = logging.getLogger(__name__)

_sessions: dict[str, dict[str, Any]] = {}
_SESSION_TIMEOUT = 600
_cleanup_task: Optional[asyncio.Task] = None


def _build_multimodal_result(
    text_summary: str,
    image_base64: str,
    image_mime: str = "image/png",
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": text_summary},
    ]
    if image_base64:
        data_url = f"data:{image_mime};base64,{image_base64}"
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    return {
        "_multimodal": True,
        "content": content,
        "text_summary": text_summary,
    }


_session_lock: Optional[asyncio.Lock] = None


def _session_guard() -> asyncio.Lock:
    """Lazily create the session-creation lock (avoids loop binding at import)."""
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


async def _get_session(task_id: str, config: Optional[Any] = None) -> dict[str, Any]:
    """Get or create a browser session for the given task_id.

    Creation is serialised: two concurrent misses used to launch two browsers,
    overwrite the dict entry, and leak the first Chromium process.
    """
    global _cleanup_task

    async with _session_guard():
        if task_id in _sessions:
            _sessions[task_id]["last_used"] = time.time()
            return _sessions[task_id]

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed. Install with:\n"
                "  pip install playwright && playwright install chromium"
            )

        headless = True
        if config and hasattr(config, "headless"):
            headless = config.headless

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        session = {
            "playwright": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "last_used": time.time(),
        }
        _sessions[task_id] = session

        if _cleanup_task is None or _cleanup_task.done():
            _cleanup_task = asyncio.create_task(_cleanup_sessions())

        return session


async def _cleanup_sessions() -> None:
    """Periodically close idle browser sessions."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        to_remove = []
        for tid, sess in _sessions.items():
            if now - sess["last_used"] > _SESSION_TIMEOUT:
                to_remove.append(tid)
        for tid in to_remove:
            await _close_session(tid)


async def _close_session(task_id: str) -> None:
    """Close a browser session and release resources."""
    session = _sessions.pop(task_id, None)
    if not session:
        return
    try:
        await session["context"].close()
    except Exception as e:
        logger.debug("Failed to close browser context for %s: %s", task_id, e)
    try:
        await session["browser"].close()
    except Exception as e:
        logger.debug("Failed to close browser for %s: %s", task_id, e)
    try:
        await session["playwright"].stop()
    except Exception as e:
        logger.debug("Failed to stop playwright for %s: %s", task_id, e)


async def _get_page(task_id: str, config: Optional[Any] = None):
    session = await _get_session(task_id, config)
    return session["page"]


def _get_task_id(context: ToolContext) -> str:
    return context.session_id or "default"


_REF_ATTR = "data-markbot-ref"

# Marks interactive elements with a reference the model can address. Playwright's
# accessibility snapshot exposes role/name but neither a ref nor geometry, so the
# click-by-ref and click-by-bounds paths could never match anything. Refs are
# regenerated per snapshot, so they always describe the current DOM.
_ANNOTATE_JS = """
() => {
  const SEL = 'a[href], button, input, textarea, select, summary, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="radio"], [role="combobox"], [role="tab"], [onclick], [contenteditable="true"]';
  document.querySelectorAll('[data-markbot-ref]').forEach(el => el.removeAttribute('data-markbot-ref'));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    if (rect.width === 0 || rect.height === 0 || el.disabled) continue;
    n += 1;
    const ref = 'e' + n;
    el.setAttribute('data-markbot-ref', ref);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const label = (
      el.innerText || el.value || el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') || el.getAttribute('name') ||
      el.getAttribute('title') || ''
    ).replace(/\\s+/g, ' ').trim().slice(0, 120);
    out.push({ref, tag, type, label});
    if (n >= 200) break;
  }
  return out;
}
"""


def _normalize_ref(element: str) -> str:
    """Accept ``e5``, ``@e5`` or ``5`` and return the canonical ``e5`` form."""
    ref = (element or "").strip().lstrip("@")
    if ref and not ref.startswith("e"):
        ref = f"e{ref}"
    return ref


async def _annotate_page(page: Any) -> list[dict[str, Any]]:
    """Tag interactive elements on the page and return their refs."""
    try:
        result = await page.evaluate(_ANNOTATE_JS)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.debug("Annotate page failed: %s", e)
        return []


def _format_interactive_elements(elements: list[dict[str, Any]]) -> str:
    """Render annotated elements as ``@e1 button "Submit"`` lines."""
    lines: list[str] = []
    for el in elements:
        tag = el.get("tag") or "element"
        typ = el.get("type") or ""
        if typ and tag in ("input", "button"):
            tag = f"{tag}[{typ}]"
        label = el.get("label") or ""
        lines.append(f"@{el.get('ref')} {tag}" + (f' "{label}"' if label else ""))
    return "\n".join(lines)


class BrowserNavigateTool(Tool):
    """Navigate to a URL in the browser."""

    # Not read-only: navigation changes the shared page state, so it must not
    # join a concurrent batch with other browser calls.

    @property
    def name(self) -> str:
        return "browser_navigate"

    @property
    def description(self) -> str:
        return (
            "Navigate to a URL in the browser. Initializes the session and loads the page. "
            "Must be called before other browser tools. For simple information retrieval, prefer "
            "web_search or web_extract (faster, cheaper). For plain-text endpoints (URLs ending "
            "in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any "
            "documented API endpoint), prefer web_extract or curl via terminal; the browser "
            "stack is much slower for these. Use browser tools when you need to interact with a "
            "page (click, fill forms, dynamic content). Returns a compact page snapshot with "
            "interactive elements and ref IDs — no need to call browser_snapshot separately "
            "after navigating."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to."},
            },
            "required": ["url"],
        }

    async def _legacy_execute(self, url: str = "", **kwargs: Any) -> Any:
        if not url:
            return "Error: 'url' parameter is required"

        from markbot.utils.ssrf import validate_resolved_url, validate_url_target
        ok, err = validate_url_target(url)
        if not ok:
            return f"Error: {err}"

        from markbot.utils.website_policy import check_website_policy
        policy_error = check_website_policy(url)
        if policy_error:
            return f"Error: {policy_error}"

        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))
        timeout = 30000

        try:
            response = await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if response and response.url and response.url != url:
                ok, err = validate_resolved_url(response.url)
                if not ok:
                    return f"Error: redirect blocked - {err}"
                # Re-check the allow/deny policy on the final URL: a redirect
                # can land on a domain the operator explicitly blocked.
                policy_error = check_website_policy(response.url)
                if policy_error:
                    return f"Error: redirect blocked - {policy_error}"
            status = response.status if response else "unknown"
            title = await page.title()
            # Return navigation status + compact snapshot so the model can
            # immediately interact without a separate browser_snapshot call.
            snapshot_text = _format_interactive_elements(await _annotate_page(page))
            header = f"Navigated to {url}\nStatus: {status}\nTitle: {title}"
            if snapshot_text:
                max_chars = 8000
                if len(snapshot_text) > max_chars:
                    snapshot_text = snapshot_text[:max_chars] + f"\n... (truncated, {len(snapshot_text)} total chars)"
                return f"{header}\n\n{snapshot_text}"
            return header
        except Exception as e:
            return f"Navigation error: {e}"


class BrowserSnapshotTool(Tool):
    """Get a snapshot of the page's interactive elements."""

    # Re-annotating rewrites the ref attributes, so two snapshots must not run
    # concurrently against the same page.

    @property
    def name(self) -> str:
        return "browser_snapshot"

    @property
    def description(self) -> str:
        return (
            "Get a snapshot of the page's interactive elements. "
            "Returns elements with ref IDs (like @e1, @e2) for browser_click "
            "and browser_type. Snapshots over 8000 chars are truncated. "
            "Requires browser_navigate first. browser_navigate already returns "
            "a snapshot — use this to refresh after interactions that change the page."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            elements = await _annotate_page(page)
            if not elements:
                return "No interactive elements found on the page"

            result = _format_interactive_elements(elements)

            max_chars = 8000
            if len(result) > max_chars:
                result = result[:max_chars] + f"\n... (truncated, {len(result)} total chars)"

            return result
        except Exception as e:
            return f"Snapshot error: {e}"


class BrowserClickTool(Tool):
    """Click an element on the page."""

    @property
    def name(self) -> str:
        return "browser_click"

    @property
    def description(self) -> str:
        return (
            "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). "
            "The ref IDs are shown in square brackets in the snapshot output. "
            "Requires browser_navigate to be called first (which returns a snapshot with ref IDs)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "element": {"type": "string", "description": "Element reference (e.g. 'e5' or '@e5')."},
            },
            "required": ["element"],
        }

    async def _legacy_execute(self, element: str = "", **kwargs: Any) -> Any:
        if not element:
            return "Error: 'element' parameter is required"

        element_ref = _normalize_ref(element)
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            locator = page.locator(f'[{_REF_ATTR}="{element_ref}"]')
            if await locator.count() == 0:
                # The DOM may have changed since the last snapshot; re-annotate
                # once and retry with the same ref before giving up.
                await _annotate_page(page)
                locator = page.locator(f'[{_REF_ATTR}="{element_ref}"]')
            if await locator.count() > 0:
                await locator.first.click()
                return f"Clicked element @{element_ref}"
        except Exception as e:
            logger.debug("Click by ref failed: %s", e)

        return (
            f"Could not find element @{element_ref} on the page. "
            "Call browser_snapshot to refresh the element refs."
        )


class BrowserTypeTool(Tool):
    """Type text into an element on the page."""

    @property
    def name(self) -> str:
        return "browser_type"

    @property
    def description(self) -> str:
        return (
            "Type text into an input field identified by its ref ID. "
            "Clears the field first, then types the new text. "
            "Requires browser_navigate to be called first (which returns a snapshot with ref IDs)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "element": {"type": "string", "description": "Element reference (e.g. 'e5')."},
                "text": {"type": "string", "description": "Text to type."},
            },
            "required": ["element", "text"],
        }

    async def _legacy_execute(self, element: str = "", text: str = "", **kwargs: Any) -> Any:
        if not element:
            return "Error: 'element' parameter is required"
        if not text:
            return "Error: 'text' parameter is required"

        element_ref = _normalize_ref(element)
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            locator = page.locator(f'[{_REF_ATTR}="{element_ref}"]')
            if await locator.count() == 0:
                await _annotate_page(page)
                locator = page.locator(f'[{_REF_ATTR}="{element_ref}"]')
            if await locator.count() > 0:
                # ``fill`` clears the field and sets the value atomically, which
                # also works for contenteditable targets (the previous
                # Meta+a shortcut only cleared fields on macOS).
                await locator.first.fill(text)
                return f"Typed '{text[:50]}' into element @{element_ref}"
        except Exception as e:
            return f"Type error: {e}"

        return (
            f"Could not find element @{element_ref} on the page. "
            "Call browser_snapshot to refresh the element refs."
        )


class BrowserScrollTool(Tool):
    """Scroll the page."""

    _is_read_only = True

    @property
    def name(self) -> str:
        return "browser_scroll"

    @property
    def description(self) -> str:
        return (
            "Scroll the page in a direction. Use this to reveal more content that "
            "may be below or above the current viewport. "
            "Requires browser_navigate to be called first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "description": "Scroll direction.",
                    "enum": ["up", "down"],
                    "default": "down",
                },
                "amount": {
                    "type": "integer",
                    "description": "Scroll amount in pixels.",
                    "default": 300,
                },
            },
        }

    async def _legacy_execute(self, direction: str = "down", amount: int = 300, **kwargs: Any) -> Any:
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        delta_y = -amount if direction == "up" else amount
        try:
            await page.mouse.wheel(0, delta_y)
            return f"Scrolled {direction} by {amount}px"
        except Exception as e:
            return f"Scroll error: {e}"


class BrowserPressTool(Tool):
    """Press a keyboard key."""

    @property
    def name(self) -> str:
        return "browser_press"

    @property
    def description(self) -> str:
        return (
            "Press a keyboard key. Useful for submitting forms (Enter), "
            "navigating (Tab), or keyboard shortcuts. "
            "Requires browser_navigate to be called first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown').",
                },
            },
            "required": ["key"],
        }

    async def _legacy_execute(self, key: str = "", **kwargs: Any) -> Any:
        if not key:
            return "Error: 'key' parameter is required"

        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            await page.keyboard.press(key)
            return f"Pressed key: {key}"
        except Exception as e:
            return f"Press error: {e}"


class BrowserBackTool(Tool):
    """Navigate back in browser history."""

    _is_read_only = True

    @property
    def name(self) -> str:
        return "browser_back"

    @property
    def description(self) -> str:
        return (
            "Navigate back to the previous page in browser history. "
            "Requires browser_navigate to be called first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            await page.go_back(wait_until="domcontentloaded")
            title = await page.title()
            url = page.url
            return f"Navigated back to: {url}\nTitle: {title}"
        except Exception as e:
            return f"Back navigation error: {e}"


class BrowserVisionTool(Tool):
    """Take a screenshot of the current page."""

    _is_read_only = True

    @property
    def name(self) -> str:
        return "browser_vision"

    @property
    def description(self) -> str:
        return (
            "Take a screenshot of the current page so you can inspect it visually. "
            "Use this when you need to understand what the page looks like — especially for "
            "CAPTCHAs, visual verification challenges, complex layouts, or cases where the "
            "text snapshot misses important visual information. When your active model has "
            "native vision, the screenshot is attached to your context directly; otherwise "
            "the framework falls back to an auxiliary vision model and returns a text analysis. "
            "Requires browser_navigate to be called first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "annotate": {
                    "type": "boolean",
                    "description": "If true, annotate the screenshot with element numbers.",
                    "default": False,
                },
            },
        }

    async def _legacy_execute(self, annotate: bool = False, **kwargs: Any) -> Any:
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            screenshot_bytes = await page.screenshot(type="jpeg", quality=85)
            image_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            title = await page.title()
            url = page.url

            return _build_multimodal_result(
                text_summary=f"Screenshot of {url}\nTitle: {title}",
                image_base64=image_b64,
                image_mime="image/jpeg",
            )
        except Exception as e:
            return f"Screenshot error: {e}"


class BrowserConsoleTool(Tool):
    """Get browser console output or evaluate JavaScript.

    Reading the console buffer is genuinely read-only. Supplying an
    ``expression`` is not: ``page.evaluate()`` runs arbitrary JavaScript in
    the page, which can read ``document.cookie`` / ``localStorage``, exfiltrate
    the DOM to an attacker's host, or drive the page. The tool therefore
    declares itself *not* read-only and gates the mutating half on an explicit
    permission check, so a prompt-injected page cannot make the agent run JS
    with no confirmation — including in PLAN mode, where
    ``check_permission`` is consulted before the read-only shortcut.
    """

    # Deliberately False. See the class docstring: `expression` executes
    # arbitrary JavaScript, which is not a read-only operation.
    _is_read_only = False

    @property
    def name(self) -> str:
        return "browser_console"

    @property
    def description(self) -> str:
        return (
            "Get browser console output and JavaScript errors from the current page. "
            "Returns console.log/warn/error/info messages and uncaught JS exceptions. "
            "Use this to detect silent JavaScript errors, failed API calls, and application warnings. "
            "When 'expression' is provided, evaluates JavaScript in the page context and "
            "returns the result — use this for DOM inspection, reading page state, or "
            "extracting data programmatically. Requires browser_navigate to be called first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "clear": {
                    "type": "boolean",
                    "description": "If true, clear the message buffers after reading.",
                    "default": False,
                },
                "expression": {
                    "type": "string",
                    "description": (
                        "JavaScript expression to evaluate in the page context. "
                        "Runs in the browser like DevTools console — full access to DOM, "
                        "window, document. Return values are serialized to JSON. "
                        "Example: 'document.title' or 'document.querySelectorAll(\"a\").length'"
                    ),
                },
            },
        }

    async def check_permission(self, params: dict, context) -> PermissionDecision:
        """Confirm only the JavaScript-evaluation half.

        Reading the console buffer is a genuine read and stays
        auto-approved; ``expression`` executes caller-supplied JS in the
        page, which is not. Splitting here keeps the common debugging case
        frictionless while closing the injection path.
        """
        if not (params or {}).get("expression"):
            return PermissionDecision(
                behavior="allow", reason="reading the console buffer is read-only"
            )
        return await super().check_permission(params, context)

    async def _legacy_execute(self, clear: bool = False, expression: str = "", **kwargs: Any) -> Any:
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            if expression:
                # Defence in depth: `check_permission` is the gate, but a
                # future caller that bypasses the pipeline should not get
                # arbitrary JS execution for free either.
                verdict = await self.check_permission(
                    {"expression": expression}, context
                )
                if verdict.behavior == "deny":
                    return f"Error: {verdict.reason or 'expression evaluation denied'}"
                result = await page.evaluate(expression)
                if isinstance(result, (dict, list)):
                    import json
                    return json.dumps(result, indent=2, ensure_ascii=False)
                return str(result)

            logs = await page.evaluate(
                "() => window.__markbot_console_logs || []"
            )
            if clear:
                await page.evaluate("() => { window.__markbot_console_logs = [] }")

            if not logs:
                return "No console output captured. Console logs are collected from page load."

            lines = []
            for entry in logs:
                level = entry.get("level", "log")
                text = entry.get("text", "")
                lines.append(f"[{level}] {text}")
            return "\n".join(lines)
        except Exception as e:
            return f"Console error: {e}"


class BrowserGetImagesTool(Tool):
    """Get all images on the current page."""

    _is_read_only = True

    @property
    def name(self) -> str:
        return "browser_get_images"

    @property
    def description(self) -> str:
        return (
            "Get a list of all images on the current page with their URLs and alt text. "
            "Useful for finding images to analyze with the vision tool. "
            "Requires browser_navigate to be called first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def _legacy_execute(self, **kwargs: Any) -> Any:
        context = kwargs.get("_tool_context")
        page = await _get_page(_get_task_id(context))

        try:
            images = await page.evaluate(
                "() => Array.from(document.querySelectorAll('img')).map(img => ({"
                "  src: img.src || '',"
                "  alt: img.alt || '',"
                "  width: img.naturalWidth || 0,"
                "  height: img.naturalHeight || 0,"
                "}))"
            )

            if not images:
                return "No images found on the page."

            lines = []
            for i, img in enumerate(images[:50], 1):
                src = img.get("src", "")
                alt = img.get("alt", "")
                dims = f"{img.get('width', 0)}x{img.get('height', 0)}"
                alt_part = f' alt="{alt}"' if alt else ""
                lines.append(f"[{i}] {src} ({dims}){alt_part}")

            return f"Found {len(images)} image(s) on the page:\n" + "\n".join(lines)
        except Exception as e:
            return f"Get images error: {e}"


BROWSER_TOOLS: list[Tool] = [
    BrowserNavigateTool(),
    BrowserSnapshotTool(),
    BrowserClickTool(),
    BrowserTypeTool(),
    BrowserScrollTool(),
    BrowserPressTool(),
    BrowserBackTool(),
    BrowserVisionTool(),
    BrowserConsoleTool(),
    BrowserGetImagesTool(),
]
