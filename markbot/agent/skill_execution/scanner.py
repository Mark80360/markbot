"""Security scanner for skill scripts.

Performs static analysis to detect dangerous code patterns before execution.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger


@dataclass
class Finding:
    """A security finding from static analysis."""

    line: int
    pattern: str
    severity: str
    message: Optional[str] = None


@dataclass
class ScanResult:
    """Result of a security scan."""

    is_safe: bool
    findings: list[Finding] = field(default_factory=list)
    scan_duration_ms: float = 0.0


class SecurityScanner:
    """Static analysis scanner for skill scripts."""

    # Dangerous patterns by language
    PYTHON_PATTERNS = [
        (r"eval\s*\(", "high", "eval() can execute arbitrary code"),
        (r"exec\s*\(", "high", "exec() can execute arbitrary code"),
        (r"compile\s*\(", "medium", "compile() can create executable code"),
        (r"__import__\s*\(", "medium", "Dynamic imports may bypass security"),
        (r"importlib\..*import_module", "medium", "Dynamic imports may bypass security"),
        (r"os\.system\s*\(", "critical", "os.system() executes shell commands"),
        (r"subprocess\..*shell\s*=\s*True", "critical", "subprocess with shell=True is dangerous"),
        (r"spawning\s*\.", "high", "May execute child processes"),
        (r"pty\s*\.", "high", "PTY module may be used for shell access"),
        (r"os\.remove\s*\(", "high", "File deletion detected"),
        (r"os\.unlink\s*\(", "high", "File deletion detected"),
        (r"shutil\.(rmtree|remove_tree)", "high", "Directory deletion detected"),
        (r"pathlib.*\.unlink\s*\(", "high", "File deletion detected"),
        (r"pathlib.*\.rmdir\s*\(", "medium", "Directory deletion detected"),
        (r"urllib\.request", "low", "Network access detected"),
        (r"http\.client", "low", "Network access detected"),
        (r"socket\.", "low", "Network access detected"),
        (r"requests\.(get|post|put|delete|patch)", "low", "HTTP requests detected"),
        (r"httpx\.", "low", "HTTP requests detected"),
        (r"os\.setuid|os\.setgid", "critical", "Privilege escalation attempt"),
        (r"ctypes\.CDLL|ctypes\.windll", "high", "Loading native libraries detected"),
        (r"pickle\.(loads?|load)", "critical", "Pickle deserialization is dangerous"),
        (r"yaml\.(load|unsafe_load)", "high", "Unsafe YAML loading detected"),
    ]

    SHELL_PATTERNS = [
        (r"^curl\s|^wget\s", "medium", "Downloading external content"),
        (r"^eval\s", "critical", "eval is dangerous in shell scripts"),
        (r"\$\([^)]+\)", "medium", "Command substitution detected"),
        (r"`[^`]+`", "medium", "Command substitution detected"),
        (r"\brm\s+-rf\s", "critical", "Force recursive delete is very dangerous"),
        (r"\bdd\s+|mkfs\.", "critical", "Disk operations detected"),
        (r"\bpasswd\s|chsh\s|chfn\s", "high", "Password/system configuration changes"),
        (r"\b(nc|netcat)\s+-", "medium", "Network connections detected"),
        (r"\bsudo\s|su\s+-", "high", "Privilege escalation detected"),
    ]

    NODE_PATTERNS = [
        (r"eval\s*\(", "critical", "eval() can execute arbitrary code"),
        (r"new\s+Function\s*\(", "critical", "Function constructor can execute arbitrary code"),
        (r"setTimeout\s*\([^,]*['\"`]", "high", "setTimeout with string is dangerous"),
        (r"setInterval\s*\([^,]*['\"`]", "high", "setInterval with string is dangerous"),
        (r"require\s*\(", "low", "Dynamic require detected"),
        (r"import\s*\(", "low", "Dynamic import detected"),
        (r"fs\.unlink|fs\.rmdir|fs\.rm", "high", "File/directory deletion detected"),
        (r"fs\.writeFile", "medium", "File write detected"),
        (r"child_process", "high", "Child process creation detected"),
        (r"spawn\s*\(|exec\s*\(|execSync", "high", "Process execution detected"),
        (r"http\.(get|request)|https\.(get|request)", "low", "HTTP requests detected"),
        (r"fetch\s*\(", "low", "fetch API usage detected"),
        (r"JSON\.parse", "low", "JSON parsing detected"),
        (r"bindings\s*\(|node-gyp", "medium", "Native module loading detected"),
    ]

    LANGUAGE_PATTERNS = {
        "python": PYTHON_PATTERNS,
        "shell": SHELL_PATTERNS,
        "sh": SHELL_PATTERNS,
        "bash": SHELL_PATTERNS,
        "node": NODE_PATTERNS,
        "nodejs": NODE_PATTERNS,
    }

    BLOCKING_SEVERITIES = {"critical", "high"}

    def __init__(self):
        """Initialize the security scanner."""
        self._compiled_patterns: dict[str, list[tuple]] = {}

    def _get_compiled_patterns(self, language: str) -> list[tuple]:
        """Get compiled regex patterns for a language."""
        if language not in self._compiled_patterns:
            raw_patterns = self.LANGUAGE_PATTERNS.get(language, [])
            compiled = []
            for pattern, severity, message in raw_patterns:
                try:
                    compiled.append((re.compile(pattern, re.IGNORECASE), severity, message))
                except re.error as e:
                    logger.warning("Invalid regex pattern '{}': {}", pattern, e)
            self._compiled_patterns[language] = compiled
        return self._compiled_patterns[language]

    def scan(self, script_path: Path, language: Optional[str] = None) -> ScanResult:
        """Scan a script file for dangerous patterns."""
        import time

        start_time = time.perf_counter()

        if not script_path.exists():
            return ScanResult(
                is_safe=False,
                findings=[Finding(0, "", "critical", f"Script not found: {script_path}")],
            )

        if language is None:
            language = self._detect_language(script_path)

        try:
            content = script_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ScanResult(
                is_safe=False,
                findings=[Finding(0, "", "critical", "Could not decode script file")],
            )
        except Exception as e:
            return ScanResult(
                is_safe=False,
                findings=[Finding(0, "", "critical", f"Error reading file: {e}")],
            )

        findings = []
        patterns = self._get_compiled_patterns(language)

        for pattern, severity, message in patterns:
            for match in pattern.finditer(content):
                line_num = content[: match.start()].count("\n") + 1
                finding = Finding(
                    line=line_num,
                    pattern=pattern.pattern,
                    severity=severity,
                    message=message,
                )
                findings.append(finding)

        findings.extend(self._check_obfuscation(content))

        is_safe = not any(f.severity in self.BLOCKING_SEVERITIES for f in findings)

        scan_duration = (time.perf_counter() - start_time) * 1000

        return ScanResult(
            is_safe=is_safe,
            findings=findings,
            scan_duration_ms=scan_duration,
        )

    def _detect_language(self, script_path: Path) -> str:
        """Detect language from file extension."""
        ext_map = {
            ".py": "python",
            ".sh": "shell",
            ".bash": "bash",
            ".zsh": "shell",
            ".js": "node",
            ".mjs": "node",
            ".cjs": "node",
        }
        return ext_map.get(script_path.suffix.lower(), "python")

    def _check_obfuscation(self, content: str) -> list[Finding]:
        """Check for code obfuscation indicators."""
        findings = []

        base64_pattern = re.compile(r'[A-Za-z0-9+/]{40,}={0,2}')
        if len(base64_pattern.findall(content)) > 5:
            findings.append(Finding(
                line=0,
                pattern="base64_obfuscation",
                severity="medium",
                message="Suspicious: Multiple base64-like strings detected",
            ))

        concat_pattern = re.compile(r'["\']\s*\+\s*["\']')
        if len(concat_pattern.findall(content)) > 20:
            findings.append(Finding(
                line=0,
                pattern="string_concat",
                severity="low",
                message="Suspicious: Excessive string concatenation",
            ))

        return findings

    def get_allowed_patterns(self, language: str) -> list[str]:
        """Get list of all patterns being checked for a language."""
        patterns = self.LANGUAGE_PATTERNS.get(language, [])
        return [p[0] for p in patterns]
