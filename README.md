# MarkBot 🦞

> Version 2.2.4

An advanced AI-powered automation and development assistant designed for developers and power users. MarkBot excels at complex task planning and software development, combining the best features of modern AI assistants with specialized capabilities for technical workflows.

## Core Strengths

- **Multi-Model Support with Auto-Failover**: Configure multiple LLM providers in a priority chain. When the primary model fails or is overloaded, MarkBot automatically falls back to the next model.
- **Task Planning & Orchestration**: Break down complex projects into manageable steps, track progress, and coordinate multiple sub-tasks autonomously
- **Software Development**: Write, review, debug, and refactor code with deep understanding of project context and best practices
- **ReMeLight Memory System**: Advanced memory management powered by [reme-ai](https://github.com/remember-ai/reme-ai), with compaction, summarization, and semantic search for context-aware responses
- **Extensible Architecture**: Customize and extend capabilities through a powerful skills system with security scanning and guardrails

## Features

- **Multiple LLM Providers**: Anthropic, OpenAI, Azure OpenAI, DeepSeek, OpenRouter, Gemini, Moonshot, Zhipu, DashScope, Groq, and more (30+ providers supported)
- **Multi-Model Chain with Auto-Failover**: Configure multiple models in priority chain with automatic failover on errors or overload
- **Multi-Channel Support**: DingTalk, Feishu, QQ, WeChat (Weixin), Email, with auto-reconnect and health monitoring
- **ReMeLight Memory**: Advanced memory management with compaction, summarization, semantic search, and Dream optimization
- **4-Tier Progressive Compaction**: Context Collapse → Micro-Compact → Auto-Compaction → History Snip, escalating only when needed
- **Token & Cost Tracking**: Real-time token usage monitoring with cache token support and per-model pricing
- **Budget Control**: Configurable per-session budget caps with custom pricing overrides
- **Skills System**: Modular skill framework with security scanning, guardrails, and sandboxed execution
- **Cron Jobs**: Schedule and automate recurring tasks with precision
- **Dream Service**: Periodic AI-driven memory optimization on a cron schedule
- **MCP Support**: Model Context Protocol for seamless tool integration (stdio, SSE, streamable HTTP)
- **Sub-Agent Architecture**: Delegate specialized tasks with capability-based delegation tokens (CapabilityToken)
- **Web Integration**: Built-in web browsing, content extraction, and search (Brave, Tavily, DuckDuckGo, SearXNG, Jina)
- **Permission System**: Configurable permission modes (default, plan, accept_edits, bypass, auto) with per-tool allow/deny/ask policies
- **Slash Commands**: Built-in commands like `/new`, `/compact`, `/stop`, `/status`, and more
- **Event Bus**: Event-driven architecture for message passing and component communication
- **Context Explorer**: Explore project context with semantic search and catalog
- **Todo Management**: Built-in persistent todo tracking tool for task management
- **Codebase Exploration**: Understand project structure and code context with deep exploration tools
- **Voice Transcription**: Audio transcription via Groq Whisper integration
- **Interaction Logging**: Full audit trail of LLM request/response pairs for analysis

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
│  │           ReMeLight Memory System                           │    │
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
│  Retryable: 429, 529, 500-504, timeout, overloaded                  │
│  Unavailable: 401-403, 402, model not found                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Providers                                      │
│            (Anthropic, OpenAI, Azure, DeepSeek, etc.)               │
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
```

## Supported LLM Providers

MarkBot supports 30+ LLM providers out of the box:

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
| **vLLM** | High-throughput local inference |
| **Ollama** | Local model runner |
| **OpenVINO Model Server** | Intel-optimized inference |
| **Custom** | Any OpenAI-compatible endpoint |

### Auxiliary
| Provider | Description |
|----------|-------------|
| **Groq** | Fast inference + Whisper transcription |

## Supported Channels

MarkBot can integrate with multiple messaging platforms:

| Channel | Description | Status |
|---------|-------------|--------|
| **DingTalk** | Alibaba DingTalk bot | ✅ Supported |
| **Feishu/Lark** | ByteDance Feishu/Lark | ✅ Supported |
| **QQ** | Tencent QQ bot | ✅ Supported |
| **WeChat** | WeChat integration | ✅ Supported |
| **Email** | SMTP/IMAP email | ✅ Supported |

Channels are auto-discovered via `pkgutil` scanning and `entry_points` plugin system. Built-in channels take priority over external plugins. Each channel has managed lifecycle with health checks and exponential-backoff auto-reconnect.

## Quick Start

### Step 1: Initialize Configuration

```bash
# Recommended for first-time users: linear guided setup
markbot onboard --guided

# Or use the menu-driven wizard
markbot onboard --wizard

# Non-interactive (for Docker/CI/scripts)
markbot onboard --defaults
```

The guided wizard will walk you through:
1. **Security Notice** — understand the risks of AI with file/command access
2. **Provider Setup** — choose and configure your LLM provider (API key, base URL, etc.)
3. **Model Configuration** — add models for your provider
4. **Model Chain** — set up priority chain with auto-failover
5. **Channel Setup** — configure messaging platforms (optional)
6. **Agent & Tools** — fine-tune agent behavior and tool permissions

### Step 2: Configure Your Provider

Edit `~/.markbot/config.json`:

```json
{
  "providers": {
    "anthropic": {
      "apiKey": "sk-ant-...",
      "models": [
        {
          "id": "claude-sonnet",
          "name": "claude-3-5-sonnet-20241022",
          "maxTokens": 8192,
          "contextWindow": 128000,
          "temperature": 0.7
        }
      ]
    }
  },
  "agents": {
    "defaults": {
      "modelChain": ["anthropic/claude-sonnet"],
      "timezone": "Asia/Shanghai"
    }
  }
}
```

Or use environment variables:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

#### Multi-Model Chain Configuration

MarkBot supports configuring multiple models with automatic failover:

```json
{
  "providers": {
    "anthropic": {
      "apiKey": "sk-ant-...",
      "models": [
        {
          "id": "claude-opus",
          "name": "claude-3-opus-20240229",
          "maxTokens": 4096,
          "contextWindow": 200000
        }
      ]
    },
    "deepseek": {
      "apiKey": "sk-...",
      "models": [
        {
          "id": "deepseek-chat",
          "name": "deepseek-chat",
          "maxTokens": 8192,
          "contextWindow": 65536
        }
      ]
    }
  },
  "agents": {
    "defaults": {
      "modelChain": [
        "anthropic/claude-opus",
        "deepseek/deepseek-chat"
      ],
      "maxToolIterations": 30,
      "contextWindowTokens": 120000
    }
  }
}
```

When the primary model fails or is overloaded, MarkBot automatically falls back to the next model in the chain. The FallbackManager distinguishes between retryable errors (429, 529, 500-504, timeout, overloaded) and model-unavailable errors (401-403, 402, model not found), skipping to the next model appropriately.

### Step 3: Start Chatting

```bash
markbot agent
```

Or send a single message:

```bash
markbot agent -m "Hello!"
```

### Step 4: Start Gateway Server (Optional)

For multi-channel support, start the gateway server:

```bash
markbot gateway start
```

## Commands

### Gateway Management

```bash
markbot gateway start    # Start the gateway server
markbot gateway status   # Check gateway status
markbot gateway stop     # Stop the gateway
markbot gateway restart  # Restart the gateway
```

### Agent Commands

```bash
markbot agent                    # Start interactive chat
markbot agent -m "message"       # Send single message
```

### Skill Management

```bash
markbot skills list              # List all skills
markbot skills create            # Create a new skill
markbot skills validate          # Validate skill format
markbot skills package           # Package skill for distribution
```

### Session Management

```bash
markbot session list             # List all sessions
markbot session show <id>        # Show session details
markbot session delete <id>      # Delete a session
markbot session export <id>      # Export session to file
```

### Configuration

```bash
markbot config list                              # List all config
markbot config get agents.defaults.modelChain     # Get config value
markbot config set agents.defaults.timezone Asia/Shanghai  # Set config value
markbot config provider                          # Interactively configure LLM provider
markbot config channel                           # Interactively configure channels
markbot config show                              # Display rich config summary
```

### Diagnostics

```bash
markbot doctor                    # Run diagnostic checks on installation
markbot doctor --deep             # Extra: test provider connectivity & channel reachability
markbot doctor fix                # Apply automated safe fixes
markbot doctor fix --dry-run      # Preview fixes without writing files
markbot doctor fix --list         # List available fix ids
markbot doctor fix --only clean-stale-pid -y  # Run specific fix with confirmation
```

`doctor` checks: environment, config validity, model chain, provider credentials, workspace, channels, MCP servers, skills, memory/embedding, cron jobs, sessions, gateway status, disk space, and optional dependencies.

`doctor fix` supports:
- **Safe fixes** (default): ensure data/workspace dirs, clean stale PID files
- **Risky fixes** (require `-y`): seed empty jobs.json, trim oversized gateway logs

All file modifications are backed up under `~/.markbot/doctor-fix-backups/` before applying.

### Other Commands

```bash
markbot onboard --guided          # Linear step-by-step guided setup (recommended)
markbot onboard --wizard          # Menu-driven interactive wizard
markbot onboard --defaults        # Non-interactive, use all defaults
markbot status                    # Show system status
markbot version                   # Show version info
```

### In-Chat Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new session with memory consolidation |
| `/compact` | Manually trigger memory compaction |
| `/compact_str` | View current compressed summary |
| `/clear` | Clear history and compressed summary |
| `/stop` | Stop current request and cancel subagents |
| `/restart` | Restart the bot in-place |
| `/status` | Show bot status (model, tokens, session info) |
| `/help` | Show available commands |

## Memory System

MarkBot uses a **ReMeLight memory architecture** powered by [reme-ai](https://github.com/remember-ai/reme-ai) with three core components:

| Component | Purpose | Storage |
|-----------|---------|---------|
| **MEMORY.md** | Curated long-term memory (preferences, decisions, lessons) | Markdown file |
| **memory/YYYY-MM-DD.md** | Daily conversation summaries | Markdown files |
| **Compressed Summary** | Context window management (in-memory) | Per session |

### Memory Operations

- **Automatic Compaction**: When context exceeds the configured threshold ratio (default 85% of window), older messages are summarized
- **Async Summarization**: After each turn, background task writes daily notes
- **Semantic Search**: `memory_search` tool searches MEMORY.md and daily notes via vector search using configurable embedding backend (OpenAI or Ollama)
- **Bootstrap Guidance**: First interaction triggers BOOTSTRAP.md setup
- **Dream Optimization**: Periodic AI-driven memory consolidation on a cron schedule (default: daily at 23:00)
- **Memory Self-Management**: Tools for `memory_save`, `memory_forget`, `memory_list`, and `memory_dream`

### Embedding Configuration

```json
{
  "tools": {
    "memory": {
      "embeddingBackend": "openai",
      "embeddingApiKey": "",
      "embeddingBaseUrl": "",
      "embeddingModelName": "",
      "memoryCompactThreshold": 0,
      "memoryCompactReserve": 10000,
      "memorySummaryEnabled": true,
      "contextCompactEnabled": true,
      "dreamCron": "0 23 * * *"
    }
  }
}
```

### Memory Context Injection

Memory context is automatically injected into the system prompt:

```
System Prompt
├── Identity & Bootstrap Files (AGENTS.md, SOUL.md, PROFILE.md)
├── Skills (always-active + summary)
├── Memory Context          ← Injected here
│   ├── MEMORY.md           # Curated long-term memory
│   └── Compressed Summary  # Current conversation summary
└── Runtime Context
```

This ensures the agent has access to relevant context from all memory layers when processing each message.

## 4-Tier Progressive Compaction

MarkBot uses a 4-tier progressive compression strategy that escalates only when needed:

| Level | Action | Description |
|-------|--------|-------------|
| **1** | Context Collapse | Truncate individual `tool_result` blocks exceeding char limit (default: 4,000) |
| **2** | Micro-Compact | Remove old `tool_result` content, keep recent N turns (default: 6) |
| **3** | Auto-Compaction | LLM generates summary to replace old history (keep recent 5 pairs) |
| **4** | History Snip | Force-drop oldest messages (last resort, keep minimum 10) |

### Compaction Configuration

```json
{
  "compaction": {
    "collapseToolResultChars": 4000,
    "microCompactKeepTurns": 6,
    "autoCompactKeepRecent": 5,
    "snipKeepMessages": 10,
    "thresholdRatio": 0.85,
    "maxCompactOutputTokens": 4000,
    "reservedOutputTokens": 8000,
    "autoCompactBuffer": 13000
  }
}
```

## Subagent System

MarkBot supports spawning background subagents for complex, time-consuming tasks.

### Capability-Based Delegation

Subagents use **CapabilityToken** — an AI-declared capability boundary that explicitly states what tools/actions a subagent may perform:

```python
CapabilityToken(
    allowed_tools=("read_file", "glob", "grep"),
    forbidden_tools=("exec", "write_file"),
    max_iterations=10,
    description="Read-only code review",
)
```

A built-in `read_only()` factory provides a common read-only capability profile.

### Spawning a Subagent

```
spawn(task="Analyze the codebase and generate documentation", label="docs-gen")
```

Returns: `Subagent [docs-gen] started (id: abc123). I'll notify you when it completes.`

### Progress Tracking

Monitor subagent execution in real-time:

```
# Check status
check_subagent(task_id="abc123", action="status")

# View full output
check_subagent(task_id="abc123", action="output")

# View last 50 lines
check_subagent(task_id="abc123", action="tail")

# List all active subagents
list_subagents()
```

### Output Files

Subagent outputs are saved to disk for later reference:

```
.markbot/tasks/
├── abc123.output    # Task output log
├── def456.output    # Another task
└── ...
```

Output files persist after task completion, allowing you to review results at any time.

## Tools

MarkBot provides a comprehensive set of built-in tools:

### Filesystem Tools
| Tool | Description |
|------|-------------|
| `read_file` | Read file contents with line-based pagination |
| `write_file` | Write content to files (with automatic backup) |
| `edit_file` | Search-and-replace editing of files |
| `list_dir` | List directory contents |
| `delete_file` | Delete files with safety checks |

### Search Tools
| Tool | Description |
|------|-------------|
| `glob` | Find files by name pattern |
| `grep` | Search file contents with regex |
| `explore` | Deep code exploration with AST parsing and result caching |

### Execution Tools
| Tool | Description |
|------|-------------|
| `exec` | Execute shell commands with rate limiting and security checks |

### Web Tools
| Tool | Description |
|------|-------------|
| `web_search` | Search the web (Brave, Tavily, DuckDuckGo, SearXNG, Jina) |
| `web_extract` | Extract and optionally summarize content from URLs |
| `web_fetch` | Fetch single URL content (legacy alias for web_extract) |

### Memory Tools
| Tool | Description |
|------|-------------|
| `memory_search` | Semantic/full-text search in MEMORY.md and daily logs |
| `memory_save` | Save information to long-term memory |
| `memory_forget` | Remove information from long-term memory |
| `memory_list` | List memory entries |
| `memory_dream` | Trigger Dream memory optimization |

### Context Tools
| Tool | Description |
|------|-------------|
| `explore_context_catalog` | View available context sources (table of contents) |
| `search_context` | Search within specific context source |
| `load_context` | Load full content from a specific entry |

### Interaction Tools
| Tool | Description |
|------|-------------|
| `message` | Send messages to users (with file/media attachment support) |
| `ask_user_question` | Ask structured questions with predefined options |

### Cognitive Tools
| Tool | Description |
|------|-------------|
| `think` | Unified thinking tool with modes: analyze, challenge, inversion, first-principles, plan, evaluate, learn, improve, code-analysis, research-plan |

### Scheduling Tools
| Tool | Description |
|------|-------------|
| `cron` | Schedule reminders and recurring tasks |

### MCP Tools
| Tool | Description |
|------|-------------|
| `mcp_*` | Dynamically registered from configured MCP servers |

### Other Tools
| Tool | Description |
|------|-------------|
| `todo` | Persistent task tracking with status/priority filtering |
| `spawn` | Spawn background subagents |
| `check_subagent` | Monitor subagent progress |
| `list_subagents` | List active subagents |

## Skills

Skills extend MarkBot's capabilities with specialized instructions and tools.

### Built-in Skills

| Skill | Description |
|-------|-------------|
| `skill-creator` | Create new skills from scratch with evaluation and benchmarking |
| `summarize` | Summarize URLs, files, YouTube videos |
| `memory` | ReMeLight memory system with compaction, summarization, and semantic search |
| `cron` | Schedule reminders and recurring tasks |
| `github` | GitHub interaction via `gh` CLI |
| `tmux` | Remote-control tmux sessions |
| `weather` | Weather information using wttr.in and Open-Meteo |
| `clawhub` | Search and install skills from ClawHub registry |
| `surprise-me` | Create delightful unexpected experiences by combining skills dynamically |

### Skill Security

Skills go through a multi-layer security system:

1. **Security Scanner** — Static analysis to detect dangerous code patterns (exfiltration, prompt injection, destructive ops, reverse shells, obfuscation, privilege escalation, etc.)
2. **Trust-Level Policy** — Access control based on skill source:

   | Trust Level | Safe | Caution | Dangerous |
   |-------------|------|---------|-----------|
   | `builtin` | Allow | Allow | Allow |
   | `workspace` | Allow | Allow | Block |
   | `external` | Allow | Block | Block |
   | `agent-created` | Allow | Allow | Ask |

3. **Guardrail System** — Post-execution validation that checks agent behavior against skill-defined constraints
4. **Sandbox** — Restricted execution environment with resource limits, file access control, and timeout management

### Creating Custom Skills

Skills are directories containing a `SKILL.md` file:

```
~/.markbot/workspace/skills/my-skill/
└── SKILL.md          # Skill definition
```

Example `SKILL.md`:

```yaml
---
name: my-skill
description: What this skill does
---

# My Skill

Instructions for using this skill...
```

## Permission System

MarkBot provides a configurable permission system for tool access control:

| Mode | Description |
|------|-------------|
| `default` | Standard permission checks |
| `plan` | Planning mode — read-only by default |
| `accept_edits` | Auto-accept file edit operations |
| `bypass_permissions` | Skip all permission checks |
| `auto` | Automatic permission decisions based on context |

Per-tool policies can be configured with `always_allow`, `always_deny`, and `always_ask` sets.

## Configuration Reference

### Agent Defaults

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.markbot/workspace",
      "modelChain": ["anthropic/claude-sonnet"],
      "maxTokens": 8192,
      "contextWindowTokens": 65536,
      "temperature": 0.1,
      "maxToolIterations": 40,
      "reasoningEffort": null,
      "timezone": "UTC"
    }
  }
}
```

`reasoningEffort` enables LLM thinking mode with values: `low`, `medium`, `high`.

### Budget Configuration

```json
{
  "budget": {
    "enabled": true,
    "maxBudgetUsd": null,
    "warnThresholdUsd": 0.5,
    "customPricing": null
  }
}
```

Custom pricing allows per-model price overrides:

```json
{
  "customPricing": {
    "my-model": {
      "inputPer1k": 0.003,
      "outputPer1k": 0.006
    }
  }
}
```

### Web Tools Configuration

```json
{
  "tools": {
    "web": {
      "proxy": null,
      "search": {
        "provider": "brave",
        "apiKey": "",
        "baseUrl": "",
        "maxResults": 5
      }
    }
  }
}
```

Supported search providers: `brave`, `tavily`, `duckduckgo`, `searxng`, `jina`.

### Shell Exec Configuration

```json
{
  "tools": {
    "exec": {
      "enable": true,
      "timeout": 60,
      "pathAppend": "",
      "allowedInternalIps": []
    }
  }
}
```

### Filesystem Configuration

```json
{
  "tools": {
    "filesystem": {
      "backupDir": "~/.markbot/.markbot_backups",
      "maxBackups": 50,
      "safeDelete": true
    }
  }
}
```

When `safeDelete` is `true`, deleted files are moved to `backupDir` (recycle bin mode). When `false`, files are permanently deleted.

### MCP Server Configuration

```json
{
  "tools": {
    "mcpServers": {
      "my-server": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
        "env": {},
        "toolTimeout": 30,
        "enabledTools": ["*"]
      },
      "my-http-server": {
        "type": "sse",
        "url": "http://localhost:3000/sse",
        "headers": {},
        "toolTimeout": 30,
        "enabledTools": ["*"]
      }
    }
  }
}
```

Supported MCP transport types: `stdio`, `sse`, `streamableHttp`.

`enabledTools` accepts raw MCP tool names or wrapped `mcp_<server>_<tool>` names. Use `["*"]` for all tools, `[]` for no tools.

### Gateway Configuration

```json
{
  "gateway": {
    "host": "0.0.0.0",
    "port": 18790,
    "heartbeat": {
      "enabled": true,
      "intervalS": 1800,
      "keepRecentMessages": 8
    }
  }
}
```

## Project Structure

```
markbot/
├── agent/
│   ├── __init__.py              # Agent core exports
│   ├── loop.py                  # Main agent execution loop
│   ├── context.py               # Context building with memory injection
│   ├── compact.py               # 4-tier progressive compaction
│   ├── cost.py                  # Cost tracking & budget control
│   ├── stream.py                # Stream filter (think-tag stripping)
│   ├── tokens.py                # Token usage tracking & estimation
│   ├── tool_binder.py           # Tool registration for agent loop
│   ├── hooks/
│   │   ├── bootstrap.py         # First-run bootstrap hook
│   │   └── compaction.py        # Memory compaction hook
│   ├── mcp/
│   │   └── manager.py           # MCP server connection manager
│   ├── pipeline/
│   │   ├── engine.py            # Message processing pipeline
│   │   └── middleware.py        # Built-in middleware (QuestionResponse, MemoryLifecycle)
│   ├── services/
│   │   ├── executor.py          # Tool execution (truncation, sanitization, persistence)
│   │   └── interaction.py       # Interaction audit logger
│   └── subagent/
│       ├── __init__.py          # Subagent exports
│       ├── capability.py        # CapabilityToken for delegation control
│       ├── manager.py           # Subagent manager
│       ├── spawn.py             # Spawn subagent tool
│       ├── progress.py          # Progress tracking
│       └── tools.py             # Subagent tools
├── bus/
│   ├── events.py                # Event definitions (InboundMessage, OutboundMessage)
│   └── queue.py                 # Message bus (inbound/outbound queues)
├── channels/
│   ├── base.py                  # Channel base class
│   ├── dingtalk.py              # DingTalk
│   ├── feishu.py                # Feishu/Lark
│   ├── weixin.py                # WeChat
│   ├── qq.py                    # QQ
│   ├── email.py                 # Email
│   ├── manager.py               # Channel manager (start/stop/dispatch)
│   ├── lifecycle.py             # Health checks, auto-reconnect, retry policy
│   └── discovery.py             # Auto-discovery (pkgutil + entry_points)
├── cli/
│   ├── commands.py              # Main CLI commands (typer app)
│   ├── onboard.py               # Interactive onboarding wizard
│   ├── doctor.py                # Diagnostic checks & fixes
│   ├── skills.py                # Skill management CLI
│   ├── stream.py                # Stream rendering (ThinkingSpinner, StreamRenderer)
│   ├── models.py                # CLI model helpers
│   └── slash_commands/
│       ├── builtin.py           # Built-in slash commands
│       └── router.py            # Command routing (priority/exact/prefix/intercept)
├── config/
│   ├── schema.py                # Pydantic config schema
│   ├── loader.py                # Config loader
│   └── paths.py                 # Path utilities
├── memory/
│   ├── base.py                  # Base memory manager
│   ├── manager.py               # ReMeLight memory manager
│   └── daily_log.py             # Daily conversation logs
├── providers/
│   ├── base.py                  # Provider base class
│   ├── anthropic.py             # Anthropic (Claude) native SDK
│   ├── openai_compat.py         # OpenAI-compatible providers
│   ├── azure_openai.py          # Azure OpenAI
│   ├── openai_codex.py          # OpenAI Codex (OAuth)
│   ├── fallback.py              # Multi-model fallback chain
│   ├── transcription.py         # Groq Whisper voice transcription
│   └── registry.py              # Provider registry (ProviderSpec)
├── schedule/
│   ├── cron.py                  # Cron job scheduler
│   ├── dream.py                 # Dream service (memory optimization)
│   ├── evaluator.py             # Cron expression evaluator
│   └── heartbeat.py             # Heartbeat service
├── session/
│   ├── app_state.py             # App state provider (React-like API)
│   ├── session.py               # Session management (JSONL persistence)
│   ├── store.py                 # State store
│   └── types.py                 # State types
├── skills/
│   ├── __init__.py              # Unified skills exports
│   ├── loader.py                # Skill loader
│   ├── registry.py              # Skill registry
│   ├── tool.py                  # Skill tool, SkillViewTool, SkillsListTool
│   ├── sandbox.py               # Sandboxed execution environment
│   ├── scanner.py               # Security scanner (static analysis)
│   ├── guardrail.py             # Post-execution guardrail validation
│   ├── config.py                # Skill config resolver
│   ├── manage.py                # SkillManageTool
│   ├── helpers.py               # Skill helper utilities
│   ├── skill-creator/           # Skill creation with evaluation
│   ├── summarize/               # Content summarization
│   ├── memory/                  # Memory management
│   ├── cron/                    # Cron scheduling
│   ├── github/                  # GitHub integration
│   ├── tmux/                    # Tmux control
│   ├── weather/                 # Weather info
│   ├── clawhub/                 # ClawHub registry
│   └── surprise-me/             # Dynamic skill combination
├── templates/
│   ├── AGENTS.md                # Agent instructions
│   ├── TOOLS.md                 # Tool descriptions
│   ├── SOUL.md                  # Agent personality
│   ├── USER.md                  # User context
│   ├── MEMORY.md                # Memory guidelines
│   ├── HEARTBEAT.md             # Heartbeat config
│   ├── PROFILE.md               # Agent profile
│   ├── BOOTSTRAP.md             # First-run bootstrap
│   └── agents/                  # Additional agent guides
│       ├── GROUP_CHAT_GUIDE.md
│       ├── SEARCH_PROTOCOL.md
│       └── HEARTBEAT_GUIDE.md
├── tools/
│   ├── base.py                  # Tool base class
│   ├── registry.py              # Tool registry
│   ├── filesystem.py            # File operations (read, write, edit, list, delete)
│   ├── shell.py                 # Command execution with security
│   ├── web.py                   # Web search, extract, fetch
│   ├── search.py                # Glob and Grep search
│   ├── memory.py                # Memory search
│   ├── memory_tools.py          # Memory self-management (save, forget, list, dream)
│   ├── mcp.py                   # MCP client & tool wrapping
│   ├── think.py                 # Unified thinking/planning/reflection tool
│   ├── explore.py               # Deep codebase exploration
│   ├── context_explorer.py      # Context catalog exploration
│   ├── question.py              # Interactive question tool
│   ├── message.py               # Message handling with media support
│   ├── todo.py                  # Persistent todo management
│   └── cron.py                  # Cron job management tool
├── types/
│   ├── tool.py                  # Tool types (ToolDefinition, ToolParameter, ToolContext)
│   ├── skill.py                 # Skill types
│   └── permission.py            # Permission types (PermissionMode, ToolPermissionContext)
└── utils/
    ├── helpers.py               # Helper functions
    ├── constants.py              # Shared constants
    └── network.py               # Network utilities
```

## Development

```bash
# Setup
git clone https://github.com/mickletang/markbot.git
cd markbot
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=markbot

# Format code
ruff format .

# Lint
ruff check .
```

### Adding a New LLM Provider

1. Add a `ProviderSpec` to `markbot/providers/registry.py`
2. Add a field to `ProvidersConfig` in `markbot/config/schema.py`
3. Done! The provider will be auto-discovered

### Adding a New Channel

1. Create a new file in `markbot/channels/`
2. Subclass `BaseChannel` from `markbot/channels/base.py`
3. Implement required methods (`start`, `stop`, `send`, `send_delta`)
4. The channel will be auto-discovered via `markbot/channels/discovery.py`

### Adding a New Tool

1. Create a new file in `markbot/tools/`
2. Subclass `BaseTool` (or `Tool`) from `markbot/tools/base.py`
3. Implement `definition`, `name`, `description`, `parameters`, and `_legacy_execute`
4. Register in `markbot/agent/tool_binder.py`

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

AGPL-3.0 License

Copyright (c) 2026 MarkBot contributors
