"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
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
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
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
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
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

class MemoryToolsConfig(Base):
    """Memory system configuration (ReMeLight)."""

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

class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    filesystem: FilesystemToolConfig = Field(default_factory=FilesystemToolConfig)
    memory: MemoryToolsConfig = Field(default_factory=MemoryToolsConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class CompactionConfig(Base):
    """Multi-level context compaction configuration."""

    collapse_tool_result_chars: int = Field(
        default=4_000,
        ge=1_000,
        le=50_000,
        description="Max chars per tool_result block before collapse truncation",
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


class Config(BaseSettings):
    """Root configuration for markbot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

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

        if not hasattr(self.providers, provider_id):
            raise ValueError(f"Provider '{provider_id}' not found in config")

        provider = getattr(self.providers, provider_id)
        if not isinstance(provider, ProviderConfig):
            raise ValueError(f"Invalid provider config for '{provider_id}'")

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
