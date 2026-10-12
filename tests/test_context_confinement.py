"""Tests for workspace path confinement in the context-explorer tools.

``load_context`` is declared read-only, so the permission layer auto-approves
it in every mode including PLAN. It therefore has to confine its own reads:
before the fix, a model-supplied ``context_id`` of ``../pyproject.toml`` was
passed through ``workspace / filename`` and read verbatim, exposing any file
on the host — including ``config.json`` with every provider API key.
"""

from __future__ import annotations

import pytest

from markbot.tools.context_explorer import LoadContextTool


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# agents\ninside workspace\n", encoding="utf-8")
    (tmp_path / "SECRET.md").write_text("inside-secret-marker\n", encoding="utf-8")
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "guide.md").write_text("guide body\n", encoding="utf-8")
    # A real file just outside the workspace, standing in for config.json.
    (tmp_path.parent / "outside_secret.md").write_text(
        "outside-secret-marker\n", encoding="utf-8"
    )
    return tmp_path


class TestLoadContextConfinement:
    async def test_parent_traversal_refused(self, workspace):
        tool = LoadContextTool(workspace=workspace)
        out = await tool._legacy_execute("../outside_secret.md")
        assert "outside-secret-marker" not in out
        assert "outside the workspace" in out

    async def test_deep_traversal_refused(self, workspace):
        tool = LoadContextTool(workspace=workspace)
        out = await tool._legacy_execute("../../../../../../etc/passwd")
        assert "root:" not in out
        assert "outside the workspace" in out

    async def test_underscore_encoded_traversal_refused(self, workspace):
        """``_`` is rewritten to ``.``; that must not become an escape."""
        tool = LoadContextTool(workspace=workspace)
        out = await tool._legacy_execute(".._outside_secret.md")
        assert "outside-secret-marker" not in out

    async def test_workspace_file_still_loads(self, workspace):
        tool = LoadContextTool(workspace=workspace)
        out = await tool._legacy_execute("AGENTS_md")
        assert "inside workspace" in out

    async def test_workspace_docs_id_still_loads(self, workspace):
        """``docs/guide.md`` -> ``docs_guide_md`` is the normal happy path."""
        tool = LoadContextTool(workspace=workspace)
        out = await tool._legacy_execute("docs_guide_md")
        assert "guide body" in out

    async def test_absolute_path_refused(self, workspace):
        tool = LoadContextTool(workspace=workspace)
        out = await tool._legacy_execute(str(workspace.parent / "outside_secret.md"))
        assert "outside-secret-marker" not in out

    async def test_directory_is_not_read(self, workspace):
        """A path that resolves to a directory must not blow up or return it."""
        tool = LoadContextTool(workspace=workspace)
        out = await tool._legacy_execute("docs")
        assert "guide body" not in out

    async def test_no_workspace_configured_is_safe(self):
        """With no workspace there is nothing to resolve against."""
        tool = LoadContextTool(workspace=None)
        out = await tool._legacy_execute("../anything.md")
        assert "not found" in out.lower() or "error" in out.lower()
