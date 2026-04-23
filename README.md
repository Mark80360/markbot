# MarkBot 🦞

An advanced AI-powered automation and development assistant designed for developers and power users. MarkBot excels at complex task planning and software development, combining the best features of modern AI assistants with specialized capabilities for technical workflows.

## Core Strengths

- **Multi-Model Support with Auto-Failover**: Configure multiple LLM providers in a priority chain. When the primary model fails or is overloaded, MarkBot automatically falls back to the next model.
- **Task Planning & Orchestration**: Break down complex projects into manageable steps, track progress, and coordinate multiple sub-tasks autonomously
- **Software Development**: Write, review, debug, and refactor code with deep understanding of project context and best practices
- **Tiered Memory System**: Multi-layered memory architecture (Hot/Warm/Cold) for context-aware responses
- **Extensible Architecture**: Customize and extend capabilities through a powerful skills system

## Features

- **Multiple LLM Providers**: Anthropic, OpenAI, Azure OpenAI, DeepSeek, OpenRouter, Gemini, Moonshot, Zhipu, DashScope, Groq, and more (30+ providers supported)
- **Multi-Model Chain with Auto-Failover**: Configure multiple models in priority chain with automatic failover on errors or overload
- **Multi-Channel Support**: DingTalk, Feishu, QQ, WeChat (Weixin), Email, and more
- **Tiered Memory Architecture**: Hot (working), Warm (session), Cold (persistent) memory layers
- **Token Tracking**: Real-time token usage monitoring with cache token support
- **Conversation Compression**: Automatic summarization of old conversation turns to optimize context
- **Cost Tracking**: Monitor API usage costs with configurable budgets
- **Skills System**: Modular skill framework for adding specialized capabilities
- **Cron Jobs**: Schedule and automate recurring tasks with precision
- **MCP Support**: Model Context Protocol for seamless tool integration
- **Sub-Agent Architecture**: Delegate specialized tasks to focused sub-agents with real-time progress tracking
- **Subagent Progress Tracking**: Monitor subagent execution with activity logs, token counts, and output files
- **Web Integration**: Built-in web browsing, content extraction, and API interaction
- **Command Router**: Built-in commands like `/new`, `/help`, `/stop`
- **Skill Execution**: Run skill scripts in sandboxed environments
- **Event Bus**: Event-driven architecture for message passing and component communication
- **Context Explorer**: Explore project context with semantic search and catalog
- **Todo Management**: Built-in todo tracking tool for task management
- **Codebase Exploration**: Understand project structure and code context with deep exploration tools

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Channels                               │
│  (DingTalk, Feishu, QQ, WeChat, Email, etc.)              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Message Bus                            │
│         (Event Queue, State Management)                    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Agent Loop                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │   Context   │  │   Memory    │  │      Tools         │ │
│  │   Builder   │  │   Manager   │  │   (Filesystem,     │ │
│  │             │  │             │  │    Shell, Web,     │ │
│  │             │  │             │  │    Spawn, etc.)    │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              Tiered Memory System                    │   │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────────────┐    │   │
│  │  │   Hot   │→│  Warm   │→│      Cold       │    │   │
│  │  │(Working) │  │(Session)│  │   (Persistent)  │    │   │
│  │  └─────────┘  └─────────┘  └─────────────────┘    │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │           Token & Cost Management                    │   │
│  │  ┌─────────────────┐  ┌─────────────────────┐    │   │
│  │  │  Token Tracker  │  │    Compactor       │    │   │
│  │  │  (Usage Monitor) │  │ (Context Compress) │    │   │
│  │  │  + Cost Tracker │  │                    │    │   │
│  │  └─────────────────┘  └─────────────────────┘    │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              Subagent System                         │   │
│  │  ┌─────────────┐  ┌─────────────────────┐       │   │
│  │  │   Spawn    │  │  Progress Tracker   │       │   │
│  │  └─────────────┘  └─────────────────────┘       │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Fallback Manager                          │
│         (Multi-Model Chain with Auto-Failover)           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Providers                              │
│        (Anthropic, OpenAI, Azure, DeepSeek, etc.)        │
└─────────────────────────────────────────────────────────────┘
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

## Supported LLM Providers

MarkBot supports 30+ LLM providers out of the box:

### Direct Providers
| Provider | Models | Authentication |
|----------|--------|----------------|
| **Anthropic** | Claude 3.5/3.7 Sonnet, Claude 3 Opus/Haiku | API Key |
| **OpenAI** | GPT-4o, GPT-4, GPT-3.5 | API Key |
| **Azure OpenAI** | GPT-4, GPT-3.5 | Azure credentials |
| **DeepSeek** | DeepSeek-V3, DeepSeek-R1, DeepSeek-Coder | API Key |
| **Gemini** | Gemini Pro, Gemini Ultra | API Key |
| **Moonshot** | Kimi K2.5, Kimi K1.5 | API Key |
| **Zhipu (智谱)** | GLM-4, GLM-3 | API Key |
| **DashScope (通义)** | Qwen2.5, Qwen-Coder | API Key |
| **MiniMax** | MiniMax models | API Key |
| **Mistral** | Mistral Large, Medium, Small | API Key |
| **Step Fun (阶跃星辰)** | Step models | API Key |

### Gateway Services
| Provider | Description |
|----------|-------------|
| **OpenRouter** | Universal gateway to 100+ models |
| **AiHubMix** | OpenAI-compatible gateway |
| **SiliconFlow (硅基流动)** | Chinese model gateway |
| **VolcEngine (火山引擎)** | ByteDance cloud models |
| **VolcEngine Coding Plan** | ByteDance coding-specific models |
| **BytePlus** | VolcEngine international |
| **BytePlus Coding Plan** | BytePlus coding-specific models |

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

## Quick Start

### Step 1: Initialize Configuration

```bash
markbot onboard
```

This interactive wizard will guide you through setting up your workspace and configuring your preferred LLM provider.

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

When the primary model fails or is overloaded, MarkBot automatically falls back to the next model in the chain.

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
markbot config edit                              # Edit config in editor
```

### Other Commands

```bash
markbot onboard                  # Run setup wizard
markbot status                   # Show system status
markbot version                  # Show version info
```

### In-Chat Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new session with memory consolidation |
| `/help` | Show available commands |
| `/stop` | Stop current request |

## Memory System

MarkBot uses a **ReMeLight memory architecture** with three core components:

| Component | Purpose | Storage |
|-----------|---------|---------|
| **MEMORY.md** | Curated long-term memory (preferences, decisions, lessons) | Markdown file |
| **memory/YYYY-MM-DD.md** | Daily conversation summaries | Markdown files |
| **Compressed Summary** | Context window management (in-memory) | Per session |

### Memory Operations

- **Automatic Compaction**: When context exceeds 75% of window, older messages are summarized
- **Async Summarization**: After each turn, background task writes daily notes
- **Semantic Search**: `memory_search` tool searches MEMORY.md and daily notes via vector search
- **Bootstrap Guidance**: First interaction triggers BOOTSTRAP.md setup

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

## Subagent System

MarkBot supports spawning background subagents for complex, time-consuming tasks.

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

## Project Structure

```
markbot/
├── agent/
│   ├── loop.py              # Main agent execution loop
│   ├── context.py           # Context building with memory injection
│   ├── compact.py           # Conversation compression
│   ├── tokens.py            # Token usage tracking
│   ├── cost_tracker.py      # Cost tracking for API usage
│   ├── memory/              # ReMeLight memory system
│   │   ├── manager.py       # Memory manager (compaction, search, summary)
│   │   ├── base.py          # Base memory class
│   │   ├── daily_log.py     # Daily conversation logs
│   │   ├── hooks/           # Memory operation hooks
│   │   │   ├── bootstrap.py     # First-run bootstrap hook
│   │   │   └── compaction.py    # Context compaction hook
│   ├── subagent/            # Subagent system
│   │   ├── manager.py       # Subagent manager
│   │   ├── spawn.py         # Spawn subagent tool
│   │   ├── progress.py      # Progress tracking
│   │   └── tools.py         # Subagent tools
│   ├── services/            # Agent services
│   │   ├── message_pipeline.py  # Message processing pipeline
│   │   ├── middleware.py    # Request middleware
│   │   ├── tool_executor.py # Tool execution service
│   │   └── interaction_log.py   # Interaction logging
│   └── tools/               # Built-in tools
│       ├── filesystem.py    # File operations (Read, Write, Edit, List, Delete)
│       ├── shell.py         # Command execution
│       ├── spawn.py         # Subagent spawning
│       ├── web.py           # Web browsing, search, extraction
│       ├── search.py        # Glob and Grep search
│       ├── memory.py        # Memory search
│       ├── cron.py          # Cron job management
│       ├── mcp.py           # MCP (Model Context Protocol) tools
│       ├── think.py         # Thinking/chain-of-thought tool
│       ├── explore.py       # Codebase exploration
│       ├── question.py      # Interactive question tool
│       ├── context_explorer.py  # Context catalog exploration
│       ├── message.py       # Message handling tools
│       ├── todo.py          # Todo management
│       └── registry.py     # Tool registry
├── bus/                     # Event bus system
│   ├── events.py            # Event definitions
│   └── queue.py             # Message queue
├── channels/                # Channel integrations
│   ├── feishu.py           # Feishu/Lark
│   ├── dingtalk.py         # DingTalk
│   ├── weixin.py           # WeChat
│   ├── qq.py               # QQ
│   ├── email.py            # Email
│   ├── base.py             # Channel base class
│   ├── manager.py          # Channel manager
│   ├── discovery.py        # Channel discovery
│   └── registry.py         # Channel registry
├── providers/               # LLM providers (30+ supported)
│   ├── anthropic.py        # Anthropic (Claude)
│   ├── openai_compat.py    # OpenAI-compatible providers
│   ├── azure_openai.py     # Azure OpenAI
│   ├── openai_codex.py    # OpenAI Codex
│   ├── fallback.py         # Multi-model fallback chain
│   ├── transcription.py    # Voice transcription
│   ├── registry.py         # Provider registry
│   └── base.py             # Provider base class
├── config/                  # Configuration
│   ├── schema.py           # Config schema
│   ├── loader.py           # Config loader
│   └── paths.py            # Path utilities
├── memory/                  # Memory system (legacy alias)
├── scheduling/              # Scheduling system
│   ├── cron.py             # Cron job scheduler
│   ├── evaluator.py        # Cron expression evaluator
│   └── heartbeat.py        # Heartbeat service
├── skills/                  # Built-in skills
│   ├── skill-creator/      # Skill creation with evaluation
│   ├── summarize/          # Content summarization
│   ├── memory/             # Memory management
│   ├── cron/               # Cron scheduling
│   ├── github/             # GitHub integration
│   ├── tmux/               # Tmux control
│   ├── weather/            # Weather info
│   ├── clawhub/            # ClawHub registry
│   ├── surprise-me/       # Dynamic skill combination
│   ├── loader.py           # Skill loader
│   ├── registry.py         # Skill registry
│   ├── tool.py             # Skill tool
│   ├── sandbox.py          # Sandboxed execution
│   └── scanner.py          # Security scanner
├── state/                   # Application state
│   ├── app_state.py        # App state management
│   ├── session.py          # Session management
│   ├── store.py            # State store
│   └── types.py            # State types
├── subagent/                # Subagent system (legacy alias)
├── templates/               # Agent templates
│   ├── AGENTS.md           # Agent instructions
│   ├── TOOLS.md            # Tool descriptions
│   ├── SOUL.md             # Agent personality
│   ├── USER.md             # User context
│   ├── MEMORY.md           # Memory guidelines
│   ├── HEARTBEAT.md        # Heartbeat config
│   ├── PROFILE.md          # Agent profile
│   ├── BOOTSTRAP.md        # First-run bootstrap
│   └── agents/             # Additional agent guides
├── cli/                     # CLI commands
│   ├── commands.py         # Main CLI commands
│   ├── onboard.py          # Onboarding wizard
│   ├── skills.py           # Skill management CLI
│   ├── slash_commands/     # In-chat slash commands
│   │   ├── builtin.py      # Built-in commands
│   │   └── router.py       # Command router
│   ├── stream.py           # Stream rendering
│   └── models.py           # CLI models
├── types/                   # Type definitions
│   ├── tool.py             # Tool types
│   ├── skill.py            # Skill types
│   └── permission.py       # Permission types
└── utils/                   # Utilities
    ├── helpers.py          # Helper functions
    ├── constants.py        # Constants
    └── network.py          # Network utilities
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

# Type checking (if configured)
pyright
```

### Adding a New LLM Provider

1. Add a `ProviderSpec` to `markbot/providers/registry.py`
2. Add a field to `ProvidersConfig` in `markbot/config/schema.py`
3. Done! The provider will be auto-discovered

### Adding a New Channel

1. Create a new file in `markbot/channels/`
2. Subclass `BaseChannel` from `markbot/channels/base.py`
3. Implement required methods
4. The channel will be auto-discovered via `markbot/channels/registry.py`

### Adding a New Tool

1. Create a new file in `markbot/agent/tools/`
2. Subclass `BaseTool` from `markbot/agent/tools/base.py`
3. Register in the tool registry

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

AGPL-3.0 License

Copyright (c) 2024 MarkBot contributors
