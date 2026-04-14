---
name: memory
description: >
  ReMeLight memory system with conversation compaction, async summarization,
  and semantic search. Use memory_search tool to recall past conversations,
  user preferences, decisions, or project context stored in MEMORY.md and
  daily notes. Auto-compacts context when conversation grows too long.
  Trigger when: user asks about prior discussions, preferences, decisions,
  project history, or needs to recall something mentioned earlier.
always: true
---

# Memory System

Memory system using ReMeLight for conversation compaction, async summarization, and semantic search.

## Architecture

### MEMORY.md — Long-Term Curated Memory
- **Purpose**: Curated long-term facts, like a human's long-term memory
- **Lifetime**: Permanent, updated by async summarization
- **Storage**: `workspace/MEMORY.md`
- **Use for**: User preferences, key decisions, project context, lessons learned
- **Auto-loaded**: Included in system prompt for main sessions

**Update MEMORY.md when:**
- User states preferences ("I prefer dark mode", "My name is Mark")
- Project decisions are made ("API uses OAuth2", "Database is PostgreSQL")
- Important lessons learned from mistakes

### memory/YYYY-MM-DD.md — Daily Notes
- **Purpose**: Auto-generated daily conversation summaries
- **Lifetime**: Permanent
- **Storage**: `workspace/memory/YYYY-MM-DD.md`
- **Use for**: Reviewing recent activity, finding past conversations
- **Search**: Via `memory_search` tool

### Compressed Summary — Session Context
- **Purpose**: Condensed summary of older conversation messages
- **Lifetime**: Current session only
- **Storage**: `workspace/memory/.compressed_summary`
- **Use for**: Maintaining context when history exceeds context window
- **Auto-triggered**: When messages exceed 75% of context window

## Memory Tools

### memory_search
Search MEMORY.md and daily notes semantically. Use before answering questions about prior work, decisions, or user preferences.

**Parameters:**
- `query` (required): Semantic search query
- `max_results` (default: 5): Maximum results to return
- `min_score` (default: 0.1): Minimum similarity score

**Example:**
```
memory_search(query="user preference for theme", max_results=3)
```

### force_memory_search (Optional)
When enabled in config, automatically searches before each LLM call and injects relevant memories. Configured via `tools.memory.force_memory_search`.

## Memory Operations

### Automatic Compaction
When conversation exceeds 75% of context window:
1. Older messages summarized via `compact_memory`
2. Recent messages preserved
3. System prompt always preserved
4. Summary stored in `.compressed_summary`

### Async Summarization
After each conversation turn:
1. Background task summarizes via `summary_memory`
2. Result appended to `.compressed_summary`
3. Daily summary written to `memory/YYYY-MM-DD.md`
4. Heartbeat summarization at scheduled times updates MEMORY.md with key facts

## Slash Commands

- `/compact` — Manually trigger conversation compaction
- `/new` — Start fresh session (saves summary first)
- `/clear` — Clear history and summary

## Quick Reference

| Component | Type | Persistence | Auto-Context |
|-----------|------|-------------|--------------|
| MEMORY.md | Curated facts | Permanent | ✅ Main sessions |
| memory/*.md | Daily notes | Permanent | On demand via search |
| .compressed_summary | Session context | Current session | Auto-injected |
