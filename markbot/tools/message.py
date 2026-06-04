"""Message tool for sending messages to users."""

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from markbot.bus.events import OutboundMessage
from markbot.tools.base import Tool

if TYPE_CHECKING:
    from markbot.types.tool import ToolContext


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._sent_in_turn: bool = False
        self.last_message: OutboundMessage | None = None
        self._last_routed_channel: str = ""
        self._last_routed_chat_id: str = ""

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context (fallback for non-ToolContext callers)."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send a message to the user, optionally with file attachments. "
            "This is the ONLY way to deliver files (images, documents, audio, video) to the user. "
            "Use the 'media' parameter with file paths to attach files. "
            "Do NOT use read_file to send files — that only reads content for your own analysis."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    def _resolve_routing(self, context: "ToolContext | None", channel: str | None, chat_id: str | None, message_id: str | None) -> tuple[str, str, str | None]:
        """Resolve routing info: prefer ToolContext, then explicit params, then defaults."""
        ctx_ch = context.channel if context else ""
        ctx_cid = context.chat_id if context else ""
        ctx_mid = context.message_id if context else None

        resolved_channel = channel or ctx_ch or self._default_channel
        resolved_chat_id = chat_id or ctx_cid or self._default_chat_id
        resolved_message_id = message_id or ctx_mid or self._default_message_id

        self._last_routed_channel = resolved_channel
        self._last_routed_chat_id = resolved_chat_id

        return resolved_channel, resolved_chat_id, resolved_message_id

    async def _legacy_execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        context = kwargs.get("_tool_context")
        channel, chat_id, message_id = self._resolve_routing(context, channel, chat_id, message_id)

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata={
                "message_id": message_id,
            },
        )

        self.last_message = msg

        try:
            await self._send_callback(msg)
            if channel == self._last_routed_channel and chat_id == self._last_routed_chat_id:
                self._sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
