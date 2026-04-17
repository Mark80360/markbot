"""Unified thinking, planning, and reflection tool.

Merges the former separate tools:
- think: Deep thinking and reasoning (analyze, challenge, inversion, first-principles)
- plan: Task planning and decomposition
- reflect: Reflection, evaluation, and learning from results
- code-analysis & research-plan: Structured code research frameworks
"""

from typing import Any
from markbot.agent.tools.base import Tool


class ThinkTool(Tool):
    """Unified tool for structured thinking, planning, reflection, and code analysis."""

    @property
    def name(self) -> str:
        return "think"

    @property
    def description(self) -> str:
        return (
            "Unified cognitive tool for deep thinking, planning, reflection, and code analysis. "
            "Use this to:\n"
            "- **Analyze** complex problems before acting\n"
            "- **Plan** tasks by breaking them into steps\n"
            "- **Reflect** on results to learn and improve\n"
            "- **Research codebases** with structured exploration strategies\n\n"
            "Modes:\n"
            "- **analyze**: General analysis framework\n"
            "- **challenge**: Challenge assumptions and find contradictions\n"
            "- **inversion**: Inversion thinking (what would cause failure)\n"
            "- **first-principles**: Break down to fundamental truths\n"
            "- **plan**: Decompose tasks into actionable steps with constraints\n"
            "- **evaluate**: Assess outcomes against expectations\n"
            "- **learn**: Extract lessons and patterns from results\n"
            "- **improve**: Identify gaps and create action items\n"
            "- **code-analysis**: Structured code research framework (produces actionable exploration plan)\n"
            "- **research-plan**: Create step-by-step exploration plan for investigating a codebase"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The question, problem, task, or topic to think about. "
                        "For 'plan' mode, use 'task' alias. For 'reflect' modes, describes what was attempted."
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
                        "Cognitive mode:\n"
                        "**Thinking modes:**\n"
                        "- analyze: General analysis framework (default)\n"
                        "- challenge: Challenge assumptions\n"
                        "- inversion: Inversion thinking (what would cause failure)\n"
                        "- first-principles: Break down to fundamental truths\n\n"
                        "**Planning mode:**\n"
                        "- plan: Task decomposition into manageable steps\n\n"
                        "**Reflection modes:**\n"
                        "- evaluate: Assess success/failure of completed work\n"
                        "- learn: Extract insights and patterns from results\n"
                        "- improve: Identify gaps and create improvement actions\n\n"
                        "**Code research modes:**\n"
                        "- code-analysis: Structured code research with specific files/symbols to investigate\n"
                        "- research-plan: Step-by-step exploration plan for codebase investigation"
                    ),
                    "default": "analyze"
                },
                "context": {
                    "type": "string",
                    "description": "Optional context, background, or additional information"
                },
                "task": {
                    "type": "string",
                    "description": "Alias for 'question' — used in plan mode for the goal/task description"
                },
                "result": {
                    "type": "string",
                    "description": "The actual result or outcome (for evaluate/learn/improve modes)"
                },
                "expected": {
                    "type": "string",
                    "description": "What was expected or desired (for evaluate/learn/improve modes)"
                },
                "constraints": {
                    "type": "string",
                    "description": "Constraints or limitations for plan mode (time, resources, etc.)"
                },
                "detail_level": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Level of detail for plan mode (default: medium)",
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
5. **What's the simplest solution from first principles?**""",
        }

        return prompts.get(mode, prompts["analyze"])

    def _build_plan(self, task: str, constraints: str, detail_level: str) -> str:
        detail_hints = {
            "high": "Include every atomic step, edge case, and verification point.",
            "medium": "Include major phases and key milestones.",
            "low": "High-level phases only — fill in details during execution.",
        }
        return f"""## Task Plan

**Goal**: {task}
{f"**Constraints**: {constraints}" if constraints else ""}
**Detail Level**: {detail_level} ({detail_hints.get(detail_level, '')})

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

    def _build_reflection(self, mode: str, task: str, result: str, expected: str) -> str:
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

    @staticmethod
    def _build_code_analysis_framework(question: str, context: str) -> str:
        focus_hint = context.split()[0] if context else ""
        return f"""## Code Analysis Framework

**Research Question**: {question}
{f"**Context**: {context}" if context else ""}

### Phase 1: Map the Terrain (use `explore` tool)
- Run `explore(mode="overview")` to get project structure, tech stack, architecture patterns
- Identify entry points, config files, key modules from the overview output
- Note the architecture patterns detected (MVC, service layer, event bus, etc.)

### Phase 2: Identify Key Symbols (use `explore` + `grep`)
- From the overview, pick 3-5 core symbols (classes/functions) that seem central
- For each symbol, run `explore(mode="trace", target="<symbol>")` to find definitions and all usages
- Use `grep(pattern="<concept>", include="*.py")` to find related concepts not yet traced

### Phase 3: Deep Dive into Critical Paths (use `explore` + `read_file`)
- Pick the top 2-3 most important files identified in Phase 1-2
- Run `explore(mode="analyze", target="<filepath>")` for each to get AST-level insight (classes, functions, imports)
- Use `read_file(path="<filepath>", offset=N, limit=200)` to read critical sections in full

### Phase 4: Trace Data & Control Flow
- For key functions, trace their callers using `explore(mode="trace", target="<func_name>")`
- Follow import chains to understand module dependencies: `explore(mode="dependencies")`
- Look at how data flows between modules by reading connection points

### Phase 5: Synthesize Findings
- After gathering evidence from Phases 1-4, synthesize into structured conclusions
- Support every claim with specific file:line references
- Identify gaps where more investigation is needed

### Immediate Next Action
Start with: `explore(mode="overview"{f', focus="{focus_hint}")' if focus_hint else ''})`"""

    @staticmethod
    def _build_research_plan(question: str, context: str) -> str:
        return f"""## Research Plan

**Goal**: {question}
{f"**Context**: {context}" if context else ""}

### Step 1: Project Reconnaissance
```
explore(mode="overview")
```
Expected output: Directory structure, tech stack, architecture patterns, key files list.

### Step 2: Entry Point Analysis
From Step 1's output, identify entry points and run:
```
read_file(path="<entry_point_file>")
```
Understand how the application boots and what it initializes.

### Step 3: Core Module Deep Dive
Identify 2-3 core modules from the overview and analyze each:
```
explore(mode="analyze", target="<core_module_path>")
```
Extract: classes, functions, imports, responsibilities.

### Step 4: Symbol Tracing
Pick the most important symbols discovered in Step 3:
```
explore(mode="trace", target="<key_class_or_function>")
```
Map: definition location, all usages, call chain, related symbols.

### Step 5: Dependency Mapping
```
explore(mode="dependencies")
```
Understand how modules connect and which are central vs peripheral.

### Step 6: Focused Investigation
Based on findings, use targeted grep/read to answer specific sub-questions:
```
grep(pattern="<specific_term>", include="*.py")
read_file(path="<specific_file>", offset=<line>, limit=<count>)
```

### Step 7: Report Structure
Organize findings as:
1. **Architecture Summary** — high-level structure and patterns
2. **Key Components** — what each major piece does
3. **Data Flow** — how data moves through the system
4. **Notable Design Decisions** — interesting patterns or trade-offs
5. **Potential Issues** — observations about problems or improvements

### Execution Order
Execute steps sequentially. Each step informs what to look for in the next.
Do NOT skip steps or jump to conclusions without reading actual code."""
