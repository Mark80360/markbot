"""Tests for Mixture-of-Agents (markbot/agent/moa.py).

Covers: MoA config parsing, advisor resolution (explicit + auto + cap),
parallel advisor flow with a working aggregator, partial advisor failure,
all-advisor failure, aggregator failure, and the /moa message helpers.
"""

from __future__ import annotations

import pytest

from markbot.agent.moa import _resolve_advisors, run_moa
from markbot.cli.slash_commands.builtin import _history_to_text, _last_user_message
from markbot.config.schema import (
    AgentDefaults,
    AgentsConfig,
    Config,
    ModelConfig,
    MoAConfig,
    ProviderConfig,
    ProvidersConfig,
)
from markbot.providers.base import LLMResponse


def _moa_config(
    *,
    moa: MoAConfig | None = None,
    model_chain: list[str] | None = None,
    providers: ProvidersConfig | None = None,
) -> Config:
    return Config(
        providers=providers or ProvidersConfig(),
        agents=AgentsConfig(
            defaults=AgentDefaults(
                model_chain=model_chain or ["deepseek/reasoner"],
                moa=moa or MoAConfig(),
            )
        ),
    )


def _providers_explicit() -> ProvidersConfig:
    return ProvidersConfig(
        deepseek=ProviderConfig(
            api_key="sk-ds",
            models=[
                ModelConfig(id="reasoner", name="deepseek-reasoner"),
                ModelConfig(id="deepseek-chat", name="deepseek-chat"),
            ],
        ),
        zhipu=ProviderConfig(
            api_key="sk-zp",
            models=[ModelConfig(id="glm-4-flash", name="glm-4-flash")],
        ),
    )


class _FakeProvider:
    """Provider stand-in dispatching on the requested model name."""

    def __init__(self, responses: dict) -> None:
        self._responses = responses
        self.calls: list[tuple[str, list]] = []

    async def chat_with_retry(self, messages=None, tools=None, model=None, max_tokens=None, temperature=None, **kwargs) -> LLMResponse:
        self.calls.append((model, messages))
        entry = self._responses[model]
        if callable(entry):
            return entry(messages)
        return entry


def _advisor_answer(model: str) -> LLMResponse:
    return LLMResponse(content=f"advice-from-{model}", finish_reason="stop")


def _aggregator_answer(messages: list) -> LLMResponse:
    if "aggregator" in messages[0]["content"]:
        return LLMResponse(content="SYNTHESIZED ANSWER", finish_reason="stop")
    return LLMResponse(content="wrong-path", finish_reason="stop")


def _error_response() -> LLMResponse:
    return LLMResponse(
        content="You exceeded your current quota", finish_reason="error"
    )


def _patch_fake(monkeypatch, responses: dict) -> _FakeProvider:
    import markbot.agent.moa as moa_mod

    fake = _FakeProvider(responses)
    monkeypatch.setattr(moa_mod, "_make_provider", lambda pc, spec: fake)
    return fake


def test_moa_config_schema_parses():
    cfg = _moa_config(
        moa=MoAConfig(
            advisors=["deepseek/deepseek-chat", "zhipu/glm-4-flash"],
            num_advisors=2,
            aggregator="deepseek/reasoner",
        )
    )
    moa = cfg.agents.defaults.moa
    assert moa.advisors == ["deepseek/deepseek-chat", "zhipu/glm-4-flash"]
    assert moa.num_advisors == 2
    assert moa.aggregator == "deepseek/reasoner"


def test_resolve_advisors_explicit_in_order():
    cfg = _moa_config(
        moa=MoAConfig(advisors=["deepseek/deepseek-chat", "zhipu/glm-4-flash"]),
        providers=_providers_explicit(),
    )
    refs = _resolve_advisors(cfg, cfg.agents.defaults.moa)
    assert [model for _, model, _ in refs] == ["deepseek-chat", "glm-4-flash"]


def test_resolve_advisors_ignores_unresolvable_refs():
    cfg = _moa_config(
        moa=MoAConfig(advisors=["deepseek/deepseek-chat", "nope/does-not-exist"]),
        providers=_providers_explicit(),
    )
    refs = _resolve_advisors(cfg, cfg.agents.defaults.moa)
    assert [model for _, model, _ in refs] == ["deepseek-chat"]


def test_resolve_advisors_auto_derives_default_aux_models():
    # deepseek + zhipu both declare default_aux_model and are configured
    cfg = _moa_config(providers=ProvidersConfig(
        deepseek=ProviderConfig(api_key="sk-ds"),
        zhipu=ProviderConfig(api_key="sk-zp"),
    ))
    refs = _resolve_advisors(cfg, cfg.agents.defaults.moa)
    assert [model for _, model, _ in refs] == ["deepseek-chat", "glm-4-flash"]


def test_resolve_advisors_auto_capped_by_num_advisors():
    cfg = _moa_config(
        moa=MoAConfig(num_advisors=1),
        providers=ProvidersConfig(
            deepseek=ProviderConfig(api_key="sk-ds"),
            zhipu=ProviderConfig(api_key="sk-zp"),
        ),
    )
    refs = _resolve_advisors(cfg, cfg.agents.defaults.moa)
    assert len(refs) == 1
    assert refs[0][1] == "deepseek-chat"


async def test_run_moa_happy_path_with_aggregator(monkeypatch):
    cfg = _moa_config(
        moa=MoAConfig(advisors=["deepseek/deepseek-chat", "zhipu/glm-4-flash"]),
        providers=_providers_explicit(),
    )
    fake = _patch_fake(
        monkeypatch,
        {
            "deepseek-chat": _advisor_answer("deepseek-chat"),
            "glm-4-flash": _advisor_answer("glm-4-flash"),
            "deepseek-reasoner": _aggregator_answer,
        },
    )

    result = await run_moa("What is MoA?", config=cfg)

    assert result.final_answer == "SYNTHESIZED ANSWER"
    assert result.advisors_ok == 2
    assert result.advisor_responses == [
        "advice-from-deepseek-chat",
        "advice-from-glm-4-flash",
    ]
    assert result.aggregator == "deepseek-reasoner"
    assert result.advisor_failures == []
    # both advisors ran in parallel, then the aggregator ran once
    assert [m for m, _ in fake.calls] == [
        "deepseek-chat", "glm-4-flash", "deepseek-reasoner",
    ]


async def test_run_moa_advisors_receive_history(monkeypatch):
    cfg = _moa_config(
        moa=MoAConfig(advisors=["deepseek/deepseek-chat"]),
        providers=_providers_explicit(),
    )
    fake = _patch_fake(
        monkeypatch,
        {
            "deepseek-chat": _advisor_answer("deepseek-chat"),
            "deepseek-reasoner": _aggregator_answer,
        },
    )

    await run_moa("follow-up question", history="user: prior context", config=cfg)

    advisor_messages = fake.calls[0][1]
    assert any("prior context" in m["content"] for m in advisor_messages)


async def test_run_moa_partial_advisor_failure(monkeypatch):
    cfg = _moa_config(
        moa=MoAConfig(advisors=["deepseek/deepseek-chat", "zhipu/glm-4-flash"]),
        providers=_providers_explicit(),
    )
    _patch_fake(
        monkeypatch,
        {
            "deepseek-chat": _error_response(),
            "glm-4-flash": _advisor_answer("glm-4-flash"),
            "deepseek-reasoner": _aggregator_answer,
        },
    )

    result = await run_moa("question", config=cfg)

    assert result.final_answer == "SYNTHESIZED ANSWER"
    assert result.advisors_ok == 1
    assert len(result.advisor_failures) == 1
    assert "deepseek-chat" in result.advisor_failures[0]


async def test_run_moa_all_advisors_fail(monkeypatch):
    cfg = _moa_config(
        moa=MoAConfig(advisors=["deepseek/deepseek-chat", "zhipu/glm-4-flash"]),
        providers=_providers_explicit(),
    )
    _patch_fake(
        monkeypatch,
        {
            "deepseek-chat": _error_response(),
            "glm-4-flash": _error_response(),
        },
    )

    result = await run_moa("question", config=cfg)

    assert result.final_answer is None
    assert result.advisors_ok == 0
    assert result.advisors_total == 2
    assert "all advisors failed" in result.advisor_failures


async def test_run_moa_aggregator_failure(monkeypatch):
    cfg = _moa_config(
        moa=MoAConfig(advisors=["deepseek/deepseek-chat"]),
        providers=_providers_explicit(),
    )

    def _raising_aggregator(messages):
        raise RuntimeError("aggregator exploded")

    _patch_fake(
        monkeypatch,
        {
            "deepseek-chat": _advisor_answer("deepseek-chat"),
            "deepseek-reasoner": _raising_aggregator,
        },
    )

    result = await run_moa("question", config=cfg)

    assert result.final_answer is None
    assert result.advisors_ok == 1
    assert any("aggregator" in f for f in result.advisor_failures)


async def test_run_moa_no_advisors():
    cfg = _moa_config()
    result = await run_moa("question", config=cfg)

    assert result.final_answer is None
    assert result.advisors_total == 0
    assert result.advisor_failures == ["no advisors available"]


def test_history_helpers():
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "tool", "content": "ignored"},
        {"role": "user", "content": [{"type": "text", "text": "with image"}]},
        {"role": "user", "content": ""},
    ]
    assert _last_user_message(history) == "with image"
    assert _history_to_text(history) == (
        "user: hello\nassistant: hi there\nuser: with image"
    )