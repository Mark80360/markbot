"""Base channel interface for chat platforms."""

from __future__ import annotations

import asyncio
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

from markbot.bus.events import InboundMessage, OutboundMessage
from markbot.bus.queue import MessageBus

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


class SendErrorKind(str, Enum):
    """Outbound send failure categories (mirrors hermes-agent gateway)."""

    TOO_LONG = "too_long"
    BAD_FORMAT = "bad_format"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


class SendError(RuntimeError):
    """Raised by channels on delivery failure with a structured classification.

    Channels that can distinguish failure reasons should raise this instead of
    a bare RuntimeError so the channel manager can apply the right policy:

    - ``rate_limited`` with ``retry_after`` → wait server-authoritative seconds.
    - ``not_found`` / ``forbidden`` with ``chat_level=True`` → mark chat dead.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: SendErrorKind = SendErrorKind.UNKNOWN,
        retry_after: float | None = None,
        chat_level: bool = True,
    ):
        super().__init__(message)
        self.kind = SendErrorKind(kind)
        self.retry_after = retry_after
        self.chat_level = chat_level


@dataclass
class SendErrorInfo:
    """Classification result for an arbitrary send exception."""

    kind: SendErrorKind
    retry_after: float | None = None
    chat_level: bool = True

    @classmethod
    def of(cls, exc: Exception) -> "SendErrorInfo":
        return classify_send_error(exc)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


_RATE_LIMIT_PATTERNS = (
    "rate.limit",
    "too.many.requests",
    "flood",
    "retry.after",
    "throttl",
    "frequen",
    "\\b429\\b",
)
_FORBIDDEN_PATTERNS = (
    "forbidden",
    "permission.denied",
    "banned",
    "denied",
    "not.authorized",
    "unauthorized",
    "no permission",
)
_NOT_FOUND_PATTERNS = (
    "not.found",
    "\\b404\\b",
    "does.not.exist",
    "invalid.chat",
    "conversation.*not",
    "chat.*not.found",
    "user.*not.found",
)
_TOO_LONG_PATTERNS = (
    "too.long",
    "too.large",
    "exceeds.*limit",
    "max.*length",
    "\\b413\\b",
    "too many bytes",
)


def _extract_retry_after(exc: Exception) -> float | None:
    if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        header = exc.response.headers.get("Retry-After")
    else:
        header = None
    if header:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass
    return None


def classify_send_error(exc: Exception) -> SendErrorInfo:
    """Best-effort classification of an outbound send exception.

    Channels should prefer raising :class:`SendError` with an explicit kind;
    this is a heuristic fallback for arbitrary exceptions (SDK errors, bare
    RuntimeError, httpx failures, ...).
    """
    if isinstance(exc, SendError):
        return SendErrorInfo(exc.kind, exc.retry_after, exc.chat_level)

    text = f"{type(exc).__name__}: {exc}"

    # Timeouts: unambiguous transient failures.
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return SendErrorInfo(SendErrorKind.TRANSIENT)

    if httpx is not None:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code == 403:
                return SendErrorInfo(SendErrorKind.FORBIDDEN)
            if code == 404:
                return SendErrorInfo(SendErrorKind.NOT_FOUND, chat_level=True)
            if code == 413:
                return SendErrorInfo(SendErrorKind.TOO_LONG)
            if code == 429:
                return SendErrorInfo(
                    SendErrorKind.RATE_LIMITED,
                    retry_after=_extract_retry_after(exc),
                )
            if 400 <= code < 500:
                return SendErrorInfo(SendErrorKind.BAD_FORMAT)
            if 500 <= code < 600:
                return SendErrorInfo(SendErrorKind.TRANSIENT)
        elif isinstance(exc, httpx.TransportError):
            # Connect/read/write/pool errors: no response, safe to retry.
            return SendErrorInfo(SendErrorKind.TRANSIENT)

    if _matches(text, _RATE_LIMIT_PATTERNS):
        return SendErrorInfo(SendErrorKind.RATE_LIMITED)
    if _matches(text, _FORBIDDEN_PATTERNS):
        return SendErrorInfo(SendErrorKind.FORBIDDEN)
    if _matches(text, _NOT_FOUND_PATTERNS):
        return SendErrorInfo(SendErrorKind.NOT_FOUND)
    if _matches(text, _TOO_LONG_PATTERNS):
        return SendErrorInfo(SendErrorKind.TOO_LONG)
    return SendErrorInfo(SendErrorKind.UNKNOWN)


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    to integrate with the markbot message bus.
    """

    name: str = "base"
    display_name: str = "Base"
    transcription_api_key: str = ""

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

    async def transcribe_audio(self, file_path: str | Path) -> str:
        """Transcribe an audio file via Groq Whisper. Returns empty string on failure."""
        if not self.transcription_api_key:
            return ""
        try:
            from markbot.providers.transcription import GroqTranscriptionProvider

            provider = GroqTranscriptionProvider(api_key=self.transcription_api_key)
            return await provider.transcribe(file_path)
        except Exception as e:
            logger.warning("{}: audio transcription failed: {}", self.name, e)
            return ""

    async def login(self, force: bool = False) -> bool:
        """
        Perform channel-specific interactive login (e.g. QR code scan).

        Args:
            force: If True, ignore existing credentials and force re-authentication.

        Returns True if already authenticated or login succeeds.
        Override in subclasses that support interactive login.
        """
        return True

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

        Implementations should raise on delivery failure so the channel manager
        can apply any retry policy in one place.
        """
        pass

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Deliver a streaming text chunk.

        Override in subclasses to enable streaming. Implementations should
        raise on delivery failure so the channel manager can retry.
        """
        pass

    async def health_check(self) -> dict[str, Any]:
        """
        Perform a health check on this channel.

        Returns:
            A dict with keys:
            - healthy (bool): True if channel is healthy
            - latency_ms (int | None): Response latency in milliseconds if applicable
            - error (str | None): Error message if not healthy
            - details (dict): Channel-specific health details

        Override in subclasses to implement actual health checks. The default
        implementation checks if the channel is running.
        """
        return {
            "healthy": self._running,
            "latency_ms": None,
            "error": None if self._running else "Channel not running",
            "details": {"running": self._running}
        }

    async def restart(self) -> None:
        """Restart the channel by stopping and starting it.

        Override in subclasses if special cleanup is needed before restart.
        Default implementation calls stop() then start().
        """
        try:
            await self.stop()
        except Exception as e:
            logger.warning("{}: error during stop before restart: {}", self.name, e)
        try:
            await self.start()
        except Exception as e:
            logger.error("{}: failed to restart: {}", self.name, e)
            raise

    @property
    def supports_streaming(self) -> bool:
        """True when config enables streaming AND this subclass implements send_delta."""
        cfg = self.config
        streaming = cfg.get("streaming", False) if isinstance(cfg, dict) else getattr(cfg, "streaming", False)
        return bool(streaming) and type(self).send_delta is not BaseChannel.send_delta

    def is_allowed(self, sender_id: str) -> bool:
        """Check if *sender_id* is permitted.  Empty list → deny all; ``"*"`` → allow all."""
        allow_list = getattr(self.config, "allow_from", [])
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
            return

        meta = metadata or {}
        if self.supports_streaming:
            meta = {**meta, "_wants_stream": True}

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=meta,
            session_key_override=session_key,
        )

        await self.bus.publish_inbound(msg)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """Return default config for onboard. Override in plugins to auto-populate config.json."""
        return {"enabled": False}

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running
