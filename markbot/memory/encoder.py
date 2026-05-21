"""Active memory encoder — detect patterns and proactively encode preferences.

Monitors conversation patterns to detect:
- Repeated user corrections (same preference stated 2+ times)
- Explicit preference declarations ("always do X", "I prefer Y")
- Repeated task patterns (same workflow done 3+ times)

When a pattern is detected, the encoder:
1. Checks if the preference already exists in PROFILE.md or MEMORY.md
2. If not, appends it as a structured entry via MemoryStore
3. Marks it as auto-detected so the user can review/edit

This makes the assistant progressively learn user preferences without
requiring explicit configuration.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from markbot.memory.tool import MemoryStore


@dataclass
class PreferenceEntry:
    content: str
    source: str = "auto_detected"
    detected_at: float = 0.0
    confidence: int = 1

    def to_line(self) -> str:
        return f"- {self.content}  [source: {self.source}, confidence: {self.confidence}]"


@dataclass
class PatternMatch:
    pattern_type: str
    content: str
    raw_text: str
    confidence: int = 1


_PREFERENCE_PATTERNS = [
    re.compile(
        r"(?:always|never|please\s+always|make\s+sure\s+to|I\s+prefer|"
        r"I\s+like|I\s+want|from\s+now\s+on|going\s+forward|"
        r"by\s+default|默认|以后|总是|不要|记得)\s+(.+?)(?:\.|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:don'?t|do\s+not|never)\s+(.+?)(?:\.|$)",
        re.IGNORECASE,
    ),
]

_CORRECTION_PATTERNS = [
    re.compile(
        r"(?:no[,.]?\s+I\s+meant|actually|I\s+said|that's\s+not\s+right|"
        r"不是|不对|我说的是|其实)\s+(.+?)(?:\.|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:use\s+|switch\s+to\s+|change\s+to\s+)(.+?)(?:\s+instead|\.|$)",
        re.IGNORECASE,
    ),
]

_DETECTION_COOLDOWN_SECONDS = 300


class MemoryEncoder:
    """Detect and persist user preferences from conversation patterns.

    Usage::

        encoder = MemoryEncoder(workspace, memory_store=store)
        matches = encoder.scan_message(user_text)
        if matches:
            encoder.encode_preferences(matches)
    """

    def __init__(
        self,
        workspace: Path,
        memory_store: Optional[MemoryStore] = None,
    ) -> None:
        self.workspace = workspace
        self._memory_store = memory_store
        self._profile_path = workspace / "PROFILE.md"
        self._memory_path = workspace / "MEMORY.md"
        self._detection_log_path = workspace / "memory" / "encoder_log.json"
        self._recent_detections: dict[str, float] = {}

    def set_memory_store(self, store: MemoryStore) -> None:
        self._memory_store = store

    def scan_message(self, text: str) -> list[PatternMatch]:
        if not text or not isinstance(text, str):
            return []

        matches: list[PatternMatch] = []

        for pattern in _PREFERENCE_PATTERNS:
            for m in pattern.finditer(text):
                content = m.group(1).strip()[:200]
                if content and len(content) > 5:
                    matches.append(PatternMatch(
                        pattern_type="preference",
                        content=content,
                        raw_text=m.group(0)[:200],
                    ))

        for pattern in _CORRECTION_PATTERNS:
            for m in pattern.finditer(text):
                content = m.group(1).strip()[:200]
                if content and len(content) > 5:
                    matches.append(PatternMatch(
                        pattern_type="correction",
                        content=content,
                        raw_text=m.group(0)[:200],
                    ))

        return matches

    def scan_messages(self, messages: list[dict]) -> list[PatternMatch]:
        all_matches: list[PatternMatch] = []
        for msg in messages[-20:]:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if isinstance(content, str):
                all_matches.extend(self.scan_message(content))

        return self._deduplicate(all_matches)

    def encode_preferences(self, matches: list[PatternMatch]) -> int:
        encoded = 0
        now = time.time()

        for match in matches:
            key = match.content.lower().strip()
            if key in self._recent_detections:
                if now - self._recent_detections[key] < _DETECTION_COOLDOWN_SECONDS:
                    continue

            existing = self._read_existing_preferences()
            already_exists = any(
                key in existing_entry.lower()
                for existing_entry in existing
            )

            if already_exists:
                self._increment_confidence(match.content)
                self._recent_detections[key] = now
                continue

            if match.pattern_type == "preference":
                if self._append_to_store("user", match):
                    encoded += 1
            elif match.pattern_type == "correction":
                if self._append_to_store("memory", match):
                    encoded += 1

            self._recent_detections[key] = now

        if encoded > 0:
            logger.info("Encoded {} new preferences", encoded)

        return encoded

    def _deduplicate(self, matches: list[PatternMatch]) -> list[PatternMatch]:
        seen: dict[str, PatternMatch] = {}
        for m in matches:
            key = m.content.lower().strip()
            if key in seen:
                seen[key].confidence += 1
            else:
                seen[key] = PatternMatch(
                    pattern_type=m.pattern_type,
                    content=m.content,
                    raw_text=m.raw_text,
                    confidence=1,
                )
        return sorted(seen.values(), key=lambda m: -m.confidence)

    def _read_existing_preferences(self) -> list[str]:
        entries: list[str] = []

        if self._memory_store:
            for target in ("user", "memory"):
                result = self._memory_store.read(target)
                for entry in result.get("entries", []):
                    entries.append(entry)
            return entries

        for path in (self._profile_path, self._memory_path):
            if path.exists():
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line.startswith("- ") or line.startswith("* "):
                            entries.append(line.lstrip("-* ").strip())
                except Exception:
                    pass
        return entries

    def _append_to_store(self, target: str, match: PatternMatch) -> bool:
        if self._memory_store:
            entry = PreferenceEntry(
                content=match.content,
                source="auto_detected",
                detected_at=time.time(),
                confidence=match.confidence,
            )
            result = self._memory_store.add(target, entry.to_line())
            if result.get("success", False):
                logger.info("Appended to {} via MemoryStore: {}", target, match.content[:60])
                return True
            logger.debug("MemoryStore.add failed for {}: {}", target, result.get("message", ""))
            return False

        return self._append_to_file(target, match)

    def _append_to_file(self, target: str, match: PatternMatch) -> bool:
        if target == "user":
            path = self._profile_path
            section_title = "User Preferences"
        else:
            path = self._memory_path
            section_title = "Auto-Detected Patterns"

        entry = PreferenceEntry(
            content=match.content,
            source="auto_detected",
            detected_at=time.time(),
            confidence=match.confidence,
        )

        try:
            if path.exists():
                content = path.read_text(encoding="utf-8")
            else:
                content = f"# {section_title}\n\n"

            lines = content.rstrip().splitlines()

            section_start = None
            for i, line in enumerate(lines):
                if line.strip().lower() == section_title.lower():
                    section_start = i
                    break

            if section_start is None:
                lines.append("")
                lines.append(f"## {section_title}")
                section_start = len(lines) - 1

            section_entries = 0
            insert_pos = len(lines)
            for i in range(section_start + 1, len(lines)):
                if lines[i].startswith("## "):
                    insert_pos = i
                    break
                if lines[i].strip().startswith("- ") or lines[i].strip().startswith("* "):
                    section_entries += 1

            lines.insert(insert_pos, entry.to_line())

            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            logger.info("Appended to {}: {}", path.name, match.content[:60])
            return True

        except Exception as e:
            logger.warning("Failed to append to {}: {}", path.name, e)
            return False

    def _increment_confidence(self, content: str) -> None:
        if self._memory_store:
            for target in ("user", "memory"):
                result = self._memory_store.read(target)
                for entry in result.get("entries", []):
                    if content.lower().strip() in entry.lower():
                        m = re.search(r"confidence:\s*(\d+)", entry)
                        if m:
                            old_conf = int(m.group(1))
                            new_conf = old_conf + 1
                            new_entry = entry.replace(
                                f"confidence: {old_conf}",
                                f"confidence: {new_conf}",
                            )
                            self._memory_store.replace(target, entry, new_entry)
                            return
            return

        for path in (self._profile_path, self._memory_path):
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                modified = False
                for i, line in enumerate(lines):
                    if content.lower().strip() in line.lower():
                        m = re.search(r"confidence:\s*(\d+)", line)
                        if m:
                            old_conf = int(m.group(1))
                            new_conf = old_conf + 1
                            lines[i] = line.replace(
                                f"confidence: {old_conf}",
                                f"confidence: {new_conf}",
                            )
                            modified = True
                            break
                if modified:
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    return
            except Exception:
                pass
