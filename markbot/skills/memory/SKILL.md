---
name: memory
description: L1-L4 tiered memory system inspired by Swarmbot architecture.
always: true
---

# Memory

Tiered memory system with 4 layers (L1-L4) for efficient context management.

## Architecture

### L1 Whiteboard - Loop-Level Temporary
- **Purpose**: Temporary workspace for single conversation turn
- **Lifetime**: Cleared after each assistant response
- **Storage**: `memory/whiteboard/{chat_id}.json`
- **Use for**: Drafting responses, calculations, temporary notes

### L1.5 Session - Chat-Level Sliding Window
- **Purpose**: Recent conversation history
- **Lifetime**: Last 8 turns (configurable)
- **Storage**: In-memory, saved to session
- **Use for**: Maintaining short-term conversation context

### L2 Hot - Global Important Facts
- **Purpose**: Critical user preferences and project facts
- **Lifetime**: Permanent, max 20 items (configurable)
- **Storage**: `memory/hot.json`
- **Use for**: User preferences, key relationships, project context
- **Auto-loaded**: ✅ Always included in LLM context

**When to update:**
- User preferences ("I prefer dark mode", "My name is Mark")
- Project facts ("API uses OAuth2", "Database is PostgreSQL")
- Important relationships ("Alice is the tech lead")

### L3 Warm - Daily Activity Log
- **Purpose**: Append-only chronological activity log
- **Lifetime**: 30 days retention (configurable)
- **Storage**: `memory/HISTORY.md`
- **Use for**: Event tracking, activity history, audit trail
- **Auto-loaded**: ❌ Search on demand

**Search methods:**

For small HISTORY.md: Use `read_file` + in-memory search

For large files: Use targeted search:
- **Linux/macOS:** `grep -i "keyword" memory/HISTORY.md`
- **Windows:** `findstr /i "keyword" memory\HISTORY.md`
- **Python:** `python -c "from pathlib import Path; text = Path('memory/HISTORY.md').read_text(); print('\n'.join([l for l in text.splitlines() if 'keyword' in l.lower()][-20:]))"`

### L4 Cold - Semantic Long-term Storage
- **Purpose**: Vector-based semantic search for large history
- **Lifetime**: Permanent (until manually deleted)
- **Storage**: ChromaDB vector database (`memory/chroma/`)
- **Use for**: Finding semantically similar past interactions
- **Requirement**: Optional - requires `chromadb` dependency

## Memory Consolidation

When conversations grow large, the system automatically:
1. Summarizes old session turns
2. Appends to L3 Warm (HISTORY.md)
3. Extracts long-term facts → L2 Hot
4. Clears L1 Whiteboard

This happens transparently every 8 turns (configurable).

## Quick Reference

| Layer | Type | Persistence | Search | Auto-Context |
|-------|------|-------------|---------|--------------|
| L1 Whiteboard | Temporary | Single turn | - | Yes |
| L1.5 Session | History | 8 turns | - | Yes |
| L2 Hot | Facts | Permanent | Key-based | ✅ Always |
| L3 Warm | Events | 30 days | Grep/text | On demand |
| L4 Cold | Semantic | Permanent | Vector similarity | On demand |
