# Markbot Skills System

Modular skill system with separated **content** (`builtin/`) layers.

## Architecture

```
skills/
├── __init__.py              # Unified public API (backward compatible)
├── README.md                # This file
└── builtin/                 # 📦 Built-in Skills (business logic)
    ├── clawhub/             #    Search & install from ClawHub registry
    ├── cron/                #    Schedule reminders & recurring tasks
    ├── github/              #    Interact with GitHub via `gh` CLI
    ├── memory/              #    File-based memory system
    ├── skill-creator/       #    Create & manage new skills
    ├── summarize/           #    Summarize URLs, files, videos
    ├── surprise-me/         #    Combine skills dynamically
    ├── tmux/                #    Remote-control tmux sessions
    └── weather/             #    Weather info (wttr.in, Open-Meteo)
```

## Design Principles

### Separation of Concerns
- **`builtin/`** contains *skill definitions*: what each skill does (SKILL.md + resources)

### Why This Structure?
1. **Maintainability**: Engine code and business content don't interfere with each other
2. **Security**: Core modules are fully controlled; builtin skills can be scanned independently
3. **Extensibility**: Users add custom skills to `workspace/skills/`, keeping builtins pristine
4. **Clarity**: Clear package boundaries prevent accidental imports of non-module files

## Skill Format

Each skill is a directory containing:

```
skill-name/
├── SKILL.md                 # Required: YAML frontmatter + Markdown instructions
├── scripts/                 # Optional: Executable scripts (Python, Shell, etc.)
├── references/              # Optional: Supporting documentation
├── templates/               # Optional: File templates for code generation
└── assets/                  # Optional: Static resources (HTML, images)
```

### SKILL.md Structure

```markdown
---
name: skill-name
description: What this skill does
when_to_use: When to activate this skill
---

# Detailed instructions for the agent...

## Procedures
Step-by-step workflows...

## Available Scripts
- `skill-name.script_name`: Description
```

## Usage

### For Developers

```python
from markbot.skills import SkillLoader, SkillRegistry

# Load all skills (workspace + builtin)
loader = SkillLoader(workspace_path)
skills = loader.load_all()

# Or use the full registry (with security scanning, config resolution)
registry = SkillRegistry(workspace_path, tool_registry=tool_reg)
registry.load_all()
```

### For Agents (via System Prompt)

The agent sees this guidance in its context:

```markdown
## Skills — MANDATORY KNOWLEDGE SYSTEM

- Built-in skills: <project>/markbot/skills/builtin/{skill-name}/SKILL.md
- Custom skills: {workspace}/skills/{skill-name}/SKILL.md
```

Agents use:
- `skill_view(name)` - Load full SKILL.md instructions
- `skill_name.script()` - Execute a skill script
- `skill_manage(...)` - Create/edit/delete skills

## Available Built-in Skills

| Skill | Description | Type |
|-------|-------------|------|
| `clawhub` | Search and install skills from ClawHub registry | [executable] |
| `cron` | Schedule reminders and recurring tasks | [executable] |
| `github` | Interact with GitHub using the `gh` CLI | [executable] |
| `memory` | File-based memory system with compaction, summarization, and keyword search | [instruction] |
| `skill-creator` | Create new skills (meta-skill) | [executable] |
| `summarize` | Summarize URLs, files, and YouTube videos | [executable] |
| `surprise-me` | Create delightful experiences by combining skills dynamically | [instruction] |
| `tmux` | Remote-control tmux sessions | [executable] |
| `weather` | Get weather info using wttr.in and Open-Meteo | [executable] |

**Legend**:
- `[executable]` - Has runnable scripts
- `[instruction]` - Provides guidance only (no scripts)

## Security Model

Skills are security-scanned before activation:

| Trust Level | Safe | Caution | Dangerous |
|-------------|------|---------|-----------|
| **builtin** | ✅ Allow | ✅ Allow | ✅ Allow |
| **workspace** | ✅ Allow | ✅ Allow | 🚫 Block |
| **external** | ✅ Allow | 🚫 Block | 🚫 Block |
| **agent-created** | ✅ Allow | ✅ Allow | ⚠️ Ask User |

Scanning covers: exfiltration, prompt injection, destructive operations, reverse shells, obfuscation, privilege escalation.

## Custom Skills

Users can create workspace-specific skills in `{workspace}/skills/`:

```bash
# Using the skill-creator tool
skill_manage(action='create', name='my-skill', content='...')

# Or manually
mkdir -p workspace/skills/my-skill/scripts
# Create SKILL.md with proper frontmatter
```

Custom skills override builtins with the same name (workspace takes priority).

## Attribution

The skill format and metadata structure are adapted from [OpenClaw](https://github.com/openclaw/openclaw)'s skill system to maintain compatibility.

## Migration Notes

**Previous structure** (before refactoring):
```
skills/
├── loader.py, registry.py, ...  # Mixed with skill definitions
├── clawhub/, cron/, ...         # All in one directory
```

**Current structure** (after refactoring):
- Skill definitions → `builtin/`
- Public API unchanged: `from markbot.skills import ...` still works
