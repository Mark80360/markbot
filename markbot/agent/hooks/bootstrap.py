"""Bootstrap hook for first-time user interaction guidance.

Checks for BOOTSTRAP.md on first user interaction and prepends
guidance to help set up agent identity and preferences.
Ported from bootstrap hook.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from markbot.memory.base import BaseMemoryManager


class BootstrapHook:
    """Hook for bootstrap guidance on first user interaction.

    Looks for BOOTSTRAP.md in working directory and if found,
    prepends guidance to first user message.
    """

    def __init__(
        self,
        working_dir: Path,
        language: str = "zh",
    ):
        self.working_dir = Path(working_dir)
        self.language = language

    def check_and_inject(self, messages: list[dict]) -> str | None:
        """Check BOOTSTRAP.md and return guidance text for injection.

        Called by AgentLoop before first user interaction.
        Returns guidance string to prepend, or None if not needed.

        Args:
            messages: Current conversation messages

        Returns:
            Guidance text string, or None if no injection needed
        """
        try:
            bootstrap_path = self.working_dir / "BOOTSTRAP.md"
            bootstrap_completed_flag = self.working_dir / ".bootstrap_completed"

            if bootstrap_completed_flag.exists():
                return None

            if not bootstrap_path.exists():
                return None

            if not self._is_first_user_interaction(messages):
                return None

            guidance = self._build_bootstrap_guidance()
            logger.debug("Found BOOTSTRAP.md [{}], returning guidance", self.language)

            bootstrap_completed_flag.touch()
            logger.debug("Created bootstrap completion flag")

            return guidance

        except Exception as e:
            logger.error("Failed to process bootstrap: {}", e)
            return None

    @staticmethod
    def _is_first_user_interaction(messages: list[dict]) -> bool:
        user_count = sum(1 for m in messages if m.get("role") == "user")
        return user_count <= 1

    def _build_bootstrap_guidance(self) -> str:
        return (
            "# BOOTSTRAP MODE\n"
            "\n"
            "`BOOTSTRAP.md` exists — first-time setup.\n"
            "\n"
            "1. Read BOOTSTRAP.md, greet the user, "
            "and guide them through setup.\n"
            "2. Follow BOOTSTRAP.md instructions "
            "to define identity and preferences.\n"
            "3. Create/update files "
            "(PROFILE.md, MEMORY.md, etc.) as described.\n"
            "4. Delete BOOTSTRAP.md when done.\n"
            "\n"
            "If the user wants to skip, answer their "
            "question directly instead.\n"
            "\n"
            "---\n"
            "\n"
        )
