"""Shared constants for all agent tools.

This module provides unified constants to ensure consistency across
search, filesystem, explore, and other tools.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Memory file constants (canonical definitions — import from here)
# ---------------------------------------------------------------------------
MEMORY_FILENAME: str = "MEMORY.md"
USER_FILENAME: str = "PROFILE.md"
DEFAULT_MEMORY_CHAR_LIMIT: int = 4000
DEFAULT_USER_CHAR_LIMIT: int = 2000

# Channels where curated MEMORY.md may be auto-loaded into the system prompt.
# Shared / messaging channels must not receive private long-term memory.
MAIN_MEMORY_CHANNELS: frozenset[str] = frozenset({
    "cli",
    "web",
    "api",
    "test",
    "local",
})

# Messaging / shared channels where MEMORY.md always-on injection is forbidden.
SHARED_MEMORY_CHANNELS: frozenset[str] = frozenset({
    "dingtalk",
    "feishu",
    "qq",
    "weixin",
    "wechat",
    "email",
    "telegram",
    "discord",
    "slack",
    "group",
})

# Standard directories to ignore in file operations (comprehensive list)
IGNORE_DIRS = frozenset({
    # Version Control
    ".git", ".hg", ".svn", ".bzr", ".cvs",

    # JavaScript/Node.js
    "node_modules", ".npm", ".yarn", ".pnpm",
    "bower_components", "jspm_packages",

    # Python
    "__pycache__", ".pyc_cache", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".coverage", "htmlcov",
    ".venv", "venv", ".virtualenv", "env", ".env",
    ".tox", ".eggs", "*.egg-info", "dist", "build",

    # Frontend build artifacts
    ".next", ".nuxt", ".output", ".cache", ".turbo",
    ".parcel-cache", ".vite",

    # Java/Gradle
    "target", ".gradle", ".idea", ".settings",

    # IDE & Editor
    ".vscode", ".vim", ".emacs.desktop",
    ".DS_Store", "Thumbs.db", "desktop.ini",

    # Dependencies
    "vendor", "site-packages", "third_party",

    # Temp & Cache
    ".tmp", ".temp", ".cache", "tmp", "temp",

    # OS generated
    ".Trashes", ".Spotlight-V100", ".fseventsd",
}).union({".github", ".gitlab"}) - {""}

# Binary file extensions to skip in content search
BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".tiff", ".tif", ".psd", ".raw", ".heic", ".heif", ".avif",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".opus",
    ".aiff", ".pcm", ".ape",
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mkv", ".m4v",
    ".3gp", ".ts", ".mts", ".vob",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".bz2", ".xz", ".lzma",
    ".cab", ".iso", ".dmg", ".deb", ".rpm",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf",
    ".pyc", ".pyo", ".pyd", ".class", ".o", ".obj", ".so", ".dll",
    ".dylib", ".lib", ".a", ".exe", ".msi", ".app", ".out",
    ".jar", ".war", ".ear", ".node",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pfb",
    ".sqlite", ".db", ".sqlite3", ".parquet", ".h5", ".hdf5",
    ".accdb", ".mdb",
    ".bin", ".dat", ".pak", ".wasm", ".swf", ".unity3d",
})

# Maximum values for tool operations
MAX_SEARCH_RESULTS = 500
_MAX_OUTPUT_CHARS = 10_000
_MAX_FILE_SIZE_READ = 128_000  # bytes
MAX_CONTEXT_LINES = 20
_MAX_RECURSION_DEPTH = 10

# Security patterns for command execution
DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    r"(?:^|[;&|]\s*)format\b",
    r"\b(mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff|halt|init\s+[06])\b",
    r":\(\)\s*\{.*\};\s*:",
    r"\bcurl\s+.*\|\s*(bash|sh|python|perl|ruby|node)\b",
    r"\bwget\s+.*\|\s*(bash|sh|python|perl|ruby|node)\b",
    r"\beval\s+\$\(.*\)",
    r"\bsource\s+/dev/",
    r"\bchmod\s+777\b",
    r"\bchown\b.*\s+/(etc|boot|s?bin|root|lib64?|usr/(?:s?bin|lib))\b",
    r">\s+/etc/",
    r"\bmv\b.*\s+/(etc|boot|s?bin|root|lib64?|usr/(?:s?bin|lib))\b",
    r"\bcp\b.*\s+/(etc|boot|s?bin|root|lib64?|usr/(?:s?bin|lib))\b",
    r"\bmv\b.*[A-Za-z]:[\\/](Windows|Program\s*Files|ProgramData)[\\/]",
    r"\bcp\b.*[A-Za-z]:[\\/](Windows|Program\s*Files|ProgramData)[\\/]",
    r"\$\{IFS\}",
    r"\$@\s",
    r"\bxargs\s+.*\b(sh|bash|python|perl)\b",
    r"\b(base64|xxd)\s+.*-d.*\|\s*(sh|bash)\b",
    r"\bopenssl\s+enc\s+.*-d.*\|\s*(sh|bash)\b",
    r"\bsudo\s+.*\brm\s+-[rf]{1,2}\b",
    r"\bsudo\s+su\b",
    r"\bchmod\s+[0-7]*7[0-7]*7[0-7]*\s+/",
    r"\bnc\s+.*-e\s+/bin/",
    r"\b/dev/tcp/",
]

# -- Bootstrap file tiers ------------------------------------------------
# Tier 1 (ESSENTIAL): loaded into every system prompt. Small, high-value.
BOOTSTRAP_FILES_ESSENTIAL: list[str] = ["AGENTS.md", "SOUL.md", USER_FILENAME]
# Tier 2 (CONDITIONAL): loaded on-demand or per-session-type.
BOOTSTRAP_FILES_CONDITIONAL: list[str] = [MEMORY_FILENAME]
# Tier 3 (REFERENCE): loaded only when explicitly requested via context tools.
# TOOLS.md is redundant with function-calling tool schemas.
# ARCHITECTURE.md is reference material, rarely needed mid-conversation.
BOOTSTRAP_FILES_REFERENCE: list[str] = ["TOOLS.md", "ARCHITECTURE.md"]
# Legacy union for backward compatibility (template sync, etc.)
BOOTSTRAP_FILES: list[str] = [
    *BOOTSTRAP_FILES_ESSENTIAL,
    *BOOTSTRAP_FILES_CONDITIONAL,
    *BOOTSTRAP_FILES_REFERENCE,
]

# Template files that should exist on disk (excludes conditionally-used files
# like BOOTSTRAP.md, HEARTBEAT.md which are intentionally not in BOOTSTRAP_FILES)
_TEMPLATE_ESSENTIAL: frozenset[str] = frozenset({
    "AGENTS.md", "SOUL.md", "TOOLS.md", MEMORY_FILENAME,
    USER_FILENAME, "ARCHITECTURE.md",
})

# Conditional/reference templates that exist on disk but are not loaded into
# the system prompt automatically (loaded on-demand or per-event).
_TEMPLATE_CONDITIONAL: frozenset[str] = frozenset({
    "BOOTSTRAP.md",               # deleted after first-run onboarding
    "HEARTBEAT.md",               # heartbeat task file
    "clean-state-checklist.md",   # reference checklist
    "evaluator-rubric.md",        # reference rubric
    "quality-document.md",        # reference quality snapshot
})

# All known template .md files (used by check_template_sync to detect drift).
_ALL_TEMPLATE_FILES: frozenset[str] = _TEMPLATE_ESSENTIAL | _TEMPLATE_CONDITIONAL


def check_template_sync(templates_dir: "Path | None" = None) -> list[str]:
    """Cross-check template constants against the templates directory.

    Returns a list of warning messages for any discrepancies found:
    - Template files on disk that are not in the known allowlist (drift)
    - Essential template files missing from disk

    This function is called at startup to catch drift between the
    hardcoded constants and the actual template files on disk.
    """
    from pathlib import Path

    warnings: list[str] = []

    if templates_dir is None:
        templates_dir = Path(__file__).resolve().parent.parent / "templates"

    if not templates_dir.is_dir():
        return warnings

    disk_files: set[str] = set()
    for p in templates_dir.iterdir():
        if p.is_file() and p.suffix == ".md":
            disk_files.add(p.name)

    unexpected_on_disk = disk_files - _ALL_TEMPLATE_FILES
    if unexpected_on_disk:
        warnings.append(
            f"Template file(s) on disk but not in known template sets: "
            f"{sorted(unexpected_on_disk)}. "
            f"Either add them to _TEMPLATE_CONDITIONAL in constants.py, "
            f"or remove them from the templates directory."
        )

    missing_essential = _TEMPLATE_ESSENTIAL - disk_files
    if missing_essential:
        warnings.append(
            f"Essential template file(s) missing from disk: "
            f"{sorted(missing_essential)}. "
            f"These are required for the system to function."
        )

    return warnings

# Context builder cache TTL (seconds)
CONTEXT_CACHE_TTL: float = 300.0

# Guidance injection TTL per session (seconds)
GUIDANCE_INJECTION_TTL: float = 3600.0

# Maximum characters for git status in system context
MAX_GIT_STATUS_CHARS: int = 2000

# Maximum characters for compressed summary before truncation.
# Kept modest so the summary stays a *compressed* handoff, not a second
# context window.  At ~4 chars/token this is ~5k tokens — enough for a
# structured multi-section summary while leaving the model context window
# for live conversation.  A larger value lets the summary grow until it
# dominates the context budget and starves the very compaction that
# produces it (positive feedback loop).
MAX_COMPRESSED_SUMMARY_CHARS: int = 20_000

# Maximum characters for MEMORY.md before section-based truncation
# Should be >= DEFAULT_MEMORY_CHAR_LIMIT from memory.tool (the write limit)
MAX_MEMORY_MD_CHARS: int = max(8_000, DEFAULT_MEMORY_CHAR_LIMIT * 2)

# Maximum characters for daily log search results
MAX_DAILY_LOG_RESULT_CHARS: int = 2000

# Default context window tokens
DEFAULT_CONTEXT_WINDOW_TOKENS: int = 65_536

# Default compaction threshold ratio
DEFAULT_COMPACTION_THRESHOLD: float = 0.75

# -- Memory system constants ------------------------------------------------

# Maximum prefetch results per turn
MAX_PREFETCH_RESULTS: int = 3

# Minimum relevance score for prefetch recall
MIN_PREFETCH_SCORE: float = 0.15

# Maximum entries in MemoryStore before cleanup / rejection
MAX_MEMORY_ENTRIES: int = 100
MAX_USER_ENTRIES: int = 50

# Access-count threshold for promoting vector hits into curated MEMORY.md.
# Higher than historical default (5) so process noise does not leak in.
DEFAULT_CONSOLIDATION_PROMOTE_ACCESS: int = 8

# Memory security scanner cooldown (seconds) between same-pattern detections
MEMORY_SCANNER_COOLDOWN: int = 300

# Frozen snapshot refresh interval (successful curated writes)
MEMORY_SNAPSHOT_REFRESH_INTERVAL: int = 1

# Context fencing tags
MEMORY_CONTEXT_TAG_OPEN: str = "<memory-context>"
MEMORY_CONTEXT_TAG_CLOSE: str = "</memory-context>"

# Maximum number of MEMORY.md backups to retain from dream() runs.
# Older backups are pruned to keep the workspace from growing without bound.
DREAM_BACKUP_KEEP: int = 5

# Soft per-entry cap (chars) used by summary_memory() and dream() when
# staging entries, to keep a single bloated entry from monopolizing the
# entire MemoryStore budget.
SINGLE_ENTRY_SOFT_LIMIT: int = 1500

# Whether automatic conversation summaries may write into curated MEMORY.md.
# Default False: summaries go to daily logs + vector index; only explicit
# memory_save / dream promotion / high-confidence encoder writes land in
# MEMORY.md.
MEMORY_AUTO_SUMMARY_TO_CURATED: bool = False

# Agent idle timeout in minutes. When no inbound message is received for
# any session within this window, a timeout notification is sent back and
# the session's resources (active tasks, locks, scrubber state) are
# cleaned up. Set to 0 to disable idle timeout.
AGENT_IDLE_TIMEOUT_MINUTES: int = 30


def is_main_memory_session(channel: str | None) -> bool:
    """Return True when curated MEMORY.md may be always-on injected.

    Main/local surfaces (cli/web/api/test/local) are trusted private sessions.
    Explicit shared/messaging channels are rejected. Empty/None channels default
    to main-session behaviour for local tooling compatibility. Any other
    unrecognized channel name fails closed (no curated always-on / search).
    """
    if not channel:
        return True
    name = str(channel).strip().lower()
    if not name:
        return True
    if name in SHARED_MEMORY_CHANNELS:
        return False
    if name in MAIN_MEMORY_CHANNELS:
        return True
    # Unrecognized names are treated as shared/untrusted.
    return False
