"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("�?))
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)


class AgentDefaults(Base):
    """Default agent configuration (V2)."""

    workspace: str = "~/.markbot/workspace"
    model_chain: list[str] = Field(
        default_factory=list,
        description="Ordered list of provider/model references for fallback"
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    max_tool_iterations: int = 40
    reasoning_effort: str | None = None  # low / medium / high - enables LLM thinking mode
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    profile: Literal["coding", "assistant", "unattended"] = Field(
        "coding",
        description=(
            "Runtime profile controlling default tool surface, permission mode, "
            "and skill quality filters. coding=dev workflows; assistant=interactive "
            "personal assistant with confirmation; unattended=cron/heartbeat/autopilot."
        ),
    )
    default_permission_mode: Literal["default", "plan", "accept_edits", "auto", "bypass_permissions"] | None = Field(
        None,
        description=(
            "Permission mode applied to AppStateProvider at gateway startup. "
            "None inherits the selected profile default (coding/unattended→auto, "
            "assistant→default). Interactive ``/mode`` still overrides at runtime. "
            "Cron/autopilot/heartbeat force AUTO via process_direct."
        ),
    )
    auxiliary_vision: "AuxiliaryVisionConfig" = Field(
        default_factory=lambda: AuxiliaryVisionConfig(),
        description="Auxiliary vision model for non-vision primary models",
    )

    @field_validator("timezone", mode="before")
    @classmethod
    def _normalize_timezone(cls, v: str | None) -> str:
        from markbot.utils.helpers import normalize_timezone
        return normalize_timezone(v)


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ModelConfig(Base):
    """Single model configuration within a provider."""

    id: str = Field(..., description="Unique model identifier within provider")
    name: str = Field(..., description="Actual model name passed to API")
    max_tokens: int = Field(8192, ge=1, description="Max output tokens")
    context_window: int = Field(65536, ge=1024, description="Context window size")
    temperature: float | None = Field(None, ge=0.0, le=2.0, description="Override default temperature")
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(None, description="Reasoning effort level")
    capabilities: list[str] = Field(
        default_factory=lambda: ["text"],
        description=(
            "Declared model capabilities. Drives capability-aware routing in the fallback chain "
            "(e.g. per-model image stripping) and selection in feature-specific tool calls. "
            "Allowed values: text, image (vision input / 识图), image_edit, image_generate, "
            "video, audio, music, embedding, tool_use."
        ),
    )

    @field_validator("capabilities", mode="before")
    @classmethod
    def _normalize_capabilities(cls, v: Any) -> list[str]:
        """Accept a comma-separated string, single string, or list; lowercase + dedupe + validate enum.

        String forms are split on commas so users can write
        ``capabilities: "text, image"`` in YAML / ``.markbot/config.json``
        without converting to a list first.
        """
        allowed = {
            "text", "image", "image_edit", "image_generate",
            "video", "audio", "music", "embedding", "tool_use",
        }
        if v is None or v == "":
            return ["text"]
        if isinstance(v, str):
            items = [chunk for chunk in v.split(",") if chunk.strip()]
        elif isinstance(v, list):
            items = [str(x) for x in v]
        else:
            raise ValueError(f"capabilities must be a string or list of strings, got {type(v).__name__}")
        out: list[str] = []
        for raw in items:
            tag = raw.strip().lower()
            if not tag:
                continue
            if tag not in allowed:
                raise ValueError(
                    f"Unknown capability {raw!r}; allowed: {sorted(allowed)}"
                )
            if tag not in out:
                out.append(tag)
        return out or ["text"]

    def has_capability(self, cap: str) -> bool:
        """Return True if *cap* (case-insensitive) is declared."""
        want = cap.strip().lower()
        return any(c.lower() == want for c in self.capabilities)


class ProviderConfig(Base):
    """LLM provider configuration (V2)."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None
    models: list[ModelConfig] = Field(
        default_factory=list,
        description="List of models available under this provider"
    )

    def get_model(self, model_id: str) -> ModelConfig | None:
        """Get model config by ID."""
        return next((m for m in self.models if m.id == model_id), None)

    @property
    def is_configured(self) -> bool:
        """Check if provider has at least one model configured."""
        return bool(self.api_key and self.models)


class ProvidersConfig(Base):
    """Configuration for LLM providers.

    Supports both named fields (for YAML/config file compatibility) and
    dynamic dict-based provider registration.

    Named providers (e.g. providers.anthropic) work as before.
    Dynamic providers can be added at runtime via ``set_provider()``.
    """

    model_config = ConfigDict(extra="allow")

    custom: ProviderConfig = Field(default_factory=ProviderConfig)
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)
    xai: ProviderConfig = Field(default_factory=ProviderConfig)
    nvidia: ProviderConfig = Field(default_factory=ProviderConfig)
    huggingface: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)

    def model_post_init(self, __context) -> None:
        self._dynamic_providers: dict[str, ProviderConfig] = {}

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        """Get a provider by ID, checking both named fields and dynamic providers."""
        if hasattr(self, provider_id):
            val = getattr(self, provider_id)
            if isinstance(val, ProviderConfig):
                return val
        return self._dynamic_providers.get(provider_id)

    def set_provider(self, provider_id: str, config: ProviderConfig) -> None:
        """Register or update a provider dynamically.

        For built-in provider names, updates the named field.
        For custom names, stores in the dynamic dict.
        """
        if hasattr(self, provider_id) and isinstance(getattr(self, provider_id, None), ProviderConfig):
            setattr(self, provider_id, config)
        else:
            self._dynamic_providers[provider_id] = config

    def list_provider_ids(self) -> list[str]:
        """List all available provider IDs (named + dynamic)."""
        ids = []
        for field_name in type(self).model_fields:
            val = getattr(self, field_name, None)
            if isinstance(val, ProviderConfig) and val.is_configured:
                ids.append(field_name)
        for pid in self._dynamic_providers:
            if pid not in ids:
                ids.append(pid)
        return ids


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "127.0.0.1"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True
    timeout: int = 60
    restrict_to_workspace: bool = Field(
        default=True,
        description="If true, block shell commands that reference paths outside the working directory",
    )
    require_allowlist: bool = Field(
        default=False,
        description=(
            "If true, only commands matching allow_patterns may run. "
            "When false and allow_patterns is empty, deny_patterns alone apply."
        ),
    )
    allow_patterns: list[str] = Field(
        default_factory=list,
        description="Optional allowlist regexes. Used when non-empty or require_allowlist=True.",
    )
    deny_patterns: list[str] = Field(
        default_factory=list,
        description="Extra deny regexes merged with built-in dangerous command patterns.",
    )
    path_append: str = ""
    allowed_internal_ips: list[str] = Field(
        default_factory=list,
        description="List of internal IPs to skip SSRF check in shell commands",
    )


class FilesystemToolConfig(Base):
    """File system tools configuration."""

    backup_dir: str = "~/.markbot/.markbot_backups"  # Backup directory for file operations
    max_backups: int = Field(default=50, ge=10, le=500)  # Maximum number of backups to retain
    safe_delete: bool = Field(
        default=True,
        description="If true, deleted files are moved to backup_dir (recycle bin mode). If false, files are permanently deleted."
    )

class CodeExecutionConfig(Base):
    """Code execution tool (run_code) configuration."""

    enable: bool = True
    timeout: int = Field(
        default=60,
        ge=5,
        le=300,
        description="Default execution timeout in seconds",
    )
    max_memory_mb: int = Field(
        default=256,
        ge=64,
        le=4096,
        description="Maximum memory (MB) for sandbox execution",
    )
    allowed_dependencies: list[str] = Field(
        default_factory=list,
        description="If non-empty, only these pip packages are allowed to be installed. Empty = allow all.",
    )

class MemoryToolsConfig(Base):
    """Memory system configuration (file-based)."""

    embedding_backend: str = Field(
        default="openai",
        description="Embedding backend: openai or ollama"
    )
    embedding_api_key: str = Field(
        default="",
        description="API key for embedding model (leave empty for local models)"
    )
    embedding_base_url: str = Field(
        default="",
        description="Base URL for embedding API (leave empty for default)"
    )
    embedding_model_name: str = Field(
        default="",
        description="Embedding model name (leave empty for default)"
    )
    memory_compact_threshold: int = Field(
        default=0,
        ge=0,
        description="Token threshold to trigger compaction (0 = auto: 75% of context window)"
    )
    memory_compact_reserve: int = Field(
        default=10_000,
        ge=1_000,
        description="Tokens to reserve after compaction for new messages"
    )
    memory_summary_enabled: bool = Field(
        default=True,
        description="Enable async memory summarization to MEMORY.md"
    )
    context_compact_enabled: bool = Field(
        default=True,
        description="Enable automatic context compaction when threshold exceeded"
    )
    dream_cron: str = Field(
        default="0 23 * * *",
        description="Cron expression for dream-based memory optimization job (empty string to disable)"
    )
    # External memory plugin configuration
    provider: str | None = Field(
        default=None,
        description="External memory provider name (e.g. 'chroma'). None = use built-in file-based memory."
    )
    provider_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Configuration dict passed to the external memory provider"
    )
    # -- Long-term (vector) memory -----------------------------------------
    long_term_enabled: bool = Field(
        default=True,
        description="Enable semantic vector recall in memory_search (hybrid keyword + vector). "
                    "When True, turns/notes/delegations are embedded and recalled by meaning."
    )
    vector_backend: str = Field(
        default="sqlite",
        description="Vector store backend: 'sqlite' (default, zero extra deps) or 'chroma' (needs markbot[chroma])."
    )
    vector_max_records: int = Field(
        default=50_000,
        ge=100,
        description="Hard cap on stored vectors; oldest low-importance records are evicted (LRU) past this."
    )
    vector_max_scan_records: int = Field(
        default=20_000,
        ge=100,
        description="Maximum vectors loaded into RAM for one SQLite vector query. Use Chroma for larger corpora."
    )
    vector_min_content_chars: int = Field(
        default=12,
        description="Minimum content length (chars) to index as a vector; shorter text is skipped."
    )
    vector_top_k_multiplier: int = Field(
        default=2,
        ge=1,
        description="Over-fetch factor for vector recall before fusion with keyword results."
    )
    vector_min_score: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity for a vector result to be included in fused output."
    )
    # -- Consolidation / forgetting ----------------------------------------
    consolidation_enabled: bool = Field(
        default=True,
        description="Enable periodic dedup + importance-decay of the vector index (runs during dream)."
    )
    consolidation_dedup_threshold: float = Field(
        default=0.95,
        ge=0.5,
        le=1.0,
        description="Cosine similarity above which two vectors are treated as duplicates during consolidation."
    )
    consolidation_age_decay_days: float = Field(
        default=90.0,
        ge=1.0,
        description="Half-life for age-based importance decay; older rarely-recalled records decay faster."
    )
    consolidation_promote_access: int = Field(
        default=8,
        ge=1,
        description=(
            "access_count above which a high-signal record may be proposed for "
            "promotion to MEMORY.md. Turn transcripts are never auto-promoted."
        ),
    )
    auto_summary_to_curated: bool = Field(
        default=False,
        description=(
            "If true, automatic conversation summaries may write into curated "
            "MEMORY.md. Default false: summaries go to daily logs + vector index only."
        ),
    )
    # -- Force-search knobs (previously orphaned getattr defaults) ---------
    force_memory_search: bool = Field(
        default=False,
        description="Automatically inject memory search results before each LLM call."
    )
    force_max_results: int = Field(
        default=1,
        ge=1,
        description="Max results for forced memory search injection."
    )
    force_min_score: float = Field(
        default=0.3,
        ge=0.0,
        description="Minimum score for forced memory search injection."
    )
    daily_log_retention_days: int = Field(
        default=30,
        ge=0,
        description="Days to retain raw memory/daily interaction logs; 0 disables pruning."
    )
    # -- Compaction ratios (previously orphaned getattr defaults) ---------
    memory_compact_ratio: float = Field(
        default=0.75,
        ge=0.1,
        le=0.99,
        description="Fraction of context window at which compaction triggers."
    )
    memory_reserve_ratio: float = Field(
        default=0.1,
        ge=0.0,
        le=0.9,
        description="Fraction of messages to reserve (not compact) during context compression."
    )


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools
    enabled: bool = True  # If False, server is skipped during connection

class ComputerUseConfig(Base):
    """Computer use (desktop control) tool configuration."""

    enable: bool = Field(
        default=True,
        description="Enable the computer_use tool for desktop control",
    )
    backend: str = Field(
        default="cua",
        description="Backend to use: 'cua' (cua-driver via MCP) or 'noop' (testing stub)",
    )
    capture_after_actions: bool = Field(
        default=True,
        description="Automatically capture a screenshot after each action",
    )
    max_elements: int = Field(
        default=200,
        ge=10,
        le=1000,
        description="Max AX elements returned in som/vision capture mode",
    )
    blocked_key_combos: list[str] = Field(
        default_factory=lambda: [
            "cmd+shift+backspace",
            "cmd+option+escape",
        ],
        description="Key combinations that are always blocked for safety",
    )
    blocked_type_patterns: list[str] = Field(
        default_factory=lambda: [
            "sudo rm -rf /",
            "sudo rm -rf ~",
            "rm -rf /",
            "rm -rf ~",
            ":(){ :|:& };:",
        ],
        description="Type patterns that are always blocked for safety",
    )


class BrowserConfig(Base):
    """Browser automation tool configuration."""

    enable: bool = Field(
        default=True,
        description="Enable browser automation tools",
    )
    backend: str = Field(
        default="playwright",
        description="Browser backend: 'playwright' (local) or 'browserbase' (cloud)",
    )
    headless: bool = Field(
        default=True,
        description="Run browser in headless mode",
    )
    record_session: bool = Field(
        default=False,
        description="Record browser sessions as .webm files",
    )
    default_timeout: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Default navigation/action timeout in seconds",
    )
    snapshot_max_chars: int = Field(
        default=8000,
        ge=1000,
        le=50000,
        description="Max characters for accessibility snapshot before LLM summarization",
    )
    blocked_domains: list[str] = Field(
        default_factory=list,
        description="Domains blocked from browser access (glob patterns, e.g. '*.internal.com')",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="If non-empty, only these domains are allowed (glob patterns); takes precedence over blocked_domains",
    )


class AuxiliaryVisionConfig(Base):
    """Auxiliary vision model configuration for pre-analyzing screenshots.

    When the main model in the model chain cannot process images (e.g.
    DeepSeek, Groq text-only models), screenshots from ``computer_use`` are
    sent to the auxiliary vision model for description. The resulting text
    description is fed back to the main model, preserving visual context
    without requiring the main model to support image input.

    Set ``provider`` + ``model`` to activate; leave empty to fall back to
    the tool's text_summary downgrade (lossy).
    """

    force_text_only: bool = Field(
        default=False,
        description="Force all multimodal tool results to text-only (disable image passing to LLM)",
    )
    provider: str = Field(
        default="",
        description=(
            "Provider ID for the auxiliary vision model (e.g. 'openai', 'anthropic', "
            "'dashscope'). Must match a configured provider in `providers`. "
            "Empty = use text_summary fallback when main model lacks vision."
        ),
    )
    model: str = Field(
        default="",
        description=(
            "Model name for the auxiliary vision call (e.g. 'gpt-4o', 'claude-3-5-sonnet', "
            "'qwen2.5-vl-72b-instruct'). Must be a vision-capable model. "
            "Empty = use text_summary fallback."
        ),
    )


class SkillsConfig(Base):
    """Skill loading and quality gates."""

    min_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum SkillImprover score for non-builtin skills to appear in the "
            "skills index / always-active set. 0 disables the gate. Profiles may "
            "raise this further."
        ),
    )
    hide_stale: bool = Field(
        default=True,
        description="Hide stale/archived skills from the default skills index.",
    )


class ReliabilityConfig(Base):
    """Background job reliability (cron / heartbeat)."""

    cron_max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Extra retries after the first failed cron job attempt",
    )
    cron_retry_delay_s: float = Field(
        default=5.0,
        ge=0.5,
        le=300.0,
        description="Base delay between cron retries (linear backoff multiplier applied)",
    )
    dead_letter_keep: int = Field(
        default=50,
        ge=5,
        le=500,
        description="Max dead-letter failure records retained on disk",
    )
    notify_on_failure: bool = Field(
        default=True,
        description="Notify the job's channel when retries are exhausted",
    )


class GuardrailsConfig(Base):
    """Tool-loop guardrail thresholds (pure decision engine)."""

    enabled: bool = Field(
        default=True,
        description="Enable tool-loop guardrails (exact-failure / no-progress / window halt)",
    )
    exact_failure_warn: int = Field(default=2, ge=1, le=20)
    exact_failure_block: int = Field(default=4, ge=1, le=50)
    tool_streak_warn: int = Field(default=3, ge=1, le=20)
    tool_streak_block: int = Field(default=5, ge=1, le=50)
    no_progress_warn: int = Field(default=2, ge=1, le=20)
    no_progress_block: int = Field(default=4, ge=1, le=50)
    window_size: int = Field(default=6, ge=2, le=40)
    window_failure_threshold: int = Field(default=4, ge=2, le=40)
    max_reflections: int = Field(default=2, ge=0, le=10)
    failed_methods_cap: int = Field(
        default=30,
        ge=1,
        le=200,
        description="Max entries retained in the cross-loop failed-methods blacklist",
    )
    idempotent_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Tools treated as idempotent for no-progress detection. "
            "Empty list uses the engine built-in default set."
        ),
    )


class DelegationConfig(Base):
    """Subagent spawn control plane (depth / concurrency / blocked tools)."""

    max_spawn_depth: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Max parent→child spawn depth. 1 = children cannot spawn.",
    )
    max_concurrent_children: int = Field(
        default=3,
        ge=1,
        le=32,
        description="Max concurrently running subagents per process",
    )
    max_children_per_session: int = Field(
        default=8,
        ge=1,
        le=100,
        description="Max subagents a single session may spawn",
    )
    blocked_tools: list[str] = Field(
        default_factory=lambda: [
            "spawn",
            "message",
            "cron",
            "ask_user_question",
            "skill_manage",
            "computer_use",
        ],
        description="Tools always forbidden for subagents (merged into CapabilityToken)",
    )
    force_auto_permission: bool = Field(
        default=True,
        description="Force AUTO permission mode inside subagents (avoid interactive deadlock)",
    )
    allow_nested_spawn: bool = Field(
        default=False,
        description="Allow children to call spawn (orchestrator role)",
    )


class CachePolicyConfig(Base):
    """Prefix-cache mutation policy."""

    defer_mutations: bool = Field(
        default=True,
        description=(
            "Defer tool-surface / system-prompt mutations until the next turn "
            "so the active conversation keeps a byte-stable prefix cache."
        ),
    )


class OutcomeGateConfigSchema(Base):
    """Verify-before-finish outcome gate."""

    enabled: bool = Field(default=True)
    max_nudges: int = Field(default=2, ge=0, le=10)
    surfaces: list[str] = Field(default_factory=lambda: ["cli", "web"])
    require_verification_for_mutations: bool = Field(default=True)
    require_verification_for_side_effects: bool = Field(default=True)


class RuntimeBudgetAxesConfig(Base):
    """Optional hard ceilings beyond CostTracker USD budget."""

    max_wall_seconds: float | None = Field(
        default=None,
        description="Hard wall-clock limit for a single turn (seconds)",
    )
    max_total_tokens: int | None = Field(
        default=None,
        description="Hard total-token limit for a single turn",
    )


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    filesystem: FilesystemToolConfig = Field(default_factory=FilesystemToolConfig)
    code_execution: CodeExecutionConfig = Field(default_factory=CodeExecutionConfig)
    memory: MemoryToolsConfig = Field(default_factory=MemoryToolsConfig)
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    restrict_to_workspace: bool = True  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    delegation: DelegationConfig = Field(default_factory=DelegationConfig)
    cache_policy: CachePolicyConfig = Field(default_factory=CachePolicyConfig)
    outcome_gate: OutcomeGateConfigSchema = Field(default_factory=OutcomeGateConfigSchema)
    runtime_budget: RuntimeBudgetAxesConfig = Field(default_factory=RuntimeBudgetAxesConfig)


class CompactionConfig(Base):
    """Multi-level context compaction configuration."""

    collapse_tool_result_chars: int = Field(
        default=4_000,
        ge=1_000,
        le=50_000,
        description="Max chars per tool_result block before collapse truncation",
    )
    collapse_head_chars: int = Field(
        default=900,
        ge=100,
        le=10_000,
        description="Head chars to preserve during context collapse (head+tail strategy)",
    )
    collapse_tail_chars: int = Field(
        default=500,
        ge=100,
        le=10_000,
        description="Tail chars to preserve during context collapse (head+tail strategy)",
    )
    micro_compact_keep_turns: int = Field(
        default=6,
        ge=2,
        le=20,
        description="Number of recent tool-result turns to preserve during micro-compact",
    )
    auto_compact_keep_recent: int = Field(
        default=5,
        ge=2,
        le=15,
        description="Number of recent message pairs to keep after auto-compaction (LLM summary)",
    )
    snip_keep_messages: int = Field(
        default=10,
        ge=3,
        le=30,
        description="Minimum messages to keep when history snip (last resort)",
    )
    threshold_ratio: float = Field(
        default=0.85,
        ge=0.5,
        le=0.99,
        description="Trigger compaction when context exceeds this fraction of window",
    )
    max_compact_output_tokens: int = Field(
        default=4_000,
        ge=1_000,
        le=16_000,
        description="Max tokens for LLM-generated compaction summary",
    )
    reserved_output_tokens: int = Field(
        default=8_000,
        ge=1_000,
        le=32_000,
        description="Tokens reserved for LLM output when calculating compaction threshold",
    )
    auto_compact_buffer: int = Field(
        default=13_000,
        ge=1_000,
        le=50_000,
        description="Extra buffer tokens subtracted from window before auto-compaction trigger",
    )
    tool_output_inline_chars: int = Field(
        default=16_000,
        ge=1_000,
        le=100_000,
        description="Max chars of tool output to keep inline before offloading to file",
    )
    tool_output_preview_chars: int = Field(
        default=3_000,
        ge=256,
        le=16_000,
        description="Preview chars to keep when tool output is offloaded to file",
    )
    system_prompt_token_budget: int = Field(
        default=16_000,
        ge=2_000,
        le=100_000,
        description="Max tokens for system prompt; lower-priority sections truncated if exceeded",
    )


class DisplayConfig(Base):
    """Display / UI preferences.

    Currently only holds the UI language for static user-facing messages
    (permission approval prompts, slash-command replies).  Does NOT affect
    agent responses, log lines, tool outputs, or slash-command descriptions
    -- those follow the user's input language naturally.  Supported: en, zh.
    Unknown values fall back to en.
    """

    language: str = Field(
        default="en",
        description=(
            "UI language for static user-facing messages (approval prompts, "
            "slash-command replies). Does NOT affect agent responses, log "
            "lines, tool outputs, or slash-command descriptions. "
            "Supported: en, zh. Unknown values fall back to en."
        ),
    )


class BudgetConfig(Base):
    """Cost tracking and budget control configuration."""

    enabled: bool = True
    max_budget_usd: float | None = Field(
        default=None,
        ge=0.01,
        description="Per-session budget cap in USD. None = unlimited",
    )
    warn_threshold_usd: float = Field(
        default=0.5,
        ge=0.01,
        description="Log a warning when cost exceeds this amount",
    )
    custom_pricing: dict[str, dict[str, float]] | None = Field(
        default=None,
        description="Override per-model pricing: {model_name: {input_per_1k, output_per_1k, ...}}",
    )


class SsrfConfig(Base):
    """SSRF protection block lists. Configurable per deployment."""

    blocked_hostnames: list[str] = Field(
        default_factory=lambda: [
            "metadata.google.internal",
            "metadata.goog",
        ],
        description="Hostnames always blocked (e.g. cloud metadata endpoints)",
    )
    always_blocked_ips: list[str] = Field(
        default_factory=lambda: [
            "169.254.169.254",
            "169.254.170.2",
            "169.254.169.253",
            "fd00:ec2::254",
            "100.100.100.200",
        ],
        description="IPs always blocked regardless of allow_private",
    )
    blocked_networks: list[str] = Field(
        default_factory=lambda: [
            "0.0.0.0/8",
            "10.0.0.0/8",
            "100.64.0.0/10",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "198.18.0.0/15",
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        ],
        description="Private/internal networks blocked when allow_private=False",
    )


class Config(BaseSettings):
    """Root configuration for markbot."""

    ssrf: SsrfConfig = Field(default_factory=SsrfConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def resolve_model(self, model_ref: str) -> tuple[ProviderConfig, ModelConfig]:
        """
        Resolve a model reference to (provider_config, model_config).

        Args:
            model_ref: Format "providerId/modelId"

        Returns:
            Tuple of (ProviderConfig, ModelConfig)

        Raises:
            ValueError: If reference format is invalid or not found
        """
        if "/" not in model_ref:
            raise ValueError(f"Invalid model reference format: {model_ref}. Expected 'providerId/modelId'")

        provider_id, model_id = model_ref.split("/", 1)

        provider = self.providers.get_provider(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found in config")

        model = provider.get_model(model_id)
        if model is None:
            available = [m.id for m in provider.models]
            raise ValueError(
                f"Model '{model_id}' not found in provider '{provider_id}'. "
                f"Available models: {available}"
            )

        return provider, model

    def validate_model_chain(self) -> list[str]:
        """
        Validate all references in model_chain.

        Returns:
            List of error messages (empty if valid)
        """
        errors = []
        for i, ref in enumerate(self.agents.defaults.model_chain):
            try:
                self.resolve_model(ref)
            except ValueError as e:
                errors.append(f"model_chain[{i}] ({ref}): {e}")
        return errors

    @property
    def primary_model_ref(self) -> str | None:
        """Get the first (primary) model reference."""
        return self.agents.defaults.model_chain[0] if self.agents.defaults.model_chain else None

    model_config = ConfigDict(env_prefix="MARKBOT_", env_nested_delimiter="__")

