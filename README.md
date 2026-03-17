# MarkBot 🦞

An advanced AI-powered automation and development assistant designed for developers and power users. MarkBot excels at complex task planning and software development, combining the best features of modern AI assistants with specialized capabilities for technical workflows.

## Core Strengths

- **Task Planning & Orchestration**: Break down complex projects into manageable steps, track progress, and coordinate multiple sub-tasks autonomously
- **Software Development**: Write, review, debug, and refactor code with deep understanding of project context and best practices
- **Multi-Modal Integration**: Seamlessly interact with various platforms and services through a unified interface
- **Extensible Architecture**: Customize and extend capabilities through a powerful skills system

## Features

- **Multiple LLM Providers**: OpenAI, Azure OpenAI, Anthropic, DeepSeek, Groq, and 15+ providers
- **Multi-channel Support**: DingTalk, Feishu, Email, and more
- **Advanced Agent Framework**: Sophisticated agent loop with tools, memory, and sub-agent delegation
- **Skills System**: Modular skill framework for adding specialized capabilities
- **Cron Jobs**: Schedule and automate recurring tasks with precision
- **Heartbeat Service**: Maintain persistent connections and real-time monitoring
- **MCP Support**: Model Context Protocol for seamless tool integration
- **Three-Layer Memory System**: Core, working, and episodic memory with time-based decay
- **Sub-Agent Architecture**: Delegate specialized tasks to focused sub-agents
- **Web Integration**: Built-in web browsing, content extraction, and API interaction
- **Code Analysis**: Deep codebase understanding and intelligent code navigation

## Installation

### Prerequisites

- Python 3.11 or higher
- pip package manager

### Install from PyPI

```bash
pip install markbot-ai
```

### Install from Source

```bash
git clone https://github.com/mickletang/markbot.git
cd markbot
pip install -e .
```

### Development Installation

```bash
pip install markbot-ai[dev]
```

This installs additional development dependencies including pytest and ruff.

## Quick Start

### Step 1: Initialize Configuration

Run the onboard command to create the default configuration and workspace:

```bash
markbot onboard
```

This creates:
- Configuration file at `~/.markbot/config.json`
- Workspace directory at `~/.markbot/workspace`
- Template files for agent prompts and memory

### Step 2: Configure Your Provider

Edit the configuration file to set up your LLM provider:

```bash
markbot config set providers.openai.apiKey your-api-key
```

Or edit `~/.markbot/config.json` directly:

```json
{
  "providers": {
    "openai": {
      "apiKey": "your-api-key"
    }
  }
}
```

### Step 3: Interactive Chat

Start an interactive chat session:

```bash
markbot agent
```

### Step 4: Send a Message (Non-interactive)

Send a single message without entering interactive mode:

```bash
markbot agent -m "Hello!"
```

### Step 5: Start Gateway Server

Start the gateway service to enable multi-channel support:

```bash
markbot gateway start
```

Custom options:

```bash
markbot gateway start --port 8080  # Custom port
markbot gateway start -v           # Verbose output
```

**Windows:** Use `--foreground` to run in the foreground:

```bash
markbot gateway start --foreground
```

## Commands Reference

### Gateway Management

Manage the MarkBot gateway service:

```bash
markbot gateway start    # Start the gateway service
markbot gateway status   # Check gateway status
markbot gateway stop     # Stop the gateway service
markbot gateway restart  # Restart the gateway service
```

**Options for `gateway start`:**
- `--port, -p`: Set the gateway port (default: 18790)
- `--workspace, -w`: Specify workspace directory
- `--config, -c`: Specify config file path
- `--verbose, -v`: Enable verbose output
- `--daemon, -d`: Run as daemon (default)
- `--foreground`: Run in foreground (useful for Windows)

### Check Status

View MarkBot status and configuration:

```bash
markbot status
```

This displays:
- Configuration file location
- Workspace directory
- Current model
- Provider API key status

### Provider Management

Authenticate with OAuth providers:

```bash
markbot provider login openai-codex
markbot provider login github-copilot
```

### Configuration Management

Manage your MarkBot configuration:

```bash
markbot config list                              # List all configuration
markbot config list --prefix agents              # Filter by prefix
markbot config get agents.defaults.model         # Get specific value
markbot config set agents.defaults.model anthropic/claude-opus-4-5  # Set value
markbot config get providers.openai.apiKey --raw  # Get raw value
```

**Note:** Configuration keys use camelCase format in the JSON file (e.g., `apiKey`, `maxTokens`), but can be accessed using dot notation in CLI commands.

### Channel Management

Check channel status:

```bash
markbot channels status
```

### Pairing Management

Manage channel access pairing requests:

```bash
markbot pairing list              # List pending requests
markbot pairing approve <request-id>   # Approve a request
markbot pairing cancel <request-id>    # Cancel a request
```

## Configuration

MarkBot uses a JSON configuration file. Default location: `~/.markbot/config.json`.

## Project Structure

```
markbot/
├── agent/              # Agent core components
│   ├── loop.py         # Main agent execution loop
│   ├── memory.py       # Memory management
│   ├── context.py      # Context handling
│   ├── skills.py       # Skill system
│   ├── subagent.py     # Sub-agent delegation
│   └── tools/          # Built-in tools
├── channels/           # Message channel integrations
├── providers/          # LLM provider implementations
├── skills/             # Built-in skills
├── cli/                # CLI commands
├── config/             # Configuration management
├── cron/               # Scheduled task service
├── heartbeat/          # Heartbeat monitoring
├── bus/                # Message bus
├── session/            # Session management
├── templates/          # Agent prompt templates
├── utils/              # Utility functions
├── __init__.py         # Package initialization
└── __main__.py         # Entry point
```

## Workspace

MarkBot uses a workspace directory to store your data and customizations. Default location: `~/.markbot/workspace`.

### Workspace Structure

```
~/.markbot/workspace/
├── AGENTS.md          # Agent system prompt (customizable)
├── SOUL.md            # Agent personality (customizable)
├── USER.md            # User instructions (customizable)
├── HEARTBEAT.md       # Heartbeat system prompt (customizable)
├── TOOLS.md           # Tool descriptions (customizable)
├── memory/
│   ├── ENTRIES.json   # Structured memory entries
│   ├── ENTITIES.json  # Tracked entities (people, projects)
│   ├── MEMORY.md      # Human-readable memory (auto-generated)
│   └── HISTORY.md     # Append-only event log
└── skills/            # Custom skills directory
```

### Customizing Agent Behavior

Edit the template files in your workspace to customize agent behavior:

- **AGENTS.md**: Define the agent's role, capabilities, and behavior
- **SOUL.md**: Set the agent's personality and communication style
- **USER.md**: Provide context about yourself and your preferences

### Memory System

MarkBot uses a three-layer memory system with intelligent selective loading:

**Memory Layers:**

1. **Core Layer** (~500 tokens): Always loaded
   - User identity, core preferences
   - Highest importance memories

2. **Working Layer** (~1500 tokens): Loaded by relevance
   - Recent projects, tasks, facts
   - Boosted when matching current context

3. **Episodic Layer**: Search-only
   - Historical events, lessons learned
   - Accessed via search, not auto-loaded

**Memory Categories:**

| Category | Default Layer | Decay Rate |
|----------|---------------|------------|
| Identity | Core | Very slow |
| Preference | Core | Slow |
| Fact | Working | Moderate |
| Project | Working | Moderate |
| Task | Working | Fast |
| Event | Episodic | Moderate |
| Lesson | Episodic | Slow |
| Contact | Working | Moderate |

**Features:**

- **Time-based Decay**: Memories fade over time based on category
- **Access Tracking**: Frequently accessed memories stay relevant longer
- **Entity Tracking**: People, projects, and topics are automatically tracked
- **AI-powered Extraction**: Entities extracted during memory consolidation (no regex patterns)
- **Automatic Cleanup**: Low-relevance memories pruned after 500 entries
- **Dual Storage**: `ENTRIES.json` (structured) + `MEMORY.md` (human-readable)

**Storage Files:**

- `ENTRIES.json`: Structured memory entries with metadata
- `ENTITIES.json`: Tracked entities with mention counts
- `MEMORY.md`: Auto-generated human-readable view
- `HISTORY.md`: Append-only event log (rotates at 10MB)

## Skills

MarkBot includes a variety of built-in skills that extend its capabilities. Skills are modular and can be easily created or customized.

### Built-in Skills

| Skill | Description | Usage |
|-------|-------------|-------|
| `skill-creator` | Create new skills from scratch | Ask the agent to create a new skill |
| `summarize` | Summarize URLs, files, and YouTube videos | "Summarize this URL: https://example.com" |
| `cron` | Schedule reminders and recurring tasks | "Remind me to take a break every 2 hours" |
| `github` | Interact with GitHub using the `gh` CLI | "Check the status of my pull requests" |
| `memory` | Three-layer memory with categories, decay, and entity tracking | "Remember that I prefer Python for data analysis" |
| `tmux` | Remote-control tmux sessions | "List all tmux sessions" |
| `weather` | Get weather info using wttr.in and Open-Meteo | "What's the weather in Tokyo?" |
| `clawhub` | Search and install skills from ClawHub registry | "Search for a skill for task management" |

### Using Skills

Skills are automatically loaded and available in chat. Simply ask the agent to use a skill:

```bash
markbot agent
> Summarize this article: https://example.com/article
> Set a reminder for 3pm: "Meeting with team"
> Check the weather in New York
```

### Creating Custom Skills

Use the `skill-creator` skill to create new skills:

```bash
markbot agent
> Create a new skill for managing my todo list
```

The skill-creator will guide you through the process and generate a new skill directory with the required `SKILL.md` file.

### Skill Format

Each skill is a directory containing a `SKILL.md` file with:

1. **YAML Frontmatter**: Metadata (name, description, etc.)
2. **Markdown Instructions**: Detailed instructions for the agent

Example:

```yaml
---
name: my-skill
description: A brief description of what this skill does
---

# My Skill

Instructions for the agent on how to use this skill...
```

## Development

### Setting Up Development Environment

```bash
# Clone the repository
git clone https://github.com/mickletang/markbot.git
cd markbot

# Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install with dev dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_agent.py

# Run with coverage
pytest --cov=markbot --cov-report=html
```

### Code Quality

```bash
# Format code
ruff format .

# Lint code
ruff check .

# Fix linting issues
ruff check --fix .
```

### Running CLI

```bash
# Run from source
python -m markbot

# Run specific command
python -m markbot agent
python -m markbot gateway start
```

### Building Package

```bash
# Build wheel and source distribution
python -m build

# Install built package
pip install dist/markbot_ai-*.whl
```

### Project Architecture

MarkBot is built with a modular architecture:

- **Agent Loop**: Core execution engine that manages conversation flow
- **Tool System**: Extensible tools for various capabilities
- **Provider Layer**: Abstraction for different LLM providers
- **Channel Layer**: Integration with various messaging platforms
- **Skill System**: Modular capabilities that can be loaded/unloaded
- **Memory System**: Three-layer memory with categories, decay, and entity tracking
- **Cron Service**: Scheduled task execution
- **Heartbeat Service**: Maintains persistent connections

### Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Run tests and linting
6. Submit a pull request

### License

AGPL-3.0 License
