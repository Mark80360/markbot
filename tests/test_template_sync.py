"""Tests for template-code synchronization mechanism."""

from pathlib import Path

from markbot.utils.constants import BOOTSTRAP_FILES, check_template_sync


class TestBootstrapFilesConstant:
    def test_is_list_of_strings(self):
        assert isinstance(BOOTSTRAP_FILES, list)
        assert all(isinstance(f, str) for f in BOOTSTRAP_FILES)

    def test_all_entries_are_md_filenames(self):
        for f in BOOTSTRAP_FILES:
            assert f.endswith(".md"), f"{f} should end with .md"

    def test_no_duplicates(self):
        assert len(BOOTSTRAP_FILES) == len(set(BOOTSTRAP_FILES))

    def test_core_files_present(self):
        core = {"SOUL.md", "PROFILE.md", "TOOLS.md", "MEMORY.md"}
        assert core.issubset(set(BOOTSTRAP_FILES))


class TestCheckTemplateSync:
    def test_returns_list(self):
        result = check_template_sync()
        assert isinstance(result, list)

    def test_detects_missing_from_constant(self, tmp_path):
        (tmp_path / "HEARTBEAT.md").write_text("heartbeat content")
        (tmp_path / "BOOTSTRAP.md").write_text("bootstrap content")
        warnings = check_template_sync(templates_dir=tmp_path)
        missing = [w for w in warnings if "not in BOOTSTRAP_FILES" in w]
        assert len(missing) >= 1

    def test_detects_missing_from_disk(self, tmp_path):
        warnings = check_template_sync(templates_dir=tmp_path)
        missing = [w for w in warnings if "no template on disk" in w]
        assert len(missing) >= 1

    def test_no_warnings_when_perfectly_synced(self, tmp_path):
        for f in BOOTSTRAP_FILES:
            (tmp_path / f).write_text(f"content of {f}")
        warnings = check_template_sync(templates_dir=tmp_path)
        assert len(warnings) == 0

    def test_nonexistent_dir_returns_empty(self):
        warnings = check_template_sync(templates_dir=Path("/nonexistent/path"))
        assert warnings == []

    def test_ignores_non_md_files(self, tmp_path):
        (tmp_path / "notes.txt").write_text("not a template")
        for f in BOOTSTRAP_FILES:
            (tmp_path / f).write_text(f"content of {f}")
        warnings = check_template_sync(templates_dir=tmp_path)
        assert len(warnings) == 0

    def test_default_templates_dir_exists(self):
        result = check_template_sync()
        assert isinstance(result, list)
