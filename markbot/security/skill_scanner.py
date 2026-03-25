"""Skill security scanner for MarkBot.

Scans skill packages for potential security issues before installation or loading.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    """Severity levels for security findings."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"
    SAFE = "SAFE"


class ThreatCategory(str, Enum):
    """Categories of security threats."""

    PROMPT_INJECTION = "prompt_injection"
    COMMAND_INJECTION = "command_injection"
    DATA_EXFILTRATION = "data_exfiltration"
    HARDCODED_SECRETS = "hardcoded_secrets"
    OBFUSCATION = "obfuscation"
    SUSPICIOUS_PATTERNS = "suspicious_patterns"
    MALWARE = "malware"


@dataclass
class Finding:
    """A single security finding."""

    severity: Severity
    category: ThreatCategory
    title: str
    description: str
    file_path: str
    line_number: int | None = None
    line_content: str | None = None
    rule_id: str | None = None


@dataclass
class ScanResult:
    """Result of a skill security scan."""

    skill_name: str
    skill_directory: str
    findings: list[Finding] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    scan_duration_seconds: float = 0.0

    @property
    def is_safe(self) -> bool:
        """Check if scan found no critical or high severity issues."""
        for finding in self.findings:
            if finding.severity in (Severity.CRITICAL, Severity.HIGH):
                return False
        return True

    @property
    def max_severity(self) -> Severity:
        """Get the maximum severity level found."""
        if not self.findings:
            return Severity.SAFE

        severity_order = [
            Severity.SAFE,
            Severity.INFO,
            Severity.LOW,
            Severity.MEDIUM,
            Severity.HIGH,
            Severity.CRITICAL,
        ]
        max_idx = 0
        for finding in self.findings:
            try:
                idx = severity_order.index(finding.severity)
                max_idx = max(max_idx, idx)
            except ValueError:
                pass
        return severity_order[max_idx]


# Security patterns to detect
SECURITY_PATTERNS: list[dict[str, Any]] = [
    # Prompt injection patterns
    {
        "id": "PROMPT-001",
        "category": ThreatCategory.PROMPT_INJECTION,
        "severity": Severity.HIGH,
        "pattern": r"ignore\s+(previous|prior|above)\s+instructions",
        "title": "Potential prompt injection",
        "description": "Detected phrase commonly used in prompt injection attacks.",
    },
    {
        "id": "PROMPT-002",
        "category": ThreatCategory.PROMPT_INJECTION,
        "severity": Severity.HIGH,
        "pattern": r"(system\s+prompt|ignore\s+system)",
        "title": "System prompt manipulation attempt",
        "description": "Detected attempt to reference or manipulate system prompts.",
    },
    # Command injection patterns
    {
        "id": "CMD-001",
        "category": ThreatCategory.COMMAND_INJECTION,
        "severity": Severity.CRITICAL,
        "pattern": r"(eval|exec)\s*\(",
        "title": "Code execution function detected",
        "description": "Use of eval() or exec() can lead to arbitrary code execution.",
    },
    {
        "id": "CMD-002",
        "category": ThreatCategory.COMMAND_INJECTION,
        "severity": Severity.HIGH,
        "pattern": r"subprocess\.call\s*\([^)]*shell\s*=\s*True",
        "title": "Shell injection risk",
        "description": "Using shell=True with subprocess can lead to command injection.",
    },
    {
        "id": "CMD-003",
        "category": ThreatCategory.COMMAND_INJECTION,
        "severity": Severity.MEDIUM,
        "pattern": r"os\.system\s*\(",
        "title": "System command execution",
        "description": "os.system() can be dangerous if user input is passed to it.",
    },
    # Data exfiltration patterns
    {
        "id": "EXFIL-001",
        "category": ThreatCategory.DATA_EXFILTRATION,
        "severity": Severity.HIGH,
        "pattern": r"(requests\.post|urllib\.request\.urlopen|http\.client)\s*\(",
        "title": "Outbound network request",
        "description": "Detected potential outbound network request.",
    },
    # Hardcoded secrets
    {
        "id": "SECRET-001",
        "category": ThreatCategory.HARDCODED_SECRETS,
        "severity": Severity.CRITICAL,
        "pattern": r"(api[_-]?key|apikey|password|secret|token)\s*[=:]\s*[\"'][^\"']{8,}[\"']",
        "title": "Potential hardcoded secret",
        "description": "Detected potential hardcoded API key, password, or secret.",
    },
    {
        "id": "SECRET-002",
        "category": ThreatCategory.HARDCODED_SECRETS,
        "severity": Severity.CRITICAL,
        "pattern": r"(AKIA|ghp_|gho_|glpat-|sk-|sk_live|sk_test)[a-zA-Z0-9]{10,}",
        "title": "Hardcoded credential detected",
        "description": "Detected pattern matching known credential formats.",
    },
    # Obfuscation patterns
    {
        "id": "OBF-001",
        "category": ThreatCategory.OBFUSCATION,
        "severity": Severity.MEDIUM,
        "pattern": r"(base64|binascii)\.b64decode\s*\(",
        "title": "Base64 decoding detected",
        "description": "Base64 decoding may be used to obfuscate malicious code.",
    },
    {
        "id": "OBF-002",
        "category": ThreatCategory.OBFUSCATION,
        "severity": Severity.MEDIUM,
        "pattern": r"(chr|ord)\s*\(\s*\d+",
        "title": "Character code manipulation",
        "description": "Character code manipulation may indicate obfuscation.",
    },
    # Suspicious patterns
    {
        "id": "SUSP-001",
        "category": ThreatCategory.SUSPICIOUS_PATTERNS,
        "severity": Severity.MEDIUM,
        "pattern": r"(__import__|importlib\.import_module)\s*\(",
        "title": "Dynamic import detected",
        "description": "Dynamic imports can be used to load malicious code.",
    },
    {
        "id": "SUSP-002",
        "category": ThreatCategory.SUSPICIOUS_PATTERNS,
        "severity": Severity.LOW,
        "pattern": r"(open|file)\s*\([^)]*[\"']w[\"']",
        "title": "File write operation",
        "description": "Detected file write operation - ensure path is validated.",
    },
]

# File extensions to skip
SKIP_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
    ".eot",
    ".ttf",
    ".otf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pyc",
    ".pyo",
}

# Maximum file size to scan (10 MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# Maximum number of files to scan
MAX_FILES = 500


class SkillScanner:
    """Security scanner for skill packages."""

    def __init__(
        self,
        max_files: int = MAX_FILES,
        max_file_size: int = MAX_FILE_SIZE,
        skip_extensions: set[str] | None = None,
    ):
        self.max_files = max_files
        self.max_file_size = max_file_size
        self.skip_extensions = skip_extensions or SKIP_EXTENSIONS

    def scan_skill(
        self,
        skill_dir: str | Path,
        skill_name: str | None = None,
    ) -> ScanResult:
        """Scan a skill directory for security issues.

        Args:
            skill_dir: Path to the skill directory.
            skill_name: Optional name of the skill.

        Returns:
            ScanResult containing all findings.
        """
        import time

        t0 = time.monotonic()
        skill_path = Path(skill_dir).resolve()
        name = skill_name or skill_path.name

        if not skill_path.is_dir():
            logger.warning("Skill directory does not exist: %s", skill_path)
            return ScanResult(
                skill_name=name,
                skill_directory=str(skill_path),
                scan_duration_seconds=time.monotonic() - t0,
            )

        # Discover files
        files = self._discover_files(skill_path)
        logger.debug("Discovered %d scannable files in %s", len(files), skill_path)

        # Scan each file
        all_findings: list[Finding] = []
        scanned_files: list[str] = []

        for file_path in files:
            scanned_files.append(str(file_path.relative_to(skill_path)))
            findings = self._scan_file(file_path, skill_path)
            all_findings.extend(findings)

        return ScanResult(
            skill_name=name,
            skill_directory=str(skill_path),
            findings=all_findings,
            scanned_files=scanned_files,
            scan_duration_seconds=time.monotonic() - t0,
        )

    def _discover_files(self, skill_path: Path) -> list[Path]:
        """Discover scannable files in skill directory."""
        files: list[Path] = []

        for item in skill_path.rglob("*"):
            if not item.is_file():
                continue

            # Skip hidden files
            if any(part.startswith(".") for part in item.relative_to(skill_path).parts):
                continue

            # Skip by extension
            if item.suffix.lower() in self.skip_extensions:
                continue

            # Skip large files
            try:
                if item.stat().st_size > self.max_file_size:
                    logger.debug("Skipping large file: %s", item)
                    continue
            except OSError:
                continue

            files.append(item)

            # Limit number of files
            if len(files) >= self.max_files:
                logger.warning("Reached max file limit (%d), skipping remaining files", self.max_files)
                break

        return files

    def _scan_file(self, file_path: Path, skill_path: Path) -> list[Finding]:
        """Scan a single file for security issues."""
        findings: list[Finding] = []

        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError) as e:
            logger.debug("Could not read file %s: %s", file_path, e)
            return findings

        rel_path = str(file_path.relative_to(skill_path))
        lines = content.split("\n")

        for rule in SECURITY_PATTERNS:
            pattern = rule["pattern"]

            for line_num, line in enumerate(lines, 1):
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    finding = Finding(
                        severity=rule["severity"],
                        category=rule["category"],
                        title=rule["title"],
                        description=rule["description"],
                        file_path=rel_path,
                        line_number=line_num,
                        line_content=line.strip()[:200],  # Truncate long lines
                        rule_id=rule["id"],
                    )
                    findings.append(finding)

        return findings

    def quick_check(self, skill_dir: str | Path) -> bool:
        """Quick security check - returns True if safe, False if issues found.

        Args:
            skill_dir: Path to the skill directory.

        Returns:
            True if no critical/high severity issues, False otherwise.
        """
        result = self.scan_skill(skill_dir)
        return result.is_safe
