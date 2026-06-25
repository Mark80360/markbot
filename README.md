# MarkBot 🦞

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

- **Multiple LLM Providers**: Anthropic, OpenAI, Azure OpenAI, DeepSeek, OpenRouter, Gemini, Moonshot, Zhipu, DashScope, Groq, HuggingFace, xAI, NVIDIA NIM, and more (28 providers supported)
- **Multi-Model Chain with Auto-Failover**: Configure multiple models in priority chain with automatic failover on errors or overload
- **Multi-Channel Support**: DingTalk, Feishu, QQ, WeChat (Weixin), Email, with auto-reconnect, health monitoring, and auto-restart on failure
- **OAuth Authentication**: Native support for OpenAI Codex and GitHub Copilot with OAuth flow
- **Local Model Support**: Integration with vLLM, Ollama, and OVMS for local deployments

### Memory & Context

- **Memory System**: Advanced memory management with compaction, summarization, semantic search, and Dream optimization
- **4-Tier Progressive Compaction**: Context Collapse → Micro-Compact → Auto-Compaction → History Snip, escalating only when needed
- **Dream Service**: Periodic AI-driven memory optimization on a cron schedule for intelligent context management

### Tool System

- **Built-in Tools**: 40+ tools including Filesystem (read/write/edit/list/delete), Shell, Web (search/fetch/extract), Search (glob/grep), Code Execution, MCP, Memory (search/save/forget/list/dream), Todo, Think, Question, Message, Explore, Context Explorer, Cron, Subagent (spawn/check/list), Skills, Autopilot (7 tools), Computer Use, Browser (10 tools)
- **MCP Support**: Model Context Protocol for seamless tool integration (stdio, SSE, streamable HTTP)
- **Web Integration**: Built-in web browsing, content extraction, and search (Brave, Tavily, DuckDuckGo, SearXNG, Jina)
- **Code Execution**: Sandboxed Python code execution with security scanning and resource limits
- **Computer Use**: Cross-platform desktop control with screenshot capture, mouse/keyboard automation, and element-based interaction (cua-driver on macOS, pyautogui on Linux/Windows)
- **Browser Automation**: Playwright-based browser control with navigate, snapshot, click, type, scroll, press, back, and vision tools
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
│         (Auto-discovery + health monitoring + auto-restart)         │
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
pip install -e ".[weixin]"            # WeChat integration (qrcode, pycryptodome)
pip install -e ".[langsmith]"         # LangSmith tracing
pip install -e ".[chroma]"            # ChromaDB vector memory provider
pip install -e ".[local-embeddings]"  # Local sentence-transformers embedder (offline long-term memory)
pip install -e ".[desktop]"           # Computer Use desktop control (pyautogui, Pillow) — umbrella
pip install -e ".[desktop-macos]"     # Computer Use extras for macOS (pyautogui, Pillow)
pip install -e ".[desktop-linux]"     # Computer Use extras for Linux (adds pyatspi)
pip install -e ".[desktop-windows]"   # Computer Use extras for Windows (pyautogui, Pillow)
pip install -e ".[web]"               # Web UI server (FastAPI, uvicorn)
pip install playwright && playwright install chromium  # Browser automation
```

> **Tip**: The `desktop-*` extras all resolve to the same Python deps today; pick whichever matches the host. On Linux you also need system packages (`python3-tk`, `python3-xlib`, `scrot`, `at-spi2-core`, `wmctrl`/`xdotool`) and a reachable display. See the [Computer Use](#computer-use) section below for the full setup.

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
| `/steer` | Inject mid-task instruction into running agent |
| `/status` | Show session status, token usage, and statistics |
| `/restart` | Restart the agent process |
| `/help` | Show available slash commands |

## Supported LLM Providers

MarkBot supports 28 LLM providers out of the box:

### Direct Providers
| Provider | Models | Authentication | Prompt Caching |
|----------|--------|----------------|----------------|
| **Anthropic** | Claude 4.5 Sonnet/Haiku, Claude 4 Sonnet, Claude 3.5/3.7 Sonnet, Claude 3 Opus | API Key | ✅ |
| **OpenAI** | GPT-4o, GPT-4, GPT-3.5 | API Key | — |
| **OpenAI Codex** | GPT-5.1 Codex | OAuth | — |
| **GitHub Copilot** | Copilot models | OAuth | — |
| **Azure OpenAI** | GPT-4, GPT-3.5 | Azure credentials | — |
| **DeepSeek** | DeepSeek-V3, DeepSeek-R1, DeepSeek-Coder | API Key | — |
| **Gemini** | Gemini 2.5 Pro/Flash (and newer) | API Key | — |
| **Moonshot** | Kimi K2, K2.5, K2-Turbo-Preview, K1.5 | API Key | — |
| **Zhipu (智谱)** | GLM-4, GLM-3 | API Key | — |
| **DashScope (通义)** | Qwen2.5, Qwen-Coder | API Key | — |
| **MiniMax** | MiniMax M2.7 / M3 (and newer) | API Key | — |
| **Mistral** | Mistral Large, Medium, Small (OpenAI-compatible API) | API Key | — |
| **Step Fun (阶跃星辰)** | Step models | API Key | — |
| **xAI** | Grok models | API Key | — |
| **NVIDIA NIM** | Nemotron and other NVIDIA models | API Key | — |
| **Groq** | Groq models (Whisper, LLM) | API Key | — |

### Gateway Services
| Provider | Description | Prompt Caching |
|----------|-------------|----------------|
| **OpenRouter** | Universal gateway to 200+ models | ✅ |
| **AiHubMix** | OpenAI-compatible gateway | — |
| **SiliconFlow (硅基流动)** | Chinese model gateway | — |
| **VolcEngine (火山引擎)** | ByteDance cloud models | — |
| **VolcEngine Coding Plan** | ByteDance coding-enhanced models | — |
| **BytePlus** | VolcEngine international gateway | — |
| **BytePlus Coding Plan** | BytePlus coding-enhanced models | — |
| **HuggingFace** | HuggingFace Inference API | — |

### OAuth-Based Providers

The two providers below are listed above as well (in **Direct Providers**); the
rows here are just a quick reference for their auth model. Both use OAuth
flows via the `oauth-cli-kit` package — no API key is stored on disk.

| Provider | Backend | Notes |
|----------|---------|-------|
| **OpenAI Codex** | OpenAI Responses API (`chatgpt.com/backend-api`) | Default model: `openai-codex/gpt-5.1-codex` |
| **GitHub Copilot** | OpenAI-compatible (`api.githubcopilot.com`) | Uses GitHub device flow |

### Direct / Bring-Your-Own Endpoint

`custom` is registered as `is_direct=true` — the user supplies the
`api_base` (and optionally an `api_key`) themselves. It is the recommended
choice when you have an OpenAI-compatible HTTP endpoint that does not
match any of the named providers above. Aliases: `custom`, `ollama`,
`local`, `vllm`, `llamacpp`.

### Local Deployment

These providers are matched by config key (not by `api_base`) and are
flagged `is_local=true`. They all speak the OpenAI Chat Completions
protocol, so you can plug in a `custom` block instead if you prefer.

| Provider | Description |
|----------|-------------|
| **vLLM** | High-throughput LLM serving engine |
| **Ollama** | Local model runner (default `http://localhost:11434/v1`) |
| **OVMS** | OpenVINO Model Server (`http://localhost:8000/v3`) |

### Provider Aliases

Each provider supports alias-based lookup via `find_by_name()`. For example:
- `anthropic` can also be referenced as `claude`
- `gemini` accepts `google` or `google-gemini`
- `custom` covers `ollama`, `local`, `vllm`, `llamacpp` (see *Direct / Bring-Your-Own Endpoint* above)
- `dashscope` accepts `alibaba`, `alibaba-cloud`, `qwen-dashscope`

Provider metadata includes `description` and `signup_url` for guided setup wizards.

## Configuration

MarkBot uses a JSON configuration file located at `~/.markbot/config.json` by default.

### Basic Configuration Example

```json
{
  "agents": {
    "defaults": {
      "model_chain": [
        "anthropic/claude-sonnet-4-5-20250514",
        "openai/gpt-4o",
        "deepseek/deepseek-chat"
      ],
      "max_tokens": 8192,
      "temperature": 0.1,
      "timezone": "Asia/Shanghai",
      "workspace": "~/.markbot/workspace",
      "auxiliaryVision": {
        "forceTextOnly": false,
        "provider": "openai",
        "model": "gpt-4o"
      }
    }
  },
  "providers": {
    "anthropic": {
      "api_key": "${ANTHROPIC_API_KEY}",
      "models": [
        {
          "id": "claude-sonnet-4-20250514",
          "name": "claude-sonnet-4-20250514",
          "max_tokens": 8192,
          "context_window": 200000,
          "capabilities": ["image"]
        }
      ]
    },
    "deepseek": {
      "api_key": "${DEEPSEEK_API_KEY}",
      "models": [
        {
          "id": "deepseek-chat",
          "name": "deepseek-chat",
          "capabilities": []
        }
      ]
    }
  },
  "channels": {
    "dingtalk": {
      "enabled": true
    }
  },
  "tools": {
    "web": {
      "search": {
        "provider": "brave",
        "api_key": "${BRAVE_API_KEY}"
      }
    },
    "exec": {
      "enable": true,
      "timeout": 60
    },
    "filesystem": {
      "backup_dir": "~/.markbot/.markbot_backups",
      "safe_delete": true
    },
    "code_execution": {
      "enable": true,
      "timeout": 60,
      "max_memory_mb": 256
    },
    "memory": {
      "embedding_backend": "openai",
      "memory_summary_enabled": true,
      "context_compact_enabled": true,
      "dream_cron": "0 23 * * *"
    }
  },
  "compaction": {
    "collapse_tool_result_chars": 4000,
    "micro_compact_keep_turns": 6,
    "auto_compact_keep_recent": 5,
    "threshold_ratio": 0.85
  },
  "budget": {
    "enabled": true,
    "max_budget_usd": null,
    "warn_threshold_usd": 0.5
  }
}
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

- **Hybrid Semantic Search (Long-Term Memory)**: Vector-based similarity search that recalls content *by meaning, not just keywords*. Uses a layered embedder (OpenAI-compatible API → local sentence-transformers → zero-dependency hashing fallback) so it works in every environment. Results are fused with keyword search via Reciprocal Rank Fusion. Turn histories, memory writes, and subagent delegations are all indexed automatically.
- **Vector Consolidation**: Periodic dedup (cosine-based near-duplicate merging), importance decay, and auto-promotion of frequently-recalled memories into `MEMORY.md`. Keeps the index from growing without bound.
- **4-Tier Progressive Compaction**: Context Collapse → Micro-Compact → Auto-Compaction → History Snip, escalating only when needed
- **Head+Tail Context Collapse**: Preserves both beginning and end of content during truncation
- **CompactAttachment**: Preserves key context across compaction rounds
- **Tool Output Offloading**: Oversized tool results offloaded to files with inline previews
- **PTL Retry**: Prompt-Too-Long retry with head truncation
- **Summarization**: Extract key information from interactions into MEMORY.md
- **Dream Optimization**: Periodic AI-driven memory reorganization on cron schedule (includes vector consolidation)
- **Daily Logs**: Time-based memory organization in `memory/daily/*.md`
- **Security Scanner**: Injection and exfiltration detection for memory content
- **Sensitive Data Redaction**: Automatic scrubbing of API keys, tokens, passwords, JWTs, and connection strings before LLM summarization
- **Context Fencing**: `<memory-context>` tags with streaming scrubber
- **Pluggable Vector Backends**: SQLite (default, zero extra deps) or ChromaDB (`pip install markbot[chroma]`)
- **Plugin Discovery**: External memory providers via entry points, naming convention (`markbot_memory_*`), or manual registration

### Long-Term Memory Configuration

Long-term (vector) memory is **enabled by default** and requires zero configuration — it auto-selects the best available embedder and uses the built-in SQLite vector store. To tune it:

```json
{
  "tools": {
    "memory": {
      "longTermEnabled": true,
      "vectorBackend": "sqlite",
      "vectorMaxRecords": 50000,
      "vectorMinScore": 0.15,
      "embeddingBackend": "openai",
      "embeddingApiKey": "sk-...",
      "embeddingModelName": "text-embedding-3-small"
    }
  }
}
```

**Embedding backends** (auto-selected by priority):
1. `openai` — when `embeddingApiKey` is set (best quality, needs network). Point `embeddingBaseUrl` at any OpenAI-compatible service.
2. Local `sentence-transformers` — when installed via `pip install 'markbot[local-embeddings]'` (multilingual, offline after first download).
3. Hashing fallback — always available, zero dependencies.

**Vector stores**:
- `sqlite` (default): standard library only, cosine ranking in memory. Handles up to ~50k vectors.
- `chroma`: `pip install 'markbot[chroma]'` then set `"vectorBackend": "chroma"`. Uses our embedder (not Chroma's bundled model) for consistency.

To **disable** long-term memory (keyword search only): `"longTermEnabled": false`.

### Memory Provider Plugins

MarkBot supports pluggable memory providers through the `MemoryProvider` ABC. External providers are discovered automatically:

1. **Entry Points**: Packages declaring `markbot.memory_providers` in their `pyproject.toml`
2. **Naming Convention**: Installed packages matching `markbot_memory_*` or `markbot-memory-*`
3. **Manual Registration**: Via `MemoryPluginDiscovery.register()`

Configuration in `config.json`:

```json
{
  "tools": {
    "memory": {
      "provider": "chroma",
      "provider_config": {
        "host": "localhost",
        "port": 8000
      }
    }
  }
}
```

The built-in ChromaDB provider (`markbot.memory.providers.chroma`) supports both local persistent and remote HTTP modes. Install with `pip install -e ".[chroma]"`.

### Configuration

```json
{
  "tools": {
    "memory": {
      "embedding_backend": "openai",
      "embedding_api_key": "",
      "embedding_base_url": "",
      "embedding_model_name": "",
      "memory_compact_threshold": 0,
      "memory_compact_reserve": 10000,
      "memory_summary_enabled": true,
      "context_compact_enabled": true,
      "dream_cron": "0 23 * * *"
    }
  },
  "compaction": {
    "collapse_tool_result_chars": 4000,
    "collapse_head_chars": 900,
    "collapse_tail_chars": 500,
    "micro_compact_keep_turns": 6,
    "auto_compact_keep_recent": 5,
    "snip_keep_messages": 10,
    "threshold_ratio": 0.85,
    "max_compact_output_tokens": 4000,
    "tool_output_inline_chars": 16000,
    "tool_output_preview_chars": 3000,
    "system_prompt_token_budget": 16000
  }
}
```

## Computer Use & Browser Automation

MarkBot includes two powerful automation capabilities for desktop and web interaction.

### Computer Use

Cross-platform desktop control tool that lets the AI agent interact with your computer:

- **Screenshot Capture**: Take screenshots with numbered element overlays (SOM mode) for precise interaction
- **Mouse Control**: Click, double-click, right-click, middle-click, drag, and scroll by element index or pixel coordinates
- **Keyboard Input**: Type text, press key combinations, and use keyboard shortcuts
- **App Management**: List running applications, focus specific apps
- **Element-Based Interaction**: Click by element index (e.g., `element=14`) for reliability — much more robust than pixel coordinates
- **Safety Features**: Blocked destructive key combinations and type patterns, permission-based access control

**Backend Selection** (automatic by default):
- **macOS + cua-driver**: Background operation without stealing the user's cursor or keyboard focus
- **Linux + AT-SPI** (`pyatspi` + `pyautogui`): Foreground operation with real element bounds — preferred when an a11y stack is available
- **Linux/Windows + pyautogui**: Foreground fallback with coordinate-only targeting when AT-SPI is unavailable
- **Override**: Set `MARKBOT_COMPUTER_USE_BACKEND` environment variable (`cua`, `atspi`, `pyautogui`, or `noop`)

**Installation**:

`pyproject.toml` ships the umbrella `.[desktop]` extra plus three
platform-specific aliases (`desktop-macos` / `desktop-linux` /
`desktop-windows`). They all resolve to the same Python deps today; pick
whichever matches your host. Each platform additionally needs a few
system-level packages and (on macOS) a separate binary driver.

```bash
# Cross-platform Python deps — works on macOS, Linux, and Windows
pip install -e ".[desktop]"
# Or pick the platform-specific alias:
pip install -e ".[desktop-linux]"     # adds pyatspi on Linux
pip install -e ".[desktop-macos]"     # macOS only
pip install -e ".[desktop-windows]"   # Windows only
```

**macOS extras** (for the `cua` background backend — optional; if skipped,
markbot falls back to the `pyautogui` backend automatically):

```bash
# cua-driver: ships a private SkyLight-based driver (not Apple-public)
#   Ref: https://github.com/trycua/cua
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"

# Or point markbot at an existing binary
export MARKBOT_CUA_DRIVER_CMD=/absolute/path/to/cua-driver
```

After install, the binary is auto-detected via `shutil.which('cua-driver')`.
Grant the running Terminal / markbot process **Accessibility** and
**Screen Recording** permissions in *System Settings → Privacy & Security*
or the `capture` action will return empty results.

**Linux extras**:

```bash
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install -y \
    python3-tk python3-xlib scrot \
    at-spi2-core at-spi2-atk \
    wmctrl xdotool

# Fedora / RHEL
sudo dnf install -y python3-tkinter python3-xlibb scrot \
                   at-spi2-core at-spi2-atk wmctrl xdotool

# Arch / Manjaro
sudo pacman -S tk python-xlib scrot at-spi2-core wmctrl xdotool
```

- A reachable display is required: set `$DISPLAY` (X11) or `$WAYLAND_DISPLAY`,
  or run under `Xvfb` (markbot auto-probes `Xvfb` on `:99` when neither is set).
- The `atspi` backend needs a running AT-SPI registry daemon — typically
  provided by GNOME, KDE, or `at-spi2-core` plus a session bus.
- `wmctrl` *or* `xdotool` is used by `list_apps`; install at least one.
- Force a specific backend with `MARKBOT_COMPUTER_USE_BACKEND=atspi` /
  `pyautogui` (the former hard-errors if the a11y stack is missing).

**Windows extras**:

- No additional system packages required — `pyautogui` and PowerShell are
  pre-installed on supported Windows versions.
- `list_apps` enumerates processes via PowerShell's `Get-Process`; make sure
  the markbot process has a desktop session (not running headless as a
  Windows service).
- The markbot backend runs **in the foreground** (real cursor / active
  window) and only supports coordinate-based targeting; SOM overlays and
  element indexing are unavailable.

**Configuration** in `config.json`:

```json
{
  "tools": {
    "computer_use": {
      "enable": true,
      "backend": "cua",
      "capture_after_actions": true,
      "max_elements": 200,
      "blocked_key_combos": ["cmd+shift+backspace", "cmd+option+escape"],
      "blocked_type_patterns": ["sudo rm -rf /", "rm -rf ~"]
    }
  }
}
```

#### Vision Routing & Auxiliary Vision Model

Computer Use and Browser tools return screenshots as multimodal content
(text + image blocks). When the primary model in `model_chain` **cannot**
process images (e.g. DeepSeek, Groq text-only models), MarkBot must
downgrade the screenshot to text-only to avoid provider errors.

Three strategies are available, applied in priority order:

1. **Auxiliary vision model** (recommended) — the screenshot is sent to a
   separate vision-capable model for description, and the resulting text is
   fed back to the primary model. This preserves visual information without
   requiring the primary model to support image input.

2. **`text_summary` fallback** (default) — the tool's built-in text summary
   (element list, coordinates, active window title) is used as a lossy
   substitute. No extra model call is made.

3. **`force_text_only`** — explicitly disable image passing for all models,
   even vision-capable ones. Useful for debugging or cost control.

**How the primary model's vision capability is detected** (in order):

- Per-model `capabilities` declaration in `config.json` (preferred — see below)
- Built-in provider/model pattern tables (e.g. `anthropic` → vision, `groq` → no vision)
- Default: assume vision is supported

**Configure an auxiliary vision model** under `agents.defaults` in `config.json`:

```json
{
  "agents": {
    "defaults": {
      "auxiliaryVision": {
        "forceTextOnly": false,
        "provider": "openai",
        "model": "gpt-4o"
      }
    }
  }
}
```

**Declare per-model capabilities** so MarkBot knows whether the primary
model can ingest images directly:

```json
{
  "providers": {
    "deepseek": {
      "api_key": "${DEEPSEEK_API_KEY}",
      "models": [
        {
          "id": "deepseek-chat",
          "name": "deepseek-chat",
          "capabilities": []
        },
        {
          "id": "deepseek-vl",
          "name": "deepseek-vl",
          "capabilities": ["image"]
        }
      ]
    }
  }
}
```

When the primary model lacks the `image` capability and an auxiliary vision
model is configured, the flow is:

```
tool returns screenshot
    ↓
primary model supports image? ──yes──→ pass image to primary model
    ↓ no
auxiliary vision configured? ──no───→ use text_summary (lossy fallback)
    ↓ yes
auxiliary model describes image ────→ feed text description to primary model
```

> **Tip**: Pair a fast, cheap vision model (e.g. `gpt-4o-mini`,
> `qwen2.5-vl-72b-instruct`) as the auxiliary with a strong reasoning model
> (e.g. `deepseek-chat`, `groq/llama-3.3-70b`) as the primary to keep costs
> low while preserving visual context.

### Browser Automation

Playwright-based browser control for web page interaction:

- **Navigate**: Open URLs and initialize browser sessions
- **Snapshot**: Get accessibility tree with interactive element references
- **Click/Type**: Interact with elements by their ref IDs (e.g., `element='e5'`)
- **Scroll/Press**: Scroll pages and press keyboard shortcuts
- **Vision**: Visual verification with screenshots for CAPTCHAs or visual content
- **Session Isolation**: Each task gets an isolated browser session with auto-cleanup
- **Domain Filtering**: Block or allow specific domains via glob patterns

**Installation**:

```bash
pip install playwright
playwright install chromium
```

**Configuration** in `config.json`:

```json
{
  "tools": {
    "browser": {
      "enable": true,
      "backend": "playwright",
      "headless": true,
      "record_session": false,
      "default_timeout": 30,
      "snapshot_max_chars": 8000,
      "blocked_domains": [],
      "allowed_domains": []
    }
  }
}
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

## Deployment

MarkBot can be run in three modes, depending on the use case. All three
read the same `~/.markbot/config.json`, so you can switch modes without
touching your model/key configuration.

### 1. Interactive CLI (REPL)

Best for local development and one-off sessions.

```bash
markbot                       # default workspace (~/.markbot/workspace)
markbot --workspace /path/to/ws
```

The CLI loads the agent loop, reads the model chain from
`agents.defaults.model_chain`, and prompts for input in a Rich REPL.
Press `Ctrl+C` (or send `/stop`) to break out of an in-flight iteration.

### 2. Gateway (background service with channels)

Best for production: long-lived daemon that holds all your chat
channels (DingTalk / Feishu / QQ / WeChat / Email) and a Heartbeat
service that watches the workspace for triggers. The daemon is
controlled by `markbot gateway` and stores its PID under
`~/.markbot/gateway/`.

```bash
markbot gateway start             # daemonize (default)
markbot gateway start --foreground # run in the foreground
markbot gateway start --port 18790 --workspace /srv/markbot
markbot gateway status            # health summary + uptime
markbot gateway stop
markbot gateway restart
```

`markbot gateway start` honours the following env vars and config keys:

| Source | Key | Default | Purpose |
|--------|-----|---------|---------|
| CLI | `--port` | `18790` | Diagnostic / control port advertised to peers |
| YAML | `gateway.host` | `0.0.0.0` | Bind address for embedded services |
| YAML | `gateway.heartbeat.enabled` | `true` | Enable the workspace heartbeat service |
| YAML | `gateway.heartbeat.interval_s` | `1800` | Heartbeat interval (seconds) |
| YAML | `channels.<name>.enabled` | `false` | Toggle each channel individually |

Logs are written to `~/.markbot/gateway/gateway.log` (or
`./markbot-gateway.log` when run in the foreground). The agent loop's
per-event log lives at `~/.markbot/logs/agent.log`. Use
`markbot doctor` if the gateway fails to start.

### 3. Web UI server (FastAPI + WebSocket)

Best for self-hosting a single-user chat UI in the browser. The web
server reuses the same agent loop and tool registry as the gateway,
but exposes a SPA over a WebSocket channel plus a small REST surface
(`/api/status`, `/api/sessions`, `/api/skills`, `/api/cron`, …).

```bash
pip install -e ".[web]"            # install FastAPI + uvicorn
markbot web                        # default: http://127.0.0.1:9120
markbot web --host 0.0.0.0 --port 8080
markbot web --config /etc/markbot/config.json --workspace /srv/markbot
```

Authentication is a single session token that the server generates at
startup. The CLI prints the URL together with the token on first
boot — paste the `?token=…` value (or send the
`x-markbot-session-token` header) to authenticate subsequent
requests. The token persists for the lifetime of the process; call
`markbot web` with a fresh restart to rotate it.

The web server reuses the gateway's tool stack (web tools, browser,
computer use, MCP) and the same `~/.markbot/workspace` directory, so
skills and memory are shared between modes. To run **gateway + web
side by side**, start the gateway first, then run `markbot web` on a
different port.

### Docker / systemd tips

A minimal `Dockerfile` and a `markbot.service` unit are not shipped
out of the box, but the daemon maps cleanly onto either:

```bash
# systemd unit (drop into /etc/systemd/system/markbot.service)
[Service]
ExecStart=/usr/local/bin/markbot gateway start --foreground
Restart=on-failure
User=markbot
WorkingDirectory=/var/lib/markbot
Environment=MARKBOT_CONFIG=/etc/markbot/config.json
```

```dockerfile
# minimal Dockerfile
FROM python:3.12-slim
RUN pip install markbot[web,desktop-linux,chroma]
COPY config.json /etc/markbot/config.json
ENV MARKBOT_CONFIG=/etc/markbot/config.json
EXPOSE 9120
CMD ["markbot", "web", "--host", "0.0.0.0", "--port", "9120"]
```

### Environment variables

| Variable | Effect |
|----------|--------|
| `MARKBOT_CONFIG` | Override the config file path (default `~/.markbot/config.json`) |
| `MARKBOT_WORKSPACE` | Override the workspace directory |
| `MARKBOT_COMPUTER_USE_BACKEND` | Force `cua` / `atspi` / `pyautogui` / `noop` |
| `MARKBOT_CUA_DRIVER_CMD` | Path to a pre-installed `cua-driver` binary |
| `MARKBOT_VISION_FORCE_TEXT_ONLY` | `1`/`true` = force all multimodal tool results to text-only (skip images); `0`/`false` = allow images. Overrides `agents.defaults.auxiliaryVision.forceTextOnly` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / … | Provider keys (used as fallbacks for `${...}` substitutions in `config.json`) |

## Development

### Project Structure

```
markbot/
├── agent/              # Core agent loop and processing
│   ├── loop.py             # Main agent processing engine
│   ├── iteration.py        # Iteration runner for agent loop
│   ├── tool_binder.py      # Tool registration and binding
│   ├── compact.py          # 4-tier progressive compaction
│   ├── context.py          # Context builder for agent prompts
│   ├── container.py        # Agent context container
│   ├── cost.py             # Token & cost tracking with budget control
│   ├── tokens.py           # Token estimation
│   ├── tool_output.py      # Tool output offloading + head+tail collapse
│   ├── stream.py           # Stream filtering (think-tag removal)
│   ├── stream_scrubber.py  # Stream scrubber for memory-context fencing
│   ├── anthropic_breakpoints.py # Anthropic cache_control breakpoint strategy
│   ├── cache_chip.py       # Per-block cache chip / prefix-cache bookkeeping
│   ├── cache_discipline.py # Cache discipline / TTL policy
│   ├── cache_protocol.py   # Provider-agnostic cache protocol types
│   ├── prefix_cache.py     # Cross-provider prefix-cache helpers
│   ├── llm_response_cache.py # LLM response cache
│   ├── token_estimate_cache.py # Memoized token estimates
│   ├── prompt_persist.py   # Persist & restore raw prompt payloads
│   ├── turn_metadata.py    # Per-turn metadata (cache hits, usage, etc.)
│   ├── pipeline/           # Message pipeline middleware
│   │   ├── engine.py       # Pipeline engine
│   │   └── middleware.py   # Built-in middleware (QuestionResponse, MemoryLifecycle)
│   ├── subagent/           # Background task delegation
│   │   ├── capability.py   # CapabilityToken for delegation
│   │   ├── manager.py      # Subagent manager
│   │   ├── progress.py     # Progress tracking
│   │   ├── spawn.py        # Spawn tool
│   │   └── tools.py        # Check/List subagent tools
│   ├── mcp/                # MCP protocol support
│   │   └── manager.py      # MCP connection manager
│   ├── hooks/              # Agent lifecycle hooks
│   │   ├── bootstrap.py    # Bootstrap hooks
│   │   └── compaction.py   # Compaction hooks
│   └── services/           # Agent services
│       ├── executor.py     # Tool execution service
│       └── interaction.py  # Interaction logger
├── channels/           # Multi-channel messaging
│   ├── base.py         # BaseChannel ABC
│   ├── manager.py      # ChannelManager (health checks + auto-restart)
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
│   ├── mcp.py          # MCP client tool wrapper
│   ├── browser.py      # Playwright browser automation (10 tools)
│   └── computer_use/   # Cross-platform desktop control
│       ├── tool.py             # ComputerUseTool
│       ├── backend.py          # Abstract backend interface
│       ├── cua_backend.py      # macOS cua-driver backend
│       ├── pyautogui_backend.py # Cross-platform pyautogui backend
│       ├── atspi_backend.py    # Linux AT-SPI a11y backend
│       ├── noop_backend.py     # Testing stub backend
│       ├── schema.py           # Tool schema definition
│       └── vision_routing.py   # Vision model routing
├── skills/             # Skill system
│   ├── core/           # Skill framework
│   │   ├── loader.py   # Skill loading
│   │   ├── registry.py # Skill registry
│   │   ├── scanner.py  # Security scanning
│   │   ├── guardrail.py # Safety guardrails
│   │   ├── sandbox.py  # Sandboxed execution
│   │   ├── config.py   # Config resolution
│   │   ├── preamble.py # Skill preamble injection
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
│   ├── registry.py     # ProviderSpec registry (28 providers)
│   ├── fallback.py     # FallbackManager with circuit breaker
│   ├── errors.py       # Provider error taxonomy
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
│   ├── manager.py      # Main MemoryManager (file-backed + vector index)
│   ├── longterm.py     # Long-term vector memory (hybrid semantic search)
│   ├── consolidation.py # Vector consolidation (dedup, decay, promotion)
│   ├── provider.py     # MemoryProvider ABC
│   ├── daily_log.py    # Daily log management
│   ├── encoder.py      # Encoding helpers
│   ├── embedder.py     # Layered embedder (openai / sentence-transformers / hashing)
│   ├── vectorstore.py          # Vector store protocol
│   ├── vectorstore_factory.py  # Vector store factory (sqlite / chroma)
│   ├── scanner.py      # Security scanner
│   ├── fencing.py      # Context fencing
│   ├── tool.py         # MemoryStore
│   ├── plugins/        # Memory plugin discovery
│   │   └── discovery.py # MemoryPluginDiscovery (entry points, naming, manual)
│   ├── providers/      # Memory provider implementations
│   │   └── chroma.py   # ChromaDB vector memory provider
│   └── vectorstores/   # Built-in vector store implementations
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
│   ├── tools.py        # Autopilot tools (7 tools)
│   ├── types.py        # Task types
│   └── verification.py # Verification system
├── cli/                # Command-line interface
│   ├── commands.py     # Main CLI entry point
│   ├── runtime.py      # Provider/factory wiring used by subcommands
│   ├── daemon.py       # Gateway daemonization (start/stop/status)
│   ├── ui.py           # Rich UI helpers + banner
│   ├── stream.py       # Stream rendering
│   ├── progress.py     # Progress indicators
│   ├── onboard.py      # Interactive onboarding
│   ├── doctor.py       # Diagnostic commands
│   ├── skills.py       # Skill management commands
│   ├── autopilot.py    # Autopilot commands
│   ├── models.py       # Model suggestions
│   ├── slash_commands/ # Slash command routing
│   │   ├── router.py   # CommandRouter
│   │   └── builtin.py  # Built-in commands
│   └── groups/         # Top-level command groups
│       ├── agent.py        # `markbot agent` subcommands
│       ├── channels.py     # `markbot channels` subcommands
│       ├── config.py       # `markbot config` subcommands
│       ├── gateway.py      # `markbot gateway` lifecycle
│       ├── onboard.py      # `markbot onboard` wizard
│       ├── plugins.py      # `markbot plugins` (skill plugins)
│       ├── provider.py     # `markbot provider` subcommands
│       ├── status.py       # `markbot status`
│       └── web.py          # `markbot web` UI server
├── web/                # Web UI server (FastAPI)
│   ├── server.py       # App factory + WebSocket chat
│   ├── auth.py         # Token-based auth middleware
│   ├── store.py        # Web session store
│   ├── routers/        # REST routers (status/config/sessions/...)
│   └── static/         # Compiled SPA assets
├── types/              # Type definitions
│   ├── exceptions.py   # Custom exceptions
│   ├── permission.py   # Permission types
│   ├── protocols.py    # Protocol definitions
│   ├── skill.py        # Skill types
│   └── tool.py         # Tool types
├── utils/              # Utility functions
│   ├── constants.py    # Shared constants
│   ├── helpers.py      # Helper functions
│   ├── tokens.py       # Token utilities
│   ├── atomic.py       # Atomic file write helpers
│   ├── ssrf.py         # SSRF guard for outbound HTTP
│   └── website_policy.py # Per-domain website policy (allow/deny/proxy)
├── log/                # Logging
│   ├── core.py         # Core logging (loguru)
│   ├── filter.py       # Log filters
│   ├── format.py       # Log formatters
│   └── redact.py       # Sensitive-data redaction
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
