"""Tests for user provider plugins — externally registered ProviderSpecs.

Covers the two registration paths (programmatic + filesystem discovery),
override semantics, priority preservation, and failure tolerance.

Filesystem discovery is **opt-in**. That is a security property, not a
convenience: ``<data dir>/providers`` is writable by the agent's own file
tools, so importing every ``*.py`` found there turned a plain file write
into in-process code execution that skipped the permission prompt and the
sandbox. The tests below pin both halves — nothing loads by default, and
the two documented opt-ins work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from markbot.config.loader import get_config_path, set_config_path
from markbot.providers.registry import (
    PROVIDERS,
    ProviderSpec,
    _reset_provider_registry,
    find_by_name,
    list_specs,
    register_provider_spec,
)

_NEW_SPEC = """
from markbot.providers.registry import ProviderSpec, register_provider_spec

register_provider_spec(ProviderSpec(
    name={name!r},
    keywords={keywords!r},
    env_key={env_key!r},
    display_name={display_name!r},
    default_api_base={api_base!r},
))
"""

#: Plugin body that leaves a marker file behind, so a test can prove the
#: module actually executed rather than merely existing on disk.
_MARKER_SPEC = """
from pathlib import Path
from markbot.providers.registry import ProviderSpec, register_provider_spec

Path({marker!r}).write_text("loaded", encoding="utf-8")
register_provider_spec(ProviderSpec(
    name={name!r},
    keywords=({name!r},),
    env_key="MARKER_KEY",
    display_name={name!r},
))
"""


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Each test starts from a clean registry and config path."""
    prev_path = get_config_path()
    yield
    set_config_path(prev_path)
    _reset_provider_registry()


def _write_plugin(data_dir: Path, filename: str, body: str) -> Path:
    plugins_dir = data_dir / "providers"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    path = plugins_dir / filename
    path.write_text(body, encoding="utf-8")
    # Plugins are refused when world-writable; be explicit so the test does
    # not depend on the runner's umask.
    os.chmod(path, 0o644)
    return path


def _write_config(data_dir: Path, security: dict) -> None:
    (data_dir / "config.json").write_text(
        json.dumps({"security": security}), encoding="utf-8"
    )


def _enable_plugins(data_dir: Path, *, allowlist: list[str] | None = None) -> None:
    """Opt in via config — the same path an operator would use."""
    if allowlist is None:
        _write_config(data_dir, {"allow_user_provider_plugins": True})
    else:
        _write_config(data_dir, {"user_provider_plugin_allowlist": allowlist})


def _simple_spec(name: str, *, api_base: str = "https://x.example/v1") -> str:
    return _NEW_SPEC.format(
        name=name,
        keywords=(name,),
        env_key=f"{name.upper()}_API_KEY",
        display_name=name.title(),
        api_base=api_base,
    )


def test_programmatic_registration_appends_new_provider(tmp_path):
    set_config_path(tmp_path / "config.json")

    spec = ProviderSpec(
        name="mycloud",
        keywords=("mycloud",),
        env_key="MYCLOUD_API_KEY",
        display_name="MyCloud",
        default_api_base="https://api.mycloud.example/v1",
    )
    register_provider_spec(spec)

    assert find_by_name("mycloud") is spec
    names = [s.name for s in list_specs()]
    # new user providers go AFTER the bundled list (lowest priority)
    assert names == [s.name for s in PROVIDERS] + ["mycloud"]


def test_programmatic_registration_lookup_by_alias():
    spec = ProviderSpec(
        name="acme_inference",
        keywords=("acme",),
        aliases=("acme-inf",),
        env_key="ACME_API_KEY",
        display_name="Acme",
    )
    register_provider_spec(spec)

    assert find_by_name("acme-inf") is spec
    # hyphens normalize to the canonical snake_case name too
    assert find_by_name("acme-inference") is spec


def test_filesystem_discovery_adds_new_provider(tmp_path):
    data_dir = tmp_path
    _write_plugin(data_dir, "mycloud.py", _simple_spec("mycloud"))
    set_config_path(data_dir / "config.json")
    _enable_plugins(data_dir)

    spec = find_by_name("mycloud")
    assert spec is not None
    assert spec.display_name == "Mycloud"
    names = [s.name for s in list_specs()]
    assert names == [s.name for s in PROVIDERS] + ["mycloud"]


def test_filesystem_override_replaces_bundled_in_place(tmp_path):
    data_dir = tmp_path
    _write_plugin(
        data_dir,
        "deepseek_override.py",
        _NEW_SPEC.format(
            name="deepseek",
            keywords=("deepseek",),
            env_key="DEEPSEEK_API_KEY",
            display_name="DeepSeek Override",
            api_base="https://api.deepseek.example",
        ),
    )
    set_config_path(data_dir / "config.json")
    _enable_plugins(data_dir)

    spec = find_by_name("deepseek")
    assert spec is not None
    assert spec.display_name == "DeepSeek Override"

    # last-writer-wins replaces the bundled spec IN PLACE, so the
    # documented priority order (= match priority / fallback) is preserved.
    names = [s.name for s in list_specs()]
    bundled_names = [s.name for s in PROVIDERS]
    assert names == bundled_names
    assert names.index("deepseek") == bundled_names.index("deepseek")


def test_broken_plugin_does_not_block_others(tmp_path):
    data_dir = tmp_path
    _write_plugin(data_dir, "broken.py", "raise RuntimeError('boom')\n")
    _write_plugin(data_dir, "good.py", _simple_spec("goodprovider"))
    set_config_path(data_dir / "config.json")
    _enable_plugins(data_dir)

    # the healthy plugin still loads; the broken one is skipped, not fatal
    assert find_by_name("goodprovider") is not None
    assert find_by_name("broken") is None


def test_private_files_are_ignored(tmp_path):
    data_dir = tmp_path
    _write_plugin(data_dir, "_helper.py", "x = 1\n")
    _write_plugin(data_dir, "provider_a.py", _simple_spec("provider_a"))
    set_config_path(data_dir / "config.json")
    _enable_plugins(data_dir)

    # underscore-prefixed helper modules are never imported as plugins
    names = [s.name for s in list_specs()]
    assert "_helper" not in names
    assert find_by_name("provider_a") is not None


def test_no_plugins_dir_yields_bundled_registry(tmp_path):
    # data dir exists but has no providers/ subdirectory → nothing extra
    set_config_path(tmp_path / "config.json")
    assert [s.name for s in list_specs()] == [s.name for s in PROVIDERS]


class TestPluginsAreOptIn:
    """The default must be "do not execute anything found on disk"."""

    def test_nothing_loads_without_config(self, tmp_path):
        marker = tmp_path / "executed.txt"
        _write_plugin(
            tmp_path,
            "evil.py",
            _MARKER_SPEC.format(marker=str(marker), name="evilprovider"),
        )
        set_config_path(tmp_path / "config.json")

        assert find_by_name("evilprovider") is None
        assert not marker.exists(), "plugin code ran without opt-in"
        assert [s.name for s in list_specs()] == [s.name for s in PROVIDERS]

    def test_skipped_plugins_are_named_in_a_warning(self, tmp_path):
        """An operator whose plugin silently did nothing must be able to
        find out why from the log."""
        _write_plugin(tmp_path, "evil.py", _simple_spec("evilprovider"))
        set_config_path(tmp_path / "config.json")

        from loguru import logger as _logger

        from markbot.providers import registry as reg

        messages: list[str] = []
        sink_id = _logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            reg._discover_user_plugins()
        finally:
            _logger.remove(sink_id)

        joined = "".join(messages)
        assert "evil.py" in joined
        assert "allow_user_provider_plugins" in joined

    def test_allow_all_flag_loads_plugins(self, tmp_path):
        _write_plugin(tmp_path, "acme.py", _simple_spec("acmeprovider"))
        set_config_path(tmp_path / "config.json")
        _enable_plugins(tmp_path)

        assert find_by_name("acmeprovider") is not None

    def test_allowlist_loads_only_the_named_file(self, tmp_path):
        _write_plugin(tmp_path, "wanted.py", _simple_spec("wantedprovider"))
        _write_plugin(tmp_path, "other.py", _simple_spec("otherprovider"))
        set_config_path(tmp_path / "config.json")
        _enable_plugins(tmp_path, allowlist=["wanted.py"])

        assert find_by_name("wantedprovider") is not None
        assert find_by_name("otherprovider") is None

    def test_allowlist_accepts_a_stem(self, tmp_path):
        _write_plugin(tmp_path, "wanted.py", _simple_spec("wantedprovider"))
        set_config_path(tmp_path / "config.json")
        _enable_plugins(tmp_path, allowlist=["wanted"])

        assert find_by_name("wantedprovider") is not None

    def test_world_writable_plugin_is_refused_even_when_enabled(self, tmp_path):
        """If anyone can replace the file, "opted in" no longer means the
        operator approved *this* code."""
        path = _write_plugin(tmp_path, "shared.py", _simple_spec("sharedprovider"))
        os.chmod(path, 0o666)
        set_config_path(tmp_path / "config.json")
        _enable_plugins(tmp_path)

        assert find_by_name("sharedprovider") is None

    def test_unreadable_config_fails_closed(self, tmp_path):
        _write_plugin(tmp_path, "acme.py", _simple_spec("acmeprovider"))
        set_config_path(tmp_path / "config.json")
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")

        assert find_by_name("acmeprovider") is None

    def test_a_plugin_cannot_name_itself_into_the_allowlist(self, tmp_path):
        _write_plugin(
            tmp_path, "allow_user_provider_plugins.py", _simple_spec("sneaky")
        )
        set_config_path(tmp_path / "config.json")
        _write_config(tmp_path, {"allow_user_provider_plugins": False})

        assert find_by_name("sneaky") is None
