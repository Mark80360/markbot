"""Packaging and shipped-artifact invariants.

These are the checks a CI build would run, expressed as tests so they run on
every ``pytest`` instead of only in a release pipeline. The failure mode they
guard against is nasty precisely because it is invisible in a source
checkout: the code works, the tests pass, and only the *installed* package is
broken.
"""

from __future__ import annotations

import fnmatch
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
WEB_STATIC = REPO_ROOT / "markbot" / "web" / "static"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _include_patterns(pyproject: dict) -> list[str]:
    return pyproject["tool"]["hatch"]["build"]["include"]


def _is_included(rel_path: str, patterns: list[str]) -> bool:
    """Match *rel_path* against build globs using pathspec semantics.

    ``fnmatch`` alone treats ``**`` as "any characters", which makes
    ``a/**/b`` fail to match ``a/b`` — but hatchling (like gitignore) reads
    ``**/`` as "zero or more directories". Both readings are tried so the
    test agrees with the real build instead of inventing a stricter rule.
    """
    for pattern in patterns:
        candidates = {pattern}
        while "/**/" in pattern:
            pattern = pattern.replace("/**/", "/", 1)
            candidates.add(pattern)
        if any(fnmatch.fnmatch(rel_path, c) for c in candidates):
            return True
    return False


class TestWheelIncludesTheWebUi:
    """`web/server.py` mounts ``markbot/web/static/assets`` and serves
    ``index.html``. The build include list matched no ``.html/.css/.js``
    pattern, so an installed wheel shipped the server with no front end."""

    def test_every_shipped_static_file_is_included(self, pyproject):
        patterns = _include_patterns(pyproject)
        files = [p for p in WEB_STATIC.rglob("*") if p.is_file()]
        assert files, "expected the built web UI to exist in the repo"

        missing = [
            str(p.relative_to(REPO_ROOT))
            for p in files
            if not _is_included(str(p.relative_to(REPO_ROOT)), patterns)
        ]
        assert not missing, (
            "these runtime files would be absent from the wheel "
            f"(add a matching include pattern): {missing}"
        )

    def test_index_html_is_included(self, pyproject):
        rel = str((WEB_STATIC / "index.html").relative_to(REPO_ROOT))
        assert _is_included(rel, _include_patterns(pyproject))

    def test_no_orphan_build_artifacts(self):
        """Every asset in ``assets/`` must be referenced by ``index.html``.

        A stale bundle from an older build is dead weight in the wheel and,
        worse, an unreviewed copy of the app that still gets served.
        """
        index = (WEB_STATIC / "index.html").read_text(encoding="utf-8")
        referenced = set(re.findall(r'(?:src|href)="([^"]+)"', index))
        referenced_names = {Path(r).name for r in referenced}

        orphans = [
            p.name
            for p in (WEB_STATIC / "assets").iterdir()
            if p.is_file() and p.name not in referenced_names
        ]
        assert not orphans, f"unreferenced build artifacts in assets/: {orphans}"


class TestDeclaredDependencies:
    def test_direct_imports_are_declared(self, pyproject):
        """``aiohttp`` was imported at module scope by ``channels/qq.py`` and
        never declared — it only worked because ``qq-botpy`` happened to
        depend on it."""
        deps = " ".join(pyproject["project"]["dependencies"])
        assert "aiohttp" in deps

    def test_every_runtime_dependency_has_an_upper_bound(self, pyproject):
        """An unbounded major version is a breaking change with no warning.

        ``openai`` was the single exception out of 28 runtime deps.
        """
        unbounded = [
            dep
            for dep in pyproject["project"]["dependencies"]
            if "<" not in dep
        ]
        assert not unbounded, f"dependencies without an upper bound: {unbounded}"

    def test_openai_is_pinned_below_three(self, pyproject):
        spec = next(
            d for d in pyproject["project"]["dependencies"] if d.startswith("openai")
        )
        assert "<3.0.0" in spec


class TestRepoHygiene:
    def test_gitignore_covers_tsbuildinfo(self):
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "*.tsbuildinfo" in ignored

    def test_gitignore_covers_root_patch_helpers(self):
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".tmp_patch_loop.py" in ignored

    def test_readme_does_not_reference_missing_scripts(self):
        """README told developers to run ``python run_debug.py``; the file is
        gitignored and absent, so the documented debug entry point was a
        guaranteed ``No such file``."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for match in re.findall(r"python ([A-Za-z0-9_./-]+\.py)", readme):
            assert (REPO_ROOT / match).exists(), f"README references missing {match}"


class TestTemplateSync:
    """``sync_workspace_templates`` filtered on ``.endswith('.md')``, so
    ``feature_list.json`` and ``init.sh`` shipped in the package but were
    never created in a workspace."""

    def test_non_markdown_templates_are_synced(self, tmp_path):
        from markbot.utils.helpers import sync_workspace_templates

        added = sync_workspace_templates(tmp_path, silent=True)
        assert added, "nothing was synced"

        names = {Path(p).name for p in added}
        assert "feature_list.json" in names
        assert "init.sh" in names

    def test_shell_template_is_executable(self, tmp_path):
        from markbot.utils.helpers import sync_workspace_templates

        sync_workspace_templates(tmp_path, silent=True)
        init_sh = tmp_path / "init.sh"
        assert init_sh.exists()
        assert init_sh.stat().st_mode & 0o111, "a shipped shell helper must be runnable"

    def test_existing_files_are_not_overwritten(self, tmp_path):
        from markbot.utils.helpers import sync_workspace_templates

        (tmp_path / "AGENTS.md").write_text("mine", encoding="utf-8")
        sync_workspace_templates(tmp_path, silent=True)
        assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "mine"

    def test_nested_templates_are_synced(self, tmp_path):
        from markbot.utils.helpers import sync_workspace_templates

        sync_workspace_templates(tmp_path, silent=True)
        assert (tmp_path / "agents" / "GROUP_CHAT_GUIDE.md").exists()


class TestQqGracefulDegradation:
    """The ``try: import botpy`` block is an explicit "channel unavailable is
    fine" design. A hard ``import aiohttp`` above it defeated that: a missing
    aiohttp raised before ``QQ_AVAILABLE`` could ever be set to False."""

    def test_aiohttp_import_is_guarded(self):
        src = (REPO_ROOT / "markbot" / "channels" / "qq.py").read_text(encoding="utf-8")
        lines = [ln for ln in src.splitlines() if ln.startswith("import aiohttp")]
        assert not lines, "a module-level `import aiohttp` bypasses the degradation path"

    def test_module_imports_without_aiohttp(self, monkeypatch):
        """Simulate aiohttp being absent and assert the module still imports
        and reports the channel unavailable."""
        import importlib

        import markbot.channels.qq as qq_mod

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def _fake_import(name, *args, **kwargs):
            if name == "aiohttp":
                raise ImportError("simulated: aiohttp not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fake_import)
        try:
            reloaded = importlib.reload(qq_mod)
            assert reloaded.QQ_AVAILABLE is False
            assert reloaded.aiohttp is None
        finally:
            monkeypatch.undo()
            importlib.reload(qq_mod)
