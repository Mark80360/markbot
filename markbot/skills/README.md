# markbot Skills

This directory contains built-in skills that extend markbot's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

## Attribution

These skills are adapted from [OpenClaw](https://github.com/openclaw/openclaw)'s skill system.
The skill format and metadata structure follow OpenClaw's conventions to maintain compatibility.

## Available Skills

| Skill | Description |
|-------|-------------|
| `clawhub` | Search and install skills from ClawHub registry |
| `cron` | Schedule reminders and recurring tasks |
| `github` | Interact with GitHub using the `gh` CLI |
| `memory` | L1-L4 tiered memory system inspired by Swarmbot architecture |
| `skill-creator` | Create new skills |
| `summarize` | Summarize URLs, files, and YouTube videos |
| `surprise-me` | Create delightful unexpected experiences by combining skills dynamically |
| `tmux` | Remote-control tmux sessions |
| `weather` | Get weather info using wttr.in and Open-Meteo |