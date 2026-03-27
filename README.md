# MarkBot 🦞

An advanced AI-powered automation and development assistant designed for developers and power users. MarkBot excels at complex task planning and software development, combining the best features of modern AI assistants with specialized capabilities for technical workflows.

## Core Strengths

- **Task Planning & Orchestration**: Break down complex projects into manageable steps, track progress, and coordinate multiple sub-tasks autonomously
- **Software Development**: Write, review, debug, and refactor code with deep understanding of project context and best practices
- **Tiered Memory System**: Multi-layered memory architecture (Hot/Warm/Cold) for context-aware responses
- **Extensible Architecture**: Customize and extend capabilities through a powerful skills system

## Features

- **Multiple LLM Providers**: Anthropic, OpenAI, Azure OpenAI, DeepSeek, OpenRouter, Groq, and more
- **Multi-Channel Support**: DingTalk, Feishu, QQ, WeChat, Email, and more
- **Tiered Memory Architecture**: Hot (working), Warm (session), Cold (persistent) memory layers
- **Skills System**: Modular skill framework for adding specialized capabilities
- **Cron Jobs**: Schedule and automate recurring tasks with precision
- **MCP Support**: Model Context Protocol for seamless tool integration
- **Sub-Agent Architecture**: Delegate specialized tasks to focused sub-agents
- **Web Integration**: Built-in web browsing, content extraction, and API interaction
- **Command Router**: Built-in commands like `/new`, `/help`, `/stop`
- **Skill Execution**: Run skill scripts in sandboxed environments

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Channels                               │
│  (DingTalk, Feishu, QQ, WeChat, Email, etc.)              │
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
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Providers                               │
│        (Anthropic, OpenAI, Azure, DeepSeek, etc.)          │
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

## Quick Start

### Step 1: Initialize Configuration

```bash
markbot onboard
```

### Step 2: Configure Your Provider

Edit `~/.markbot/config.json`:

```json
{
  "providers": {
    "anthropic": {
      "apiKey": "sk-ant-..."
    }
  }
}
```

### Step 3: Start Chatting

```bash
markbot agent
```

Or send a single message:

```bash
markbot agent -m "Hello!"
```

### Step 4: Start Gateway Server

```bash
markbot gateway start
```

## Commands

### Gateway Management

```bash
markbot gateway start    # Start the gateway
markbot gateway status   # Check status
markbot gateway stop     # Stop the gateway
markbot gateway restart  # Restart
```

### Agent Commands (in chat)

| Command | Description |
|---------|-------------|
| `/new` | Start a new session with memory consolidation |
| `/help` | Show available commands |
| `/stop` | Stop current request |

### Configuration

```bash
markbot config list                              # List all config
markbot config get agents.defaults.model          # Get value
markbot config set agents.defaults.model claude-3-5-sonnet  # Set value
```

## Memory System

MarkBot uses a **tiered memory architecture** with three layers:

| Layer | Purpose | Retention |
|-------|---------|-----------|
| **Hot Memory** | Working context, whiteboard | Per-turn |
| **Warm Memory** | Session context, recent facts | Per session |
| **Cold Memory** | Long-term storage, profiles | Persistent |

### Cold Memory Structure

```
~/.markbot/workspace/
├── memory/
│   ├── memories/           # Structured memories by category
│   │   ├── profile/        # User profile
│   │   ├── preferences/     # User preferences
│   │   ├── entities/       # Tracked entities
│   │   ├── events/         # Events
│   │   ├── cases/          # Cases
│   │   └── patterns/       # Patterns
│   └── HISTORY.md          # Append-only event log
```

### Memory Extraction

Memories are automatically extracted from conversations and stored in structured markdown files. The system uses LLM-powered extraction with deduplication to avoid redundancy.

## Skills

Skills extend MarkBot's capabilities with specialized instructions and tools.

### Built-in Skills

| Skill | Description |
|-------|-------------|
| `skill-creator` | Create new skills from scratch |
| `summarize` | Summarize URLs, files, YouTube videos |
| `memory` | Structured memory management |
| `cron` | Schedule reminders and recurring tasks |
| `github` | GitHub interaction via `gh` CLI |
| `tmux` | Remote-control tmux sessions |
| `weather` | Weather information |
| `clawhub` | Search skills from ClawHub registry |

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
│   ├── context.py           # Context building
│   ├── tiered_memory/       # Tiered memory system
│   │   ├── hot_memory.py    # Working memory
│   │   ├── warm_memory.py   # Session memory
│   │   ├── cold_memory.py   # Persistent memory
│   │   └── manager.py       # Memory manager
│   ├── skill_execution/     # Skill script runner
│   │   ├── sandbox.py       # Sandboxed execution
│   │   └── scanner.py       # Skill scanner
│   └── tools/               # Built-in tools
├── channels/                # Channel integrations
│   ├── feishu.py           # Feishu/Lark
│   ├── dingtalk.py         # DingTalk
│   ├── weixin.py           # WeChat
│   └── ...
├── providers/               # LLM providers
│   ├── anthropic_provider.py
│   ├── openai_compat_provider.py
│   └── ...
├── command/                 # Built-in commands
├── skills/                  # Built-in skills
├── templates/               # Agent templates
├── cli/                     # CLI commands
└── config/                  # Configuration
```

## Development

```bash
# Setup
git clone https://github.com/mickletang/markbot.git
cd markbot
pip install -e ".[dev]"

# Run tests
pytest

# Format code
ruff format .

# Lint
ruff check .
```

## License

AGPL-3.0 License
