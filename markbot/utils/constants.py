"""Shared constants for all agent tools.

This module provides unified constants to ensure consistency across
search, filesystem, explore, and other tools.
"""

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

BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "MEMORY.md", "PROFILE.md", "ARCHITECTURE.md"]

_TEMPLATE_DIR_NAMES: frozenset[str] = frozenset({
    "AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "MEMORY.md",
    "PROFILE.md", "ARCHITECTURE.md", "HEARTBEAT.md", "BOOTSTRAP.md",
})


def check_template_sync(templates_dir: "Path | None" = None) -> list[str]:
    """Cross-check BOOTSTRAP_FILES constant against the templates directory.

    Returns a list of warning messages for any discrepancies found:
    - Template files in the directory that are not in BOOTSTRAP_FILES
    - Entries in BOOTSTRAP_FILES that have no corresponding template file

    This function is called at startup to catch drift between the
    hardcoded constant and the actual template files on disk.
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

    known = set(BOOTSTRAP_FILES)
    in_template_dir = disk_files & _TEMPLATE_DIR_NAMES

    missing_from_constant = in_template_dir - known
    if missing_from_constant:
        warnings.append(
            f"Template file(s) on disk but not in BOOTSTRAP_FILES: "
            f"{sorted(missing_from_constant)}. "
            f"Consider adding them if they should be loaded at startup."
        )

    missing_from_disk = known - disk_files
    if missing_from_disk:
        warnings.append(
            f"BOOTSTRAP_FILES entry(s) with no template on disk: "
            f"{sorted(missing_from_disk)}. "
            f"Consider removing them or creating the template files."
        )

    return warnings

# Context builder cache TTL (seconds)
CONTEXT_CACHE_TTL: float = 300.0

# Guidance injection TTL per session (seconds)
GUIDANCE_INJECTION_TTL: float = 3600.0

# Maximum characters for git status in system context
MAX_GIT_STATUS_CHARS: int = 2000

# Maximum characters for compressed summary before truncation
MAX_COMPRESSED_SUMMARY_CHARS: int = 200_000

# Maximum characters for MEMORY.md before section-based truncation
MAX_MEMORY_MD_CHARS: int = 8_000

# Maximum characters for daily log search results
MAX_DAILY_LOG_RESULT_CHARS: int = 2000

# Default context window tokens
DEFAULT_CONTEXT_WINDOW_TOKENS: int = 65_536

# Default compaction threshold ratio
DEFAULT_COMPACTION_THRESHOLD: float = 0.75

# -- Memory system constants ------------------------------------------------

# Default character limits for MemoryStore entries
DEFAULT_MEMORY_CHAR_LIMIT: int = 4000
DEFAULT_USER_CHAR_LIMIT: int = 2000

# Maximum prefetch results per turn
MAX_PREFETCH_RESULTS: int = 3

# Minimum relevance score for prefetch recall
MIN_PREFETCH_SCORE: float = 0.15

# Maximum entries in MemoryStore before cleanup hint
MAX_MEMORY_ENTRIES: int = 100
MAX_USER_ENTRIES: int = 50

# Memory security scanner cooldown (seconds) between same-pattern detections
MEMORY_SCANNER_COOLDOWN: int = 300

# Frozen snapshot refresh interval (turns)
MEMORY_SNAPSHOT_REFRESH_INTERVAL: int = 10

# Context fencing tags
MEMORY_CONTEXT_TAG_OPEN: str = "<memory-context>"
MEMORY_CONTEXT_TAG_CLOSE: str = "</memory-context>"
