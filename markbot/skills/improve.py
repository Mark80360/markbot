"""Skill improver — automated skill quality evaluation and improvement.

Provides the self-improvement loop:
  1. Evaluate a skill's quality (description clarity, completeness, usage patterns)
  2. Generate improvement suggestions via LLM
  3. Auto-apply safe improvements (description optimization)

Works in conjunction with the Curator to maintain and improve skills over time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class EvalResult:
    """Result of evaluating a skill's quality."""

    skill_name: str
    score: float = 0.0  # 0.0 - 1.0
    description_clarity: float = 0.0
    completeness: float = 0.0
    usage_engagement: float = 0.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class SkillImprover:
    """Evaluates and improves skill quality.

    Usage:
        improver = SkillImprover(workspace)
        result = improver.run_eval("my_skill")
        suggestions = improver.suggest_improvements("my_skill", result)
    """

    def __init__(self, workspace: Path, llm_provider: Any = None):
        self._workspace = workspace
        self._llm = llm_provider

    def run_eval(self, skill_name: str, skill_def: Any = None) -> EvalResult:
        """Evaluate a skill's quality based on heuristics.

        Args:
            skill_name: Name of the skill to evaluate.
            skill_def: Optional SkillDefinition object.

        Returns:
            EvalResult with scores and issues.
        """
        result = EvalResult(skill_name=skill_name)

        # Evaluate description clarity
        if skill_def and hasattr(skill_def, 'description'):
            desc = skill_def.description
            result.description_clarity = self._score_description(desc)
            if result.description_clarity < 0.5:
                result.issues.append("Description is too vague or short")
                result.suggestions.append("Add specific details about what the skill does and when to use it")

        # Evaluate completeness
        if skill_def:
            result.completeness = self._score_completeness(skill_def)
            if result.completeness < 0.5:
                result.issues.append("Skill may be incomplete (missing scripts, when_to_use, etc.)")

        # Evaluate usage engagement
        if skill_def and hasattr(skill_def, 'use_count'):
            result.usage_engagement = self._score_usage(skill_def)
            if skill_def.use_count == 0 and skill_def.view_count == 0:
                result.issues.append("Skill has never been used or viewed")
                result.suggestions.append("Consider if this skill is discoverable enough")

        # Check SKILL.md content
        skill_path = self._workspace / "skills" / skill_name
        if skill_path.exists():
            md_path = skill_path / "SKILL.md"
            if md_path.exists():
                content = md_path.read_text(encoding="utf-8")
                md_issues = self._check_markdown_quality(content)
                result.issues.extend(md_issues)

        # Compute overall score
        weights = [0.3, 0.4, 0.3]
        scores = [result.description_clarity, result.completeness, result.usage_engagement]
        result.score = sum(w * s for w, s in zip(weights, scores))

        return result

    async def suggest_improvements(
        self,
        skill_name: str,
        eval_result: EvalResult,
    ) -> list[str]:
        """Generate improvement suggestions using LLM.

        Args:
            skill_name: Name of the skill.
            eval_result: The evaluation result.

        Returns:
            List of improvement suggestion strings.
        """
        if not self._llm:
            return eval_result.suggestions

        skill_path = self._workspace / "skills" / skill_name / "SKILL.md"
        if not skill_path.exists():
            return eval_result.suggestions

        content = skill_path.read_text(encoding="utf-8")

        prompt = f"""You are reviewing a skill definition for an AI agent.
Evaluate the following SKILL.md and suggest improvements.

Current issues found:
{chr(10).join(f'- {i}' for i in eval_result.issues)}

SKILL.md content:
```
{content[:3000]}
```

Provide 2-3 specific, actionable improvement suggestions.
Focus on: description clarity, when_to_use precision, and completeness.
Do NOT suggest adding dangerous scripts or external API calls."""

        try:
            response = await self._llm.generate(prompt)
            if response and hasattr(response, 'content'):
                return self._parse_suggestions(response.content or "")
        except Exception as e:
            logger.warning("LLM suggestion generation failed: {}", e)

        return eval_result.suggestions

    def _score_description(self, description: str) -> float:
        """Score description clarity (0.0 - 1.0)."""
        if not description:
            return 0.0
        length = len(description)
        if length < 20:
            return 0.2
        if length < 50:
            return 0.5
        if length > 200:
            return 1.0
        return 0.7

    def _score_completeness(self, skill_def: Any) -> float:
        """Score skill completeness (0.0 - 1.0)."""
        score = 0.0
        if hasattr(skill_def, 'description') and skill_def.description:
            score += 0.25
        if hasattr(skill_def, 'when_to_use') and skill_def.when_to_use:
            score += 0.25
        if hasattr(skill_def, 'scripts') and skill_def.scripts:
            score += 0.3
        if hasattr(skill_def, 'config_vars') and skill_def.config_vars:
            score += 0.1
        if hasattr(skill_def, 'conditions') and skill_def.conditions:
            if skill_def.conditions.requires_tools or skill_def.conditions.fallback_for_tools:
                score += 0.1
        return min(score, 1.0)

    def _score_usage(self, skill_def: Any) -> float:
        """Score usage engagement (0.0 - 1.0)."""
        if not hasattr(skill_def, 'use_count'):
            return 0.5
        if skill_def.use_count == 0:
            return 0.0
        if skill_def.use_count < 3:
            return 0.3
        if skill_def.use_count < 10:
            return 0.6
        return 1.0

    @staticmethod
    def _check_markdown_quality(content: str) -> list[str]:
        """Check SKILL.md for common quality issues."""
        issues = []
        if len(content) < 100:
            issues.append("SKILL.md is very short (< 100 chars)")
        if "```" not in content and ("script" in content.lower() or "command" in content.lower()):
            issues.append("Mentions scripts/commands but has no code blocks")
        if content.count("#") < 2:
            issues.append("SKILL.md has minimal structure (few headings)")
        return issues

    @staticmethod
    def _parse_suggestions(text: str) -> list[str]:
        """Parse LLM response into a list of suggestions."""
        suggestions = []
        for line in text.split("\n"):
            line = line.strip()
            if re.match(r"^[\d\-\*]+[\.\)]\s+", line):
                cleaned = re.sub(r"^[\d\-\*]+[\.\)]\s+", "", line)
                if cleaned:
                    suggestions.append(cleaned)
        return suggestions if suggestions else [text.strip()]


__all__ = ["SkillImprover", "EvalResult"]
