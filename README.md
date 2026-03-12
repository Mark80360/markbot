# MarkBot 🦞

A lightweight personal AI assistant framework.

## Features

- **Multiple LLM Providers**: OpenAI, Azure OpenAI, LiteLLM, Anthropic, and more
- **Multi-channel Support**: Discord, DingTalk, Feishu, Email, and more
- **Agent Framework**: Built-in agent loop with tools, memory, and sub-agents
- **Skills System**: Extensible skill framework for adding capabilities
- **Cron Jobs**: Schedule and run recurring tasks
- **Heartbeat Service**: Monitor and maintain persistent connections
- **MCP Support**: Model Context Protocol integration

## Installation

```bash
pip install markbot-ai
```

## Quick Start

### Interactive Chat

```bash
markbot
```

### With Specific Provider

```bash
markbot --provider openai
```

### Run a Skill

```bash
markbot run-skill <skill-name>
```

## Configuration

MarkBot uses a YAML configuration file. Default location: `~/.markbot/config.yaml`.

### Example Configuration

```yaml
provider:
  type: litellm
  model: gpt-4o

channels:
  - type: discord
    token: your-bot-token

workspace: ~/markbot-workspace
```

## Project Structure

```
markbot/
├── agent/          # Agent core (loop, tools, memory, context)
├── channels/      # Message channel integrations
├── providers/     # LLM provider implementations
├── skills/        # Built-in skills
├── cli/           # CLI commands
├── cron/          # Scheduled task service
├── heartbeat/     # Heartbeat monitoring
└── templates/     # Agent prompt templates
```

## Workspace

MarkBot uses a workspace directory for:
- Agent prompts (AGENTS.md, SOUL.md, USER.md)
- Memory (daily notes, long-term memory)
- Skill definitions

## Skills

Built-in skills:
- `skill-creator` - Create new skills
- `summarize` - Summarize content
- `cron` - Manage cron jobs
- `github` - GitHub integration
- `memory` - Memory management
- `tmux` - Tmux session management
- `weather` - Weather information
- `clawhub` - ClawHub integration

## Development

```bash
# Install with dev dependencies
pip install markbot-ai[dev]

# Run tests
pytest

# Run CLI
python -m markbot
```

## License

AGPL-3.0 License
