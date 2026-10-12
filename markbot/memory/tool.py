"""Memory tool — persistent curated memory (Hermes-aligned).

Two stores:
- MEMORY.md: agent notes (environment facts, project conventions, lessons)
- PROFILE.md: what the agent knows about the user

On-disk format is a flat entry list joined by ``§``:

    User prefers dark mode
    §
    Project uses postgres + redis

Frozen snapshot policy:
- System prompt injects a snapshot frozen at load / session start
- Tool writes update live entries + disk immediately
- Snapshot does NOT auto-refresh mid-session (protects prefix cache)
- Tool responses always reflect live state
- Explicit refresh_snapshot() is for session boundaries / dream

Security: MemorySecurityScanner before write.
Context fencing: <memory-context> tags.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from markbot.utils.constants import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    MAX_MEMORY_ENTRIES,
    MAX_MEMORY_MD_CHARS,
    MAX_USER_ENTRIES,
    MEMORY_FILENAME,
    MEMORY_SNAPSHOT_REFRESH_INTERVAL,
    USER_FILENAME,
)

from .fencing import MEMORY_TRUST_USER, fence_context
from .scanner import MemorySecurityScanner

fcntl = None
msvcrt = None
try:
    import fcntl as _fcntl  # noqa: E402
    fcntl = _fcntl
except ImportError:
    try:
        import msvcrt as _msvcrt  # noqa: E402
        msvcrt = _msvcrt
    except ImportError:
        pass


# Hermes canonical delimiter.
ENTRY_DELIMITER = "\n§\n"


class MemoryStore:
    """Bounded curated memory with file persistence.

    Parallel state:
      - _system_prompt_snapshot: frozen at load, used for system prompt
      - memory_entries / user_entries: live state, mutated by tools, on disk
    """

    def __init__(
        self,
        working_dir: str | Path,
        memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
        user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
        on_write: Optional[Callable[[str, str, str], None]] = None,
    ):
        self._working_dir = Path(working_dir)
        self._memory_path = self._working_dir / MEMORY_FILENAME
        self._user_path = self._working_dir / USER_FILENAME

        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit

        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self._system_prompt_snapshot: str = ""

        self._scanner = MemorySecurityScanner()
        self._lock_path = self._working_dir / ".memory.lock"
        self._state_lock = threading.RLock()
        self._on_write = on_write
        self._writes_since_snapshot = 0
        # 0 = never auto-refresh mid-session (Hermes frozen mode).
        self._snapshot_refresh_interval = max(0, int(MEMORY_SNAPSHOT_REFRESH_INTERVAL))

        self._load_all()

    @property
    def system_prompt_snapshot(self) -> str:
        return self._system_prompt_snapshot

    def _notify_write(self, action: str, target: str, content: str) -> None:
        if self._on_write:
            try:
                self._on_write(action, target, content)
            except Exception:
                pass

    def refresh_snapshot(self) -> None:
        """Refresh frozen snapshot from live entries (session boundary / dream)."""
        with self._state_lock:
            self._build_snapshot()

    def _entry_total_chars(self, entries: List[str]) -> int:
        if not entries:
            return 0
        return sum(len(e) for e in entries) + len(ENTRY_DELIMITER) * max(len(entries) - 1, 0)

    def _find_matches(self, entries: List[str], old_text: str) -> List[int]:
        return [i for i, entry in enumerate(entries) if old_text in entry]

    def add(
        self, target: str, content: str, *, supersedes: str | None = None
    ) -> Dict[str, Any]:
        """Add an entry, optionally declaring that it replaces an earlier one.

        ``supersedes`` records a supersede edge. Automatic contradiction
        detection is deliberately *not* attempted here: guessing that
        "lives in Berlin" and "lives in Paris" describe the same fact would
        risk silently deleting a correct user statement. Instead the caller —
        which can see the conversation — declares the replacement, and
        retrieval then prefers the newer entry.
        """
        error = self._scanner.scan(content)
        if error:
            return {"success": False, "message": error, "entries": []}

        content = self._scanner.sanitize(content).strip()
        if not content:
            return {"success": False, "message": "Empty content.", "entries": []}

        with self._state_lock:
            self._reload_target_locked(target)

            if target == "memory":
                entries = self.memory_entries
                char_limit = self.memory_char_limit
            else:
                entries = self.user_entries
                char_limit = self.user_char_limit

            if content in entries:
                return {
                    "success": True,
                    "message": "Entry already exists (no-op).",
                    "entries": list(entries),
                }

            entry_cap = MAX_MEMORY_ENTRIES if target == "memory" else MAX_USER_ENTRIES
            if len(entries) >= entry_cap:
                return {
                    "success": False,
                    "message": (
                        f"Entry count limit reached ({entry_cap}). "
                        "Remove or replace existing entries first."
                    ),
                    "entries": list(entries),
                }

            current_total = self._entry_total_chars(entries)
            extra_delim = len(ENTRY_DELIMITER) if entries else 0
            if current_total + extra_delim + len(content) > char_limit:
                return {
                    "success": False,
                    "message": (
                        f"Character limit reached ({char_limit}). "
                        "Remove or replace existing entries first. "
                        f"Current usage: {current_total}/{char_limit}."
                    ),
                    "entries": list(entries),
                }

            entries.append(content)
            self._persist(target)
            self._maybe_refresh_snapshot_after_write()
            self.record_entry_provenance(target, content, supersedes=supersedes)
        self._notify_write("add", target, content)
        logger.info("Added entry to {}: {}...", target, content[:60])
        return {"success": True, "message": "Entry added.", "entries": list(entries)}

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        error = self._scanner.scan(new_content)
        if error:
            return {"success": False, "message": error, "entries": []}

        new_content = self._scanner.sanitize(new_content).strip()
        if not new_content:
            return {"success": False, "message": "Empty content.", "entries": []}
        if not old_text:
            return {"success": False, "message": "old_text is required.", "entries": []}

        with self._state_lock:
            self._reload_target_locked(target)
            if target == "memory":
                entries = self.memory_entries
                char_limit = self.memory_char_limit
            else:
                entries = self.user_entries
                char_limit = self.user_char_limit

            matches = self._find_matches(entries, old_text)
            if not matches:
                return {
                    "success": False,
                    "message": f"No entry containing '{old_text[:40]}' found.",
                    "entries": list(entries),
                }
            if len(matches) > 1:
                previews = [entries[i][:60] for i in matches[:5]]
                return {
                    "success": False,
                    "message": (
                        f"Ambiguous match: {len(matches)} entries contain "
                        f"'{old_text[:40]}'. Use a more specific old_text. "
                        f"Matches: {previews}"
                    ),
                    "entries": list(entries),
                }

            idx = matches[0]
            proposed = list(entries)
            proposed[idx] = new_content
            if self._entry_total_chars(proposed) > char_limit:
                return {
                    "success": False,
                    "message": (
                        f"Character limit reached ({char_limit}). "
                        "Shorten the new content or remove other entries first."
                    ),
                    "entries": list(entries),
                }

            replaced_text = entries[idx]
            entries[idx] = new_content
            self._persist(target)
            self._maybe_refresh_snapshot_after_write()
            # A replace is an explicit supersede: the new text is the current
            # truth and the old one is retired. Recording the edge lets
            # retrieval prefer the replacement and explain the correction,
            # instead of both facts surfacing with equal confidence.
            self.record_entry_provenance(
                target, new_content, supersedes=replaced_text
            )
            self._notify_write("replace", target, new_content)
            logger.info("Replaced entry in {}: {}...", target, old_text[:40])
            return {
                "success": True,
                "message": "Entry replaced.",
                "entries": list(entries),
            }

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        if not old_text:
            return {"success": False, "message": "old_text is required.", "entries": []}

        with self._state_lock:
            self._reload_target_locked(target)
            if target == "memory":
                entries = self.memory_entries
            else:
                entries = self.user_entries

            matches = self._find_matches(entries, old_text)
            if not matches:
                return {
                    "success": False,
                    "message": f"No entry containing '{old_text[:40]}' found.",
                    "entries": list(entries),
                }
            if len(matches) > 1:
                previews = [entries[i][:60] for i in matches[:5]]
                return {
                    "success": False,
                    "message": (
                        f"Ambiguous match: {len(matches)} entries contain "
                        f"'{old_text[:40]}'. Use a more specific old_text. "
                        f"Matches: {previews}"
                    ),
                    "entries": list(entries),
                }

            removed = entries.pop(matches[0])
            self._persist(target)
            self._maybe_refresh_snapshot_after_write()
            # Drop the retired entry's provenance record so the sidecar cannot
            # accumulate orphans or resurrect a deleted fact via a stale edge.
            self._prune_meta_for_removed(removed)
            self._notify_write("remove", target, removed)
            logger.info("Removed entry from {}: {}...", target, removed[:40])
            return {
                "success": True,
                "message": "Entry removed.",
                "entries": list(entries),
            }

    def read(self, target: str) -> Dict[str, Any]:
        with self._state_lock:
            entries = self.memory_entries if target == "memory" else self.user_entries
            return {
                "success": True,
                "message": f"{len(entries)} entries.",
                "entries": list(entries),
            }

    def get_memory_context(self, query: str | None = None) -> str:
        with self._state_lock:
            if not self._system_prompt_snapshot:
                self._build_snapshot()
            if not self._system_prompt_snapshot:
                return ""
            return fence_context(
            self._system_prompt_snapshot, system_note=True,
            trust=MEMORY_TRUST_USER,
        )

    def replace_entries(self, target: str, new_entries: List[str]) -> None:
        with self._state_lock:
            cleaned: List[str] = []
            seen: set[str] = set()
            for raw in new_entries:
                entry = (raw or "").strip()
                if not entry or entry in seen:
                    continue
                seen.add(entry)
                cleaned.append(entry)
            if target == "memory":
                self.memory_entries = cleaned
            else:
                self.user_entries = cleaned
            self._persist(target)
            self._build_snapshot()

    def evict_oldest_matching(self, target: str, marker: str, needed_chars: int) -> int:
        with self._state_lock:
            entries = self.memory_entries if target == "memory" else self.user_entries
            freed = 0
            evicted = 0
            i = 0
            while i < len(entries) and freed < needed_chars:
                if marker in entries[i]:
                    freed += len(entries[i]) + len(ENTRY_DELIMITER)
                    entries.pop(i)
                    evicted += 1
                else:
                    i += 1
            if evicted:
                self._persist(target)
                self._build_snapshot()
                logger.info(
                    "Evicted {} auto-summary entries (freed ~{} chars)",
                    evicted,
                    freed,
                )
            return evicted

    def _reload_target_locked(self, target: str) -> None:
        if target == "memory":
            self.memory_entries = self._load_entries(self._memory_path)
        else:
            self.user_entries = self._load_entries(self._user_path)

    def _load_all(self) -> None:
        self.memory_entries = self._load_entries(self._memory_path)
        self.user_entries = self._load_entries(self._user_path)
        self._build_snapshot()

    @classmethod
    def parse_entries_text(cls, text: str) -> List[str]:
        """Parse MEMORY/PROFILE as Hermes ``§`` entry list only."""
        if not text or not text.strip():
            return []
        parts = text.split(ENTRY_DELIMITER)
        # Also accept a lone § on its own line (defensive for hand-edited files).
        if len(parts) == 1 and "\n§\n" not in text and "\n§" in text:
            parts = [p for chunk in text.split("\n§") for p in [chunk]]
        entries: List[str] = []
        seen: set[str] = set()
        for raw in parts:
            entry = raw.strip().strip("§").strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            entries.append(entry)
        return entries

    def _load_entries(self, path: Path) -> List[str]:
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Failed to read {}: {}", path, e)
            return []
        return self.parse_entries_text(text)

    def _maybe_refresh_snapshot_after_write(self) -> None:
        if self._snapshot_refresh_interval <= 0:
            return
        self._writes_since_snapshot += 1
        if self._writes_since_snapshot >= self._snapshot_refresh_interval:
            self._build_snapshot()
            self._writes_since_snapshot = 0

    def format_memory_prompt_block(
        self,
        *,
        include_memory: bool = True,
        include_user: bool = True,
        max_chars: int | None = None,
    ) -> str:
        parts: List[str] = []
        # Superseded entries are dropped from the injected block: the
        # replacement is present, and surfacing both with equal weight is how
        # a corrected fact ends up being acted on alongside the wrong one.
        superseded = set(self.superseded_entries())
        user_visible = [e for e in self.user_entries if e not in superseded]
        mem_visible = [e for e in self.memory_entries if e not in superseded]
        if include_user and user_visible:
            user_chars = self._entry_total_chars(self.user_entries)
            user_limit = self.user_char_limit
            pct = min(100, int(user_chars * 100 / user_limit)) if user_limit else 0
            # Age markers make staleness visible: without them a fact recorded
            # two years ago and one recorded this morning are injected
            # identically. Entries with no recorded timestamp pass through
            # unchanged, so pre-existing stores behave as before.
            user_body = self.format_entries_with_age(user_visible)
            parts.append(
                "══════════════════════════════════════════════\n"
                f"USER PROFILE [~{pct}% — {user_chars}/{user_limit} chars]\n"
                "══════════════════════════════════════════════\n"
                f"{user_body}"
            )
        if include_memory and mem_visible:
            mem_chars = self._entry_total_chars(self.memory_entries)
            mem_limit = self.memory_char_limit
            pct = min(100, int(mem_chars * 100 / mem_limit)) if mem_limit else 0
            mem_body = self.format_entries_with_age(mem_visible)
            parts.append(
                "══════════════════════════════════════════════\n"
                f"MEMORY (your personal notes) [~{pct}% — {mem_chars}/{mem_limit} chars]\n"
                "══════════════════════════════════════════════\n"
                f"{mem_body}"
            )
        text = "\n\n".join(parts)
        if max_chars is not None and max_chars > 0 and len(text) > max_chars:
            keep = max(max_chars - 80, 0)
            text = text[:keep].rstrip() + "\n\n...[memory truncated to budget]..."
        return text

    def _build_snapshot(self) -> None:
        self._system_prompt_snapshot = self.format_memory_prompt_block(
            include_memory=True,
            include_user=True,
            max_chars=MAX_MEMORY_MD_CHARS,
        )
        self._writes_since_snapshot = 0

    @contextmanager
    def _file_lock(self):
        lock_fd = None
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = open(self._lock_path, "w")
            if fcntl:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            elif msvcrt:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if lock_fd:
                try:
                    if fcntl:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    elif msvcrt:
                        try:
                            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    lock_fd.close()
                except Exception:
                    pass

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix=path.stem + "_",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _persist(self, target: str) -> None:
        if target == "memory":
            path = self._memory_path
            entries = self.memory_entries
        else:
            path = self._user_path
            entries = self.user_entries

        content = (ENTRY_DELIMITER.join(entries) + "\n") if entries else ""
        try:
            with self._file_lock():
                self._atomic_write_text(path, content)
        except Exception as e:
            logger.warning("Failed to write {}: {}", path, e)

    # ------------------------------------------------------------------
    # Entry provenance: recorded-at + supersede graph
    # ------------------------------------------------------------------
    #
    # Entries are stored as bare strings, which left the store unable to
    # answer two questions the retrieval layer needs:
    #
    #   1. *When* was this stated? Without a timestamp the agent has no way
    #      to notice it is acting on a stale fact — "the user lives in
    #      Berlin" and "the user lives in Paris" coexist forever and both
    #      are presented with identical confidence.
    #   2. *Does this replace* an earlier entry? Nothing recorded
    #      supersession, so a corrected fact did not displace the wrong one.
    #
    # The metadata lives in a sidecar JSON file rather than inside the entry
    # text. That is deliberate: entries are budgeted at ~2200 characters and
    # are injected verbatim into the system prompt, so appending a timestamp
    # to each one would consume the budget and change the exact-match
    # dedup. The sidecar can also be lost without corrupting the store.

    _META_FILENAME = ".memory_meta.json"
    #: Bounded so a long-lived store cannot grow the sidecar without limit.
    _META_MAX_RECORDS = 500

    @staticmethod
    def _entry_key(content: str) -> str:
        """Stable identity for an entry: a short digest of its text."""
        return hashlib.sha1(content.strip().encode("utf-8")).hexdigest()[:16]

    def _meta_path(self) -> Path:
        return self._working_dir / self._META_FILENAME

    def _load_meta(self) -> dict:
        path = self._meta_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.debug("Failed to read memory metadata: {}", exc)
        return {}

    def _save_meta(self, meta: dict) -> None:
        # Drop records for entries that no longer exist, then bound growth.
        live = {self._entry_key(e) for e in (*self.memory_entries, *self.user_entries)}
        pruned = {k: v for k, v in meta.items() if k in live}
        if len(pruned) > self._META_MAX_RECORDS:
            ordered = sorted(
                pruned.items(), key=lambda kv: kv[1].get("at", 0), reverse=True
            )
            pruned = dict(ordered[: self._META_MAX_RECORDS])
        try:
            with self._file_lock():
                self._atomic_write_text(
                    self._meta_path(), json.dumps(pruned, ensure_ascii=False, indent=2)
                )
        except Exception as exc:
            logger.warning("Failed to write memory metadata: {}", exc)

    def record_entry_provenance(
        self, target: str, content: str, *, supersedes: str | None = None
    ) -> None:
        """Record when *content* was stated and what it replaces.

        Args:
            target: ``"memory"`` or ``"user"``.
            content: The entry just written.
            supersedes: Optional text of an earlier entry this one replaces.
                Recorded as an edge so retrieval can prefer the newer value
                and report the correction instead of surfacing both.
        """
        with self._state_lock:
            meta = self._load_meta()
            now = time.time()
            key = self._entry_key(content)
            record = meta.setdefault(key, {})
            record["at"] = now
            record["target"] = target
            if supersedes:
                record["supersedes"] = self._entry_key(supersedes)
                record["supersedes_text"] = supersedes[:200]
            self._save_meta(meta)

    def entry_recorded_at(self, content: str) -> float | None:
        """Return the epoch timestamp for *content*, or None if unknown."""
        return self._load_meta().get(self._entry_key(content), {}).get("at")

    def _prune_meta_for_removed(self, *contents: str) -> None:
        """Remove provenance records for entries that no longer exist.

        Also drops any edge that pointed *at* a removed entry, so a fact
        deleted and later re-added is not silently filtered out by a stale
        supersede edge.
        """
        with self._state_lock:
            meta = self._load_meta()
            if not meta:
                return
            dead = {self._entry_key(c) for c in contents}
            changed = False
            for key in list(meta):
                if key in dead or meta[key].get("supersedes") in dead:
                    del meta[key]
                    changed = True
            if changed:
                self._save_meta(meta)

    def superseded_entries(self) -> dict[str, str]:
        """Map ``retired entry text -> replacing entry text``.

        Both edge shapes are covered:

        * ``add(supersedes=…)`` — the old entry is *still present* and must be
          filtered out of injection, because the replacement supersedes it.
        * ``replace(…)`` — the old text no longer exists as an entry. The edge
          is kept only so the correction remains explainable ("this replaced
          that"), and resolves via the recorded ``supersedes_text`` snapshot.

        An edge whose *new* end is gone is dropped, so a stale edge cannot
        hide a fact that was deleted and later re-added.
        """
        meta = self._load_meta()
        out: dict[str, str] = {}
        by_key = {self._entry_key(e): e for e in self._memory_entries_and_users()}
        for key, record in meta.items():
            old_key = record.get("supersedes")
            if not old_key:
                continue
            new_text = by_key.get(key)
            if new_text is None:
                # The replacement itself was removed; the edge is meaningless.
                continue
            old_text = by_key.get(old_key) or record.get("supersedes_text")
            if old_text:
                out[old_text] = new_text
        return out

    def _memory_entries_and_users(self) -> List[str]:
        return [*self.memory_entries, *self.user_entries]

    def format_entries_with_age(self, entries: List[str], *, now: float | None = None) -> str:
        """Render entries with a relative age marker.

        Staleness is otherwise invisible to the model: a fact recorded two
        years ago and one recorded this morning are injected identically.
        The marker is deliberately terse to protect the character budget, and
        entries with no recorded timestamp are passed through unchanged so
        pre-existing stores behave exactly as before.
        """
        meta = self._load_meta()
        ref = now if now is not None else time.time()
        lines: list[str] = []
        for entry in entries:
            at = meta.get(self._entry_key(entry), {}).get("at")
            if not at:
                lines.append(entry)
                continue
            days = max(0, int((ref - at) // 86400))
            if days <= 0:
                marker = "(today)"
            elif days == 1:
                marker = "(1 day ago)"
            elif days < 30:
                marker = f"({days} days ago)"
            elif days < 365:
                marker = f"({days // 30} mo ago)"
            else:
                marker = f"({days // 365} y ago)"
            lines.append(f"{entry} {marker}")
        return ENTRY_DELIMITER.join(lines)


__all__ = [
    "MemoryStore",
    "ENTRY_DELIMITER",
    "MEMORY_FILENAME",
    "USER_FILENAME",
]
