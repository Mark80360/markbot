"""Security scanner for skill scripts and content.

Performs static analysis to detect dangerous code patterns before execution.
Covers: exfiltration, prompt injection, destructive operations, persistence,
reverse shells, obfuscation, privilege escalation, supply chain, and more.

Trust-level policy determines whether a skill is allowed based on scan verdict
and source trust level.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger


@dataclass
class Finding:
    line: int
    pattern: str
    severity: str
    message: Optional[str] = None


@dataclass
class ScanResult:
    is_safe: bool
    findings: list[Finding] = field(default_factory=list)
    scan_duration_ms: float = 0.0
    verdict: str = "safe"


TRUST_POLICY = {
    "builtin":       {"safe": "allow", "caution": "allow", "dangerous": "allow"},
    "workspace":     {"safe": "allow", "caution": "allow", "dangerous": "block"},
    "external":      {"safe": "allow", "caution": "block", "dangerous": "block"},
    "agent-created": {"safe": "allow", "caution": "allow", "dangerous": "block"},
}

BLOCKING_SEVERITIES = {"critical", "high"}


def should_allow(result: ScanResult, trust_level: str = "workspace") -> tuple[bool, str]:
    """Determine if a skill should be allowed based on scan result and trust level.

    Returns (allowed, reason).
    """
    policy = TRUST_POLICY.get(trust_level, TRUST_POLICY["workspace"])
    action = policy.get(result.verdict, "block")

    if action == "allow":
        return True, ""
    elif action == "ask":
        return True, f"Review recommended: {result.verdict} findings detected"
    else:
        return False, f"Blocked by {trust_level} trust policy: {result.verdict} findings"


THREAT_PATTERNS = [
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_curl", "critical", "exfiltration",
     "curl command interpolating secret environment variable"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_wget", "critical", "exfiltration",
     "wget command interpolating secret environment variable"),
    (r'os\.environ\b(?!\s*\.get\s*\(\s*["\']PATH)',
     "python_os_environ", "high", "exfiltration",
     "accesses os.environ (potential env dump)"),
    (r'os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_getenv_secret", "critical", "exfiltration",
     "reads secret via os.getenv()"),
    (r'process\.env\[',
     "node_process_env", "high", "exfiltration",
     "accesses process.env (Node.js environment)"),
    (r'\$HOME/\.ssh|\~/\.ssh',
     "ssh_dir_access", "high", "exfiltration",
     "references user SSH directory"),
    (r'\$HOME/\.aws|\~/\.aws',
     "aws_dir_access", "high", "exfiltration",
     "references user AWS credentials directory"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)',
     "read_secrets_file", "critical", "exfiltration",
     "reads known secrets file"),
    (r'printenv|env\s*\|',
     "dump_all_env", "high", "exfiltration",
     "dumps all environment variables"),
    (r'base64[^\n]*env',
     "encoded_exfil", "high", "exfiltration",
     "base64 encoding combined with environment access"),
    (r'>\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|python)',
     "tmp_staging", "critical", "exfiltration",
     "writes to /tmp then exfiltrates"),

    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions',
     "prompt_injection_ignore", "critical", "injection",
     "prompt injection: ignore previous instructions"),
    (r'you\s+are\s+(?:\w+\s+)*now\s+',
     "role_hijack", "high", "injection",
     "attempts to override the agent's role"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user',
     "deception_hide", "critical", "injection",
     "instructs agent to hide information from user"),
    (r'system\s+prompt\s+override',
     "sys_prompt_override", "critical", "injection",
     "attempts to override the system prompt"),
    (r'pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+',
     "role_pretend", "high", "injection",
     "attempts to make the agent assume a different identity"),
    (r'disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)',
     "disregard_rules", "critical", "injection",
     "instructs agent to disregard its rules"),
    (r'output\s+(?:\w+\s+)*(system|initial)\s+prompt',
     "leak_system_prompt", "high", "injection",
     "attempts to extract the system prompt"),
    (r'act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don\'t\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)',
     "bypass_restrictions", "critical", "injection",
     "instructs agent to act without restrictions"),
    (r'\bDAN\s+mode\b|Do\s+Anything\s+Now',
     "jailbreak_dan", "critical", "injection",
     "DAN jailbreak attempt"),
    (r'\bdeveloper\s+mode\b.*\benabled?\b',
     "jailbreak_dev_mode", "critical", "injection",
     "developer mode jailbreak attempt"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->',
     "html_comment_injection", "high", "injection",
     "hidden instructions in HTML comments"),

    (r'rm\s+-rf\s+/',
     "destructive_root_rm", "critical", "destructive",
     "recursive delete from root"),
    (r'rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME',
     "destructive_home_rm", "critical", "destructive",
     "recursive delete targeting home directory"),
    (r'chmod\s+777',
     "insecure_perms", "medium", "destructive",
     "sets world-writable permissions"),
    (r'>\s*/etc/',
     "system_overwrite", "critical", "destructive",
     "overwrites system configuration file"),
    (r'\bmkfs\b',
     "format_filesystem", "critical", "destructive",
     "formats a filesystem"),
    (r'\bdd\s+.*if=.*of=/dev/',
     "disk_overwrite", "critical", "destructive",
     "raw disk write operation"),
    (r'shutil\.rmtree\s*\(\s*["\']\/',
     "python_rmtree", "high", "destructive",
     "Python rmtree on absolute or root-relative path"),
    (r'truncate\s+-s\s*0\s+/',
     "truncate_system", "critical", "destructive",
     "truncates system file to zero bytes"),

    (r'\bcrontab\b',
     "persistence_cron", "medium", "persistence",
     "modifies cron jobs"),
    (r'\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b',
     "shell_rc_mod", "medium", "persistence",
     "references shell startup file"),
    (r'authorized_keys',
     "ssh_backdoor", "critical", "persistence",
     "modifies SSH authorized keys"),
    (r'ssh-keygen',
     "ssh_keygen", "medium", "persistence",
     "generates SSH keys"),
    (r'systemd.*\.service|systemctl\s+(enable|start)',
     "systemd_service", "medium", "persistence",
     "references or enables systemd service"),
    (r'launchctl\s+load|LaunchAgents|LaunchDaemons',
     "macos_launchd", "medium", "persistence",
     "macOS launch agent/daemon persistence"),
    (r'/etc/sudoers|visudo',
     "sudoers_mod", "critical", "persistence",
     "modifies sudoers (privilege escalation)"),

    (r'\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b',
     "reverse_shell", "critical", "network",
     "potential reverse shell listener"),
    (r'\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b',
     "tunnel_service", "high", "network",
     "uses tunneling service for external access"),
    (r'/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/',
     "bash_reverse_shell", "critical", "network",
     "bash interactive reverse shell via /dev/tcp"),
    (r'python[23]?\s+-c\s*["\']import\s+socket',
     "python_socket_oneliner", "critical", "network",
     "Python one-liner socket connection (likely reverse shell)"),
    (r'socket\.connect\s*\(\s*\(',
     "python_socket_connect", "high", "network",
     "Python socket connect to arbitrary host"),
    (r'webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com',
     "exfil_service", "high", "network",
     "references known data exfiltration/webhook testing service"),

    (r'base64\s+(-d|--decode)\s*\|',
     "base64_decode_pipe", "high", "obfuscation",
     "base64 decodes and pipes to execution"),
    (r'\beval\s*\(\s*["\']',
     "eval_string", "high", "obfuscation",
     "eval() with string argument"),
    (r'\bexec\s*\(\s*["\']',
     "exec_string", "high", "obfuscation",
     "exec() with string argument"),
    (r'echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)',
     "echo_pipe_exec", "critical", "obfuscation",
     "echo piped to interpreter for execution"),
    (r'compile\s*\(\s*[^\)]+,\s*["\'].*["\']\s*,\s*["\']exec["\']\s*\)',
     "python_compile_exec", "high", "obfuscation",
     "Python compile() with exec mode"),
    (r'getattr\s*\(\s*__builtins__',
     "python_getattr_builtins", "high", "obfuscation",
     "dynamic access to Python builtins (evasion technique)"),
    (r'__import__\s*\(\s*["\']os["\']\s*\)',
     "python_import_os", "high", "obfuscation",
     "dynamic import of os module"),
    (r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+',
     "chr_building", "high", "obfuscation",
     "building string from chr() calls (obfuscation)"),

    (r'subprocess\.(run|call|Popen|check_output)\s*\(',
     "python_subprocess", "medium", "execution",
     "Python subprocess execution"),
    (r'os\.system\s*\(',
     "python_os_system", "high", "execution",
     "os.system() — unguarded shell execution"),
    (r'os\.popen\s*\(',
     "python_os_popen", "high", "execution",
     "os.popen() — shell pipe execution"),
    (r'child_process\.(exec|spawn|fork)\s*\(',
     "node_child_process", "high", "execution",
     "Node.js child_process execution"),

    (r'\.\./\.\./\.\.',
     "path_traversal_deep", "high", "traversal",
     "deep relative path traversal (3+ levels up)"),
    (r'/etc/passwd|/etc/shadow',
     "system_passwd_access", "critical", "traversal",
     "references system password files"),

    (r'curl\s+[^\n]*\|\s*(ba)?sh',
     "curl_pipe_shell", "critical", "supply_chain",
     "curl piped to shell (download-and-execute)"),
    (r'wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh',
     "wget_pipe_shell", "critical", "supply_chain",
     "wget piped to shell (download-and-execute)"),
    (r'pip\s+install\s+(?!-r\s)(?!.*==)',
     "unpinned_pip_install", "medium", "supply_chain",
     "pip install without version pinning"),
    (r'npm\s+install\s+(?!.*@\d)',
     "unpinned_npm_install", "medium", "supply_chain",
     "npm install without version pinning"),

    (r'\bsudo\b',
     "sudo_usage", "high", "privilege_escalation",
     "uses sudo (privilege escalation)"),
    (r'setuid|setgid|cap_setuid',
     "setuid_setgid", "critical", "privilege_escalation",
     "setuid/setgid (privilege escalation mechanism)"),
    (r'NOPASSWD',
     "nopasswd_sudo", "critical", "privilege_escalation",
     "NOPASSWD sudoers entry (passwordless privilege escalation)"),

    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\'][A-Za-z0-9+/=_-]{20,}',
     "hardcoded_secret", "critical", "credential_exposure",
     "possible hardcoded API key, token, or secret"),
    (r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----',
     "embedded_private_key", "critical", "credential_exposure",
     "embedded private key"),
    (r'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}',
     "github_token_leaked", "critical", "credential_exposure",
     "GitHub personal access token in skill content"),
    (r'sk-[A-Za-z0-9]{20,}',
     "openai_key_leaked", "critical", "credential_exposure",
     "possible OpenAI API key in skill content"),
    (r'AKIA[0-9A-Z]{16}',
     "aws_access_key_leaked", "critical", "credential_exposure",
     "AWS access key ID in skill content"),

    (r'xmrig|stratum\+tcp|monero|coinhive|cryptonight',
     "crypto_mining", "critical", "mining",
     "cryptocurrency mining reference"),

    (r'AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules',
     "agent_config_mod", "critical", "persistence",
     "references agent config files (could persist malicious instructions)"),
]

SKILL_MD_INJECTION_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions',
     "skill_injection_ignore", "critical", "injection",
     "prompt injection in SKILL.md: ignore previous instructions"),
    (r'you\s+are\s+(?:\w+\s+)*now\s+',
     "skill_injection_role", "high", "injection",
     "role hijack attempt in SKILL.md content"),
    (r'system\s+prompt\s+override',
     "skill_injection_override", "critical", "injection",
     "system prompt override attempt in SKILL.md"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user',
     "skill_injection_deception", "critical", "injection",
     "deception instruction in SKILL.md content"),
    (r'\bDAN\s+mode\b|Do\s+Anything\s+Now',
     "skill_injection_dan", "critical", "injection",
     "DAN jailbreak in SKILL.md content"),
]


class SecurityScanner:
    """Static analysis scanner for skill scripts and SKILL.md content."""

    PYTHON_PATTERNS = [
        (r"os\.remove\s*\(", "high", "File deletion detected"),
        (r"os\.unlink\s*\(", "high", "File deletion detected"),
        (r"pathlib.*\.unlink\s*\(", "high", "File deletion detected"),
        (r"pickle\.(loads?|load)", "critical", "Pickle deserialization is dangerous"),
        (r"yaml\.(load|unsafe_load)", "high", "Unsafe YAML loading detected"),
    ]

    SHELL_PATTERNS = [
        (r"\bdd\s+|mkfs\.", "critical", "Disk operations detected"),
    ]

    NODE_PATTERNS = [
        (r"new\s+Function\s*\(", "critical", "Function constructor can execute arbitrary code"),
    ]

    LANGUAGE_PATTERNS = {
        "python": PYTHON_PATTERNS,
        "shell": SHELL_PATTERNS,
        "sh": SHELL_PATTERNS,
        "bash": SHELL_PATTERNS,
        "node": NODE_PATTERNS,
        "nodejs": NODE_PATTERNS,
    }

    def __init__(self):
        self._compiled_patterns: dict[str, list[tuple]] = {}
        self._compiled_threats: list[tuple] | None = None
        self._compiled_skill_md_threats: list[tuple] | None = None

    def _get_compiled_patterns(self, language: str) -> list[tuple]:
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

    def _get_compiled_threats(self) -> list[tuple]:
        if self._compiled_threats is None:
            self._compiled_threats = []
            for pattern, pid, severity, category, description in THREAT_PATTERNS:
                try:
                    self._compiled_threats.append((
                        re.compile(pattern, re.IGNORECASE | re.MULTILINE),
                        pid, severity, category, description,
                    ))
                except re.error as e:
                    logger.warning("Invalid threat pattern '{}': {}", pid, e)
        return self._compiled_threats

    def _get_compiled_skill_md_threats(self) -> list[tuple]:
        if self._compiled_skill_md_threats is None:
            self._compiled_skill_md_threats = []
            for pattern, pid, severity, category, description in SKILL_MD_INJECTION_PATTERNS:
                try:
                    self._compiled_skill_md_threats.append((
                        re.compile(pattern, re.IGNORECASE | re.MULTILINE),
                        pid, severity, category, description,
                    ))
                except re.error as e:
                    logger.warning("Invalid SKILL.md threat pattern '{}': {}", pid, e)
        return self._compiled_skill_md_threats

    def scan(self, script_path: Path, language: Optional[str] = None) -> ScanResult:
        """Scan a script file for dangerous patterns."""
        import time

        start_time = time.perf_counter()

        if not script_path.exists():
            return ScanResult(
                is_safe=False,
                findings=[Finding(0, "", "critical", f"Script not found: {script_path}")],
                verdict="dangerous",
            )

        if language is None:
            language = self._detect_language(script_path)

        try:
            content = script_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ScanResult(
                is_safe=False,
                findings=[Finding(0, "", "critical", "Could not decode script file")],
                verdict="dangerous",
            )
        except Exception as e:
            return ScanResult(
                is_safe=False,
                findings=[Finding(0, "", "critical", f"Error reading file: {e}")],
                verdict="dangerous",
            )

        findings = []

        patterns = self._get_compiled_patterns(language)
        for pattern, severity, message in patterns:
            for match in pattern.finditer(content):
                line_num = content[: match.start()].count("\n") + 1
                findings.append(Finding(
                    line=line_num,
                    pattern=pattern.pattern,
                    severity=severity,
                    message=message,
                ))

        threats = self._get_compiled_threats()
        for pattern, pid, severity, category, description in threats:
            for match in pattern.finditer(content):
                line_num = content[: match.start()].count("\n") + 1
                findings.append(Finding(
                    line=line_num,
                    pattern=pid,
                    severity=severity,
                    message=description,
                ))

        findings.extend(self._check_obfuscation(content))

        verdict = self._compute_verdict(findings)
        is_safe = verdict == "safe" or not any(f.severity in BLOCKING_SEVERITIES for f in findings)

        scan_duration = (time.perf_counter() - start_time) * 1000

        return ScanResult(
            is_safe=is_safe,
            findings=findings,
            scan_duration_ms=scan_duration,
            verdict=verdict,
        )

    def scan_code(
        self,
        code: str,
        language: str = "python",
        trust_level: str = "agent-created",
    ) -> ScanResult:
        """Scan a code string (not a file) for dangerous patterns.

        Used by CodeExecutionTool to validate agent-generated code before
        execution.  Reuses the same threat and language-specific patterns
        as ``scan()`` but operates on an in-memory string.
        """
        import time

        start_time = time.perf_counter()

        if not code.strip():
            return ScanResult(is_safe=True, verdict="safe")

        findings: list[Finding] = []

        patterns = self._get_compiled_patterns(language)
        for pattern, severity, message in patterns:
            for match in pattern.finditer(code):
                line_num = code[: match.start()].count("\n") + 1
                findings.append(Finding(
                    line=line_num,
                    pattern=pattern.pattern,
                    severity=severity,
                    message=message,
                ))

        threats = self._get_compiled_threats()
        for pattern, pid, severity, category, description in threats:
            for match in pattern.finditer(code):
                line_num = code[: match.start()].count("\n") + 1
                findings.append(Finding(
                    line=line_num,
                    pattern=pid,
                    severity=severity,
                    message=description,
                ))

        findings.extend(self._check_obfuscation(code))

        verdict = self._compute_verdict(findings)

        policy = TRUST_POLICY.get(trust_level, TRUST_POLICY["workspace"])
        action = policy.get(verdict, "block")
        is_safe = action != "block"

        scan_duration = (time.perf_counter() - start_time) * 1000

        return ScanResult(
            is_safe=is_safe,
            findings=findings,
            scan_duration_ms=scan_duration,
            verdict=verdict,
        )

    def scan_skill_md(self, skill_md_path: Path) -> ScanResult:
        """Scan a SKILL.md file for prompt injection and dangerous patterns."""
        import time

        start_time = time.perf_counter()

        if not skill_md_path.exists():
            return ScanResult(is_safe=True, verdict="safe")

        try:
            content = skill_md_path.read_text(encoding="utf-8")
        except Exception:
            return ScanResult(is_safe=True, verdict="safe")

        findings = []

        threats = self._get_compiled_skill_md_threats()
        for pattern, pid, severity, category, description in threats:
            for match in pattern.finditer(content):
                line_num = content[: match.start()].count("\n") + 1
                findings.append(Finding(
                    line=line_num,
                    pattern=pid,
                    severity=severity,
                    message=description,
                ))

        verdict = self._compute_verdict(findings)
        is_safe = self._is_safe_from_findings(findings)

        scan_duration = (time.perf_counter() - start_time) * 1000

        return ScanResult(
            is_safe=is_safe,
            findings=findings,
            scan_duration_ms=scan_duration,
            verdict=verdict,
        )

    def scan_skill_dir(self, skill_dir: Path, trust_level: str = "workspace") -> ScanResult:
        """Scan an entire skill directory: SKILL.md + all scripts."""
        import time

        start_time = time.perf_counter()
        all_findings: list[Finding] = []

        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            md_result = self.scan_skill_md(skill_md)
            all_findings.extend(md_result.findings)

        for f in sorted(skill_dir.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            if f.name == "SKILL.md":
                continue

            ext = f.suffix.lower()
            lang_map = {
                ".py": "python", ".sh": "shell", ".bash": "bash",
                ".js": "node", ".mjs": "node", ".cjs": "node",
            }
            language = lang_map.get(ext)
            if not language:
                continue

            result = self.scan(f, language)
            all_findings.extend(result.findings)

        verdict = self._compute_verdict(all_findings)
        is_safe = self._is_safe_from_findings(all_findings)

        scan_duration = (time.perf_counter() - start_time) * 1000

        return ScanResult(
            is_safe=is_safe,
            findings=all_findings,
            scan_duration_ms=scan_duration,
            verdict=verdict,
        )

    @staticmethod
    def _is_safe_from_findings(findings: list[Finding]) -> bool:
        return not any(f.severity in BLOCKING_SEVERITIES for f in findings)

    @staticmethod
    def _compute_verdict(findings: list[Finding]) -> str:
        """Compute scan verdict from findings."""
        if not findings:
            return "safe"

        severities = {f.severity for f in findings}
        if severities & {"critical"}:
            return "dangerous"
        if severities & {"high"}:
            return "caution"
        return "safe"

    def _detect_language(self, script_path: Path) -> str:
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
        patterns = self.LANGUAGE_PATTERNS.get(language, [])
        return [p[0] for p in patterns]
