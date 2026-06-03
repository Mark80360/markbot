"""Shared utilities for the skill system."""

from __future__ import annotations

import re
from pathlib import Path


def load_skill_body(skill_path: Path) -> str | None:
    """Load the SKILL.md body content (frontmatter stripped) from a skill directory.

    Args:
        skill_path: Path to the skill directory containing SKILL.md.

    Returns:
        Body content with frontmatter removed, or None if not found.
    """
    skill_file = skill_path / "SKILL.md"
    if not skill_file.exists():
        return None

    content = skill_file.read_text(encoding="utf-8")
    if content.startswith("---"):
        match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
        if match:
            return content[match.end():].strip()
    return content.strip() if content else None


def build_constraint_block(skill_name: str, body: str) -> str:
    """Wrap skill body in a mandatory constraint block.

    Uses XML-like tags to create a structured constraint that LLMs
    treat as non-negotiable instructions rather than suggestions.

    Args:
        skill_name: Name of the skill for constraint framing.
        body: The SKILL.md body content to wrap.

    Returns:
        Formatted constraint string.
    """
    header = (
        f'<skill-constraint name="{skill_name}">\n'
        f'CRITICAL: You are now executing the "{skill_name}" skill.\n'
        f'The following instructions are MANDATORY constraints, NOT suggestions.\n'
        f'You MUST:\n'
        f'1. Follow every step exactly as described, in order\n'
        f'2. NOT skip, reorder, or improvise steps\n'
        f'3. NOT add steps or behaviors not described below\n'
        f'4. Use ONLY the tools and methods specified in the skill\n'
        f'5. If the skill says "run script X", you MUST run script X — do NOT implement the logic yourself\n'
        f'6. The SKILL.md document overrides your general knowledge about how to do things\n'
    )
    footer = (
        f'\n</skill-constraint>\n'
        f'END OF SKILL CONSTRAINT for "{skill_name}".\n'
        f'Resume normal behavior only after completing all skill steps described above.'
    )
    return f"{header}\n{body}{footer}"
