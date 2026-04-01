"""Token usage tracking and estimation.
- Tracks token usage from API responses
- Provides estimation for messages without usage data
- Supports cache tokens (creation and read)
"""

from dataclasses import dataclass
from typing import Any, Optional
from loguru import logger


@dataclass
class TokenUsage:
    """Token usage information from API response."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    
    @property
    def total_tokens(self) -> int:
        """Calculate total tokens including cache."""
        return (
            self.input_tokens +
            self.cache_creation_input_tokens +
            self.cache_read_input_tokens +
            self.output_tokens
        )
    
    @property
    def context_tokens(self) -> int:
        """Calculate context tokens (input + cache, excluding output)."""
        return (
            self.input_tokens +
            self.cache_creation_input_tokens +
            self.cache_read_input_tokens
        )
    
    def to_dict(self) -> dict[str, int]:
        """Convert to dictionary."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
        }


class TokenTracker:
    """Tracks token usage across conversation.
    
    Reference: Claude Code token tracking
    """
    
    def __init__(self):
        """Initialize token tracker."""
        self.total_usage = TokenUsage()
        self.usage_history: list[TokenUsage] = []
    
    def update_from_response(self, response: Any) -> TokenUsage:
        """Update token usage from API response.
        
        Args:
            response: API response object with usage field
            
        Returns:
            TokenUsage for this response
        """
        usage_data = getattr(response, "usage", None)
        
        if not usage_data:
            return TokenUsage()
        
        usage = TokenUsage(
            input_tokens=getattr(usage_data, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_data, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage_data, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage_data, "cache_read_input_tokens", 0) or 0,
        )
        
        self.total_usage.input_tokens += usage.input_tokens
        self.total_usage.output_tokens += usage.output_tokens
        self.total_usage.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.total_usage.cache_read_input_tokens += usage.cache_read_input_tokens
        
        self.usage_history.append(usage)
        
        return usage
    
    def get_current_usage(self) -> TokenUsage:
        """Get current total token usage.
        
        Returns:
            Current total TokenUsage
        """
        return self.total_usage
    
    def get_last_usage(self) -> Optional[TokenUsage]:
        """Get last API response token usage.
        
        Returns:
            Last TokenUsage or None if no history
        """
        return self.usage_history[-1] if self.usage_history else None
    
    def reset(self):
        """Reset token tracking."""
        self.total_usage = TokenUsage()
        self.usage_history.clear()
    
    def get_summary(self) -> dict[str, Any]:
        """Get token usage summary.
        
        Returns:
            Summary dictionary with usage statistics
        """
        return {
            "total": self.total_usage.to_dict(),
            "api_calls": len(self.usage_history),
            "average_per_call": {
                "input_tokens": (
                    self.total_usage.input_tokens // len(self.usage_history)
                    if self.usage_history else 0
                ),
                "output_tokens": (
                    self.total_usage.output_tokens // len(self.usage_history)
                    if self.usage_history else 0
                ),
            }
        }


def estimate_tokens(text: str) -> int:
    """Estimate token count for text.
    
    Simple estimation: ~4 characters per token for English text.
    This is a rough approximation and may not be accurate for all models.
    
    Args:
        text: Text to estimate tokens for
        
    Returns:
        Estimated token count
    """
    if not text:
        return 0
    
    return len(text) // 4


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate token count for a message.
    
    Args:
        message: Message dict with role and content
        
    Returns:
        Estimated token count
    """
    content = message.get("content", "")
    
    if isinstance(content, str):
        return estimate_tokens(content)
    elif isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
                elif block.get("type") == "image":
                    total += 85
                elif block.get("type") == "tool_use":
                    total += estimate_tokens(str(block.get("input", {})))
                elif block.get("type") == "tool_result":
                    if isinstance(block.get("content"), str):
                        total += estimate_tokens(block.get("content", ""))
                    elif isinstance(block.get("content"), list):
                        for item in block.get("content", []):
                            if isinstance(item, dict) and item.get("type") == "text":
                                total += estimate_tokens(item.get("text", ""))
        return total
    
    return 0


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total token count for messages.
    
    Args:
        messages: List of message dicts
        
    Returns:
        Estimated total token count
    """
    return sum(estimate_message_tokens(msg) for msg in messages)


def get_token_usage_from_message(message: dict[str, Any]) -> Optional[TokenUsage]:
    """Extract token usage from an assistant message.
    
    Args:
        message: Message dict potentially containing usage info
        
    Returns:
        TokenUsage if available, None otherwise
    """
    if message.get("role") != "assistant":
        return None
    
    usage_data = message.get("usage")
    if not usage_data:
        return None
    
    return TokenUsage(
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        cache_creation_input_tokens=usage_data.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=usage_data.get("cache_read_input_tokens", 0),
    )


def get_current_usage_from_messages(messages: list[dict[str, Any]]) -> Optional[TokenUsage]:
    """Get current token usage from messages.
    
    Searches backwards through messages to find the most recent
    assistant message with usage information.
    
    Args:
        messages: List of message dicts
        
    Returns:
        TokenUsage from most recent assistant message, or None
    """
    for message in reversed(messages):
        usage = get_token_usage_from_message(message)
        if usage:
            return usage
    return None


def token_count_with_estimation(messages: list[dict[str, Any]]) -> int:
    """Calculate current context size with estimation.
    
    Uses the last API response's token count plus estimates for
    any messages added since.
    
    Reference: Claude Code tokenCountWithEstimation()
    
    Args:
        messages: List of message dicts
        
    Returns:
        Estimated total context tokens
    """
    last_usage_index = -1
    last_usage = None
    
    for i in range(len(messages) - 1, -1, -1):
        usage = get_token_usage_from_message(messages[i])
        if usage:
            last_usage_index = i
            last_usage = usage
            break
    
    if last_usage:
        new_messages = messages[last_usage_index + 1:]
        estimated_new = estimate_messages_tokens(new_messages)
        return last_usage.total_tokens + estimated_new
    
    return estimate_messages_tokens(messages)
