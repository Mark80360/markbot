"""Lightweight internationalization (i18n) for markbot static user-facing messages.

Scope (thin slice, by design): only the highest-impact static strings shown
to the user by markbot itself -- permission approval prompts, slash-command
replies, and a handful of CLI/gateway notices.  Agent-generated output, log
lines, error tracebacks, tool outputs, and slash-command descriptions stay
in English and are NOT translated -- they follow the user's input language
naturally (LLM behavior).

Catalog files live alongside this module as ``<lang>.json`` (e.g.
``en.json``, ``zh.json``).  Each catalog is a flat dict keyed by dotted
paths (e.g. ``permission.confirmation_header`` or ``cmd.stop.stopped``).
Missing keys fall back to English; if English is missing too, the key path
itself is returned so a broken catalog never crashes the agent.

Usage::

    from markbot.locales import t
    print(t("permission.confirmation_header"))             # current lang
    print(t("cmd.stop.stopped", count=3))                  # {count} formatted
    print(t("cmd.stop.stopped", lang="zh"))                # explicit override

Language resolution order:
    1. Explicit ``lang=`` argument passed to :func:`t`
    2. ``MARKBOT_LANGUAGE`` environment variable (for tests / quick override)
    3. ``display.language`` from config.json
    4. ``"en"`` (baseline)

Supported languages: en, zh.  Unknown values fall back to en.
"""

from __future__ import annotations

import json
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh")
DEFAULT_LANGUAGE = "en"

# Accept a few natural aliases so users who type "chinese" / "zh-CN"
# get the right catalog instead of silently falling back to English.
_LANGUAGE_ALIASES: dict[str, str] = {
    "english": "en", "en-us": "en", "en-gb": "en",
    # Simplified Chinese — explicit codes route here; bare "chinese" /
    # "mandarin" also default to Simplified since that's the larger user base.
    "chinese": "zh", "mandarin": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh",
    # Traditional Chinese — route to zh for now (no separate catalog yet).
    # When a zh-hant catalog is added, update this mapping and SUPPORTED_LANGUAGES.
    "zh-tw": "zh", "zh-hk": "zh", "zh-mo": "zh", "zh-hant": "zh",
}

_catalog_cache: dict[str, dict[str, str]] = {}
_catalog_lock = threading.Lock()


def _locales_dir() -> Path:
    """Return the directory containing locale JSON catalog files.

    Catalogs ship inside the ``markbot.locales`` package (this directory)
    so they are bundled with pip installs without extra packaging config.
    """
    # markbot/locales/i18n.py -> markbot/locales/
    return Path(__file__).resolve().parent


def _normalize_lang(value: Any) -> str:
    """Normalize a user-supplied language value to a supported code.

    Accepts supported codes directly, common aliases (``chinese`` -> ``zh``),
    and case-insensitive regional tags (``zh-CN`` -> ``zh``).  Returns the
    default language for unknown values.
    """
    if not isinstance(value, str):
        return DEFAULT_LANGUAGE
    key = value.strip().lower()
    if not key:
        return DEFAULT_LANGUAGE
    if key in SUPPORTED_LANGUAGES:
        return key
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    # Try stripping a region suffix (e.g. "zh-CN" -> "zh").
    base = key.split("-", 1)[0]
    if base in SUPPORTED_LANGUAGES:
        return base
    return DEFAULT_LANGUAGE


def _load_catalog(lang: str) -> dict[str, str]:
    """Load and flatten one locale JSON file into a dotted-key dict.

    JSON files can be nested for human readability; this produces the flat
    key space :func:`t` expects.  Cached per-language for the process.
    """
    with _catalog_lock:
        cached = _catalog_cache.get(lang)
        if cached is not None:
            return cached

    path = _locales_dir() / f"{lang}.json"
    if not path.is_file():
        logger.debug("i18n catalog missing for {} at {}", lang, path)
        with _catalog_lock:
            _catalog_cache[lang] = {}
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load i18n catalog {}: {}", path, exc)
        with _catalog_lock:
            _catalog_cache[lang] = {}
        return {}

    if not isinstance(raw, dict):
        logger.warning("i18n catalog {} is not a JSON object", path)
        with _catalog_lock:
            _catalog_cache[lang] = {}
        return {}

    flat: dict[str, str] = {}
    _flatten_into(raw, "", flat)
    with _catalog_lock:
        _catalog_cache[lang] = flat
    return flat


def _flatten_into(node: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            _flatten_into(value, child_key, out)
    elif isinstance(node, str):
        out[prefix] = node
    # Non-string, non-dict leaves are ignored -- catalogs are text-only.


@lru_cache(maxsize=1)
def _config_language_cached() -> str | None:
    """Read ``display.language`` from config.json once per process.

    Cached because ``t()`` is called in hot paths (every approval prompt,
    every slash-command reply) and re-reading config each call would be
    wasteful.  ``reset_language_cache()`` clears this when config changes
    at runtime (e.g. after a ``/mode``-style persist).
    """
    try:
        from markbot.config.loader import get_config

        cfg = get_config()
        display = getattr(cfg, "display", None)
        lang = getattr(display, "language", None) if display else None
        if lang:
            return _normalize_lang(lang)
    except Exception as exc:
        logger.debug("Could not read display.language from config: {}", exc)
    return None


def reset_language_cache() -> None:
    """Invalidate cached language resolution and catalogs.

    Call after config changes if a running process needs to pick up a
    changed ``display.language`` without restart.
    """
    _config_language_cached.cache_clear()
    with _catalog_lock:
        _catalog_cache.clear()


def get_language() -> str:
    """Resolve the active language using env > config > default order."""
    env_lang = os.environ.get("MARKBOT_LANGUAGE")
    if env_lang:
        return _normalize_lang(env_lang)
    cfg_lang = _config_language_cached()
    if cfg_lang:
        return cfg_lang
    return DEFAULT_LANGUAGE


def t(key: str, lang: str | None = None, **format_kwargs: Any) -> str:
    """Translate a dotted key to the active language.

    Parameters
    ----------
    key
        Dotted path into the catalog, e.g. ``"permission.confirmation_header"``.
    lang
        Explicit language override.  Takes precedence over env + config.
    **format_kwargs
        ``str.format`` substitution arguments (``t("cmd.stop.stopped", count=3)``
        expects a catalog entry with a ``{count}`` placeholder).

    Returns
    -------
    The translated string, or the English fallback if the key is missing in
    the target language, or the bare key if English is also missing.
    """
    target = _normalize_lang(lang) if lang else get_language()
    catalog = _load_catalog(target)
    value = catalog.get(key)

    if value is None and target != DEFAULT_LANGUAGE:
        # Fall through to English rather than showing a key path to the user.
        value = _load_catalog(DEFAULT_LANGUAGE).get(key)

    if value is None:
        # Last-ditch: return the key itself.  A broken catalog should not
        # crash anything; it just looks ugly until someone fixes it.
        logger.debug("i18n miss: key={} lang={}", key, target)
        value = key

    if format_kwargs:
        try:
            return value.format(**format_kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "i18n format failed for key={} lang={} kwargs={}: {}",
                key, target, format_kwargs, exc,
            )
            return value
    return value


__all__ = [
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "t",
    "get_language",
    "reset_language_cache",
]
