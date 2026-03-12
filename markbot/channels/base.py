"""Base channel interface for chat platforms."""

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.bus.queue import MessageBus


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the markbot message bus.
    """

    name: str = "base"

    def __init__(self, config: Any, bus: MessageBus):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus
        self._running = False
        self._allow_cache = None
        self._allow_cache_time = 0

    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.
        """
        pass

    def reload_allow_list(self) -> None:
        """Reload allow list from config file."""
        import time
        from markbot.config.loader import load_config
        config = load_config()
        channel_config = getattr(config.channels, self.name, None)
        if channel_config:
            self.config = channel_config
            self._allow_cache = None
            self._allow_cache_time = time.time()
            logger.info("{}: Reloaded allow list", self.name)

    def is_allowed(self, sender_id: str) -> bool:
        """Check if *sender_id* is permitted.  Empty list → deny all; ``"*"`` → allow all."""
        import time
        # Reload cache every 5 seconds
        if self._allow_cache is None or time.time() - self._allow_cache_time > 5:
            self._allow_cache = getattr(self.config, "allow_from", [])
            self._allow_cache_time = time.time()

        allow_list = self._allow_cache
        if not allow_list:
            logger.warning("{}: allow_from is empty — all access denied", self.name)
            return False
        if "*" in allow_list:
            return True
        return str(sender_id) in allow_list

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
            session_key: Optional session key override (e.g. thread-scoped sessions).
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender {} on channel {}. "
                "Add them to allowFrom list in config to grant access.",
                sender_id, self.name,
            )
            await self._send_access_denied_message(sender_id, chat_id)
            return

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {},
            session_key_override=session_key,
        )

        await self.bus.publish_inbound(msg)

    async def _send_access_denied_message(self, sender_id: str, chat_id: str) -> None:
        """Send access denied message to user."""
        import secrets
        import json
        import os
        from pathlib import Path
        from datetime import datetime

        # Store pairing request
        pairing_file = Path.home() / ".markbot" / "gateway" / "pairings.json"
        pairing_file.parent.mkdir(parents=True, exist_ok=True)

        pairings = {}
        if pairing_file.exists():
            with open(pairing_file) as f:
                pairings = json.load(f)

        # Check if user already has a pending pairing request
        pairing_code = None
        for code, info in pairings.items():
            if info.get("channel") == self.name and info.get("sender_id") == sender_id:
                pairing_code = code
                break

        # Create new pairing code if none exists
        if not pairing_code:
            pairing_code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
            pairings[pairing_code] = {
                "channel": self.name,
                "sender_id": sender_id,
                "chat_id": chat_id,
                "created_at": datetime.now().isoformat()
            }

            with open(pairing_file, "w") as f:
                json.dump(pairings, f, indent=2)

        message = (
            f"MarkBot: access not configured.\n"
            f"{sender_id}\n"
            f"Pairing code: {pairing_code}\n\n"
            f"Ask the bot owner to approve with:\n"
            f"markbot pairing approve {self.name} {pairing_code}"
        )

        outbound = OutboundMessage(
            channel=self.name,
            chat_id=chat_id,
            content=message,
            metadata={}
        )

        try:
            await self.send(outbound)
        except Exception as e:
            logger.error("Failed to send access denied message: {}", e)

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running
