"""Bootstrap hook for first-time user interaction guidance.

Checks for BOOTSTRAP.md on first user interaction and prepends
guidance to help set up agent identity and preferences.
Ported from CoPaw's bootstrap hook.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from ..base import BaseMemoryManager


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
            logger.debug(f"Found BOOTSTRAP.md [{self.language}], returning guidance")

            bootstrap_completed_flag.touch()
            logger.debug("Created bootstrap completion flag")

            return guidance

        except Exception as e:
            logger.error(f"Failed to process bootstrap: {e}")
            return None

    async def __call__(
        self,
        messages: list[dict],
    ) -> list[dict] | None:
        """Check and load BOOTSTRAP.md on first user interaction.

        Args:
            messages: Current conversation messages (list of dicts)

        Returns:
            Modified messages with guidance prepended, or None if no change needed
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
            logger.debug(f"Found BOOTSTRAP.md [{self.language}], injecting guidance")

            for msg in messages:
                if msg.get("role") == "user":
                    msg["content"] = guidance + msg.get("content", "")
                    break

            bootstrap_completed_flag.touch()
            logger.debug("Created bootstrap completion flag")

            return messages

        except Exception as e:
            logger.error(f"Failed to process bootstrap: {e}")
            return None

    @staticmethod
    def _is_first_user_interaction(messages: list[dict]) -> bool:
        user_count = sum(1 for m in messages if m.get("role") == "user")
        return user_count <= 1

    def _build_bootstrap_guidance(self) -> str:
        if self.language == "zh":
            return (
                "# 引导模式\n"
                "\n"
                "工作目录中存在 `BOOTSTRAP.md` — 首次设置。\n"
                "\n"
                "1. 阅读 BOOTSTRAP.md，友好地表示初次见面，"
                "引导用户完成设置。\n"
                "2. 按照 BOOTSTRAP.md 的指示，"
                "帮助用户定义你的身份和偏好。\n"
                "3. 按指南创建/更新必要文件"
                "（PROFILE.md、MEMORY.md 等）。\n"
                "4. 完成后删除 BOOTSTRAP.md。\n"
                "\n"
                "如果用户希望跳过，直接回答下面的问题即可。\n"
                "\n"
                "---\n"
                "\n"
            )
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
