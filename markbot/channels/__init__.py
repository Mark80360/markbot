"""Chat channels module with plugin architecture."""

from markbot.channels.base import BaseChannel
from markbot.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
