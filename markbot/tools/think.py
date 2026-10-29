"""Unified thinking, planning, and reflection tool.

Merges the former separate tools:
- think: Deep thinking and reasoning (analyze, challenge, inversion, first-principles)
- plan: Task planning and decomposition
- reflect: Reflection, evaluation, and learning from results
- code-analysis & research-plan: Structured code research frameworks
"""

from typing import Any

from markbot.tools.base import Tool


class ThinkTool(Tool):
    """Unified tool for structured thinking, planning, reflection, and code analysis."""


    _is_read_only = True
    @property
    def name(self) -> str:
        return "think"

    @property
    def description(self) -> str:
        return (
            "Structured thinking scratchpad. Use it when a decision genuinely needs to be "
            "decomposed before acting — not as a substitute for reasoning you can do directly.\n"
            "Modes: analyze | challenge | inversion | first-principles | plan | evaluate | "
            "learn | improve | code-analysis | research-plan."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The question, problem or task to think about. Alias: 'task'."
                    )
                },
                "mode": {
                    "type": "string",
                    "enum": [
                        "analyze", "challenge", "inversion", "first-principles",
                        "plan", "evaluate", "learn", "improve",
                        "code-analysis", "research-plan",
                    ],
                    "description": (
                        "Framework to apply (default: analyze).\n"
                        "- analyze / challenge / inversion / first-principles: problem framing\n"
                        "- plan: task decomposition\n"
                        "- evaluate / learn / improve: post-hoc reflection\n"
                        "- code-analysis / research-plan: codebase investigation strategy"
                    ),
                    "default": "analyze"
                },
                "context": {
                    "type": "string",
                    "description": "Optional background or supporting information"
                },
                "task": {
                    "type": "string",
                    "description": "Alias for 'question' (plan mode)"
                },
                "result": {
                    "type": "string",
                    "description": "Actual outcome (evaluate/learn/improve)"
                },
                "expected": {
                    "type": "string",
                    "description": "What was expected (evaluate/learn/improve)"
                },
                "constraints": {
                    "type": "string",
                    "description": "Constraints for plan mode (time, resources, ...)"
                },
                "detail_level": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Plan detail (default: medium)",
                    "default": "medium",
                },
            },
            "required": ["question"],
        }

    async def _legacy_execute(self, **kwargs: Any) -> str:
        question = kwargs.get("question") or kwargs.get("task", "")
        mode = kwargs.get("mode", "analyze")
        context = kwargs.get("context", "")

        if mode == "code-analysis":
            return self._build_code_analysis_framework(question, context)
        if mode == "research-plan":
            return self._build_research_plan(question, context)
        if mode == "plan":
            return self._build_plan(question, kwargs.get("constraints", ""), kwargs.get("detail_level", "medium"))
        if mode in ("evaluate", "learn", "improve"):
            return self._build_reflection(
                mode,
                question,
                kwargs.get("result", ""),
                kwargs.get("expected", ""),
            )

        prompts = {
            "analyze": f"""## Analysis
**Question**: {question}
{f"**Context**: {context}" if context else ""}
1. What is the real question, stripped of noise?
2. What are the components, and how do they depend on each other?
3. What is the simplest path forward?""",

            "challenge": f"""## Challenge
**Question**: {question}
{f"**Context**: {context}" if context else ""}
1. Which assumptions are being made, and which are unverified?
2. What evidence supports or contradicts each one?
3. What changes if the shaky assumptions are wrong?""",

            "inversion": f"""## Inversion
**Question**: {question}
{f"**Context**: {context}" if context else ""}
1. What would make this fail completely?
2. What are the early warning signs?
3. How do we prevent each failure mode?""",

            "first-principles": f"""## First Principles
**Question**: {question}
{f"**Context**: {context}" if context else ""}
1. What can we prove or verify here?
2. What can be derived from those truths?
3. What is the simplest solution from them?""",
        }

        return prompts.get(mode, prompts["analyze"])

    def _build_plan(self, task: str, constraints: str, detail_level: str) -> str:
        detail_hints = {
            "high": "every atomic step, edge case and verification point",
            "medium": "major phases and key milestones",
            "low": "high-level phases only — fill in details during execution",
        }
        return f"""## Task Plan

**Goal**: {task}
{f"**Constraints**: {constraints}" if constraints else ""}
**Detail**: {detail_level} ({detail_hints.get(detail_level, '')})

1. **Outcome** — success criteria for this task
2. **Steps** — major phases, then atomic tasks; mark what can run in parallel
3. **Dependencies** — what must happen first, what blocks what
4. **Order** — where to start, and which step is most uncertain
"""

    def _build_reflection(self, mode: str, task: str, result: str, expected: str) -> str:
        prompts = {
            "evaluate": f"""## Reflection: Evaluate

**Task**: {task}
**Result**: {result}
{f"**Expected**: {expected}" if expected else ""}

1. **Success** — was the goal achieved? quality of the result?
2. **Process** — what went well, what went wrong, what was unexpected?
3. **Root cause** — which factors decided the outcome?
4. **Verdict** — overall assessment""",

            "learn": f"""## Reflection: Learn

**Task**: {task}
**Result**: {result}
{f"**Expected**: {expected}" if expected else ""}

1. **Insights** — what did this teach, what surprised you?
2. **Patterns** — what is reusable vs context-specific?
3. **Gaps** — what is still unknown?
4. **Lessons** — do this again / don't do this / consider this""",

            "improve": f"""## Reflection: Improve

**Task**: {task}
**Result**: {result}
{f"**Expected**: {expected}" if expected else ""}

1. **Gap** — distance between result and expectation, and its cause
2. **Opportunities** — what can be done better, automated or simplified?
3. **Actions** — immediate fixes / short-term / long-term
4. **Metrics** — how improvement will be measured""",
        }

        return prompts.get(mode, prompts["evaluate"])

    @staticmethod
    def _build_code_analysis_framework(question: str, context: str) -> str:
        focus_hint = context.split()[0] if context else ""
        return f"""## Code Analysis Framework

**Research Question**: {question}
{f"**Context**: {context}" if context else ""}

### 1. Map
- `explore(mode="overview")`: structure, tech stack, entry points, key files

### 2. Locate
- `grep` to find the concepts, then `explore(mode="trace", target="<symbol>")` on the
  3-5 symbols that look central — definition location plus all usages

### 3. Read
- `read_file(path=..., offset=N, limit=200)` on the top 2-3 critical files

### 4. Follow
- `explore(mode="dependencies")` for the import graph, then trace callers of key functions

### 5. Synthesise
- Every claim backed by a specific `file:line`; name the gaps explicitly

### Immediate Next Action
Start with: `explore(mode="overview"{f', focus="{focus_hint}")' if focus_hint else ''})`"""

    @staticmethod
    def _build_research_plan(question: str, context: str) -> str:
        return f"""## Research Plan

**Goal**: {question}
{f"**Context**: {context}" if context else ""}

1. **Recon** — `explore(mode="overview")`: structure, tech stack, key files
2. **Entry points** — `read_file` on how the application boots
3. **Core modules** — `explore(mode="analyze", target="<path>")` for 2-3 of them
4. **Symbols** — `explore(mode="trace", target="<symbol>")`: definition, usages, call chain
5. **Dependencies** — `explore(mode="dependencies")`: central vs peripheral modules
6. **Targeted** — `grep`/`read_file` for the remaining sub-questions
7. **Report** — architecture, key components, data flow, design decisions, risks

Each step informs the next; do not jump to conclusions without reading actual code."""
