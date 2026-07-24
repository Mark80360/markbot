"""Tests for markbot.locales.i18n -- catalog parity, fallback, language resolution.

Also covers the i18n integration in permission_approval._parse_choice, which
must recognize localized option labels (e.g. "允许" / "全部允许" / "拒绝")
so a non-English UI routes approval choices correctly.
"""

from __future__ import annotations

import json
import re

import pytest

from markbot.locales import i18n


LOCALES_DIR = i18n._locales_dir()


def _load_raw(lang: str) -> dict:
    with (LOCALES_DIR / f"{lang}.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def _flatten(d, prefix="") -> dict:
    flat = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


# ---------------------------------------------------------------------------
# Catalog completeness -- this is the key invariant test.  If someone adds a
# new key to en.json they MUST add it to every other locale, else runtime
# falls back to English for those users and defeats the feature.
# ---------------------------------------------------------------------------

def test_all_locales_exist():
    """Every supported language must have a catalog file on disk."""
    for lang in i18n.SUPPORTED_LANGUAGES:
        assert (LOCALES_DIR / f"{lang}.json").is_file(), f"missing locales/{lang}.json"


@pytest.mark.parametrize("lang", [l for l in i18n.SUPPORTED_LANGUAGES if l != "en"])
def test_catalog_keys_match_english(lang: str):
    """Every non-English catalog must have the same key set as English.

    The leading ``_comment`` key is excluded — it's a human-readable note,
    not a translatable message.
    """
    en_keys = {k for k in _flatten(_load_raw("en")) if not k.startswith("_")}
    lang_keys = {k for k in _flatten(_load_raw(lang)) if not k.startswith("_")}
    missing = en_keys - lang_keys
    extra = lang_keys - en_keys
    assert not missing, f"{lang}.json missing keys: {sorted(missing)}"
    assert not extra, f"{lang}.json has keys not in en.json: {sorted(extra)}"


@pytest.mark.parametrize("lang", list(i18n.SUPPORTED_LANGUAGES))
def test_catalog_placeholders_match_english(lang: str):
    """Every translated value must use the same {placeholder} tokens as English.

    A mistranslated placeholder (e.g. ``{tool_name}`` typoed as ``{nom}``)
    would either raise KeyError at runtime or silently drop the interpolated
    value.  Pin parity at the test layer.
    """
    placeholder_re = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
    en_flat = {k: v for k, v in _flatten(_load_raw("en")).items() if not k.startswith("_")}
    lang_flat = {k: v for k, v in _flatten(_load_raw(lang)).items() if not k.startswith("_")}
    for key, en_value in en_flat.items():
        if not isinstance(en_value, str):
            continue
        en_placeholders = set(placeholder_re.findall(en_value))
        lang_value = lang_flat.get(key, "")
        if not isinstance(lang_value, str):
            continue
        lang_placeholders = set(placeholder_re.findall(lang_value))
        assert en_placeholders == lang_placeholders, (
            f"{lang}.json key={key!r}: placeholders {lang_placeholders} "
            f"don't match English {en_placeholders}"
        )


# ---------------------------------------------------------------------------
# Language resolution
# ---------------------------------------------------------------------------

def test_normalize_lang_accepts_supported():
    assert i18n._normalize_lang("zh") == "zh"
    assert i18n._normalize_lang("EN") == "en"


def test_normalize_lang_accepts_aliases():
    assert i18n._normalize_lang("chinese") == "zh"
    assert i18n._normalize_lang("zh-CN") == "zh"
    assert i18n._normalize_lang("mandarin") == "zh"
    assert i18n._normalize_lang("zh-hans") == "zh"
    assert i18n._normalize_lang("english") == "en"
    assert i18n._normalize_lang("en-us") == "en"


def test_normalize_lang_unknown_falls_back():
    assert i18n._normalize_lang("klingon") == i18n.DEFAULT_LANGUAGE
    assert i18n._normalize_lang("") == i18n.DEFAULT_LANGUAGE
    assert i18n._normalize_lang(None) == i18n.DEFAULT_LANGUAGE


def test_get_language_env_override(monkeypatch):
    """MARKBOT_LANGUAGE env var overrides config."""
    monkeypatch.setenv("MARKBOT_LANGUAGE", "zh")
    assert i18n.get_language() == "zh"


def test_get_language_env_alias(monkeypatch):
    """Env var aliases are normalized."""
    monkeypatch.setenv("MARKBOT_LANGUAGE", "chinese")
    assert i18n.get_language() == "zh"


def test_get_language_default(monkeypatch):
    """Without env or config, returns the default language."""
    monkeypatch.delenv("MARKBOT_LANGUAGE", raising=False)
    # Force _config_language_cached to return None by making get_config raise.
    import markbot.config.loader as loader_mod
    monkeypatch.setattr(loader_mod, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("test")))
    i18n._config_language_cached.cache_clear()
    assert i18n.get_language() == i18n.DEFAULT_LANGUAGE
    i18n._config_language_cached.cache_clear()


# ---------------------------------------------------------------------------
# Translation / fallback
# ---------------------------------------------------------------------------

def test_t_english_baseline():
    """English keys translate correctly."""
    i18n.reset_language_cache()
    value = i18n.t("permission.confirmation_header", lang="en")
    assert "Permission" in value or "permission" in value.lower()


def test_t_chinese_translation():
    """Chinese keys produce a different (translated) string."""
    en_val = i18n.t("permission.confirmation_header", lang="en")
    zh_val = i18n.t("permission.confirmation_header", lang="zh")
    assert en_val != zh_val
    assert "权限" in zh_val


def test_t_format_kwargs():
    """Placeholders are substituted via str.format."""
    result = i18n.t("cmd.stop.stopped", lang="en", count=5)
    assert "5" in result
    assert "task" in result.lower()


def test_t_falls_back_to_english():
    """A missing key in zh falls back to English."""
    # Inject a key only in English by temporarily patching the catalog cache.
    i18n.reset_language_cache()
    i18n._catalog_cache["zh"] = {"existing.zh": "zh_value"}
    i18n._catalog_cache["en"] = {"test.fallback_only": "english_value"}
    result = i18n.t("test.fallback_only", lang="zh")
    assert result == "english_value"
    i18n.reset_language_cache()


def test_t_missing_key_returns_key():
    """A key missing in both target and English returns the key itself."""
    i18n.reset_language_cache()
    result = i18n.t("nonexistent.key.nowhere", lang="en")
    assert result == "nonexistent.key.nowhere"


def test_t_format_failure_returns_unformatted():
    """A bad placeholder kwarg logs a warning but returns the template."""
    i18n.reset_language_cache()
    # cmd.stop.stopped expects {count}; pass nothing to trigger format failure path
    # by providing a wrong kwarg name. The value is returned without substitution.
    result = i18n.t("cmd.stop.stopped", lang="en", wrong_kwarg=1)
    # Should contain the template (with or without the placeholder)
    assert "task" in result.lower() or "count" in result


# ---------------------------------------------------------------------------
# Catalog sanity: key structural invariants
# ---------------------------------------------------------------------------

def test_catalog_has_permission_and_cmd_sections():
    """Both the permission and cmd sections must exist in English."""
    en_flat = _flatten(_load_raw("en"))
    assert any(k.startswith("permission.") for k in en_flat)
    assert any(k.startswith("cmd.") for k in en_flat)


def test_catalog_values_are_strings():
    """Every leaf value (except _comment) must be a string."""
    for lang in i18n.SUPPORTED_LANGUAGES:
        flat = {k: v for k, v in _flatten(_load_raw(lang)).items() if not k.startswith("_")}
        for key, value in flat.items():
            assert isinstance(value, str), (
                f"{lang}.json key={key!r} is {type(value).__name__}, expected str"
            )


# ---------------------------------------------------------------------------
# Permission-approval parsing integration
# ---------------------------------------------------------------------------

from markbot.agent.permission_approval import _parse_choice  # noqa: E402


class TestParseChoiceLocalized:
    """_parse_choice must recognize localized option labels, not just English.

    When display.language is ``zh``, the approval prompt shows ``允许`` /
    ``全部允许`` / ``拒绝`` as option labels.  The AskUserQuestionTool
    returns the clicked label verbatim, so _parse_choice must route these
    correctly — otherwise every Chinese-UI approval silently falls through
    to the safe default ``deny``.
    """

    def setup_method(self):
        i18n.reset_language_cache()

    def teardown_method(self):
        i18n.reset_language_cache()

    @pytest.mark.parametrize("env_lang", ["zh", "chinese", "zh-CN", "zh-hans"])
    def test_zh_allow_label(self, monkeypatch, env_lang):
        monkeypatch.setenv("MARKBOT_LANGUAGE", env_lang)
        i18n.reset_language_cache()
        assert _parse_choice("User selected: 允许") == "allow"
        assert _parse_choice("允许") == "allow"

    @pytest.mark.parametrize("env_lang", ["zh", "chinese"])
    def test_zh_allow_all_label(self, monkeypatch, env_lang):
        monkeypatch.setenv("MARKBOT_LANGUAGE", env_lang)
        i18n.reset_language_cache()
        assert _parse_choice("User selected: 全部允许") == "allow_all"
        assert _parse_choice("全部允许") == "allow_all"

    @pytest.mark.parametrize("env_lang", ["zh", "chinese"])
    def test_zh_deny_label(self, monkeypatch, env_lang):
        monkeypatch.setenv("MARKBOT_LANGUAGE", env_lang)
        i18n.reset_language_cache()
        assert _parse_choice("User selected: 拒绝") == "deny"
        assert _parse_choice("拒绝") == "deny"

    def test_en_labels_still_work(self, monkeypatch):
        """English keywords are always accepted regardless of UI language."""
        monkeypatch.setenv("MARKBOT_LANGUAGE", "zh")
        i18n.reset_language_cache()
        assert _parse_choice("allow") == "allow"
        assert _parse_choice("deny") == "deny"
        assert _parse_choice("allow all") == "allow_all"

    def test_bare_numbers_work_regardless_of_language(self, monkeypatch):
        """Numeric shortcuts 1/2/3 work in any language."""
        monkeypatch.setenv("MARKBOT_LANGUAGE", "zh")
        i18n.reset_language_cache()
        assert _parse_choice("1") == "allow"
        assert _parse_choice("2") == "allow_all"
        assert _parse_choice("3") == "deny"

    def test_unknown_still_denies(self, monkeypatch):
        """Unrecognized input falls through to the safe default (deny)."""
        monkeypatch.setenv("MARKBOT_LANGUAGE", "zh")
        i18n.reset_language_cache()
        assert _parse_choice("某个不认识的词") == "deny"

