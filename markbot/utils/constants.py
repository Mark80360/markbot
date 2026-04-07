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

    # CI/CD
    ".circleci", ".gitlab", ".github/workflows" if False else "",  # Keep .github for configs
}).union({".github", ".gitlab"}) - {""}

# Binary file extensions to skip in content search
BINARY_EXTENSIONS = frozenset({
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".tiff", ".tif", ".psd", ".raw", ".heic", ".heif", ".avif",

    # Audio
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a", ".opus",
    ".aiff", ".pcm", ".ape",

    # Video
    ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mkv", ".m4v",
    ".3gp", ".ts", ".mts", ".vob",

    # Archives
    ".zip", ".tar", ".gz", ".rar", ".7z", ".bz2", ".xz", ".lzma",
    ".cab", ".iso", ".dmg", ".deb", ".rpm",

    # Documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".rtf",

    # Compiled code
    ".pyc", ".pyo", ".pyd", ".class", ".o", ".obj", ".so", ".dll",
    ".dylib", ".lib", ".a", ".exe", ".msi", ".app", ".out",
    ".jar", ".war", ".ear", ".node",

    # Fonts
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pfb",

    # Databases
    ".sqlite", ".db", ".sqlite3", ".parquet", ".h5", ".hdf5",
    ".accdb", ".mdb",

    # Other binary
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
    r"\bchown\s+.*\s+/\b",
    r">\s+/etc/",
    r"mv\s+.*\s+/\b",
    r"cp\s+.*\s+/\b",
]
