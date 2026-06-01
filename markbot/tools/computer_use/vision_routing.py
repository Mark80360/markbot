"""Vision routing — decide whether multimodal tool results should be
downgraded to text-only for models that cannot process images.

When a tool (e.g. computer_use capture) returns a screenshot embedded in
a ``_multimodal`` envelope, the active LLM must be capable of receiving
image content inside a tool_result block.  If it is not, the screenshot
must be replaced by its ``text_summary`` fallback so the conversation can
continue without errors.

The routing decision is based on:
1. An explicit config override (``auxiliary.vision.force_text_only``).
2. A built-in model capability table that records which provider/model
   combinations accept images in tool results.
3. A runtime probe of the model chain in the active agent session.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_PROVIDER_VISION_SUPPORT: dict[str, bool] = {
    "anthropic": True,
    "openai": True,
    "google": True,
    "gemini": True,
    "openrouter": True,
    "deepseek": False,
    "ollama": True,
    "groq": False,
    "zhipu": True,
    "dashscope": True,
    "vllm": True,
    "mistral": True,
    "xai": True,
}

_KNOWN_NON_VISION_PATTERNS: list[str] = [
    "gpt-3.5",
    "deepseek-chat",
    "deepseek-reasoner",
    "llama-3",
    "qwen2.5-coder",
    "glm-4-flash",
    "minimax-01",
]

_KNOWN_VISION_PATTERNS: list[str] = [
    "claude-3",
    "claude-4",
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gemini",
    "qwen2-vl",
    "qwen2.5-vl",
    "glm-4v",
    "llava",
    "pixtral",
    "moondream",
]

_session_vision_override: Optional[bool] = None


def set_session_vision_override(value: Optional[bool]) -> None:
    """Set a per-session override for vision routing.

    Called by the agent when it knows the active model's capabilities.
    """
    global _session_vision_override
    _session_vision_override = value


def _check_config_override() -> Optional[bool]:
    env_val = os.environ.get("MARKBOT_VISION_FORCE_TEXT_ONLY", "").strip().lower()
    if env_val in ("1", "true", "yes"):
        return True
    if env_val in ("0", "false", "no"):
        return False

    try:
        from markbot.config.loader import load_config
        config = load_config()
        if config and hasattr(config, "auxiliary_vision"):
            av = config.auxiliary_vision
            if hasattr(av, "force_text_only") and av.force_text_only:
                return True
    except Exception:
        pass

    return None


def _provider_supports_vision(provider: str) -> Optional[bool]:
    return _PROVIDER_VISION_SUPPORT.get(provider)


def _model_name_supports_vision(model: str) -> Optional[bool]:
    model_lower = model.lower()
    for pattern in _KNOWN_NON_VISION_PATTERNS:
        if pattern in model_lower:
            return False
    for pattern in _KNOWN_VISION_PATTERNS:
        if pattern in model_lower:
            return True
    return None


def should_route_to_text_only(
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> bool:
    """Return True if multimodal tool results should be downgraded to text.

    Decision order:
    1. Session-level override (set by the agent).
    2. Config / env override.
    3. Provider + model capability lookup.
    4. Default: False (allow images).
    """
    if _session_vision_override is not None:
        return _session_vision_override

    config_override = _check_config_override()
    if config_override is not None:
        return config_override

    if provider:
        provider_vision = _provider_supports_vision(provider)
        if provider_vision is False:
            return True

    if model:
        model_vision = _model_name_supports_vision(model)
        if model_vision is False:
            return True

    return False
