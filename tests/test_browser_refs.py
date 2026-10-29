"""Browser tools must expose refs the click/type tools can actually resolve."""

from types import SimpleNamespace

import pytest

import markbot.tools.browser as browser


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def count(self):
        return 1 if self.page.annotated else 0

    @property
    def first(self):
        return self

    async def click(self):
        self.page.clicked.append(self.selector)

    async def fill(self, text):
        self.page.filled.append((self.selector, text))


class _Page:
    def __init__(self, elements):
        self.elements = elements
        self.annotated = True
        self.clicked = []
        self.filled = []

    def locator(self, selector):
        return _Locator(self, selector)

    async def evaluate(self, _script):
        return self.elements


@pytest.fixture
def page(monkeypatch):
    elements = [
        {"ref": "e1", "tag": "button", "type": "", "label": "Submit"},
        {"ref": "e2", "tag": "input", "type": "text", "label": "Email"},
    ]
    fake = _Page(elements)

    async def _get_page(task_id, config=None):
        return fake

    monkeypatch.setattr(browser, "_get_page", _get_page)
    return fake


def test_normalize_ref_accepts_all_forms():
    assert browser._normalize_ref("e5") == "e5"
    assert browser._normalize_ref("@e5") == "e5"
    assert browser._normalize_ref("5") == "e5"


@pytest.mark.asyncio
async def test_annotate_returns_addressed_elements(page):
    elements = await browser._annotate_page(page)
    rendered = browser._format_interactive_elements(elements)

    assert rendered.splitlines() == ['@e1 button "Submit"', '@e2 input[text] "Email"']


@pytest.mark.asyncio
async def test_click_uses_injected_ref(page):
    result = await browser.BrowserClickTool()._legacy_execute(
        element="@e1", _tool_context=SimpleNamespace(session_id="t1")
    )

    assert result == "Clicked element @e1"
    assert page.clicked == ['[data-markbot-ref="e1"]']


@pytest.mark.asyncio
async def test_type_fills_by_ref(page):
    result = await browser.BrowserTypeTool()._legacy_execute(
        element="e2",
        text="a@b.com",
        _tool_context=SimpleNamespace(session_id="t1"),
    )

    assert result == "Typed 'a@b.com' into element @e2"
    assert page.filled == [('[data-markbot-ref="e2"]', "a@b.com")]


@pytest.mark.asyncio
async def test_missing_ref_reports_recovery_hint(monkeypatch):
    page = _Page([])
    page.annotated = False

    async def _get_page(task_id, config=None):
        return page

    monkeypatch.setattr(browser, "_get_page", _get_page)

    result = await browser.BrowserClickTool()._legacy_execute(
        element="e9", _tool_context=SimpleNamespace(session_id="t1")
    )

    assert "browser_snapshot" in result
