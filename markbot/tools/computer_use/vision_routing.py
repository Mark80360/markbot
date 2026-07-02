"""Vision routing — decide whether multimodal tool results should be
downgraded to text-only for models that cannot process images.

When a tool (e.g. computer_use capture) returns a screenshot embedded in
a ``_multimodal`` envelope, the active LLM must be capable of receiving
image content inside a tool_result block.  If it is not, the screenshot
must be replaced by its ``text_summary`` fallback so the conversation can
continue without errors.

The routing decision is based on (highest priority first):
1. An explicit config override (``auxiliary.vision.force_text_only``).
2. A per-model ``capabilities`` declaration on ``ModelConfig`` —
   if the active model lists ``"image"`` we keep the image, otherwise
   we downgrade.  This is the recommended mechanism: it lives next to
   the rest of the model config in ``.markbot/config.json``.
3. A built-in model capability table that records which provider/model
   combinations accept images in tool results (legacy fallback).
4. A runtime probe of the model chain in the active agent session.
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
        if config:
            av = config.agents.defaults.auxiliary_vision
            if getattr(av, "force_text_only", False):
                return True
    except Exception:
        logger.debug("Failed to load config for vision routing override", exc_info=True)

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


def _config_declares_vision(provider: Optional[str], model: Optional[str]) -> Optional[bool]:
    """Look up the model in the loaded config and check its ``capabilities``.

    Returns True if ``"image"`` is declared, False if the model is found
    but ``"image"`` is absent, and None when no matching model is found
    (caller should fall through to the next strategy).
    """
    if not provider or not model:
        return None
    try:
        from markbot.config.loader import load_config
        from markbot.config.schema import ProvidersConfig
        config = load_config()
    except Exception:
        return None
    if config is None:
        return None
    providers = getattr(config, "providers", None)
    if not isinstance(providers, ProvidersConfig):
        return None
    provider_cfg = providers.get_provider(provider)
    if provider_cfg is None:
        return None
    # ``model`` may be the *id* (e.g. "minimax-m3") or the *name* (e.g. "MiniMax-M3").
    target = model.strip().lower()
    for m in provider_cfg.models:
        if m.id.strip().lower() == target or m.name.strip().lower() == target:
            return m.has_capability("image")
    return None


def should_route_to_text_only(
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> bool:
    """Return True if multimodal tool results should be downgraded to text.

    Decision order:
    1. Session-level override (set by the agent).
    2. Config / env override.
    3. Per-model ``capabilities`` declaration in config.json (preferred
       mechanism — colocated with the rest of the model config).
    4. Built-in provider + model pattern tables (legacy fallback).
    5. Default: False (allow images).
    """
    if _session_vision_override is not None:
        return _session_vision_override

    config_override = _check_config_override()
    if config_override is not None:
        return config_override

    # Per-model config declaration is the recommended source of truth.
    config_vision = _config_declares_vision(provider, model)
    if config_vision is not None:
        return not config_vision

    if provider:
        provider_vision = _provider_supports_vision(provider)
        if provider_vision is False:
            return True

    if model:
        model_vision = _model_name_supports_vision(model)
        if model_vision is False:
            return True

    return False


# ---------------------------------------------------------------------------
# Auxiliary vision model — pre-analyze screenshots for non-vision main models
# ---------------------------------------------------------------------------

def resolve_auxiliary_vision_model() -> tuple[str, str] | None:
    """Return ``(provider_id, model_name)`` for the auxiliary vision model.

    Reads ``agents.defaults.auxiliary_vision.provider`` / ``.model`` from
    config. Returns ``None`` when not configured (caller should fall back
    to the text_summary downgrade).
    """
    try:
        from markbot.config.loader import load_config
        config = load_config()
    except Exception:
        return None
    if config is None:
        return None
    av = config.agents.defaults.auxiliary_vision
    provider = (getattr(av, "provider", "") or "").strip()
    model = (getattr(av, "model", "") or "").strip()
    if not provider or not model:
        return None
    return (provider, model)


async def describe_image_via_auxiliary(
    image_b64: str,
    mime: str,
    original_summary: str,
) -> str | None:
    """Call the auxiliary vision model to describe a screenshot.

    Returns a text description suitable for feeding back to a non-vision
    main model, or ``None`` if the call failed (caller should fall back
    to *original_summary*).
    """
    ref = resolve_auxiliary_vision_model()
    if ref is None:
        return None
    provider_id, model_ref = ref

    try:
        from markbot.config.loader import load_config
        from markbot.providers.registry import create_provider, find_by_name
        config = load_config()
        provider_cfg = config.providers.get_provider(provider_id)
        if provider_cfg is None or not provider_cfg.api_key:
            logger.warning(
                f"Auxiliary vision provider {provider_id!r} not configured with "
                f"API key; falling back to text_summary"
            )
            return None

        # Resolve model_ref (may be either a model id or an API model name).
        # If it matches a configured model id, use that model's `name` (the
        # actual string the provider API expects); otherwise pass through.
        model_cfg = provider_cfg.get_model(model_ref) if model_ref else None
        model_name = model_cfg.name if model_cfg else model_ref
        spec = find_by_name(provider_id)
        backend = spec.backend if spec else "openai_compat"
        provider = create_provider(
            backend=backend,
            api_key=provider_cfg.api_key,
            api_base=provider_cfg.api_base,
            extra_headers=provider_cfg.extra_headers,
            spec=spec,
        )
    except Exception as exc:
        logger.warning(f"Failed to instantiate auxiliary vision provider: {exc}")
        return None

    user_prompt = (
        "You are a vision assistant. Describe this screenshot concisely for "
        "another AI agent that cannot see images. Focus on:\n"
        "1. Window/app title and active UI state\n"
        "2. All visible text, buttons, input fields, and their labels\n"
        "3. Layout structure and element positions (use a numbered list when "
        "the original summary references element indices)\n"
        "4. Any error messages, dialogs, or notable visual state\n"
        "Keep it factual and dense — the downstream agent will use your "
        "description to decide the next action.\n\n"
        f"Original tool summary (for context):\n{original_summary}"
    )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                },
            ],
        }
    ]

    try:
        response = await provider.chat_with_retry(
            messages=messages,
            tools=None,
            model=model_name,
            max_tokens=2048,
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning(f"Auxiliary vision call failed: {exc}")
        return None

    if response.finish_reason == "error" or not response.content:
        preview = (response.content or "")[:120]
        logger.warning(f"Auxiliary vision returned error: {preview}")
        return None

    return response.content
