"""MarkBot internationalization (i18n) package.

Re-exports the public API from :mod:`markbot.locales.i18n` so callers can
import directly from the package::

    from markbot.locales import t, get_language, reset_language_cache
"""

from markbot.locales.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_language,
    reset_language_cache,
    t,
)

__all__ = [
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "t",
    "get_language",
    "reset_language_cache",
]
