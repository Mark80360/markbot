"""Skills system for markbot.

Refactored to use new core types inspired by MarkBot.
This is the new skill system implementation, separate from the skills/ directory
which contains the actual skill definitions.
"""

from markbot.core.skills.registry import SkillRegistry
from markbot.core.skills.loader import SkillLoader
from markbot.core.skills.tool import SkillTool

__all__ = [
    "SkillRegistry",
    "SkillLoader",
    "SkillTool",
]
