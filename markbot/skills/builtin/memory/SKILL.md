---
name: memory
description: >
  File-based memory system with conversation compaction, async summarization,
  and keyword search. Use memory_search tool to recall past conversations,
  user preferences, decisions, or project context stored in MEMORY.md and
  daily notes. Auto-compacts context when conversation grows too long.
  Trigger when: user asks about prior discussions, preferences, decisions,
  project history, or needs to recall something mentioned earlier.
always: true
---

# Memory System

Memory system using file-based storage for conversation compaction, async summarization, and keyword search.

## Architecture

### MEMORY.md — Long-Term Curated Memory
- **Purpose**: Curated long-term facts only (high-signal)
- **Lifetime**: Permanent
- **Storage**: `workspace/MEMORY.md` (flat entry list separated by `---`)
- **Use for**: User preferences, key decisions, project context, lessons learned
- **Auto-loaded**: System prompt for main/private sessions only (cli/web/api/local)
- **Not auto-loaded**: Shared messaging channels (dingtalk/feishu/qq/email/...)
- **Shared-channel search**: `memory_search` on shared channels excludes MEMORY.md / PROFILE.md and returns only same-session logs/summaries
- **Shared-channel list/load**: `memory_list`, `memory_forget`, and context explorer cannot read MEMORY.md / PROFILE.md on shared channels

**Update MEMORY.md when:**
- User explicitly asks to remember something (`memory_save`)
- User states durable preferences ("I prefer dark mode", "My name is Mark")
- Project decisions are made ("API uses OAuth2", "Database is PostgreSQL")
- Important lessons learned from mistakes
- Dream promotion of high-access curated/summary facts

**Do NOT put in MEMORY.md:**
- Temporary task progress / TODO state
- Full conversation transcripts
- Automatic turn summaries (those go to daily logs + vector index)

### memory/daily/YYYY-MM-DD.md — Daily Notes
- **Purpose**: Raw interaction log + auto-summary notes
- **Lifetime**: Retention-limited (default 30 days)
- **Storage**: `workspace/memory/daily/YYYY-MM-DD.md`
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
Search MEMORY.md and daily notes by keyword. Use before answering questions about prior work, decisions, or user preferences.

**Parameters:**
- `query` (required): Search query
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
On compaction / periodic auto-summary:
1. Background task extracts durable facts via `summary_memory`
2. Facts are indexed into vector memory (`summary/durable`)
3. A short note is appended to `memory/daily/YYYY-MM-DD.md`
4. Curated `MEMORY.md` is NOT modified unless `auto_summary_to_curated=true`
5. Dream may later promote high-access curated/summary facts into MEMORY.md

## Slash Commands

- `/compact` — Manually trigger conversation compaction
- `/new` — Start fresh session (saves summary first)
- `/clear` — Clear history and summary

## Quick Reference

| Component | Type | Persistence | Auto-Context |
|-----------|------|-------------|--------------|
| MEMORY.md | Curated facts | Permanent | ✅ Main sessions only |
| memory/daily/*.md | Daily notes | Retention-limited | On demand via search |
| vector index | Semantic recall | Permanent (capped) | Prefetch + search |
| .compressed_summary | Session context | Current session | Auto-injected |
