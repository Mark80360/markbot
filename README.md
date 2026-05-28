# MarkBot 🦞

> Version 2.2.13

A lightweight personal AI assistant framework. MarkBot excels at complex task planning and software development, combining the best features of modern AI assistants with specialized capabilities for technical workflows.

## Core Strengths

- **Multi-Model Support with Auto-Failover**: Configure multiple LLM providers in a priority chain. When the primary model fails or is overloaded, MarkBot automatically falls back to the next model.
- **Task Planning & Orchestration**: Break down complex projects into manageable steps, track progress, and coordinate multiple sub-tasks autonomously
- **Software Development**: Write, review, debug, and refactor code with deep understanding of project context and best practices
- **Memory System**: Advanced memory management with compaction, summarization, and semantic search for context-aware responses
- **Extensible Architecture**: Customize and extend capabilities through a powerful skills system with security scanning and guardrails
- **Autopilot Pipeline**: Automated task execution with scoring, acceptance, verification, and self-repair capabilities

## Features

### Core Capabilities

- **Multiple LLM Providers**: Anthropic, OpenAI, Azure OpenAI, DeepSeek, OpenRouter, Gemini, Moonshot, Zhipu, DashScope, Groq, and more (25 providers supported)
- **Multi-Model Chain with Auto-Failover**: Configure multiple models in priority chain with automatic failover on errors or overload
- **Multi-Channel Support**: DingTalk, Feishu, QQ, WeChat (Weixin), Email, with auto-reconnect and health monitoring
- **OAuth Authentication**: Native support for OpenAI Codex and GitHub Copilot with OAuth flow
- **Local Model Support**: Integration with vLLM, Ollama, and OVMS for local deployments

### Memory & Context

- **Memory System**: Advanced memory management with compaction, summarization, semantic search, and Dream optimization
- **4-Tier Progressive Compaction**: Context Collapse → Micro-Compact → Auto-Compaction → History Snip, escalating only when needed
- **Dream Service**: Periodic AI-driven memory optimization on a cron schedule for intelligent context management

### Tool System

- **Built-in Tools**: 25+ tools including Filesystem (read/write/edit/list/delete), Shell, Web (search/fetch/extract), Search (glob/grep), Code Execution, MCP, Memory (search/save/forget/list/dream), Todo, Think, Question, Message, Explore, Context Explorer, Cron, Subagent (spawn/check/list), Skills, Autopilot
- **MCP Support**: Model Context Protocol for seamless tool integration (stdio, SSE, streamable HTTP)
- **Web Integration**: Built-in web browsing, content extraction, and search (Brave, Tavily, DuckDuckGo, SearXNG, Jina)
- **Code Execution**: Sandboxed Python code execution with security scanning and resource limits
- **Voice Transcription**: Audio transcription via Groq Whisper integration

### Agent Architecture

- **Sub-Agent System**: Delegate specialized tasks with capability-based delegation tokens (CapabilityToken) and budget/timeout controls
- **Pipeline Engine**: Middleware-based message processing with pluggable pipeline stages (QuestionResponse, MemoryLifecycle)
- **Token & Cost Tracking**: Real-time token usage monitoring with cache token support and per-model pricing
- **Budget Control**: Configurable per-session budget caps with custom pricing overrides and warning thresholds

### Automation & Skills

- **Skills System**: Modular skill framework with security scanning, guardrails, and sandboxed execution
- **Cron Jobs**: Schedule and automate recurring tasks with precision
- **Autopilot**: Automated task pipeline with Intake → Score → Accept → Execute → Verify → Repair workflow
- **Permission System**: Configurable permission modes (default, plan, accept_edits, bypass, auto) with per-tool allow/deny/ask policies

### Developer Experience

- **Slash Commands**: Built-in commands like `/new`, `/compact`, `/compact_str`, `/clear`, `/stop`, `/status`, `/restart`, `/help`
- **Event Bus**: Event-driven architecture for message passing and component communication
- **Context Explorer**: Explore project context with semantic search and catalog (explore_context_catalog, search_context, load_context)
- **Todo Management**: Built-in persistent todo tracking tool for task management
- **Codebase Exploration**: Understand project structure and code context with deep exploration tools (AST parsing, multi-file analysis)
- **Interaction Logging**: Full audit trail of LLM request/response pairs with incremental deduplication for analysis
- **Doctor Diagnostics**: Built-in diagnostic tool for environment checks and issue resolution

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Channels (Inbound)                          │
│           (DingTalk, Feishu, QQ, WeChat, Email, etc.)               │
│                  ↓ publish_inbound(msg)                             │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Message Bus                                  │
│  ┌──────────────────────┐      ┌──────────────────────────┐         │
│  │   Inbound Queue      │      │    Outbound Queue        │         │
│  │  (Channel→Agent)     │      │   (Agent→ChannelManager) │         │
│  └──────────────────────┘      └──────────────────────────┘         │
└─────────────────────────────────────────────────────────────────────┘
          │ consume_inbound()                        ▲
          ▼                                          │ publish_outbound()
┌─────────────────────────────────────────────────────────────────────┐
│                       Agent Loop                                    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Message Pipeline (Middleware Chain)                        │    │
│  │  QuestionResponseMW → MemoryLifecycleMW → Main Handler      │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  Context Builder → Memory Manager → Compactor → LLM         │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────┐  ┌─────────────────┐  ┌────────────────┐       │
│  │    Tools        │  │  Tool Executor  │  │   Subagent     │       │
│  │ (Filesystem,    │  │  (Truncation,   │  │   Manager      │       │
│  │  Shell, Web,    │  │   Sanitization, │  │ (Capability-   │       │
│  │  Search, MCP,   │  │   Persistence)  │  │  based Token)  │       │
│  │  Think, etc.)   │  │                 │  │                │       │
│  └─────────────────┘  └─────────────────┘  └────────────────┘       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                      Memory System                          │    │
│  │  ┌──────────────┐  ┌────────────┐  ┌────────────────┐       │    │
│  │  │  Compressed  │  │  Summary   │  │    Search      │       │    │
│  │  │   Summary    │  │   Task     │  │  (Embedding)   │       │    │
│  │  └──────────────┘  └────────────┘  └────────────────┘       │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │        Token & Cost Management                              │    │
│  │  ┌─────────────────┐  ┌────────────────────────────────┐    │    │
│  │  │  Token Tracker  │  │   Multi-Level Compactor        │    │    │
│  │  │  + Cost Tracker │  │  (4-tier progressive)          │    │    │
│  │  │  + Budget Cap   │  │                                │    │    │
│  │  └─────────────────┘  └────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Fallback Manager                                 │
│          (Multi-Model Chain with Auto-Failover)                     │
│  Circuit Breaker: 3 failures → open (60s cooldown)                  │
│  Retryable: timeout, rate limit, 429, 529, 502-504, overloaded      │
│  Unavailable: 401-403, 402, model not found, quota exceeded         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Providers                                      │
│  (Anthropic, OpenAI, Azure, DeepSeek, Gemini, Local Models, etc.)   │
│         Anthropic/OpenRouter support prompt caching                 │
└─────────────────────────────────────────────────────────────────────┘
          │
          │ consume_outbound()
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Channel Manager                                 │
│              (Route to appropriate channel)                         │
│         (Auto-discovery + health monitoring + auto-reconnect)       │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Channels (Outbound)                          │
│           (DingTalk, Feishu, QQ, WeChat, Email, etc.)               │
└─────────────────────────────────────────────────────────────────────┘
```

## Supporting Services (Background)

```
┌─────────────────────────────────────────────────────────────────────┐
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐       │
│  │ CronService │  │ Heartbeat    │  │  DreamService         │       │
│  │ (Scheduled  │  │  Service     │  │  (Memory Optimisation)│       │
│  │    Tasks)   │  │ (Monitoring) │  │  (Cron-based)         │       │
│  └─────────────┘  └──────────────┘  └───────────────────────┘       │
│  ┌───────────────────────┐  ┌──────────────────────────────┐        │
│  │    SessionManager     │  │  InteractionLogger           │        │
│  │    (Session State)    │  │  (Audit Trail)               │        │
│  └───────────────────────┘  └──────────────────────────────┘        │
│  ┌───────────────────────┐  ┌──────────────────────────────┐        │
│  │   CuratorService      │  │  SkillImprover               │        │
│  │   (Skill Maintenance) │  │  (Quality Evaluation)        │        │
│  └───────────────────────┘  └──────────────────────────────┘        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                    Autopilot Service                        │    │
│  │  Intake → Score → Accept → Execute → Verify → Repair        │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

## Installation

### Prerequisites

- Python 3.11 or higher
- pip package manager

### Install from Source

```bash
git clone https://github.com/mickletang/markbot.git
cd markbot
pip install -e .
```

### Development Installation

```bash
pip install -e ".[dev]"
```

### Optional Dependencies

```bash
pip install -e ".[weixin]"     # WeChat integration (qrcode, pycryptodome)
pip install -e ".[langsmith]"  # LangSmith tracing
pip install -e ".[chroma]"     # ChromaDB vector memory provider
```

## Quick Start

### Basic Usage

```bash
# Initialize configuration and workspace
markbot onboard

# Interactive wizard setup
markbot onboard --wizard

# Guided step-by-step setup (recommended for first-time users)
markbot onboard --guided

# Non-interactive defaults (for scripts/Docker)
markbot onboard --defaults

# Start interactive mode
markbot

# Start with specific workspace
markbot --workspace /path/to/workspace

# Run as gateway server
markbot gateway start
```

### CLI Commands

```bash
# Onboarding and setup
markbot onboard                  # Initialize config and workspace
markbot onboard --wizard         # Interactive menu-driven wizard
markbot onboard --guided         # Linear step-by-step guided setup
markbot onboard --defaults       # Non-interactive defaults

# Gateway management
markbot gateway start            # Start gateway server
markbot gateway stop             # Stop gateway server
markbot gateway restart          # Restart gateway server
markbot gateway status           # Check gateway status

# Skill management
markbot skills list
markbot skills install <skill-name>
markbot skills info <skill-name>

# Autopilot tasks
markbot autopilot list
markbot autopilot add
markbot autopilot run <task-id>

# Doctor diagnostics
markbot doctor
markbot doctor fix
```

### Slash Commands (in chat)

| Command | Description |
|---------|-------------|
| `/new` | Start fresh session with memory summary |
| `/compact` | Force manual context compaction |
| `/compact_str` | View current compressed summary |
| `/clear` | Clear history and compressed summary |
| `/stop` | Cancel all active tasks and subagents |
| `/status` | Show session status, token usage, and statistics |
| `/restart` | Restart the agent process |
| `/help` | Show available slash commands |

## Supported LLM Providers

MarkBot supports 25 LLM providers out of the box:

### Direct Providers
| Provider | Models | Authentication | Prompt Caching |
|----------|--------|----------------|----------------|
| **Anthropic** | Claude 3.5/3.7 Sonnet, Claude 3 Opus/Haiku, Claude 4 | API Key | ✅ |
| **OpenAI** | GPT-4o, GPT-4, GPT-3.5 | API Key | — |
| **Azure OpenAI** | GPT-4, GPT-3.5 | Azure credentials | — |
| **DeepSeek** | DeepSeek-V3, DeepSeek-R1, DeepSeek-Coder | API Key | — |
| **Gemini** | Gemini Pro, Gemini Ultra | API Key | — |
| **Moonshot** | Kimi K2.5, Kimi K1.5 | API Key | — |
| **Zhipu (智谱)** | GLM-4, GLM-3 | API Key | — |
| **DashScope (通义)** | Qwen2.5, Qwen-Coder | API Key | — |
| **MiniMax** | MiniMax models | API Key | — |
| **Mistral** | Mistral Large, Medium, Small | API Key | — |
| **Step Fun (阶跃星辰)** | Step models | API Key | — |
| **Groq** | Groq models (Whisper, LLM) | API Key | — |

### Gateway Services
| Provider | Description | Prompt Caching |
|----------|-------------|----------------|
| **OpenRouter** | Universal gateway to 100+ models | ✅ |
| **AiHubMix** | OpenAI-compatible gateway | — |
| **SiliconFlow (硅基流动)** | Chinese model gateway | — |
| **VolcEngine (火山引擎)** | ByteDance cloud models | — |
| **VolcEngine Coding Plan** | ByteDance coding-specific models | — |
| **BytePlus** | VolcEngine international | — |
| **BytePlus Coding Plan** | BytePlus coding-specific models | — |

### OAuth-Based Providers
| Provider | Description |
|----------|-------------|
| **OpenAI Codex** | OpenAI's coding assistant |
| **GitHub Copilot** | GitHub's AI pair programmer |

### Local Deployment
| Provider | Description |
|----------|-------------|
| **vLLM** | High-throughput LLM serving engine |
| **Ollama** | Local model runner |
| **OVMS** | OpenVINO Model Server |

### Custom Endpoint
| Provider | Description |
|----------|-------------|
| **Custom** | Any OpenAI-compatible endpoint |

## Configuration

MarkBot uses a YAML configuration file located at `~/.markbot/config.yaml` by default.

### Basic Configuration Example

```yaml
agents:
  defaults:
    model_chain:
      - anthropic/claude-sonnet-4-20250514
      - openai/gpt-4o
      - deepseek/deepseek-chat
    max_tokens: 8192
    temperature: 0.1
    timezone: Asia/Shanghai
    workspace: ~/.markbot/workspace

providers:
  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
    models:
      - id: claude-sonnet-4-20250514
        name: claude-sonnet-4-20250514
        max_tokens: 8192
        context_window: 200000

channels:
  dingtalk:
    enabled: true
    # ... channel-specific config

tools:
  web:
    search:
      provider: brave
      api_key: ${BRAVE_API_KEY}
  exec:
    enable: true
    timeout: 60
  filesystem:
    backup_dir: ~/.markbot/.markbot_backups
    safe_delete: true
  code_execution:
    enable: true
    timeout: 60
    max_memory_mb: 256
  memory:
    embedding_backend: openai
    memory_summary_enabled: true
    context_compact_enabled: true
    dream_cron: "0 23 * * *"

compaction:
  collapse_tool_result_chars: 4000
  micro_compact_keep_turns: 6
  auto_compact_keep_recent: 5
  threshold_ratio: 0.85

budget:
  enabled: true
  max_budget_usd: null
  warn_threshold_usd: 0.5
```

### Environment Variables

Most sensitive configuration values can be provided via environment variables using `${VAR_NAME}` syntax in the config file.

## Built-in Tools

### Filesystem & Search

| Tool | Name | Description |
|------|------|-------------|
| **Read File** | `read_file` | Read file contents with image detection and encoding support |
| **Write File** | `write_file` | Write/create files with automatic backup |
| **Edit File** | `edit_file` | Search-and-replace file editing with diff support |
| **List Dir** | `list_dir` | List directory contents with ignore patterns |
| **Delete File** | `delete_file` | Delete files with safe-delete (recycle bin) support |
| **Glob** | `glob` | Fast file pattern matching (find files by name) |
| **Grep** | `grep` | Content search with regex, binary detection, and context lines |

### Web & Search

| Tool | Name | Description |
|------|------|-------------|
| **Web Search** | `web_search` | Multi-provider web search (Brave, Tavily, DuckDuckGo, SearXNG, Jina) |
| **Web Fetch** | `web_fetch` | Fetch and extract content from a URL |
| **Web Extract** | `web_extract` | Enhanced URL content extraction with LLM summarization |

### Execution

| Tool | Name | Description |
|------|------|-------------|
| **Shell** | `exec` | Command execution with timeout, rate limiting, and safety controls |
| **Run Code** | `run_code` | Sandboxed Python code execution with security scanning |

### Memory & Context

| Tool | Name | Description |
|------|------|-------------|
| **Memory Search** | `memory_search` | Semantic/full-text search in memory files |
| **Memory Save** | `memory_save` | Save important information to long-term memory |
| **Memory Forget** | `memory_forget` | Remove information from long-term memory |
| **Memory List** | `memory_list` | List all memory entries with optional tag filtering |
| **Dream** | `dream` | Trigger AI-driven memory optimization |
| **Explore Catalog** | `explore_context_catalog` | View available context sources (table of contents) |
| **Search Context** | `search_context` | Search within specific context sources |
| **Load Context** | `load_context` | Load full content from a specific context entry |

### Agent & Planning

| Tool | Name | Description |
|------|------|-------------|
| **Think** | `think` | Unified cognitive tool: analyze, plan, reflect, code-analysis, research-plan |
| **Todo** | `todo` | Persistent task tracking with status and priority |
| **Message** | `message` | Send messages to users on chat channels |
| **Ask Question** | `ask_user_question` | Ask users structured questions with predefined options |
| **Explore** | `explore` | Deep code exploration with AST parsing and multi-file analysis |

### Subagent

| Tool | Name | Description |
|------|------|-------------|
| **Spawn** | `spawn` | Spawn a subagent with capability-based delegation token |
| **Check Subagent** | `check_subagent` | Check status of a spawned subagent |
| **List Subagents** | `list_subagents` | List all active subagents |

### Scheduling & Automation

| Tool | Name | Description |
|------|------|-------------|
| **Cron** | `cron` | Schedule reminders and recurring tasks |

### Skills & Autopilot

| Tool | Name | Description |
|------|------|-------------|
| **Skills List** | `skills_list` | List available skills |
| **Skill View** | `skill_view` | View skill details and instructions |
| **Skill Manage** | `skill_manage` | Install, uninstall, and manage skills |
| **Autopilot Tools** | `autopilot_*` | Intake, score, accept, execute, verify, and repair tasks |

### Integration

| Tool | Name | Description |
|------|------|-------------|
| **MCP** | `mcp_*` | Model Context Protocol tools (auto-registered from MCP servers) |

## Skills System

MarkBot features a modular skill framework that allows you to extend functionality:

### Built-in Skills

- **weather**: Weather information lookup
- **summarize**: Text summarization
- **surprise-me**: Random fun interactions
- **tmux**: Terminal session management
- **github**: GitHub integration
- **clawhub**: Skill marketplace access
- **skill-creator**: Create and manage custom skills
- **memory**: Advanced memory operations
- **cron**: Scheduled task management

### Skill Features

- **Security Scanning**: Automatic vulnerability detection before loading
- **Guardrails**: Configurable safety policies per skill
- **Sandboxed Execution**: Isolated execution environment
- **Conditional Activation**: Load skills based on requirements and configuration
- **Config Resolution**: Per-skill configuration with inheritance
- **Usage Tracking**: View/use counters and last activity timestamps per skill, persisted to `.skill_usage.json`
- **Lifecycle Management**: Automatic state transitions — `active` → `stale` (30 days inactive) → `archived` (90 days)
- **Curator Service**: Background maintenance agent that auto-archives stale skills and evaluates quality
- **Self-Improvement**: SkillImprover runs heuristic quality evaluations and generates improvement suggestions via LLM

## Autopilot System

The Autopilot system provides automated task execution with intelligence:

### Pipeline Stages

1. **Intake**: Receive and parse task definitions
2. **Score**: Evaluate task complexity and priority
3. **Accept**: Determine if task should be executed
4. **Execute**: Run the task with AI assistance via AgentLoop.process_direct()
5. **Verify**: Validate results against verification steps and acceptance criteria
6. **Repair**: Automatically fix issues if verification fails
7. **Complete/Fail**: Finalize task status with verification report

### Use Cases

- Automated code review and fixes
- Repository maintenance tasks
- Test generation and execution
- Documentation updates
- Dependency management

## Permission System

Granular control over agent actions through configurable permission modes:

### Modes

| Mode | Description |
|------|-------------|
| `default` | Ask for confirmation on sensitive operations |
| `plan` | Show plan before execution |
| `accept_edits` | Auto-accept file edits |
| `bypass` | Skip all confirmations |
| `auto` | Fully autonomous operation |

### Tool-Level Policies

Each tool can have individual allow/deny/ask policies for fine-grained control.

## Memory System

Advanced built-in memory management:

### Features

- **Semantic Search**: Vector-based similarity search across memories (OpenAI and Ollama embedding backends)
- **4-Tier Progressive Compaction**: Context Collapse → Micro-Compact → Auto-Compaction → History Snip, escalating only when needed
- **Head+Tail Context Collapse**: Preserves both beginning and end of content during truncation
- **CompactAttachment**: Preserves key context across compaction rounds
- **Tool Output Offloading**: Oversized tool results offloaded to files with inline previews
- **PTL Retry**: Prompt-Too-Long retry with head truncation
- **Summarization**: Extract key information from interactions into MEMORY.md
- **Dream Optimization**: Periodic AI-driven memory reorganization on cron schedule
- **Daily Logs**: Time-based memory organization in `memory/daily/*.md`
- **Security Scanner**: Injection and exfiltration detection for memory content
- **Context Fencing**: `<memory-context>` tags with streaming scrubber
- **Plugin Discovery**: External memory providers via entry points, naming convention (`markbot_memory_*`), or manual registration
- **ChromaDB Provider**: Reference implementation for vector-based semantic memory with ChromaDB

### Memory Provider Plugins

MarkBot supports pluggable memory providers through the `MemoryProvider` ABC. External providers are discovered automatically:

1. **Entry Points**: Packages declaring `markbot.memory_providers` in their `pyproject.toml`
2. **Naming Convention**: Installed packages matching `markbot_memory_*` or `markbot-memory-*`
3. **Manual Registration**: Via `MemoryPluginDiscovery.register()`

Configuration in `config.yaml`:

```yaml
tools:
  memory:
    provider: chroma          # External provider name
    provider_config:          # Provider-specific configuration
      host: localhost
      port: 8000
```

The built-in ChromaDB provider (`markbot.memory.providers.chroma`) supports both local persistent and remote HTTP modes. Install with `pip install -e ".[chroma]"`.

### Configuration

```yaml
tools:
  memory:
    embedding_backend: openai  # or "ollama" for local models
    embedding_api_key: ""
    embedding_base_url: ""
    embedding_model_name: ""
    memory_compact_threshold: 0  # 0 = auto (75% of context window)
    memory_compact_reserve: 10000
    memory_summary_enabled: true
    context_compact_enabled: true
    dream_cron: "0 23 * * *"  # Cron expression for dream optimization

compaction:
  collapse_tool_result_chars: 4000
  collapse_head_chars: 900
  collapse_tail_chars: 500
  micro_compact_keep_turns: 6
  auto_compact_keep_recent: 5
  snip_keep_messages: 10
  threshold_ratio: 0.85
  max_compact_output_tokens: 4000
  tool_output_inline_chars: 16000
  tool_output_preview_chars: 3000
  system_prompt_token_budget: 16000
```

## Monitoring & Diagnostics

### Health Checks

Automatic health monitoring for all channels with auto-reconnect and configurable intervals.

### Status Command

Real-time statistics including:
- Model information and context window usage
- Token usage (input, output, cache creation, cache read)
- Cumulative cost tracking per model
- Active tasks and sessions
- API call counts
- Subagent status

### Doctor Tool

Comprehensive diagnostics:

```bash
# Check environment and configuration
markbot doctor

# Auto-fix common issues
markbot doctor fix
```

Checks include:
- Python version compatibility
- Configuration validity and model chain verification
- File system permissions
- Provider authentication
- Channel configuration
- Workspace integrity

## Development

### Project Structure

```
markbot/
├── agent/              # Core agent loop and processing
│   ├── loop.py         # Main agent processing engine
│   ├── compact.py      # 4-tier progressive compaction
│   ├── container.py    # Agent context container
│   ├── context.py      # Context builder for agent prompts
│   ├── cost.py         # Token & cost tracking with budget control
│   ├── stream.py       # Stream filtering (think-tag removal)
│   ├── tokens.py       # Token estimation
│   ├── iteration.py    # Iteration runner for agent loop
│   ├── tool_binder.py  # Tool registration and binding
│   ├── pipeline/       # Message pipeline middleware
│   │   ├── engine.py   # Pipeline engine
│   │   └── middleware.py # Built-in middleware (QuestionResponse, MemoryLifecycle)
│   ├── subagent/       # Background task delegation
│   │   ├── capability.py  # CapabilityToken for delegation
│   │   ├── manager.py     # Subagent manager
│   │   ├── progress.py    # Progress tracking
│   │   ├── spawn.py       # Spawn tool
│   │   └── tools.py       # Check/List subagent tools
│   ├── mcp/            # MCP protocol support
│   │   └── manager.py  # MCP connection manager
│   ├── hooks/          # Agent lifecycle hooks
│   │   ├── bootstrap.py   # Bootstrap hooks
│   │   └── compaction.py  # Compaction hooks
│   └── services/       # Agent services
│       ├── executor.py    # Tool execution service
│       └── interaction.py # Interaction logger
├── channels/           # Multi-channel messaging
│   ├── base.py         # BaseChannel ABC
│   ├── manager.py      # ChannelManager
│   ├── discovery.py    # Auto-discovery for built-in + plugins
│   ├── dingtalk.py     # DingTalk integration
│   ├── feishu.py       # Feishu/Lark integration
│   ├── qq.py           # QQ Bot integration
│   ├── weixin.py       # WeChat integration
│   └── email.py        # Email (IMAP+SMTP) support
├── tools/              # Built-in tool implementations
│   ├── base.py         # BaseTool and Tool ABC
│   ├── registry.py     # ToolRegistry with permission integration
│   ├── filesystem.py   # File read/write/edit/list/delete
│   ├── shell.py        # Shell execution with safety
│   ├── web.py          # Web search/fetch/extract
│   ├── search.py       # Glob and Grep
│   ├── code.py         # Sandboxed code execution
│   ├── memory.py       # Memory search
│   ├── memory_tools.py # Memory save/forget/list/dream
│   ├── think.py        # Unified cognitive tool
│   ├── todo.py         # Persistent task tracking
│   ├── question.py     # Ask user questions
│   ├── message.py      # Message sending
│   ├── explore.py      # Deep code exploration
│   ├── context_explorer.py # Context catalog/search/load
│   ├── cron.py         # Cron scheduling
│   └── mcp.py          # MCP client tool wrapper
├── skills/             # Skill system
│   ├── core/           # Skill framework
│   │   ├── loader.py   # Skill loading
│   │   ├── registry.py # Skill registry
│   │   ├── scanner.py  # Security scanning
│   │   ├── guardrail.py # Safety guardrails
│   │   ├── sandbox.py  # Sandboxed execution
│   │   ├── config.py   # Config resolution
│   │   ├── manage.py   # Skill management tool
│   │   ├── tool.py     # Skill tools (list/view)
│   │   └── helpers.py  # Skill helpers
│   ├── usage.py        # Skill usage tracking (SkillUsageStore)
│   ├── lifecycle.py    # Lifecycle state machine (active→stale→archived)
│   ├── curator.py      # Background maintenance service
│   ├── improve.py      # Self-improvement evaluations
│   └── builtin/        # Built-in skills
│       ├── weather/    # Weather lookup
│       ├── summarize/  # Text summarization
│       ├── surprise-me/# Fun interactions
│       ├── tmux/       # Terminal session management
│       ├── github/     # GitHub integration
│       ├── clawhub/    # Skill marketplace
│       ├── skill-creator/ # Custom skill creation
│       ├── memory/     # Advanced memory operations
│       └── cron/       # Scheduled task management
├── providers/          # LLM provider integrations
│   ├── base.py         # LLMProvider ABC
│   ├── registry.py     # ProviderSpec registry (25 providers)
│   ├── fallback.py     # FallbackManager with circuit breaker
│   ├── anthropic.py    # Anthropic native SDK
│   ├── openai_compat.py # OpenAI-compatible provider
│   ├── azure_openai.py # Azure OpenAI
│   ├── openai_codex.py # OpenAI Codex (OAuth)
│   └── transcription.py # Groq Whisper transcription
├── config/             # Configuration management
│   ├── schema.py       # Pydantic config schema
│   ├── loader.py       # Config loading/saving
│   ├── paths.py        # Path resolution
│   └── validator.py    # Config validation
├── memory/             # Memory system
│   ├── base.py         # BaseMemoryManager ABC
│   ├── manager.py      # File-based memory manager
│   ├── provider.py     # MemoryProvider ABC
│   ├── daily_log.py    # Daily log management
│   ├── encoder.py      # Embedding encoder
│   ├── scanner.py      # Security scanner
│   ├── fencing.py      # Context fencing
│   ├── tool.py         # MemoryStore
│   ├── manager.py      # Main MemoryManager
│   ├── plugins/        # Memory plugin discovery
│   │   └── discovery.py # MemoryPluginDiscovery (entry points, naming, manual)
│   └── providers/      # Memory provider implementations
│       └── chroma.py   # ChromaDB vector memory provider
├── bus/                # Event bus infrastructure
│   ├── events.py       # Event types (28 event types)
│   ├── queue.py        # MessageBus (inbound/outbound)
│   └── emitter.py      # Event emitter
├── session/            # Session management
│   ├── session.py      # Session data model
│   ├── store.py        # Session persistence
│   ├── app_state.py    # Application state
│   ├── bootstrap.py    # Session bootstrap
│   ├── handoff.py      # Session handoff
│   ├── integrity.py    # Session integrity checks
│   ├── task_tracker.py # Task tracking
│   └── types.py        # Session types
├── schedule/           # Cron and scheduling
│   ├── cron.py         # CronService
│   ├── dream.py        # DreamService
│   ├── heartbeat.py    # HeartbeatService
│   └── evaluator.py    # Schedule evaluator
├── autopilot/          # Automated task pipeline
│   ├── service.py      # AutopilotService
│   ├── store.py        # Task store
│   ├── tools.py        # Autopilot tools
│   ├── types.py        # Task types
│   └── verification.py # Verification system
├── cli/                # Command-line interface
│   ├── commands.py     # Main CLI entry point
│   ├── skills.py       # Skill management commands
│   ├── autopilot.py    # Autopilot commands
│   ├── doctor.py       # Diagnostic commands
│   ├── onboard.py      # Interactive onboarding
│   ├── models.py       # Model suggestions
│   ├── stream.py       # Stream rendering
│   └── slash_commands/ # Slash command routing
│       ├── router.py   # CommandRouter
│       └── builtin.py  # Built-in commands
├── types/              # Type definitions
│   ├── exceptions.py   # Custom exceptions
│   ├── permission.py   # Permission types
│   ├── protocols.py    # Protocol definitions
│   ├── skill.py        # Skill types
│   └── tool.py         # Tool types
├── utils/              # Utility functions
│   ├── constants.py    # Shared constants
│   ├── helpers.py      # Helper functions
│   └── network.py      # Network utilities
├── log/                # Logging
│   ├── core.py         # Core logging
│   ├── filter.py       # Log filters
│   └── format.py       # Log formatters
└── templates/          # Prompt templates
    ├── SOUL.md         # Agent personality
    ├── TOOLS.md        # Tool descriptions
    ├── MEMORY.md       # Memory instructions
    ├── ARCHITECTURE.md # Architecture context
    └── agents/         # Agent-specific templates
```

### Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Code Quality

```bash
# Run linting
ruff check .

# Run tests
pytest

# Run tests with coverage
pytest --cov=markbot
```

## License

This project is licensed under the GNU Affero General Public License v3 (AGPL-3.0) - see the LICENSE file for details.

---

**MarkBot** 🦞 - Your personal AI assistant for development and automation.
