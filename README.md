# MarkBot 🦞

> Version 2.2.8

An advanced AI-powered automation and development assistant designed for developers and power users. MarkBot excels at complex task planning and software development, combining the best features of modern AI assistants with specialized capabilities for technical workflows.

## Core Strengths

- **Multi-Model Support with Auto-Failover**: Configure multiple LLM providers in a priority chain. When the primary model fails or is overloaded, MarkBot automatically falls back to the next model.
- **Task Planning & Orchestration**: Break down complex projects into manageable steps, track progress, and coordinate multiple sub-tasks autonomously
- **Software Development**: Write, review, debug, and refactor code with deep understanding of project context and best practices
- **ReMeLight Memory System**: Advanced memory management powered by [reme-ai](https://github.com/remember-ai/reme-ai), with compaction, summarization, and semantic search for context-aware responses
- **Extensible Architecture**: Customize and extend capabilities through a powerful skills system with security scanning and guardrails
- **Autopilot Pipeline**: Automated task execution with scoring, acceptance, verification, and self-repair capabilities

## Features

### Core Capabilities

- **Multiple LLM Providers**: Anthropic, OpenAI, Azure OpenAI, DeepSeek, OpenRouter, Gemini, Moonshot, Zhipu, DashScope, Groq, and more (30+ providers supported)
- **Multi-Model Chain with Auto-Failover**: Configure multiple models in priority chain with automatic failover on errors or overload
- **Multi-Channel Support**: DingTalk, Feishu, QQ, WeChat (Weixin), Email, with auto-reconnect and health monitoring
- **OAuth Authentication**: Native support for OpenAI Codex and GitHub Copilot with OAuth flow
- **Local Model Support**: Integration with vLLM, Ollama, and OVMS for local deployments

### Memory & Context

- **ReMeLight Memory**: Advanced memory management with compaction, summarization, semantic search, and Dream optimization
- **4-Tier Progressive Compaction**: Context Collapse → Micro-Compact → Auto-Compaction → History Snip, escalating only when needed
- **Dream Service**: Periodic AI-driven memory optimization on a cron schedule for intelligent context management

### Tool System

- **Built-in Tools**: 15+ tools including Filesystem, Shell, Web, Search, MCP, Memory, Todo, Think, Question, Context Explorer, Cron, Explore, Message
- **MCP Support**: Model Context Protocol for seamless tool integration (stdio, SSE, streamable HTTP)
- **Web Integration**: Built-in web browsing, content extraction, and search (Brave, Tavily, DuckDuckGo, SearXNG, Jina)
- **Voice Transcription**: Audio transcription via Groq Whisper integration

### Agent Architecture

- **Sub-Agent System**: Delegate specialized tasks with capability-based delegation tokens (CapabilityToken)
- **Pipeline Engine**: Middleware-based message processing with pluggable pipeline stages
- **Token & Cost Tracking**: Real-time token usage monitoring with cache token support and per-model pricing
- **Budget Control**: Configurable per-session budget caps with custom pricing overrides

### Automation & Skills

- **Skills System**: Modular skill framework with security scanning, guardrails, and sandboxed execution
- **Cron Jobs**: Schedule and automate recurring tasks with precision
- **Autopilot**: Automated task pipeline with Intake → Score → Accept → Execute → Verify → Repair workflow
- **Permission System**: Configurable permission modes (default, plan, accept_edits, bypass, auto) with per-tool allow/deny/ask policies

### Developer Experience

- **Slash Commands**: Built-in commands like `/new`, `/compact`, `/stop`, `/status`, `/restart`, and more
- **Event Bus**: Event-driven architecture for message passing and component communication
- **Context Explorer**: Explore project context with semantic search and catalog
- **Todo Management**: Built-in persistent todo tracking tool for task management
- **Codebase Exploration**: Understand project structure and code context with deep exploration tools
- **Interaction Logging**: Full audit trail of LLM request/response pairs for analysis
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
```

## Quick Start

### Basic Usage

```bash
# Start interactive mode
markbot

# Start with specific workspace
markbot --workspace /path/to/workspace

# Run as gateway server
markbot gateway
```

### CLI Commands

```bash
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
| `/stop` | Cancel all active tasks |
| `/status` | Show session status and statistics |
| `/restart` | Restart the agent process |

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
```

### Environment Variables

Most sensitive configuration values can be provided via environment variables using `${VAR_NAME}` syntax in the config file.

## Built-in Tools

| Tool | Description |
|------|-------------|
| **filesystem** | File read/write/edit operations with backup support |
| **shell** | Command execution with timeout and safety controls |
| **web** | Web browsing and content extraction |
| **search** | Multi-provider web search (Brave, Tavily, DuckDuckGo, SearXNG, Jina) |
| **mcp** | Model Context Protocol integration |
| **memory** | Memory search and management |
| **todo** | Persistent task tracking |
| **think** | Structured thinking and reasoning |
| **question** | Ask clarifying questions |
| **context_explorer** | Project context exploration |
| **cron** | Cron job management |
| **explore** | Codebase exploration |
| **message** | Message sending and formatting |

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

## Autopilot System

The Autopilot system provides automated task execution with intelligence:

### Pipeline Stages

1. **Intake**: Receive and parse task definitions
2. **Score**: Evaluate task complexity and priority
3. **Accept**: Determine if task should be executed
4. **Execute**: Run the task with AI assistance
5. **Verify**: Validate results against acceptance criteria
6. **Repair**: Automatically fix issues if verification fails
7. **Complete/Fail**: Finalize task status

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

## Memory System (ReMeLight)

Advanced memory management powered by reme-ai:

### Features

- **Semantic Search**: Vector-based similarity search across memories
- **Compaction**: Intelligent compression of long conversations
- **Summarization**: Extract key information from interactions
- **Dream Optimization**: Periodic AI-driven memory reorganization
- **Daily Logs**: Time-based memory organization
- **Embedding Support**: OpenAI and Ollama embedding backends

### Configuration

```yaml
tools:
  memory:
    embedding_backend: openai  # or "ollama" for local models
    memory_compact_threshold: 0  # 0 = auto (75% of context window)
```

## Monitoring & Diagnostics

### Health Checks

Automatic health monitoring for all channels with configurable intervals.

### Status Command

Real-time statistics including:
- Model information and token usage
- Cache hit rates
- Active tasks and sessions
- Cost tracking
- API call counts

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
- Configuration validity
- Database integrity
- File system permissions
- Network connectivity
- Provider authentication

## Development

### Project Structure

```
markbot/
├── agent/              # Core agent loop and processing
│   ├── loop.py        # Main agent processing engine
│   ├── pipeline/      # Message pipeline middleware
│   ├── subagent/      # Background task delegation
│   ├── mcp/           # MCP protocol support
│   └── tools/         # Tool binding and execution
├── channels/          # Multi-channel messaging
│   ├── dingtalk.py    # DingTalk integration
│   ├── feishu.py      # Feishu/Lark integration
│   ├── qq.py          # QQ Bot integration
│   ├── weixin.py      # WeChat integration
│   └── email.py       # Email support
├── tools/             # Built-in tool implementations
├── skills/            # Skill system
│   ├── core/          # Skill framework
│   └── builtin/       # Built-in skills
├── providers/         # LLM provider integrations
├── config/            # Configuration management
├── memory/            # ReMeLight memory system
├── bus/               # Event bus infrastructure
├── session/           # Session management
├── schedule/          # Cron and scheduling
├── autopilot/         # Automated task pipeline
├── cli/               # Command-line interface
│   ├── commands.py    # Main CLI entry point
│   ├── skills.py      # Skill management commands
│   ├── autopilot.py   # Autopilot commands
│   └── doctor.py      # Diagnostic commands
├── types/             # Type definitions
└── utils/             # Utility functions
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

# Run type checking (if configured)
# ruff format .

# Run tests
pytest
```

## License

This project is licensed under the AGPL-3.0 License - see the LICENSE file for details.

## Acknowledgments

- [reme-ai](https://github.com/remember-ai/reme-ai) for the ReMeLight memory system
- [Anthropic](https://anthropic.com) for Claude models
- [OpenAI](https://openai.com) for GPT models
- All contributors and community members

---

**MarkBot** 🦞 - Your personal AI assistant for development and automation.
