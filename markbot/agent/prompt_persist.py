"""Cross-session persistence for the static base section of the
system prompt.

## Why

Server-side KV prefix caches (DeepSeek, Anthropic, OpenAI Codex)
match the **leading** bytes of every request.  The base section of
the MarkBot system prompt — the mode prompt, the project context,
the skills index, the cache discipline — is byte-stable across
sessions for the same workspace.  By caching that section on disk
and reusing it when the SHA-256 matches, we can:

1. **Skip base-section assembly on session start.**  Re-rendering
   the system prompt is several hundred milliseconds of work that
   we no longer have to do.
2. **Hand the server byte-identical bytes.**  When the base
   section is the same across sessions, the server can reuse its
   cached KV states for the entire base section, giving roughly
   a 10× cost reduction on the cached tokens.

## Cache layout

::

    ~/.markbot/prompt_cache/
      <system_hash>.bin    -- the base section text
      <system_hash>.meta   -- JSON: { workspace, mtime, cached_at }

The key is the SHA-256 of the base section text, computed by
:func:`markbot.agent.prefix_cache.PrefixFingerprint.compute`.
The metadata file records the workspace path and mtime so that
workspace changes invalidate the cache even if the base section
hash collides (extremely unlikely with SHA-256, but cheap to
guard against).

## Environment override

The cache directory can be overridden by setting
``MARKBOT_PROMPT_CACHE_DIR``.  Tests use this to redirect the cache
to a temp directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from loguru import logger


CACHE_DIR_ENV = "MARKBOT_PROMPT_CACHE_DIR"
META_FILENAME_SUFFIX = ".meta"
BIN_FILENAME_SUFFIX = ".bin"
DEFAULT_DIR_NAME = ".markbot"
DEFAULT_SUBDIR = "prompt_cache"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

@dataclass
class CacheMetadata:
    """Metadata stored alongside a cached base section."""

    workspace: str
    workspace_mtime_secs: int
    cached_at_secs: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "workspace": self.workspace,
                "workspace_mtime_secs": self.workspace_mtime_secs,
                "cached_at_secs": self.cached_at_secs,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, blob: str) -> "CacheMetadata":
        d = json.loads(blob)
        return cls(
            workspace=str(d["workspace"]),
            workspace_mtime_secs=int(d["workspace_mtime_secs"]),
            cached_at_secs=int(d["cached_at_secs"]),
        )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def cache_dir() -> Optional[Path]:
    """Return the directory where prompt caches are stored.

    Honours the ``MARKBOT_PROMPT_CACHE_DIR`` env override so tests
    never touch the real ``~/.markbot`` cache.
    """
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        base = Path(override)
    else:
        home = Path.home()
        if not home.exists():
            return None
        base = home / DEFAULT_DIR_NAME / DEFAULT_SUBDIR
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create prompt cache dir {}: {}", base, exc)
        return None
    return base


def _dir_mtime_secs(path: Path) -> int:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0
    return int(mtime)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _bin_path(cache_root: Path, base_hash: str) -> Path:
    return cache_root / f"{base_hash}{BIN_FILENAME_SUFFIX}"


def _meta_path(cache_root: Path, base_hash: str) -> Path:
    return cache_root / f"{base_hash}{META_FILENAME_SUFFIX}"


def load_cached_base_section(
    base_hash: str,
    workspace: Path,
    *,
    cache_root: Optional[Path] = None,
) -> Optional[str]:
    """Try to load a cached base section.

    Returns the cached text if the entry exists and is valid, or
    ``None`` otherwise (missing, stale, corrupt, workspace-mismatch).
    """
    root = cache_root if cache_root is not None else cache_dir()
    if root is None:
        return None

    bin_path = _bin_path(root, base_hash)
    meta_path = _meta_path(root, base_hash)

    if not bin_path.exists() or not meta_path.exists():
        return None

    try:
        meta = CacheMetadata.from_json(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Prompt cache metadata unreadable: {}", exc)
        return None

    # Workspace must match exactly.
    try:
        ws_resolved = str(workspace.resolve())
    except OSError:
        ws_resolved = str(workspace)
    if meta.workspace != ws_resolved:
        logger.debug("Prompt cache workspace mismatch: {}", meta.workspace)
        return None

    # Workspace mtime must match (cheap guard against hash collisions).
    current_mtime = _dir_mtime_secs(workspace)
    if current_mtime != meta.workspace_mtime_secs:
        logger.debug(
            "Prompt cache stale: workspace mtime changed ({} -> {})",
            meta.workspace_mtime_secs,
            current_mtime,
        )
        return None

    try:
        return bin_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Prompt cache bin unreadable: {}", exc)
        return None


def save_cached_base_section(
    base_hash: str,
    workspace: Path,
    text: str,
    *,
    cache_root: Optional[Path] = None,
) -> bool:
    """Persist ``text`` under ``base_hash`` for the given workspace.

    Returns True iff both the ``.bin`` and ``.meta`` files were
    written successfully.
    """
    root = cache_root if cache_root is not None else cache_dir()
    if root is None:
        return False
    # Ensure the cache root exists; on first use this is a no-op
    # for the default ~/.markbot location but matters for tests
    # and for callers that pass a custom cache_root.
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create prompt cache root {}: {}", root, exc)
        return False

    bin_path = _bin_path(root, base_hash)
    meta_path = _meta_path(root, base_hash)

    try:
        ws_resolved = str(workspace.resolve())
    except OSError:
        ws_resolved = str(workspace)

    meta = CacheMetadata(
        workspace=ws_resolved,
        workspace_mtime_secs=_dir_mtime_secs(workspace),
        cached_at_secs=int(time.time()),
    )

    try:
        # Write atomically: write to a temp file, then rename.
        tmp_bin = bin_path.with_suffix(bin_path.suffix + ".tmp")
        tmp_meta = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp_bin.write_text(text, encoding="utf-8")
        tmp_meta.write_text(meta.to_json(), encoding="utf-8")
        os.replace(tmp_bin, bin_path)
        os.replace(tmp_meta, meta_path)
    except OSError as exc:
        logger.warning("Failed to write prompt cache for {}: {}", base_hash[:12], exc)
        return False
    return True


def clear_cached_base_section(
    base_hash: str,
    *,
    cache_root: Optional[Path] = None,
) -> None:
    """Remove a single cache entry, if present."""
    root = cache_root if cache_root is not None else cache_dir()
    if root is None:
        return
    for p in (_bin_path(root, base_hash), _meta_path(root, base_hash)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def cache_stats(*, cache_root: Optional[Path] = None) -> dict[str, int]:
    """Return ``{entries, total_bytes}`` for the cache directory.

    Used by ``/status`` to surface cache effectiveness.
    """
    root = cache_root if cache_root is not None else cache_dir()
    if root is None or not root.exists():
        return {"entries": 0, "total_bytes": 0}
    entries = 0
    total = 0
    for p in root.iterdir():
        if p.suffix == BIN_FILENAME_SUFFIX:
            entries += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return {"entries": entries, "total_bytes": total}


# ---------------------------------------------------------------------------
# Convenience: hash a base section
# ---------------------------------------------------------------------------

def hash_base_section(text: str) -> str:
    """SHA-256 hex digest of the base section text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "CacheMetadata",
    "cache_dir",
    "load_cached_base_section",
    "save_cached_base_section",
    "clear_cached_base_section",
    "cache_stats",
    "hash_base_section",
    "CACHE_DIR_ENV",
]
