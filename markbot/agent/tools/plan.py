"""Task planning and decomposition tool."""

from typing import Any
from markbot.agent.tools.base import Tool


class PlanTool(Tool):
    """Tool for planning and decomposing complex tasks."""

    @property
    def name(self) -> str:
        return "plan"

    @property
    def description(self) -> str:
        return (
            "Plan and decompose complex tasks into manageable steps. "
            "Use this before starting complex work to create a clear execution plan."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task or goal to plan"},
                "constraints": {
                    "type": "string",
                    "description": "Any constraints or limitations (time, resources, etc.)",
                },
                "detail_level": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Level of detail for the plan",
                    "default": "medium",
                },
            },
            "required": ["task"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        task = kwargs.get("task", "")
        constraints = kwargs.get("constraints", "")
        detail_level = kwargs.get("detail_level", "medium")

        return f"""## Task Plan

**Goal**: {task}
{f"**Constraints**: {constraints}" if constraints else ""}
**Detail Level**: {detail_level}

### Planning Framework:

#### 1. Understand the Task
- What is the desired outcome?
- What are the success criteria?
- What information do I need?

#### 2. Break Down into Steps
- What are the major phases?
- What are the atomic tasks?
- What can be done in parallel?

#### 3. Identify Dependencies
- What must be done first?
- What blocks other tasks?
- What has external dependencies?

#### 4. Estimate Effort
- Which steps are most complex?
- Which steps are most uncertain?
- Where should I start?

#### 5. Risk Assessment
- What could go wrong?
- What's the contingency plan?

#### 6. Execution Order
- Step 1: [First action]
- Step 2: [Next action]
- Step N: [Final action]
"""
