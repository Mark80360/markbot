"""Skill script execution system for markbot.

This module provides the infrastructure for executing scripts defined in SKILL.md files.

Features:
- Security scanning with dangerous pattern detection
- Sandbox execution with resource limits
- Multi-language support (Python, Shell, Node.js)
- Declarative script definition via YAML frontmatter

Example SKILL.md format:
---
name: my-skill
description: "Code analysis tools"
metadata:
  markbot:
    executable: true
    scripts:
      - name: "format"
        description: "Format code"
        entry: "scripts/format.py"
        language: "python"
        parameters:
          type: object
          properties:
            path:
              type: string
              description: "Path to format"
          required: ["path"]
        sandbox:
          allowed_paths: ["{workspace}"]
          network: false
          timeout: 60
---
"""

from .scanner import SecurityScanner, ScanResult, Finding
from .sandbox import Sandbox, SandboxConfig
from .skill_script import SkillScript

__all__ = [
    "SecurityScanner",
    "ScanResult",
    "Finding",
    "Sandbox",
    "SandboxConfig",
    "SkillScript",
]
