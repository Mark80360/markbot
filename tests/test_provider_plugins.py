"""Tests for user provider plugins — externally registered ProviderSpecs.

Covers the two registration paths (programmatic + filesystem discovery),
override semantics, priority preservation, and failure tolerance.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Each test starts from a clean registry and config path."""
    prev_path = get_config_path()
    yield
    set_config_path(prev_path)
    _reset_provider_registry()


def _write_plugin(data_dir: Path, filename: str, body: str) -> None:
    plugins_dir = data_dir / "providers"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / filename).write_text(body, encoding="utf-8")


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
    _write_plugin(
        data_dir,
        "mycloud.py",
        _NEW_SPEC.format(
            name="mycloud",
            keywords=("mycloud",),
            env_key="MYCLOUD_API_KEY",
            display_name="MyCloud",
            api_base="https://api.mycloud.example/v1",
        ),
    )
    set_config_path(data_dir / "config.json")

    spec = find_by_name("mycloud")
    assert spec is not None
    assert spec.display_name == "MyCloud"
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
    _write_plugin(
        data_dir,
        "good.py",
        _NEW_SPEC.format(
            name="goodprovider",
            keywords=("goodprovider",),
            env_key="GOOD_API_KEY",
            display_name="Good",
            api_base="https://good.example/v1",
        ),
    )
    set_config_path(data_dir / "config.json")

    # the healthy plugin still loads; the broken one is skipped, not fatal
    assert find_by_name("goodprovider") is not None
    assert find_by_name("broken") is None


def test_private_files_are_ignored(tmp_path):
    data_dir = tmp_path
    _write_plugin(data_dir, "_helper.py", "x = 1\n")
    _write_plugin(
        data_dir,
        "provider_a.py",
        _NEW_SPEC.format(
            name="provider_a",
            keywords=("provider-a",),
            env_key="A_API_KEY",
            display_name="A",
            api_base="https://a.example/v1",
        ),
    )
    set_config_path(data_dir / "config.json")

    # underscore-prefixed helper modules are never imported as plugins
    names = [s.name for s in list_specs()]
    assert "_helper" not in names
    assert find_by_name("provider_a") is not None


def test_no_plugins_dir_yields_bundled_registry(tmp_path):
    # data dir exists but has no providers/ subdirectory → nothing extra
    set_config_path(tmp_path / "config.json")
    assert [s.name for s in list_specs()] == [s.name for s in PROVIDERS]