"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection.

Enhanced with features from openclaw-lark-main:
- Markdown style optimization (heading downgrade, table spacing, list normalization)
- Reply mode detection (auto/static/streaming)
- Reasoning text extraction and separation
- Improved group policy and allowlist handling
"""

import asyncio
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from markbot.bus.events import OutboundMessage
from markbot.bus.queue import MessageBus
from markbot.channels.base import BaseChannel
from markbot.config.paths import get_media_dir
from markbot.config.schema import FeishuConfig

import importlib.util

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None

MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}

ReplyMode = Literal["auto", "static", "streaming"]


def optimize_markdown_style(text: str, card_version: int = 2) -> str:
    """Optimize Markdown style for Feishu cards.

    - Heading downgrade: H1 → H4, H2~H6 → H5
    - Add paragraph spacing around tables
    - Normalize ordered/unordered lists
    - Protect code blocks from modification
    """
    try:
        r = _optimize_markdown_style(text, card_version)
        r = _strip_invalid_image_keys(r)
        return r
    except Exception:
        return text


def _optimize_markdown_style(text: str, card_version: int = 2) -> str:
    MARK = "___CB_"
    code_blocks: list[str] = []
    code_block_re = re.compile(r"```[\s\S]*?```")

    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"{MARK}{len(code_blocks) - 1}___"

    r = code_block_re.sub(save_code_block, text)

    has_h1_to_h3 = bool(re.search(r"^#{1,3} ", text, re.MULTILINE))
    if has_h1_to_h3:
        r = re.sub(r"^#{2,6} (.+)$", r"##### \1", r, flags=re.MULTILINE)
        r = re.sub(r"^# (.+)$", r"#### \1", r, flags=re.MULTILINE)

    if card_version >= 2:
        r = re.sub(r"^(#{4,5} .+)\n{1,2}(#{4,5} )", r"\1\n<br>\n\2", r, flags=re.MULTILINE)

        for i, block in enumerate(code_blocks):
            r = r.replace(f"{MARK}{i}___", f"\n{block}\n")
    else:
        for i, block in enumerate(code_blocks):
            r = r.replace(f"{MARK}{i}___", block)

    # 表格前后处理：<br> 本身就是换行，不要额外加 \n
    # 表格前：段落紧接表格的情况，在表格前加 <br>
    r = re.sub(r"([^\n|])\n(\|.+\|)", r"\1<br>\n\2", r)
    # 表格后：表格紧接段落的情况，在表格后加 <br>
    r = re.sub(r"(\|[^\n]*\|)\n([^|\n])", r"\1<br>\n\2", r)

    # 清理多余换行
    r = re.sub(r"\n{3,}", "\n\n", r)
    r = re.sub(r"(<br>)\n\n+", r"\1\n", r)
    r = re.sub(r"\n\n+(<br>)", r"\n\1", r)
    return r


def _strip_invalid_image_keys(text: str) -> str:
    """Strip ![alt](value) where value is not a valid Feishu image key."""
    if "![" not in text:
        return text

    def replace_image(m: re.Match) -> str:
        value = m.group(2)
        if value.startswith("img_"):
            return m.group(0)
        return ""

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", replace_image, text)


def resolve_reply_mode(
    feishu_cfg: FeishuConfig, chat_type: str | None = None
) -> Literal["static", "streaming"]:
    """Resolve the effective reply mode based on configuration and chat type.

    Priority: replyMode.{scene} > replyMode.default > replyMode (string) > "auto"
    """
    if not feishu_cfg.streaming:
        return "static"

    mode = feishu_cfg.reply_mode or "auto"
    if mode != "auto":
        return mode if mode != "streaming" else "streaming"

    if feishu_cfg.streaming:
        return "streaming" if chat_type == "p2p" else "static"
    return "static"


def split_reasoning_text(text: str | None) -> dict[str, str | None]:
    """Split payload text into optional reasoningText and answerText.

    Handles two formats:
    1. "Reasoning:\\n_italic line_\\n…" prefix
    2. `ahreing…hra` / `<thinking>…</thinking>` XML tags
    """
    if not text or not text.strip():
        return {}

    trimmed = text.strip()
    REASONING_PREFIX = "Reasoning:\n"

    if trimmed.startswith(REASONING_PREFIX) and len(trimmed) > len(REASONING_PREFIX):
        return {"reasoning_text": _clean_reasoning_prefix(trimmed)}

    tagged_reasoning = _extract_thinking_content(text)
    stripped_answer = _strip_reasoning_tags(text)

    if not tagged_reasoning and stripped_answer == text:
        return {"answer_text": text}

    return {
        "reasoning_text": tagged_reasoning or None,
        "answer_text": stripped_answer or None,
    }


def _extract_thinking_content(text: str) -> str:
    """Extract content from `ahreing`, `<thinking>`, `<thought>` blocks."""
    if not text:
        return ""

    scan_re = re.compile(r"<\s*(\/?)\s*(?:think(?:ing)?|thought|antthinking)\s*>", re.IGNORECASE)
    result_parts: list[str] = []
    last_idx = 0
    in_thinking = False

    for m in scan_re.finditer(text):
        is_closing = m.group(1) == "/"
        if in_thinking and is_closing:
            result_parts.append(text[last_idx:m.start()])
        in_thinking = not is_closing
        last_idx = m.end()

    if in_thinking:
        result_parts.append(text[last_idx:])

    return "".join(result_parts).strip()


def _strip_reasoning_tags(text: str) -> str:
    """Strip reasoning blocks - both XML tags with their content."""
    result = re.sub(
        r"<\s*(?:think(?:ing)?|thought|antthinking)\s*>[\s\S]*?<\s*\/\s*(?:think(?:ing)?|thought|antthinking)\s*>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    result = re.sub(r"<\s*(?:think(?:ing)?|thought|antthinking)\s*>[\s\S]*$", "", result, flags=re.IGNORECASE)
    result = re.sub(r"<\s*\/\s*(?:think(?:ing)?|thought|antthinking)\s*>", "", result, flags=re.IGNORECASE)
    return result.strip()


def _clean_reasoning_prefix(text: str) -> str:
    """Clean a 'Reasoning:\\n_italic_' formatted message back to plain text."""
    cleaned = re.sub(r"^Reasoning:\s*", "", text, flags=re.IGNORECASE)
    cleaned = "\n".join(re.sub(r"^_(.+)_$", r"\1", line) for line in cleaned.split("\n"))
    return cleaned.strip()


def resolve_feishu_allowlist_match(
    allow_from: list[str | int], sender_id: str, sender_name: str | None = None
) -> dict[str, Any]:
    """Check whether a sender is permitted by a given allowlist."""
    normalized = [str(e).strip().lower() for e in allow_from if str(e).strip()]

    if not normalized:
        return {"allowed": False}

    if "*" in normalized:
        return {"allowed": True, "match_key": "*", "match_source": "wildcard"}

    sender_lower = sender_id.lower()
    if sender_lower in normalized:
        return {"allowed": True, "match_key": sender_lower, "match_source": "id"}

    return {"allowed": False}


def is_feishu_group_allowed(
    group_policy: str, allow_from: list[str | int], sender_id: str, sender_name: str | None = None
) -> bool:
    """Determine whether an inbound group message should be processed."""
    if group_policy == "disabled":
        return False
    if group_policy == "open":
        return True
    return resolve_feishu_allowlist_match(allow_from, sender_id, sender_name)["allowed"]


def split_legacy_group_allow_from(raw: list[str | int]) -> dict[str, list[str]]:
    """Split raw groupAllowFrom into legacy chat-ID entries and sender entries."""
    legacy_chat_ids: list[str] = []
    sender_allow_from: list[str] = []

    for entry in raw:
        s = str(entry)
        if s.startswith("oc_"):
            legacy_chat_ids.append(s)
        else:
            sender_allow_from.append(s)

    return {"legacy_chat_ids": legacy_chat_ids, "sender_allow_from": sender_allow_from}


def _truncate_safe(s: str | None, max_len: int = 16, suffix: str = "...") -> str:
    """Safely truncate a string, handling None values.

    Returns a truncated string with suffix if longer than max_len,
    or the original string if shorter, or 'None' if s is None.
    """
    if s is None:
        return "None"
    if len(s) <= max_len:
        return s
    return s[:max_len] + suffix


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    for elements in content.get("elements", []) if isinstance(content.get("elements"), list) else []:
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []

    if not isinstance(element, dict):
        return parts

    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract text and image keys from Feishu post (rich text) message.

    Handles three payload shapes:
    - Direct:    {"title": "...", "content": [[...]]}
    - Localized: {"zh_cn": {"title": "...", "content": [...]}}
    - Wrapped:   {"post": {"zh_cn": {"title": "...", "content": [...]}}}
    """

    def _parse_block(block: dict) -> tuple[str | None, list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if title := block.get("title"):
            texts.append(title)
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    # Unwrap optional {"post": ...} envelope
    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    # Direct format
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    # Localized: prefer known locales, then fall back to any dict child
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.

    Legacy wrapper for _extract_post_content, returns only text.
    """
    text, _ = _extract_post_content(content_json)
    return text


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"

    def __init__(self, config: FeishuConfig, bus: MessageBus, groq_api_key: str = ""):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self.groq_api_key = groq_api_key
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """Register an event handler only when the SDK supports it."""
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        import lark_oapi as lark
        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.DEBUG) \
            .build()
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_deleted_v1", self._on_reaction_deleted
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        event_handler = builder.build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG
        )

        # Start WebSocket client in a separate thread with reconnect loop.
        # A dedicated event loop is created for this thread so that lark_oapi's
        # module-level `loop = asyncio.get_event_loop()` picks up an idle loop
        # instead of the already-running main asyncio loop, which would cause
        # "This event loop is already running" errors.
        def run_ws():
            import time
            import lark_oapi.ws.client as _lark_ws_client
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            # Patch the module-level loop used by lark's ws Client.start()
            _lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        self._ws_client.start()
                    except Exception as e:
                        logger.warning("Feishu WebSocket error: {}", e)
                    if self._running:
                        time.sleep(5)
            finally:
                ws_loop.close()

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        Stop the Feishu bot.

        Notice: lark.ws.Client does not expose stop method， simply exiting the program will close the client.

        Reference: https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        logger.info("Feishu bot stopped")

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Check if the bot is @mentioned in the message."""
        if not message:
            return False

        raw_content = getattr(message, "content", None) or ""
        if "@_all" in raw_content:
            return True

        mentions = getattr(message, "mentions", None)
        if not mentions:
            return False

        for mention in mentions if isinstance(mentions, (list, tuple)) else []:
            if not mention:
                continue
            mid = getattr(mention, "id", None)
            if not mid:
                continue
            user_id = getattr(mid, "user_id", None)
            open_id = getattr(mid, "open_id", None)
            if not user_id and (open_id or "").startswith("ou_"):
                return True
        return False

    def _is_group_message_for_bot(self, message: Any, chat_id: str, sender_id: str) -> bool:
        """Check if a group message should be processed.

        Uses two-layer access control:
        1. Group-level: Check if the group is in the allowlist
        2. Sender-level: Check if the sender is allowed within the group
        """
        group_policy = self.config.group_policy
        if group_policy == "disabled":
            return False

        if group_policy == "open":
            if self.config.require_mention:
                return self._is_bot_mentioned(message)
            return True

        legacy = split_legacy_group_allow_from(self.config.group_allow_from)
        legacy_chat_ids = legacy["legacy_chat_ids"]
        sender_allow_from = legacy["sender_allow_from"]

        chat_id_lower = chat_id.lower()
        if any(cid.lower() == chat_id_lower for cid in legacy_chat_ids):
            return True

        group_config = self.config.groups.get(chat_id) or self.config.groups.get("*")
        if group_config:
            if group_config.enabled is False:
                return False

            per_group_policy = group_config.group_policy
            if per_group_policy == "disabled":
                return False
            if per_group_policy == "open":
                if self.config.require_mention:
                    return self._is_bot_mentioned(message)
                return True

            merged_allow = list(set(sender_allow_from + (group_config.allow_from or [])))
            if merged_allow:
                return is_feishu_group_allowed("allowlist", merged_allow, sender_id)

        if sender_allow_from:
            return is_feishu_group_allowed("allowlist", sender_allow_from, sender_id)

        if self.config.require_mention:
            return self._is_bot_mentioned(message)
        return True

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """Sync helper for adding reaction (runs in thread pool). Returns reaction_id."""
        from lark_oapi.api.im.v1 import CreateMessageReactionRequest, CreateMessageReactionRequestBody, Emoji
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning("Failed to add reaction: code={}, msg={}", response.code, response.msg)
                return None
            else:
                # Safely access response.data
                data = getattr(response, "data", None)
                reaction_id = getattr(data, "reaction_id", None) if data else None
                if reaction_id:
                    logger.debug("Added {} reaction to message {}: {}", emoji_type, message_id, reaction_id)
                return reaction_id
        except Exception as e:
            logger.warning("Error adding reaction: {}", e)
            return None

    def _delete_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        """Sync helper for deleting reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest
        try:
            request = DeleteMessageReactionRequest.builder() \
                .message_id(message_id) \
                .reaction_id(reaction_id) \
                .build()

            response = self._client.im.v1.message_reaction.delete(request)

            if not response.success():
                logger.warning("Failed to delete reaction: code={}, msg={}", response.code, response.msg)
            else:
                logger.debug("Deleted reaction {} from message {}", reaction_id, message_id)
        except Exception as e:
            logger.warning("Error deleting reaction: {}", e)

    async def _add_reaction(self, message_id: str, emoji_type: str = "Typing") -> str | None:
        """
        Add a reaction emoji to a message (non-blocking).

        Common emoji types: THUMBSUP,THUMBSDOWN,HEART,SMILE,JOYFUL,FROWN,BLUSH,OK,CLAP,FIREWORKS,PARTY,MUSCLE,FIRE,EYES,THINKING,PRAISE,PRAY,ROCKET,DONE,SKULL,HUNDREDPOINTS,FACEPALM,CHECK,CrossMark,COOL,Typing,SPEECHLESS
        Returns reaction_id for later deletion.
        """
        if not self._client:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    async def _delete_reaction(self, message_id: str, reaction_id: str) -> None:
        """Delete a reaction emoji from a message (non-blocking)."""
        if not self._client:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._delete_reaction_sync, message_id, reaction_id)

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None
        def split(_line: str) -> list[str]:
            return [c.strip() for c in _line.strip("|").split("|")]
        headers = split(lines[0])
        rows = [split(_line) for _line in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str, state: str = "complete") -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card.

        States:
        - "thinking": Show loading indicator
        - "streaming": Show content being streamed
        - "complete": Show final content
        """
        optimized = optimize_markdown_style(content)
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(optimized):
            before = optimized[last_end:m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = optimized[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))

        if not elements:
            elements = [{"tag": "markdown", "content": optimized}]

        if state == "streaming":
            elements.append({
                "tag": "markdown",
                "content": " ",
                "icon": {
                    "tag": "custom_icon",
                    "img_key": "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg",
                    "size": "16px 16px",
                },
                "element_id": "loading_icon",
            })

        return elements

    def _build_thinking_card(self) -> dict:
        """Build a thinking/loading card for streaming mode."""
        return {
            "config": {
                "wide_screen_mode": True,
                "streaming_mode": True,
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": "Thinking...",
                    "i18n_content": {"zh_cn": "思考中...", "en_us": "Thinking..."},
                }
            ],
        }

    def _build_reasoning_card(self, reasoning_text: str, elapsed_ms: int | None = None) -> dict:
        """Build a card showing reasoning/thinking content."""
        elapsed_str = ""
        if elapsed_ms:
            seconds = elapsed_ms / 1000
            elapsed_str = f" ({seconds:.1f}s)" if seconds < 60 else f" ({int(seconds // 60)}m {int(seconds % 60)}s)"

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"Reasoning{elapsed_str}",
                    "i18n_content": {"zh_cn": f"思考过程{elapsed_str}", "en_us": f"Reasoning{elapsed_str}"},
                },
                "template": "turquoise",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": reasoning_text,
                    "text_size": "notation",
                }
            ],
        }

    @staticmethod
    def _split_elements_by_table_limit(elements: list[dict], max_tables: int = 1) -> list[list[dict]]:
        """Split card elements into groups with at most *max_tables* table elements each.

        Feishu cards have a hard limit of one table per card (API error 11310).
        When the rendered content contains multiple markdown tables each table is
        placed in a separate card message so every table reaches the user.
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = m.group(2).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{text}**",
                },
            })
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    # ── Smart format detection ──────────────────────────────────────────
    # Patterns that indicate "complex" markdown needing card rendering
    _COMPLEX_MD_RE = re.compile(
        r"```"                        # fenced code block
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # markdown table (header + separator)
        r"|^#{1,6}\s+"                # headings
        , re.MULTILINE,
    )

    # Simple markdown patterns (bold, italic, strikethrough)
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"               # **bold**
        r"|__.+?__"                   # __bold__
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *italic* (single *)
        r"|~~.+?~~"                   # ~~strikethrough~~
        , re.DOTALL,
    )

    # Markdown link: [text](url)
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    # Unordered list items
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

    # Ordered list items
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    # Max length for plain text format
    _TEXT_MAX_LEN = 200

    # Max length for post (rich text) format; beyond this, use card
    _POST_MAX_LEN = 2000

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """Determine the optimal Feishu message format for *content*.

        Returns one of:
        - ``"text"``        – plain text, short and no markdown
        - ``"post"``        – rich text (links only, moderate length)
        - ``"interactive"`` – card with full markdown rendering
        """
        stripped = content.strip()

        # Complex markdown (code blocks, tables, headings) → always card
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"

        # Long content → card (better readability with card layout)
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"

        # Has bold/italic/strikethrough → card (post format can't render these)
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"

        # Has list items → card (post format can't render list bullets well)
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"

        # Has links → post format (supports <a> tags)
        if cls._MD_LINK_RE.search(stripped):
            return "post"

        # Short plain text → text format
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"

        # Medium plain text without any formatting → post format
        return "post"

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        """Convert markdown content to Feishu post message JSON.

        Handles links ``[text](url)`` as ``a`` tags; everything else as ``text`` tags.
        Each line becomes a paragraph (row) in the post body.
        """
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for m in cls._MD_LINK_RE.finditer(line):
                # Text before this link
                before = line[last_end:m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append({
                    "tag": "a",
                    "text": m.group(1),
                    "href": m.group(2),
                })
                last_end = m.end()

            # Remaining text after last link
            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            # Empty line → empty paragraph for spacing
            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        post_body = {
            "zh_cn": {
                "content": paragraphs,
            }
        }
        return json.dumps(post_body, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}
    _FILE_TYPE_MAP = {
        ".opus": "opus", ".mp4": "mp4", ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
        ".xls": "xls", ".xlsx": "xls", ".ppt": "ppt", ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
        logger.debug("Feishu: uploading image {}", os.path.basename(file_path))
        try:
            file_size = os.path.getsize(file_path)
            logger.debug("Feishu: image size = {} bytes", file_size)
            with open(file_path, "rb") as f:
                request = CreateImageRequest.builder() \
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    ).build()
                response = self._client.im.v1.image.create(request)
                if response.success():
                    data = getattr(response, "data", None)
                    image_key = getattr(data, "image_key", None) if data else None
                    if image_key:
                        logger.info("Feishu: uploaded image {} -> key={}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    logger.error("Feishu: failed to upload image {}: code={}, msg={}",
                               os.path.basename(file_path), response.code, response.msg)
                    return None
        except Exception as e:
            logger.exception("Feishu: error uploading image {}: {}", file_path, e)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        logger.debug("Feishu: uploading file {} (type={})", file_name, file_type)
        try:
            file_size = os.path.getsize(file_path)
            logger.debug("Feishu: file size = {} bytes", file_size)
            with open(file_path, "rb") as f:
                request = CreateFileRequest.builder() \
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    ).build()
                response = self._client.im.v1.file.create(request)
                if response.success():
                    data = getattr(response, "data", None)
                    file_key = getattr(data, "file_key", None) if data else None
                    if file_key:
                        logger.info("Feishu: uploaded file {} -> key={}", file_name, file_key)
                    return file_key
                else:
                    logger.error("Feishu: failed to upload file {}: code={}, msg={}",
                               file_name, response.code, response.msg)
                    return None
        except Exception as e:
            logger.exception("Feishu: error uploading file {}: {}", file_path, e)
            return None

    def _download_image_sync(self, message_id: str, image_key: str) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        logger.debug("Feishu: downloading image key={} from msg={}",
                    _truncate_safe(image_key), _truncate_safe(message_id))
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, 'read'):
                    file_data = file_data.read()
                file_size = len(file_data) if file_data else 0
                logger.info("Feishu: downloaded image {} ({} bytes, name={})",
                           _truncate_safe(image_key), file_size, response.file_name)
                return file_data, response.file_name
            else:
                logger.error("Feishu: failed to download image {}: code={}, msg={}", image_key, response.code, response.msg)
                return None, None
        except Exception as e:
            logger.exception("Feishu: error downloading image {}: {}", image_key, e)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu API only accepts 'image' or 'file' as type parameter
        # Convert 'audio' to 'file' for API compatibility
        if resource_type == "audio":
            resource_type = "file"

        logger.debug("Feishu: downloading {} key={} from msg={}",
                    resource_type, _truncate_safe(file_key), _truncate_safe(message_id))
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                file_size = len(file_data) if file_data else 0
                logger.info("Feishu: downloaded {} {} ({} bytes, name={})",
                           resource_type, _truncate_safe(file_key), file_size, response.file_name)
                return file_data, response.file_name
            else:
                logger.error("Feishu: failed to download {}: code={}, msg={}", resource_type, response.code, response.msg)
                return None, None
        except Exception as e:
            logger.exception("Feishu: error downloading {} {}: {}", resource_type, file_key, e)
            return None, None

    async def _download_and_save_media(
        self,
        msg_type: str,
        content_json: dict,
        message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        Download media from Feishu and save to local disk.

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("feishu")

        data, filename = None, None

        logger.debug("Feishu: downloading {} media, msg_id={}", msg_type, _truncate_safe(message_id))

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                logger.debug("Feishu: image_key={}", _truncate_safe(image_key))
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = f"{image_key[:16]}.jpg"
            else:
                logger.warning("Feishu: missing image_key or message_id for image download")

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if file_key and message_id:
                logger.debug("Feishu: file_key={}", _truncate_safe(file_key))
                data, filename = await loop.run_in_executor(
                    None, self._download_file_sync, message_id, file_key, msg_type
                )
                if not filename:
                    filename = file_key[:16]
                if msg_type == "audio" and not filename.endswith(".opus"):
                    filename = f"{filename}.opus"
            else:
                logger.warning("Feishu: missing file_key or message_id for {} download", msg_type)

        if data and filename:
            file_path = media_dir / filename
            file_path.write_bytes(data)
            logger.info("Feishu: saved {} to {} ({} bytes)", msg_type, file_path, len(data))
            return str(file_path), f"[{msg_type}: {filename}]"

        logger.warning("Feishu: failed to download {} media", msg_type)
        return None, f"[{msg_type}: download failed]"

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> bool:
        """Send a single message (text/image/file/interactive) synchronously."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        logger.debug("Feishu: sending {} message to {} (type={})",
                    msg_type, _truncate_safe(receive_id), receive_id_type)
        try:
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Feishu: failed to send {} message: code={}, msg={}, log_id={}, receive_id={}",
                    msg_type, response.code, response.msg, response.get_log_id(),
                    _truncate_safe(receive_id)
                )
                return False
            logger.info("Feishu: sent {} message to {}", msg_type, _truncate_safe(receive_id))
            return True
        except Exception as e:
            logger.exception("Feishu: error sending {} message to {}: {}", msg_type, receive_id, e)
            return False

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present.

        Enhanced features:
        - Reasoning text separation for thinking models
        - Markdown style optimization for Feishu cards
        - Reply mode support (static/streaming)
        """
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        if not msg.chat_id:
            logger.warning("Feishu: chat_id is None, cannot send message")
            return

        content_preview = (msg.content[:100] + "...") if msg.content and len(msg.content) > 100 else (msg.content or "")
        logger.info(
            "Feishu sending: chat={}, media_count={}, content_len={}, preview={}",
            _truncate_safe(msg.chat_id),
            len(msg.media),
            len(msg.content) if msg.content else 0,
            content_preview.replace("\n", "\\n"),
        )

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                logger.debug("Feishu: uploading media file {}", os.path.basename(file_path))
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        logger.info(
                            "Feishu: sent image {} to {}",
                            os.path.basename(file_path),
                            _truncate_safe(msg.chat_id),
                        )
                        await loop.run_in_executor(
                            None,
                            self._send_message_sync,
                            receive_id_type,
                            msg.chat_id,
                            "image",
                            json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                    else:
                        logger.warning("Feishu: failed to upload image {}", os.path.basename(file_path))
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        if ext in self._AUDIO_EXTS or ext in self._VIDEO_EXTS:
                            media_type = "media"
                        else:
                            media_type = "file"
                        logger.info(
                            "Feishu: sent {} {} to {}",
                            media_type,
                            os.path.basename(file_path),
                            _truncate_safe(msg.chat_id),
                        )
                        await loop.run_in_executor(
                            None,
                            self._send_message_sync,
                            receive_id_type,
                            msg.chat_id,
                            media_type,
                            json.dumps({"file_key": key}, ensure_ascii=False),
                        )
                    else:
                        logger.warning("Feishu: failed to upload file {}", os.path.basename(file_path))

            if msg.content and msg.content.strip():
                split_result = split_reasoning_text(msg.content)
                reasoning_text = split_result.get("reasoning_text")
                answer_text = split_result.get("answer_text") or msg.content

                if reasoning_text:
                    reasoning_card = self._build_reasoning_card(reasoning_text)
                    logger.debug("Feishu: sending reasoning card")
                    await loop.run_in_executor(
                        None,
                        self._send_message_sync,
                        receive_id_type,
                        msg.chat_id,
                        "interactive",
                        json.dumps(reasoning_card, ensure_ascii=False),
                    )

                content_to_send = answer_text
                fmt = self._detect_msg_format(content_to_send)
                logger.debug("Feishu: detected format '{}' for content length {}", fmt, len(content_to_send))

                if fmt == "text":
                    text_body = json.dumps({"text": content_to_send.strip()}, ensure_ascii=False)
                    logger.info("Feishu: sent text message to {}", _truncate_safe(msg.chat_id))
                    await loop.run_in_executor(
                        None,
                        self._send_message_sync,
                        receive_id_type,
                        msg.chat_id,
                        "text",
                        text_body,
                    )

                elif fmt == "post":
                    post_body = self._markdown_to_post(content_to_send)
                    logger.info("Feishu: sent post message to {}", _truncate_safe(msg.chat_id))
                    await loop.run_in_executor(
                        None,
                        self._send_message_sync,
                        receive_id_type,
                        msg.chat_id,
                        "post",
                        post_body,
                    )

                else:
                    elements = self._build_card_elements(content_to_send)
                    chunks = self._split_elements_by_table_limit(elements)
                    logger.info(
                        "Feishu: sending {} card(s) to {}",
                        len(chunks),
                        _truncate_safe(msg.chat_id),
                    )
                    for i, chunk in enumerate(chunks):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        logger.debug("Feishu: sending card {} of {}", i + 1, len(chunks))
                        await loop.run_in_executor(
                            None,
                            self._send_message_sync,
                            receive_id_type,
                            msg.chat_id,
                            "interactive",
                            json.dumps(card, ensure_ascii=False),
                        )

            if msg.metadata and msg.metadata.get("reaction_id") and msg.metadata.get("message_id"):
                logger.debug(
                    "Feishu: deleting reaction {} from msg {}",
                    msg.metadata.get("reaction_id"),
                    msg.metadata.get("message_id"),
                )
                await self._delete_reaction(msg.metadata["message_id"], msg.metadata["reaction_id"])

        except Exception as e:
            logger.exception("Error sending Feishu message: {}", e)

    def _on_message_sync(self, data: Any) -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        # Log at the very beginning to confirm SDK called this handler
        logger.info("Feishu: _on_message_sync called, data type={}", type(data).__name__ if data else "None")
        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)
        except Exception as e:
            logger.exception("Feishu: error in _on_message_sync: {}", e)

    async def _on_message(self, data: Any) -> None:
        """Handle incoming message from Feishu."""
        try:
            # Safely extract event, message, and sender with null checks
            if not data:
                logger.warning("Feishu: received empty data")
                return

            event = getattr(data, "event", None)
            if not event:
                logger.warning("Feishu: event is None")
                return

            message = getattr(event, "message", None)
            sender = getattr(event, "sender", None)

            if not message:
                logger.warning("Feishu: message is None")
                return

            # Deduplication check
            message_id = getattr(message, "message_id", None)
            if not message_id:
                logger.warning("Feishu: message_id is None")
                return

            if message_id in self._processed_message_ids:
                logger.debug("Feishu: duplicate message {} skipped", message_id)
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            sender_type = getattr(sender, "sender_type", None) if sender else None
            if sender_type == "bot":
                logger.debug("Feishu: skipping bot message {}", message_id)
                return

            # Safely extract sender_id
            sender_id_obj = getattr(sender, "sender_id", None) if sender else None
            sender_id = getattr(sender_id_obj, "open_id", "unknown") if sender_id_obj else "unknown"

            chat_id = getattr(message, "chat_id", None)
            chat_type = getattr(message, "chat_type", None)
            msg_type = getattr(message, "message_type", None)

            if not chat_id or not chat_type or not msg_type:
                logger.warning("Feishu: missing required message fields (chat_id={}, chat_type={}, msg_type={})",
                              chat_id, chat_type, msg_type)
                return

            # Log incoming message details
            logger.info("Feishu received: msg_id={}, type={}, chat_type={}, sender={}, chat={}",
                       message_id, msg_type, chat_type, _truncate_safe(sender_id),
                       _truncate_safe(chat_id))

            if chat_type == "group" and not self._is_group_message_for_bot(message, chat_id, sender_id):
                logger.debug("Feishu: skipping group message (policy check failed)")
                return

            # Add reaction and store reaction_id for later deletion
            reaction_id = await self._add_reaction(message_id, self.config.react_emoji)
            logger.debug("Feishu: added reaction {} to msg {}", reaction_id or "failed", message_id)

            # Parse content
            content_parts = []
            media_paths = []

            raw_content = getattr(message, "content", None)
            logger.debug("Feishu: raw content length={}", len(raw_content) if raw_content else 0)
            try:
                content_json = json.loads(raw_content) if raw_content else {}
            except json.JSONDecodeError as e:
                logger.warning("Feishu: JSON parse error for msg {}: {}", message_id, e)
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(msg_type, content_json, message_id)
                if file_path:
                    media_paths.append(file_path)

                # Transcribe audio using Groq Whisper
                if msg_type == "audio" and file_path and self.groq_api_key:
                    try:
                        from markbot.providers.transcription import GroqTranscriptionProvider
                        transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                        transcription = await transcriber.transcribe(file_path)
                        if transcription:
                            content_text = f"[transcription: {transcription}]"
                    except Exception as e:
                        logger.warning("Failed to transcribe audio: {}", e)

                content_parts.append(content_text)

            elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                logger.debug("Feishu: msg {} has no content or media, skipping", message_id)
                return

            # Log parsed content summary
            content_preview = content[:100] + "..." if len(content) > 100 else content
            logger.info("Feishu parsed: msg_id={}, content_len={}, media_count={}, preview={}",
                       message_id, len(content), len(media_paths), content_preview.replace("\n", "\\n"))

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            logger.debug("Feishu: forwarding to bus, reply_to={}", _truncate_safe(reply_to))
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                    "reaction_id": reaction_id,
                }
            )

        except Exception as e:
            logger.exception("Error processing Feishu message: {}", e)

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction events so they do not generate SDK noise."""
        pass

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction deleted events so they do not generate SDK noise."""
        pass

    def _on_message_read(self, data: Any) -> None:
        """Ignore read events so they do not generate SDK noise."""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Ignore p2p-enter events when a user opens a bot chat."""
        logger.debug("Bot entered p2p chat (user opened chat window)")
        pass
