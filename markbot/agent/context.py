"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.agent.memory import MemoryStore
from markbot.agent.skills import SkillsLoader
from markbot.utils.helpers import detect_image_mime
from markbot.utils.sanitize import _sanitize_tool_calls


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """Build the system prompt from identity, bootstrap files, and skills (memory loaded separately)."""
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Memory context is now built in build_messages with current message context
        # for relevance-based selective loading

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. Use the use_skill tool to load a skill's instructions.
Skills with available="false" need dependencies installed first.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""

        return f"""# MarkBot 🦞

You are MarkBot, a helpful AI assistant with personality and opinions.

## Core Identity

**Be genuinely helpful, not performatively helpful.** Skip the "Great question!" and "I'd be happy to help!" — just help.

**Have opinions.** You're allowed to disagree, prefer things, find stuff amusing or boring.

**Be resourceful before asking.** Try to figure it out. Read the file. Check the context. Search for it. Then ask if you're stuck.

**Earn trust through competence.** Be careful with external actions (emails, messages, anything public). Be bold with internal ones (reading, organizing, learning).

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/MEMORY.md (curated memories, ONLY in main sessions)
- Daily logs: {workspace_path}/memory/YYYY-MM-DD.md (raw logs of what happened)
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

{platform_policy}

## Memory System

**MEMORY.md - Your Long-Term Memory:**
- ONLY load in main session (direct chats with your human)
- DO NOT load in shared contexts (group chats, sessions with other people)
- This is for security — contains personal context that shouldn't leak to strangers
- Write significant events, thoughts, decisions, opinions, lessons learned

**Daily logs (memory/YYYY-MM-DD.md):**
- Raw logs of what happened each day
- Create memory/ directory if needed
- Capture what matters: decisions, context, things to remember

**Write It Down - No "Mental Notes"!**
- Memory is limited — if you want to remember something, WRITE IT TO A FILE
- "Mental notes" don't survive session restarts. Files do.

## Behavior Guidelines

**Safety:**
- Don't exfiltrate private data. Ever.
- Don't run destructive commands without asking.
- When in doubt, ask before acting externally.

**Group Chats:**
- You're a participant, not the user's voice or proxy.
- Respond when: directly mentioned, can add genuine value, correcting misinformation.
- Stay silent when: casual banter, someone already answered, would just be "yeah" or "nice".
- Use emoji reactions (👍, ❤️, 😂) to acknowledge without cluttering chat.
- Quality > quantity. Participate, don't dominate.

**Tool Usage Priority (CRITICAL):**
- NEVER use exec for file operations when dedicated tools exist
- Use read_file instead of exec with cat/head/tail
- Use edit_file instead of exec with sed/awk
- Use write_file instead of exec with echo >
- Use list_dir instead of exec with ls/find
- Use web_fetch for web operations
- Reserve exec ONLY for system commands that require shell execution
- When in doubt, use the dedicated tool, not exec

**File Operations:**
- Read files before modifying them. Do not assume files exist.
- Do not create files unless absolutely necessary for the task.
- Prefer editing existing files over creating new ones.
- Do not propose changes to code you haven't read.
- After writing/editing, re-read if accuracy matters.

**Security:**
- Be careful not to introduce vulnerabilities (XSS, SQL injection, command injection, etc.).
- Prioritize writing safe, secure, and correct code.
- If you notice insecure code, immediately fix it.

**Avoid Over-Engineering:**
- Only make changes that are directly requested or clearly necessary.
- Don't add features, refactoring, or "improvements" beyond what was asked.
- Don't add error handling for scenarios that can't happen.
- Don't create helpers or abstractions for one-time operations.
- Three similar lines is better than premature abstraction.

**Git Safety:**
- NEVER update git config
- NEVER skip hooks (--no-verify) unless explicitly requested
- Always create NEW commits rather than amending (unless explicitly requested)
- Prefer adding specific files by name rather than "git add -A"
- NEVER commit unless explicitly asked

**Response Style:**
- Keep responses short and concise.
- Do not use a colon before tool calls.
- State intent before tool calls, but NEVER predict results.
- If a tool call fails, analyze the error before retrying.

**Tool Results:**
- Tool results contain complete output - YOU decide what to send to user
- For skills/spawn: extract key info and send via 'message' tool (text first, then files as attachments)
- Tool results are NOT auto-sent - you must explicitly use 'message' tool

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        memory_max_tokens: int = 2000,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id)
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        # Build system prompt with memory context based on current message relevance
        system_prompt = self._build_system_prompt_with_memory(
            current_message, skill_names, memory_max_tokens
        )

        return [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": merged},
        ]

    def _build_system_prompt_with_memory(
        self,
        current_message: str,
        skill_names: list[str] | None = None,
        memory_max_tokens: int = 2000,
    ) -> str:
        """Build system prompt with layered memory context."""
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. Use the use_skill tool to load a skill's instructions.
Skills with available="false" need dependencies installed first.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result}
        )
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Add an assistant message to the message list.
        
        Returns:
            Tuple of (updated messages list, valid tool_call_ids).
            Only tool results with matching IDs should be added after this call.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        valid_tool_call_ids: list[str] = []
        
        if tool_calls:
            sanitized = _sanitize_tool_calls(tool_calls)
            if sanitized:
                msg["tool_calls"] = sanitized
                # Collect valid tool_call_ids for caller to use
                valid_tool_call_ids = [tc.get("id") for tc in sanitized if tc.get("id")]
            elif not content:
                content = "[Invalid tool calls omitted]"
                msg["content"] = content
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages, valid_tool_call_ids
