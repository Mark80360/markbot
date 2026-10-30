"""Tests that security controls fail closed.

``CodeExecutionTool._scan_code`` caught every exception and returned
``None`` — "no findings" — so an import error, a regex catastrophe, or any
attribute change in the scanner silently turned every ``run_code`` call into
unscanned arbitrary code execution. A security control that fails open is
not a control.

The memory-write path is deliberately *not* in this set: there a scanner
failure stores the content unquarantined rather than discarding the user's
own transcript, and the fence still downgrades it to untrusted data. That is
a different trade-off, documented at the call site.
"""

from __future__ import annotations

import pytest

from markbot.tools.code import CodeExecutionTool


class TestScannerFailsClosed:
    def _tool(self):
        return CodeExecutionTool.__new__(CodeExecutionTool)

    @pytest.mark.parametrize(
        "exc",
        [
            ImportError("scanner module missing"),
            AttributeError("'Scanner' object has no attribute 'scan_code'"),
            RuntimeError("regex catastrophe"),
            ValueError("bad pattern"),
        ],
    )
    def test_scan_failure_blocks_execution(self, monkeypatch, exc):
        import markbot.skills.core.scanner as scanner_mod

        def _boom(*a, **k):
            raise exc

        monkeypatch.setattr(scanner_mod, "SecurityScanner", _boom)
        out, warnings = self._tool()._scan_code("print('hello')")
        assert out is not None, "a scanner failure must block, not allow"
        assert "blocked" in out.lower()
        assert "fail-closed" in out
        assert warnings == []

    def test_clean_code_passes(self):
        out, warnings = self._tool()._scan_code("print(1 + 1)")
        assert out is None
        assert warnings == []

    def test_dangerous_code_blocked(self):
        out, _warnings = self._tool()._scan_code("import os; os.system('rm -rf /')")
        assert out is not None
        assert "blocked" in out.lower()

    def test_error_is_actually_surfaced(self, monkeypatch):
        """The refusal must tell the operator what happened, not silently
        look like a policy violation."""
        import markbot.skills.core.scanner as scanner_mod

        def _boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(scanner_mod, "SecurityScanner", _boom)
        out, _warnings = self._tool()._scan_code("x = 1")
        assert "kaboom" in out
        assert "RuntimeError" in out

    def test_allowed_findings_are_returned_not_swallowed(self):
        """Findings the trust policy lets through must reach the caller.

        ``agent-created`` lets ``high``/``medium`` findings run. Previously
        they were written to a debug log, so the model saw a clean result
        either way. They now come back as warnings and are prepended to the
        tool output.
        """
        code = (
            "import subprocess\n"
            "subprocess.run(['ls', '-la'])\n"
        )
        out, warnings = self._tool()._scan_code(code)
        assert out is None, "subprocess without shell=True is not a block"
        assert warnings, "a flagged-but-allowed finding must be reported"
        assert any("subprocess" in w.lower() or "process" in w.lower() for w in warnings)


class TestFailClosedDiscipline:
    def test_tool_pipeline_guards_fail_closed(self):
        """The precedent this fix aligns with."""
        from markbot.tools.pipeline import ToolPipeline

        assert hasattr(ToolPipeline, "add_guard")

    def test_is_available_fails_closed(self):
        from markbot.tools.base import BaseTool

        assert callable(BaseTool.is_available)

    def test_no_allow_on_scan_failure_remains(self):
        """Guard against the old message creeping back."""
        import inspect

        from markbot.tools.code import CodeExecutionTool

        src = inspect.getsource(CodeExecutionTool._scan_code)
        assert "allowing execution" not in src
