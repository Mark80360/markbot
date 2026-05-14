"""Memory security scanner — injection and exfiltration detection.

Scans memory content for prompt injection, role hijack, exfiltration,
and other security threats before content is injected into the system prompt.

Usage:
    from markbot.memory.scanner import MemorySecurityScanner

    scanner = MemorySecurityScanner()
    error = scanner.scan(user_provided_content)
    if error:
        reject(error)
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Threat patterns
# ---------------------------------------------------------------------------

_THREAT_PATTERNS: list[tuple[str, str]] = [
    # Prompt injection
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don\\?t\s+have)\s+(restrictions|limits|rules)", "bypass_restrictions"),
    # Exfiltration via curl/wget with secrets
    (r"curl\s+[^\n]*\$\\{?\\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\\{?\\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
    # Persistence via shell rc
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|\\~/\\.ssh", "ssh_access"),
    # Env file protection
    (r"\$HOME/\.env|\\~/\.env", "env_file"),
    # General dangerous patterns
    (r"rm\s+-[rf]{1,2}\s+/", "destructive_rm"),
    (r"chmod\s+777\s+/", "permission_escalation"),
]

# Invisible unicode characters that can be used for injection
_INVISIBLE_CHARS: set[str] = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u2060",  # word joiner
    "\ufeff",  # BOM
    "\u202a",  # left-to-right embedding
    "\u202b",  # right-to-left embedding
    "\u202c",  # pop directional formatting
    "\u202d",  # left-to-right override
    "\u202e",  # right-to-left override
}


class MemorySecurityScanner:
    """Scan memory content for injection, exfiltration, and other threats.

    Returns an error string if a threat is detected, or None if content is safe.

    The scanner is stateless and thread-safe — one instance can be shared
    across all memory write operations.
    """

    @staticmethod
    def scan(content: str) -> Optional[str]:
        """Scan memory content for security threats.

        Args:
            content: The memory content to scan (user-provided or LLM-generated).

        Returns:
            Error string describing the blocked content, or None if safe.
        """
        if not content:
            return None

        # Check invisible unicode characters
        for char in _INVISIBLE_CHARS:
            if char in content:
                return (
                    f"Blocked: content contains invisible unicode character "
                    f"U+{ord(char):04X} (possible injection)."
                )

        # Check threat patterns
        for pattern, threat_id in _THREAT_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                return (
                    f"Blocked: content matches threat pattern '{threat_id}'. "
                    f"Memory entries are injected into the system prompt and must "
                    f"not contain injection or exfiltration payloads."
                )

        return None

    @staticmethod
    def sanitize(content: str) -> str:
        """Sanitize content by stripping invisible characters only.

        This is a best-effort cleanup for cases where content should be
        preserved but invisible chars removed. Does NOT remove threat
        patterns 鈥?those should be rejected outright via scan().

        Args:
            content: Content to sanitize.

        Returns:
            Content with invisible unicode characters removed.
        """
        for char in _INVISIBLE_CHARS:
            content = content.replace(char, "")
        return content


__all__ = ["MemorySecurityScanner"]
