# Tool Reference

Tool signatures are provided automatically via function calling. This file documents non-obvious constraints and usage notes.

## File Operations

| Tool | Key Notes |
|------|-----------|
| `read_file` | Numbered lines, offset/limit pagination (default 2000 lines), max 128K chars. Supports images (auto-detected). UTF-8 text only — binary files rejected. |
| `write_file` | Overwrites entire file, creates parent dirs, auto-backup before overwrite. Prefer `edit_file` for partial changes. |
| `edit_file` | SEARCH/REPLACE mode. Make `old_text` unique or set `replace_all=true`. Shows unified diff of best match on failure. |
| `list_dir` | Auto-ignores `.git`, `node_modules`, `__pycache__` etc. `recursive=true` for tree view. Truncated at 200 entries. |
| `delete_file` | Requires `confirm=true` (safety guard). Safe mode moves to backup trash; otherwise permanent with backup. |
| `glob` | Find files by name pattern. Sort by `modified`/`name`/`size`. `show_details=true` for size/mtime. Max 100 results. |
| `grep` | Regex search in file contents. Returns `file:line: content` format. `context_lines` 0-5. Max 100 matches. |

## Execution

| Tool | Key Notes |
|------|-----------|
| `exec` | Shell command. Timeout 60s (configurable, max 600s). Rate limited 30/min. Dangerous patterns blocked (see SECURITY.md). Output truncated at 10K chars. **Prefer dedicated tools over exec.** |
| `run_code` | Sandboxed Python execution. Max 50K chars code, 15K chars output. Timeout 60s (max 300s). Optional `dependencies` (pip packages). Security-scanned before run. Temp files auto-cleaned unless `save_artifacts=true`. |

## Web

| Tool | Notes |
|------|-------|
| `web_search` | Up to 10 results (title, URL, snippet). Uses configured search provider. |
| `web_fetch` | Fetch URL, HTML→markdown. SSRF protected (blocks private IPs). Max 50K chars output. |
| `web_extract` | Extract structured content from URL. SSRF protected. |

## Communication

| Tool | Notes |
|------|-------|
| `message` | Send message to user. **ONLY way to deliver files** — use `media` param with file paths for attachments (images, documents, audio, video). |
| `ask_user_question` | Structured Q&A with 2-5 options. Blocks until user responds (5min timeout). Returns selected option label. |

## Thinking & Planning

`think` — Unified cognitive tool with modes:

| Mode | Purpose |
|------|---------|
| `analyze` | General analysis framework (default) |
| `challenge` | Challenge assumptions, find contradictions |
| `inversion` | What would cause failure? |
| `first-principles` | Break down to fundamental truths |
| `plan` | Task decomposition with `detail_level` (high/medium/low) and `constraints` |
| `evaluate` | Assess outcomes vs expectations |
| `learn` | Extract lessons and patterns |
| `improve` | Identify gaps, create action items |
| `code-analysis` | Structured code research framework |
| `research-plan` | Step-by-step codebase exploration plan |

## Task Management

| Tool | Notes |
|------|-------|
| `todo` | Structured task tracking. Actions: `write` (create/update by id), `list` (filter by status/priority), `delete` (by id). Stored as JSON in `workspace/todos/`. Do NOT create markdown files as task trackers. |
| `cron` | Schedule reminders/tasks. Actions: `add` (`every_seconds`/`cron_expr`/`at`), `list`, `remove` (by `job_id`). Supports IANA timezone. Cannot schedule from within cron job context. |

## Memory

| Tool | Notes |
|------|-------|
| `memory_search` | Semantic search in MEMORY.md and memory files. `max_results` (default 5), `min_score` (default 0.1). |
| `memory_save` | Save to long-term memory with optional `tags` for categorization. |
| `memory_forget` | Remove entry by `memory_id`. |
| `memory_list` | List recent memories (default 20). |
| `dream` | Trigger AI-driven memory optimization — summarizes, merges duplicates, cleans outdated entries. |

## Code Exploration

| Tool | Notes |
|------|-------|
| `explore` | Deep code exploration (heavy tool). Modes: `overview` (project map), `trace` (symbol tracking), `analyze` (deep dive), `dependencies` (module graph). `depth` 1-5. Use FIRST when researching a codebase. |

## Context Loading

| Tool | Notes |
|------|-------|
| `explore_context_catalog` | View available context sources (bootstrap files, memory, workspace). Like a table of contents. |
| `search_context` | Keyword search within specific context source. |
| `load_context` | Load full content from a specific context entry. |

## Subagents

| Tool | Notes |
|------|-------|
| `spawn` | Spawn background subagent for complex/long tasks. Returns task ID for tracking. |
| `check_subagent` | Check subagent by `task_id`. Actions: `status` (progress summary), `output` (full log), `tail` (last 50 lines). |
| `list_subagents` | List all active subagent tasks with progress summary. |

## Skills

| Tool | Notes |
|------|-------|
| `skills_list` | List all available skills (name + description). |
| `skill_view` | Load a skill's full SKILL.md content (progressive disclosure). |
| `skill_manage` | Create/edit/delete skills. Actions: `create`, `edit` (full rewrite), `patch` (targeted fix), `delete`, `write_file` (add supporting file), `remove_file`. |
| `skill_*` | Dynamic tools registered per skill script (e.g., `github.create_issue`). |

## Autopilot

| Tool | Notes |
|------|-------|
| `autopilot_intake` | Submit task to autopilot pipeline for independent execution and verification. Use for scheduled/automated tasks — use `todo` for in-session tracking. |
| `autopilot_*` | Additional autopilot tools for task lifecycle management (list, status, verify, etc.). |

## MCP

`mcp_<server>_<tool>` — Dynamic tools from connected MCP servers. Names follow `mcp_{server_name}_{tool_name}` pattern.

## Decision Guide

| Want to... | Use |
|------------|-----|
| Read file | `read_file` |
| Create/overwrite file | `write_file` |
| Edit file | `edit_file` |
| List directory | `list_dir` |
| Find by name | `glob` |
| Find in content | `grep` |
| Run shell command | `exec` (last resort) |
| Run Python code | `run_code` |
| Search web | `web_search` |
| Fetch URL | `web_fetch` |
| Extract URL content | `web_extract` |
| Send file to user | `message` + `media` |
| Ask user to choose | `ask_user_question` |
| Think/plan/reflect | `think` |
| Track tasks | `todo` |
| Schedule task | `cron` |
| Search memory | `memory_search` |
| Save to memory | `memory_save` |
| Explore codebase | `explore` |
| Browse context | `explore_context_catalog` |
| Background task | `spawn` |
| Use a skill | `skill_view` → skill scripts |
| Create a skill | `skill_manage` |
| Submit autopilot task | `autopilot_intake` |