"""Reflection and learning tool."""

from typing import Any
from markbot.agent.tools.base import Tool


class ReflectTool(Tool):
    """Tool for reflection, evaluation, and learning from results."""

    @property
    def name(self) -> str:
        return "reflect"

    @property
    def description(self) -> str:
        return (
            "Reflect on results, evaluate outcomes, and extract lessons learned. "
            "Use this after completing tasks to improve future performance."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task or goal that was attempted"},
                "result": {"type": "string", "description": "The actual result or outcome"},
                "expected": {"type": "string", "description": "What was expected or desired"},
                "mode": {
                    "type": "string",
                    "enum": ["evaluate", "learn", "improve"],
                    "description": "Reflection mode: evaluate, learn, or improve",
                    "default": "evaluate",
                },
            },
            "required": ["task", "result"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task = kwargs.get("task", "")
        result = kwargs.get("result", "")
        expected = kwargs.get("expected", "")
        mode = kwargs.get("mode", "evaluate")

        prompts = {
            "evaluate": f"""## Reflection: Evaluate

**Task**: {task}
**Result**: {result}
{f"**Expected**: {expected}" if expected else ""}

### Evaluation Framework:
1. **Success Assessment**
   - Was the goal achieved?
   - What percentage of success?
   - What was the quality of the result?

2. **Process Analysis**
   - What went well?
   - What went wrong?
   - What was unexpected?

3. **Root Cause Analysis**
   - Why did things go well?
   - Why did things go wrong?
   - What were the key factors?

4. **Verdict**
   - Overall assessment
   - Key metrics""",
            "learn": f"""## Reflection: Learn

**Task**: {task}
**Result**: {result}
{f"**Expected**: {expected}" if expected else ""}

### Learning Framework:
1. **Key Insights**
   - What did I learn?
   - What surprised me?
   - What confirmed my assumptions?

2. **Pattern Recognition**
   - What patterns emerged?
   - What's reusable?
   - What's context-specific?

3. **Knowledge Gaps**
   - What don't I know yet?
   - What needs more research?

4. **Lessons Learned**
   - Do this again: [what worked]
   - Don't do this: [what failed]
   - Consider this: [new ideas]""",
            "improve": f"""## Reflection: Improve

**Task**: {task}
**Result**: {result}
{f"**Expected**: {expected}" if expected else ""}

### Improvement Framework:
1. **Gap Analysis**
   - What's the gap between result and expectation?
   - What caused the gap?

2. **Improvement Opportunities**
   - What can be done better?
   - What can be automated?
   - What can be simplified?

3. **Action Items**
   - Immediate fixes: [quick wins]
   - Short-term improvements: [next iteration]
   - Long-term improvements: [strategic changes]

4. **Success Metrics**
   - How will I measure improvement?
   - What's the target?""",
        }

        return prompts.get(mode, prompts["evaluate"])
