---
name: memory
description: ReMeLight memory system with compaction, summarization, and semantic search.
always: true
---

# Memory

 memory system using ReMeLight for conversation compaction, summarization, and semantic search.

## Architecture

### MEMORY.md — Long-Term Curated Memory
- **Purpose**: Your curated long-term memory, like a human's long-term memory
- **Lifetime**: Permanent, manually curated
- **Storage**: `MEMORY.md` (Markdown)
- **Use for**: User preferences, key decisions, project context, lessons learned
- **Auto-loaded**: ✅ In main sessions (direct chats with your human)

**When to update:**
- User preferences ("I prefer dark mode", "My name is Mark")
- Project facts ("API uses OAuth2", "Database is PostgreSQL")
- Important decisions and their rationale
- Lessons learned from mistakes

### memory/YYYY-MM-DD.md — Daily Notes
- **Purpose**: Auto-generated daily summaries of conversations
- **Lifetime**: Permanent (Markdown files)
- **Storage**: `memory/YYYY-MM-DD.md`
- **Use for**: Reviewing recent activity, finding past conversations
- **Auto-loaded**: ❌ Search on demand via `memory_search`

### Compressed Summary — Context Window Management
- **Purpose**: Compressed summary of current conversation for context continuity
- **Lifetime**: Current session only
- **Storage**: In-memory
- **Use for**: Maintaining conversation context when history gets too long
- **Auto-triggered**: When token count exceeds 75% of context window

## Memory Operations

### Automatic Compaction
When the conversation grows too long (exceeds 75% of context window):
1. System summarizes older messages into a compressed summary
2. Recent messages are preserved
3. System prompt is always preserved
4. Summary task dispatched to background

### Async Summarization
After each conversation turn:
1. System dispatches a background summary task
2. Summary is written to `memory/YYYY-MM-DD.md`
3. Significant facts may be extracted to `MEMORY.md`

### Memory Search
Use `memory_search` tool to find information from past conversations:
- Searches `MEMORY.md` and all daily note files
- Semantic search (understands meaning, not just keywords)
- Returns relevant snippets with file paths and content

## Slash Commands

- `/compact` — Manually compact the current conversation into a summary
- `/compact_str` — View the current compressed summary
- `/new` — Start a new conversation (saves summary first)
- `/clear` — Clear history and compressed summary

## Quick Reference

| Component | Type | Persistence | Search | Auto-Context |
|-----------|------|-------------|--------|--------------|
| MEMORY.md | Curated facts | Permanent | Semantic | ✅ Main sessions |
| memory/YYYY-MM-DD.md | Daily notes | Permanent | Semantic | On demand |
| Compressed Summary | Session context | Current session | - | Auto-injected |
