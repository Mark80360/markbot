"""Mixture-of-Agents (MoA): parallel advisors + aggregator synthesis.

Advisors (cheap text models) answer the same question independently and in
parallel; an aggregator model then synthesizes the best final answer from
their responses, combining insights and resolving contradictions.

Advisor resolution order:
  1. Explicit ``agents.defaults.moa.advisors`` model references.
  2. Auto: configured providers' ``default_aux_model`` (registry order),
     capped by ``agents.defaults.moa.num_advisors``.
  3. The main model (``model_chain[0]``) as a last-resort advisor.

Aggregator:
  ``agents.defaults.moa.aggregator``, or the main model when unset.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

ADVISOR_SYSTEM = (
    "You are one of several independent AI advisors. "
    "Answer the user's question directly, accurately and concisely. "
    "Do not mention that you are an advisor."
)

AGGREGATOR_SYSTEM = (
    "You are an expert aggregator. Below are several independent answers "
    "to the same question, each from a different AI advisor. Synthesize "
    "the best possible final answer: keep the strongest points, combine "
    "complementary insights, resolve contradictions, and correct errors. "
    "Address the user directly and do not mention the advisors unless they "
    "truly differ on an important point."
)


@dataclass
class MoAResult:
    """Outcome of one MoA run."""

    question: str
    advisors_total: int = 0
    advisor_responses: list[str] = field(default_factory=list)
    advisor_failures: list[str] = field(default_factory=list)
    aggregator: str = ""
    final_answer: str | None = None

    @property
    def advisors_ok(self) -> int:
        return len(self.advisor_responses)


def _get_config() -> Any:
    try:
        from markbot.config.loader import load_config

        return load_config()
    except Exception:
        return None


def _make_provider(provider_cfg: Any, spec: Any) -> Any:
    """Instantiate a provider for an advisor/aggregator call."""
    from markbot.providers.registry import create_provider

    backend = spec.backend if spec else "openai_compat"
    keys = provider_cfg.credential_keys
    return create_provider(
        backend=backend,
        api_key=keys[0] if keys else None,
        api_base=provider_cfg.api_base,
        extra_headers=provider_cfg.extra_headers,
        spec=spec,
    )


def _resolve_advisors(config: Any, moa_cfg: Any) -> list[tuple[Any, str, Any]]:
    """Ordered ``(provider_config, model_name, spec)`` list for advisors."""
    from markbot.providers.registry import find_by_name, list_specs

    refs: list[tuple[Any, str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for raw in moa_cfg.advisors or []:
        try:
            provider_cfg, model_cfg = config.resolve_model(raw)
        except Exception:
            logger.warning("MoA: skipping unresolvable advisor ref {}", raw)
            continue
        provider_id = raw.split("/")[0]
        key = (provider_id, model_cfg.name)
        if key in seen:
            continue
        seen.add(key)
        refs.append((provider_cfg, model_cfg.name, find_by_name(provider_id)))

    if not refs:
        for spec in list_specs():
            if not spec.default_aux_model:
                continue
            provider_cfg = config.providers.get_provider(spec.name)
            if provider_cfg is None or not provider_cfg.credential_keys:
                continue
            key = (spec.name, spec.default_aux_model)
            if key in seen:
                continue
            seen.add(key)
            refs.append((provider_cfg, spec.default_aux_model, spec))

    # Cap auto-derived advisors; explicit lists are honored as-is.
    if moa_cfg.advisors:
        return refs
    return refs[: moa_cfg.num_advisors]


def _resolve_aggregator(config: Any, moa_cfg: Any) -> tuple[Any, str, Any] | None:
    """Resolve (provider_config, model_name, spec) for the aggregator."""
    from markbot.providers.registry import find_by_name

    ref = moa_cfg.aggregator or ""
    try:
        if ref:
            provider_cfg, model_cfg = config.resolve_model(ref)
            provider_id = ref.split("/")[0]
        else:
            provider_cfg, model_cfg = config.resolve_model(config.agents.defaults.model_chain[0])
            provider_id = config.agents.defaults.model_chain[0].split("/")[0]
    except Exception as exc:
        logger.warning("MoA: cannot resolve aggregator {}: {}", ref or "(main)", exc)
        return None
    return provider_cfg, model_cfg.name, find_by_name(provider_id)


async def _ask_advisor(
    provider_cfg: Any,
    spec: Any,
    model: str,
    question: str,
    history: str,
) -> tuple[str | None, str | None]:
    """One parallel advisor call. Returns (content, error)."""
    provider = _make_provider(provider_cfg, spec)
    messages: list[dict[str, str]] = [{"role": "system", "content": ADVISOR_SYSTEM}]
    if history:
        messages.append({"role": "user", "content": history})
    messages.append({"role": "user", "content": question})
    try:
        response = await provider.chat_with_retry(
            messages=messages, tools=None, model=model,
            max_tokens=2048, temperature=0.7,
        )
    except Exception as exc:
        logger.warning("MoA advisor {} raised: {}", model, exc)
        return None, str(exc)[:300]
    if response.finish_reason == "error":
        logger.warning("MoA advisor {} error: {}", model, response.content or "unknown")
        return None, (response.content or "unknown error")[:300]
    if not response.content:
        return None, "empty response"
    return response.content, None


async def _aggregate(
    provider_cfg: Any,
    spec: Any,
    model: str,
    question: str,
    advisor_responses: list[str],
) -> str:
    """Synthesize the final answer from advisor responses."""
    provider = _make_provider(provider_cfg, spec)
    blocks = [f"Question: {question}", ""]
    for index, content in enumerate(advisor_responses, start=1):
        blocks.append(f"--- Advisor {index} ---\n{content}")
    messages = [
        {"role": "system", "content": AGGREGATOR_SYSTEM},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]
    response = await provider.chat_with_retry(
        messages=messages, tools=None, model=model,
        max_tokens=4096, temperature=0.7,
    )
    if response.finish_reason == "error":
        raise RuntimeError(response.content or "aggregator error")
    if not response.content:
        raise RuntimeError("empty aggregator response")
    return response.content


async def run_moa(question: str, history: str = "", config: Any = None) -> MoAResult:
    """Run Mixture-of-Agents over *question* and return the synthesis.

    ``history`` is optional prior-conversation text passed to the advisors
    as context. Advisors run in parallel; a single advisor failure does not
    abort the run, but an aggregator failure leaves ``final_answer`` None.
    """
    result = MoAResult(question=question)
    config = config or _get_config()
    if config is None:
        result.advisor_failures.append("no configuration available")
        return result

    moa_cfg = getattr(config.agents.defaults, "moa", None)
    if moa_cfg is None:
        result.advisor_failures.append("moa not configured")
        return result

    advisors = _resolve_advisors(config, moa_cfg)
    result.advisors_total = len(advisors)
    if not advisors:
        result.advisor_failures.append("no advisors available")
        return result

    outcomes = await asyncio.gather(
        *[
            _ask_advisor(pc, spec, model, question, history)
            for pc, model, spec in advisors
        ],
        return_exceptions=True,
    )
    for (pc, model, spec), outcome in zip(advisors, outcomes):
        if isinstance(outcome, BaseException):
            result.advisor_failures.append(f"{model}: {outcome}")
            continue
        content, error = outcome
        if content:
            result.advisor_responses.append(content)
        elif error:
            result.advisor_failures.append(f"{model}: {error}")

    if not result.advisor_responses:
        result.advisor_failures.append("all advisors failed")
        return result

    aggregator = _resolve_aggregator(config, moa_cfg)
    if aggregator is None:
        result.advisor_failures.append("no aggregator available")
        return result
    provider_cfg, model, spec = aggregator
    result.aggregator = model
    try:
        result.final_answer = await _aggregate(
            provider_cfg, spec, model, question, result.advisor_responses,
        )
    except Exception as exc:
        result.advisor_failures.append(f"aggregator: {exc}")
        logger.warning("MoA aggregator failed: {}", exc)
    return result


__all__ = [
    "AGGREGATOR_SYSTEM",
    "ADVISOR_SYSTEM",
    "MoAResult",
    "run_moa",
]