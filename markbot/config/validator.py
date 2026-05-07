"""Configuration validation — cross-field checks and dry-run.

Extends the basic Pydantic schema validation with semantic rules that
span multiple fields (cross-field validation) and a dry-run mode that
simulates startup without actually connecting to any services.

Usage::

    from markbot.config.validator import validate_config, dry_run

    errors = validate_config(config)
    if errors:
        for e in errors:
            print(f"  [{e.severity}] {e.field}: {e.message}")

    ok = dry_run(config)  # simulates full startup, returns True if clean
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from markbot.config.schema import Config, ProviderConfig


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class ValidationIssue:
    field: str
    message: str
    severity: Severity = Severity.ERROR
    suggestion: str = ""


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add(self, fld: str, msg: str, *, severity: Severity = Severity.ERROR, suggestion: str = "") -> None:
        self.issues.append(ValidationIssue(field=fld, message=msg, severity=severity, suggestion=suggestion))

    def merge(self, other: "ValidationResult") -> None:
        self.issues.extend(other.issues)


def _check_provider_cross_fields(config: "Config") -> ValidationResult:
    result = ValidationResult()
    providers = config.providers

    provider_fields = [
        attr for attr in type(providers).model_fields
        if isinstance(getattr(providers, attr, None), type(providers.custom))
    ]

    for provider_name in provider_fields:
        pc: "ProviderConfig" = getattr(providers, provider_name)

        if not pc.models:
            continue

        if not pc.api_key and not pc.api_base:
            result.add(
                f"providers.{provider_name}",
                f"Provider '{provider_name}' has models configured but no api_key or api_base",
                severity=Severity.WARNING,
                suggestion=f"Set providers.{provider_name}.api_key or .api_base in config",
            )

        model_ids_seen: set[str] = set()
        for model in pc.models:
            if model.id in model_ids_seen:
                result.add(
                    f"providers.{provider_name}.models",
                    f"Duplicate model id '{model.id}' in provider '{provider_name}'",
                )
            model_ids_seen.add(model.id)

            if model.context_window < model.max_tokens:
                result.add(
                    f"providers.{provider_name}.models.{model.id}",
                    f"context_window ({model.context_window}) < max_tokens ({model.max_tokens})",
                    severity=Severity.WARNING,
                    suggestion="Increase context_window or decrease max_tokens",
                )

    return result


def _check_model_chain(config: "Config") -> ValidationResult:
    result = ValidationResult()
    chain = config.agents.defaults.model_chain

    if not chain:
        result.add(
            "agents.defaults.model_chain",
            "model_chain is empty — no LLM provider configured",
            severity=Severity.WARNING,
            suggestion="Add at least one 'providerId/modelId' entry to model_chain",
        )
        return result

    model_ref_pattern = re.compile(r"^[a-zA-Z0-9_]+/[a-zA-Z0-9_\-.]+$")
    for i, ref in enumerate(chain):
        if not model_ref_pattern.match(ref):
            result.add(
                f"agents.defaults.model_chain[{i}]",
                f"Invalid model reference format: '{ref}' (expected 'providerId/modelId')",
            )

    chain_errors = config.validate_model_chain()
    for err in chain_errors:
        for i, ref in enumerate(chain):
            if ref in err:
                result.add(f"agents.defaults.model_chain[{i}]", err, severity=Severity.ERROR)
                break

    seen: set[str] = set()
    for i, ref in enumerate(chain):
        if ref in seen:
            result.add(
                f"agents.defaults.model_chain[{i}]",
                f"Duplicate model reference: '{ref}'",
                severity=Severity.WARNING,
            )
        seen.add(ref)

    return result


def _check_budget_config(config: "Config") -> ValidationResult:
    result = ValidationResult()

    if config.budget.enabled and config.budget.max_budget_usd is not None:
        if config.budget.max_budget_usd <= 0:
            result.add(
                "budget.max_budget_usd",
                "max_budget_usd must be positive when budget is enabled",
            )
        if config.budget.warn_threshold_usd > config.budget.max_budget_usd:
            result.add(
                "budget.warn_threshold_usd",
                f"warn_threshold_usd ({config.budget.warn_threshold_usd}) > max_budget_usd ({config.budget.max_budget_usd})",
                severity=Severity.WARNING,
                suggestion="Set warn_threshold_usd below max_budget_usd",
            )

    return result


def _check_web_search_config(config: "Config") -> ValidationResult:
    result = ValidationResult()
    ws = config.tools.web.search

    key_required_providers = {"brave", "tavily", "jina"}
    if ws.provider in key_required_providers and not ws.api_key:
        result.add(
            "tools.web.search.api_key",
            f"Search provider '{ws.provider}' requires an api_key",
            severity=Severity.WARNING,
            suggestion="Set tools.web.search.api_key or switch to 'duckduckgo'",
        )

    if ws.provider == "searxng" and not ws.base_url:
        result.add(
            "tools.web.search.base_url",
            "SearXNG provider requires a base_url",
            severity=Severity.WARNING,
            suggestion="Set tools.web.search.base_url to your SearXNG instance",
        )

    return result


def _check_compaction_config(config: "Config") -> ValidationResult:
    result = ValidationResult()
    cc = config.compaction

    if cc.collapse_head_chars + cc.collapse_tail_chars > cc.collapse_tool_result_chars:
        result.add(
            "compaction",
            "collapse_head_chars + collapse_tail_chars > collapse_tool_result_chars",
            severity=Severity.WARNING,
            suggestion="Ensure head + tail chars fit within the tool result char limit",
        )

    if cc.threshold_ratio < 0.5:
        result.add(
            "compaction.threshold_ratio",
            f"threshold_ratio ({cc.threshold_ratio}) is very low — compaction will trigger early",
            severity=Severity.WARNING,
        )

    return result


def _check_mcp_config(config: "Config") -> ValidationResult:
    result = ValidationResult()

    for name, mcp in config.tools.mcp_servers.items():
        if not mcp.command and not mcp.url:
            result.add(
                f"tools.mcp_servers.{name}",
                f"MCP server '{name}' has neither command nor url configured",
            )
        if mcp.type == "stdio" and not mcp.command:
            result.add(
                f"tools.mcp_servers.{name}",
                f"MCP server '{name}' is type=stdio but has no command",
            )
        if mcp.type in ("sse", "streamableHttp") and not mcp.url:
            result.add(
                f"tools.mcp_servers.{name}",
                f"MCP server '{name}' is type={mcp.type} but has no url",
            )
        if mcp.tool_timeout < 5:
            result.add(
                f"tools.mcp_servers.{name}.tool_timeout",
                f"tool_timeout ({mcp.tool_timeout}s) is very low, may cause premature cancellation",
                severity=Severity.WARNING,
            )

    return result


def _check_memory_config(config: "Config") -> ValidationResult:
    result = ValidationResult()
    mc = config.tools.memory

    if mc.embedding_backend == "openai" and not mc.embedding_api_key:
        result.add(
            "tools.memory.embedding_api_key",
            "OpenAI embedding backend requires an api_key",
            severity=Severity.WARNING,
            suggestion="Set tools.memory.embedding_api_key or switch to 'ollama' backend",
        )

    return result


def _check_gateway_config(config: "Config") -> ValidationResult:
    result = ValidationResult()

    if not (1 <= config.gateway.port <= 65535):
        result.add("gateway.port", f"Invalid port: {config.gateway.port}")

    if config.gateway.heartbeat.interval_s < 60:
        result.add(
            "gateway.heartbeat.interval_s",
            f"Heartbeat interval ({config.gateway.heartbeat.interval_s}s) is very short",
            severity=Severity.WARNING,
            suggestion="Consider using at least 60 seconds",
        )

    return result


def validate_config(config: "Config") -> ValidationResult:
    """Run all cross-field validation checks on a Config object.

    Returns a ValidationResult containing errors and warnings.
    """
    result = ValidationResult()

    result.merge(_check_provider_cross_fields(config))
    result.merge(_check_model_chain(config))
    result.merge(_check_budget_config(config))
    result.merge(_check_web_search_config(config))
    result.merge(_check_compaction_config(config))
    result.merge(_check_mcp_config(config))
    result.merge(_check_memory_config(config))
    result.merge(_check_gateway_config(config))

    return result


async def dry_run(config: "Config") -> bool:
    """Simulate a full startup without actually connecting to services.

    Validates configuration and checks that required resources (API keys,
    network endpoints) are reachable.  Returns True if everything looks
    good, False otherwise.

    This is intended for CI/CD pipelines and ``markbot doctor`` to
    catch issues before a real deployment.
    """
    result = validate_config(config)

    for issue in result.issues:
        level = "ERROR" if issue.severity == Severity.ERROR else "WARN"
        msg = f"  [{level}] {issue.field}: {issue.message}"
        if issue.suggestion:
            msg += f" — {issue.suggestion}"
        logger.info(f"[DryRun] {msg}")

    if not result.is_valid:
        logger.error("[DryRun] Configuration has {} error(s), aborting dry-run", len(result.errors))
        return False

    logger.info("[DryRun] Configuration validation passed, checking connectivity...")

    connectivity_ok = True
    chain = config.agents.defaults.model_chain
    if chain:
        try:
            from markbot.providers.registry import find_by_name

            for ref in chain[:3]:
                parts = ref.split("/", 1)
                if len(parts) != 2:
                    continue
                provider_name, _ = parts
                spec = find_by_name(provider_name)
                if spec and spec.env_key:
                    import os
                    if not os.environ.get(spec.env_key):
                        logger.warning("[DryRun] Env var {} not set for provider '{}'", spec.env_key, provider_name)
                        connectivity_ok = False
        except Exception as e:
            logger.warning("[DryRun] Provider connectivity check failed: {}", e)
            connectivity_ok = False

    if config.tools.web.search.api_key:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                provider = config.tools.web.search.provider
                if provider == "brave":
                    await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": "test", "count": 1},
                        headers={"X-Subscription-Token": config.tools.web.search.api_key},
                    )
        except Exception:
            logger.warning("[DryRun] Web search connectivity check failed (non-fatal)")

    logger.info(
        "[DryRun] Result: config_valid={}, connectivity_ok={}",
        result.is_valid, connectivity_ok,
    )
    return result.is_valid and connectivity_ok
