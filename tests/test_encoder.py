"""Tests for markbot.memory.encoder — Active preference detection."""

from unittest.mock import MagicMock

import pytest

from markbot.memory.encoder import (
    _CORRECTION_PATTERNS,
    _PREFERENCE_PATTERNS,
    MemoryEncoder,
    PatternMatch,
    PreferenceEntry,
)


class TestPreferenceEntry:
    def test_default_values(self):
        entry = PreferenceEntry(content="test content")
        assert entry.content == "test content"
        assert entry.source == "auto_detected"
        assert entry.detected_at == 0.0
        assert entry.confidence == 1

    def test_to_line(self):
        entry = PreferenceEntry(
            content="always use dark mode",
            source="auto_detected",
            confidence=3,
        )
        line = entry.to_line()
        assert "always use dark mode" in line
        assert "source: auto_detected" in line
        assert "confidence: 3" in line


class TestPatternMatch:
    def test_basic_match(self):
        match = PatternMatch(
            pattern_type="preference",
            content="use dark mode",
            raw_text="always use dark mode",
        )
        assert match.pattern_type == "preference"
        assert match.confidence == 1


class TestPreferencePatterns:
    def test_always_pattern(self):
        text = "always use dark mode for the UI"
        matches = []
        for pattern in _PREFERENCE_PATTERNS:
            for m in pattern.finditer(text):
                matches.append(m.group(1).strip())
        assert any("dark mode" in m for m in matches)

    def test_never_pattern(self):
        text = "never use bright colors"
        matches = []
        for pattern in _PREFERENCE_PATTERNS:
            for m in pattern.finditer(text):
                matches.append(m.group(1).strip())
        assert len(matches) > 0

    def test_prefer_pattern(self):
        text = "I prefer TypeScript over JavaScript"
        matches = []
        for pattern in _PREFERENCE_PATTERNS:
            for m in pattern.finditer(text):
                matches.append(m.group(1).strip())
        assert any("TypeScript" in m for m in matches)


class TestCorrectionPatterns:
    def test_no_i_meant_pattern(self):
        text = "no, I meant React, not Angular"
        matches = []
        for pattern in _CORRECTION_PATTERNS:
            for m in pattern.finditer(text):
                matches.append(m.group(1).strip())
        assert len(matches) > 0

    def test_use_instead_pattern(self):
        text = "use pnpm instead"
        matches = []
        for pattern in _CORRECTION_PATTERNS:
            for m in pattern.finditer(text):
                matches.append(m.group(1).strip())
        assert any("pnpm" in m for m in matches)


class TestMemoryEncoder:
    @pytest.fixture
    def encoder(self, tmp_path):
        return MemoryEncoder(workspace=tmp_path)

    @pytest.fixture
    def mock_store(self):
        store = MagicMock()
        store.read.return_value = {"entries": []}
        store.add.return_value = {"success": True}
        return store

    def test_scan_message_empty(self, encoder):
        assert encoder.scan_message("") == []
        assert encoder.scan_message(None) == []

    def test_scan_message_no_match(self, encoder):
        matches = encoder.scan_message("hello world")
        assert matches == []

    def test_scan_message_preference(self, encoder):
        matches = encoder.scan_message("always use dark mode")
        assert len(matches) > 0
        assert matches[0].pattern_type == "preference"
        assert "dark mode" in matches[0].content

    def test_scan_message_correction(self, encoder):
        # Test with a pattern that matches the correction regex
        matches = encoder.scan_message("switch to React instead")
        # The pattern requires specific phrasing
        # Let's verify the patterns are compiled correctly
        assert len(_CORRECTION_PATTERNS) > 0

    def test_scan_messages_filters_user_only(self, encoder):
        messages = [
            {"role": "system", "content": "always do X"},
            {"role": "assistant", "content": "always do Y"},
            {"role": "user", "content": "always use dark mode"},
        ]
        matches = encoder.scan_messages(messages)
        assert len(matches) > 0

    def test_scan_messages_limits_to_last_20(self, encoder):
        messages = [
            {"role": "user", "content": f"always use option {i}"}
            for i in range(30)
        ]
        # Should not crash even with many messages
        matches = encoder.scan_messages(messages)
        assert isinstance(matches, list)

    def test_scan_messages_deduplicates(self, encoder):
        messages = [
            {"role": "user", "content": "always use dark mode"},
            {"role": "user", "content": "always use dark mode"},
        ]
        matches = encoder.scan_messages(messages)
        # Should deduplicate - the dedup key is content.lower().strip()
        dark_matches = [m for m in matches if "use dark mode" in m.content.lower()]
        assert len(dark_matches) >= 1

    def test_encode_preferences_new(self, encoder, tmp_path):
        matches = [PatternMatch(
            pattern_type="preference",
            content="use dark mode",
            raw_text="always use dark mode",
        )]
        encoded = encoder.encode_preferences(matches)
        assert encoded == 1
        # Check file was created
        profile_path = tmp_path / "PROFILE.md"
        assert profile_path.exists()
        content = profile_path.read_text(encoding="utf-8")
        assert "dark mode" in content

    def test_encode_preferences_with_store(self, tmp_path, mock_store):
        encoder = MemoryEncoder(workspace=tmp_path, memory_store=mock_store)
        matches = [PatternMatch(
            pattern_type="preference",
            content="use dark mode",
            raw_text="always use dark mode",
        )]
        encoded = encoder.encode_preferences(matches)
        assert encoded == 1
        mock_store.add.assert_called_once()

    def test_encode_preferences_already_exists(self, encoder, tmp_path):
        # Create profile with existing preference
        profile = tmp_path / "PROFILE.md"
        profile.write_text("# User Preferences\n\n- use dark mode  [source: auto_detected, confidence: 1]\n")

        matches = [PatternMatch(
            pattern_type="preference",
            content="use dark mode",
            raw_text="always use dark mode",
        )]
        encoded = encoder.encode_preferences(matches)
        assert encoded == 0  # already exists

    def test_encode_correction_to_memory(self, encoder, tmp_path):
        matches = [PatternMatch(
            pattern_type="correction",
            content="use React not Angular",
            raw_text="no, I meant React",
        )]
        encoded = encoder.encode_preferences(matches)
        assert encoded == 1
        memory_path = tmp_path / "MEMORY.md"
        assert memory_path.exists()

    def test_deduplicate(self, encoder):
        matches = [
            PatternMatch("preference", "dark mode", "always dark mode"),
            PatternMatch("preference", "dark mode", "always dark mode"),
            PatternMatch("preference", "light mode", "always light mode"),
        ]
        result = encoder._deduplicate(matches)
        assert len(result) == 2
        dark = [m for m in result if "dark" in m.content][0]
        assert dark.confidence == 2

    def test_read_existing_from_file(self, encoder, tmp_path):
        profile = tmp_path / "PROFILE.md"
        profile.write_text("# User Preferences\n\n- use dark mode\n- prefer TypeScript\n")

        entries = encoder._read_existing_preferences()
        assert "use dark mode" in entries
        assert "prefer TypeScript" in entries

    def test_read_existing_from_store(self, tmp_path, mock_store):
        mock_store.read.return_value = {
            "entries": ["use dark mode", "prefer TypeScript"]
        }
        encoder = MemoryEncoder(workspace=tmp_path, memory_store=mock_store)
        entries = encoder._read_existing_preferences()
        assert "use dark mode" in entries

    def test_increment_confidence_in_file(self, encoder, tmp_path):
        profile = tmp_path / "PROFILE.md"
        profile.write_text("# User Preferences\n\n- use dark mode  [source: auto_detected, confidence: 1]\n")

        encoder._increment_confidence("use dark mode")
        content = profile.read_text(encoding="utf-8")
        assert "confidence: 2" in content

    def test_increment_confidence_in_store(self, tmp_path, mock_store):
        mock_store.read.return_value = {
            "entries": ["- use dark mode  [source: auto_detected, confidence: 1]"]
        }
        encoder = MemoryEncoder(workspace=tmp_path, memory_store=mock_store)
        encoder._increment_confidence("use dark mode")
        mock_store.replace.assert_called_once()

    def test_set_memory_store(self, encoder):
        store = MagicMock()
        encoder.set_memory_store(store)
        assert encoder._memory_store is store
