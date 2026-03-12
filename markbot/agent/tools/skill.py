"""Skill invocation tool."""

from markbot.agent.skills import SkillsLoader
from markbot.agent.tools.base import Tool


class SkillTool(Tool):
    """Load and execute a skill by name."""

    def __init__(self, skills_loader: SkillsLoader):
        self.skills = skills_loader

    @property
    def name(self) -> str:
        return "use_skill"

    @property
    def description(self) -> str:
        return "Load a skill's instructions by name. Use this to access skill capabilities listed in the system prompt."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to load (e.g., 'github', 'memory', 'cron')",
                }
            },
            "required": ["skill_name"],
        }

    async def execute(self, skill_name: str) -> str:
        """Load skill content."""
        content = self.skills.load_skill(skill_name)
        if not content:
            available = [s["name"] for s in self.skills.list_skills()]
            return f"Skill '{skill_name}' not found. Available skills: {', '.join(available)}"

        # Strip frontmatter before returning
        return self.skills._strip_frontmatter(content)
