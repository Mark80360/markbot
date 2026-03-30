"""Deep thinking and reasoning tool."""

from typing import Any
from markbot.agent.tools.base import Tool


class ThinkTool(Tool):
    """Tool for structured deep thinking and reasoning."""
    
    @property
    def name(self) -> str:
        return "think"
    
    @property
    def description(self) -> str:
        return (
            "Think deeply about a problem before acting. Use this to analyze complex problems, "
            "challenge assumptions, find contradictions, or break down problems to first principles."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or problem to think about"
                },
                "mode": {
                    "type": "string",
                    "enum": ["analyze", "challenge", "inversion", "first-principles"],
                    "description": "Thinking mode: analyze, challenge, inversion, or first-principles",
                    "default": "analyze"
                },
                "context": {
                    "type": "string",
                    "description": "Optional context or background information"
                }
            },
            "required": ["question"]
        }
    
    async def execute(self, **kwargs: Any) -> str:
        question = kwargs.get("question", "")
        mode = kwargs.get("mode", "analyze")
        context = kwargs.get("context", "")
        
        prompts = {
            "analyze": f"""## Thinking: Analyze

**Question**: {question}
{f"**Context**: {context}" if context else ""}

### Analysis Framework:
1. **What's the real question?** (Strip away noise)
2. **What are the key components?**
3. **What are the relationships between components?**
4. **What are the dependencies?**
5. **What's the simplest path forward?**""",

            "challenge": f"""## Thinking: Challenge

**Question**: {question}
{f"**Context**: {context}" if context else ""}

### Challenge Framework:
1. **What assumptions are being made?**
2. **Which assumptions are shaky or unverified?**
3. **What are the potential contradictions?**
4. **What evidence supports/challenges each assumption?**
5. **What would change if assumptions are wrong?**""",

            "inversion": f"""## Thinking: Inversion

**Question**: {question}
{f"**Context**: {context}" if context else ""}

### Inversion Framework:
1. **What would make this fail completely?**
2. **What are the most likely failure modes?**
3. **What are the early warning signs?**
4. **How to prevent each failure mode?**
5. **What's the backup plan?**""",

            "first-principles": f"""## Thinking: First Principles

**Question**: {question}
{f"**Context**: {context}" if context else ""}

### First Principles Framework:
1. **What are the fundamental truths here?**
2. **What can we prove or verify?**
3. **What can be derived from these truths?**
4. **What assumptions can we eliminate?**
5. **What's the simplest solution from first principles?**"""
        }
        
        return prompts.get(mode, prompts["analyze"])
