"""Model information helpers for the onboard wizard.

.. deprecated:: 0.x
    Model database / autocomplete is temporarily disabled while litellm is
    being replaced with a new provider system.  All public function signatures
    are preserved so callers continue to work without changes.

    These functions will be re-implemented once the new model registry is in place.
"""

from __future__ import annotations

import warnings
from typing import Any


def get_all_models() -> list[str]:
    """Get list of all available models.

    .. deprecated::
        This function is disabled during migration to the new provider system.
        Returns an empty list until re-implemented.
    """
    warnings.warn(
        "get_all_models() is temporarily disabled during provider system migration",
        DeprecationWarning,
        stacklevel=2,
    )
    return []


def find_model_info(model_name: str) -> dict[str, Any] | None:
    """Find detailed information about a specific model.

    Args:
        model_name: Name or ID of the model to look up.

    Returns:
        Model information dict or None if not found.

    .. deprecated::
        This function is disabled during migration to the new provider system.
        Returns None until re-implemented.
    """
    warnings.warn(
        "find_model_info() is temporarily disabled during provider system migration",
        DeprecationWarning,
        stacklevel=2,
    )
    return None


def get_model_context_limit(model: str, provider: str = "auto") -> int | None:
    """Get context window size (in tokens) for a model.

    Args:
        model: Model name or ID.
        provider: Provider name (e.g., 'openai', 'anthropic'). Defaults to 'auto'.

    Returns:
        Context window size in tokens, or None if unknown.

    .. deprecated::
        This function is disabled during migration to the new provider system.
        Returns None until re-implemented.
    """
    warnings.warn(
        "get_model_context_limit() is temporarily disabled during provider system migration",
        DeprecationWarning,
        stacklevel=2,
    )
    return None


def get_model_suggestions(partial: str, provider: str = "auto", limit: int = 20) -> list[str]:
    """Get autocomplete suggestions for model names.

    Args:
        partial: Partial model name to complete.
        provider: Provider name to filter by. Defaults to 'auto'.
        limit: Maximum number of suggestions to return.

    Returns:
        List of matching model name suggestions.

    .. deprecated::
        This function is disabled during migration to the new provider system.
        Returns empty list until re-implemented.
    """
    warnings.warn(
        "get_model_suggestions() is temporarily disabled during provider system migration",
        DeprecationWarning,
        stacklevel=2,
    )
    return []


def format_token_count(tokens: int) -> str:
    """Format token count for display (e.g., 200000 -> '200,000').

    Args:
        tokens: Number of tokens to format.

    Returns:
        Formatted string with thousands separator.
    """
    return f"{tokens:,}"
