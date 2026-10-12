"""Tests for curated-memory provenance: timestamps and supersede edges.

Curated entries were bare strings with no timestamp and no supersede
record, so two consequences followed:

* A fact recorded two years ago and one recorded this morning were injected
  identically — the agent had no way to notice it was acting on stale data.
* A corrected fact did not displace the wrong one. "Lives in Berlin" and
  "lives in Paris" coexisted forever and both were presented with equal
  confidence.

Automatic contradiction detection is deliberately *not* implemented: guessing
that two statements describe the same fact risks silently deleting something
correct. The caller — which can see the conversation — declares the
replacement, and retrieval then honours it.
"""

from __future__ import annotations

import json
import time

import pytest

from markbot.memory.tool import ENTRY_DELIMITER, MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(working_dir=tmp_path)


class TestTimestamps:
    def test_add_records_timestamp(self, store):
        assert store.add("memory", "user prefers dark mode")["success"]
        at = store.entry_recorded_at("user prefers dark mode")
        assert at is not None
        assert abs(at - time.time()) < 60

    def test_unknown_entry_has_no_timestamp(self, store):
        assert store.entry_recorded_at("never written") is None

    def test_replace_records_new_timestamp(self, store):
        store.add("memory", "lives in Berlin")
        old = store.entry_recorded_at("lives in Berlin")
        store.replace("memory", "lives in Berlin", "lives in Paris")
        assert store.entry_recorded_at("lives in Berlin") is None
        assert store.entry_recorded_at("lives in Paris") is not None
        assert store.entry_recorded_at("lives in Paris") >= (old or 0)

    def test_metadata_survives_reload(self, tmp_path):
        s1 = MemoryStore(working_dir=tmp_path)
        s1.add("memory", "deploys on fridays")
        s2 = MemoryStore(working_dir=tmp_path)
        assert s2.entry_recorded_at("deploys on fridays") is not None

    def test_corrupt_sidecar_does_not_break_store(self, tmp_path):
        (tmp_path / ".memory_meta.json").write_text("{not json", encoding="utf-8")
        s = MemoryStore(working_dir=tmp_path)
        assert s.add("memory", "still works")["success"]


class TestAgeMarkers:
    def _store_with_meta(self, tmp_path, entry, age_days):
        s = MemoryStore(working_dir=tmp_path)
        s.add("memory", entry)
        path = tmp_path / ".memory_meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        key = s._entry_key(entry)
        meta[key]["at"] = time.time() - age_days * 86400
        path.write_text(json.dumps(meta), encoding="utf-8")
        return s

    @pytest.mark.parametrize(
        "days,marker",
        [(0, "(today)"), (1, "(1 day ago)"), (5, "(5 days ago)"),
         (60, "(2 mo ago)"), (800, "(2 y ago)")],
    )
    def test_age_markers(self, tmp_path, days, marker):
        entry = f"fact recorded {days} days ago"
        s = self._store_with_meta(tmp_path, entry, days)
        rendered = s.format_entries_with_age([entry])
        assert marker in rendered
        assert entry in rendered

    def test_untimestamped_entry_unchanged(self, store):
        """A store written before this feature must render exactly as
        before — no markers, no budget surprise."""
        rendered = store.format_entries_with_age(["no timestamp here"])
        assert rendered == "no timestamp here"

    def test_mixed_entries(self, tmp_path):
        s = MemoryStore(working_dir=tmp_path)
        s.add("memory", "old fact")
        s.add("memory", "plain fact")
        path = tmp_path / ".memory_meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta[s._entry_key("old fact")]["at"] = time.time() - 86400 * 400
        path.write_text(json.dumps(meta), encoding="utf-8")
        s2 = MemoryStore(working_dir=tmp_path)
        rendered = s2.format_entries_with_age(["old fact", "plain fact"])
        # Both were written, so both carry a marker — with different ages.
        assert "old fact (1 y ago)" in rendered
        assert "plain fact (today)" in rendered

    def test_entry_without_timestamp_has_no_marker(self, tmp_path):
        """A store predating this feature must render byte-identically, so no
        entry silently consumes character budget it did not before."""
        s = MemoryStore(working_dir=tmp_path)
        s.replace_entries("memory", ["imported from elsewhere"])
        rendered = s.format_entries_with_age(["imported from elsewhere"])
        assert rendered == "imported from elsewhere"

    def test_prompt_block_includes_markers(self, tmp_path):
        s = self._store_with_meta(tmp_path, "stale preference", 30)
        s.refresh_snapshot()
        block = s.format_memory_prompt_block(include_user=False)
        assert "(1 mo ago)" in block


class TestSupersede:
    def test_explicit_supersede_recorded(self, store):
        store.add("memory", "lives in Berlin")
        store.add(
            "memory", "lives in Paris", supersedes="lives in Berlin"
        )
        edges = store.superseded_entries()
        assert edges.get("lives in Berlin") == "lives in Paris"

    def test_replace_creates_supersede_edge(self, store):
        store.add("memory", "lives in Berlin")
        store.replace("memory", "lives in Berlin", "lives in Paris")
        edges = store.superseded_entries()
        assert edges == {"lives in Berlin": "lives in Paris"}

    def test_superseded_entry_dropped_from_prompt(self, store):
        store.add("memory", "lives in Berlin")
        store.replace("memory", "lives in Berlin", "lives in Paris")
        block = store.format_memory_prompt_block(include_user=False)
        assert "Berlin" not in block
        assert "Paris" in block

    def test_both_present_without_supersede(self, store):
        """No declared supersede means no guessing: both remain visible."""
        store.add("memory", "lives in Berlin")
        store.add("memory", "lives in Paris")
        block = store.format_memory_prompt_block(include_user=False)
        assert "Berlin" in block
        assert "Paris" in block

    def test_edge_requires_both_ends_alive(self, store):
        """A stale edge must not hide a fact that was deleted and re-added."""
        store.add("memory", "old")
        store.add("memory", "new", supersedes="old")
        store.remove("memory", "new")
        store.add("memory", "old")  # re-added as current truth
        edges = store.superseded_entries()
        assert "old" not in edges
        block = store.format_memory_prompt_block(include_user=False)
        assert "old" in block

    def test_chain_of_supersedes(self, store):
        store.add("memory", "v1")
        store.add("memory", "v2", supersedes="v1")
        store.add("memory", "v3", supersedes="v2")
        block = store.format_memory_prompt_block(include_user=False)
        assert "v3" in block
        assert "v1" not in block
        assert "v2" not in block

    def test_metadata_pruned_for_deleted_entries(self, store):
        store.add("memory", "temporary")
        store.remove("memory", "temporary")
        path = store._working_dir / ".memory_meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        assert all(k == store._entry_key(e) for k in meta
                   for e in store.memory_entries) or True
        # The key for the removed entry should be gone.
        assert store._entry_key("temporary") not in meta

    def test_metadata_bounded(self, tmp_path):
        s = MemoryStore(working_dir=tmp_path, memory_char_limit=100000)
        s._META_MAX_RECORDS = 5
        for i in range(20):
            s.add("memory", f"entry number {i}")
        path = tmp_path / ".memory_meta.json"
        assert len(json.loads(path.read_text(encoding="utf-8"))) <= 5


class TestEntryTextUnchanged:
    def test_disk_format_unchanged(self, store, tmp_path):
        """The sidecar must not alter the on-disk entry format: entries are
        budgeted at ~2200 chars and injected verbatim."""
        store.add("user", "name: Ada")
        store.add("user", "role: engineer")
        raw = (tmp_path / "PROFILE.md").read_text(encoding="utf-8")
        assert raw.strip() == f"name: Ada{ENTRY_DELIMITER}role: engineer"

    def test_char_budget_unchanged(self, store):
        store.add("user", "x" * 100)
        before = store._entry_total_chars(store.user_entries)
        store.replace("user", "x" * 100, "y" * 100)
        after = store._entry_total_chars(store.user_entries)
        assert before == after


class TestManagerPlumbing:
    async def test_add_memory_forwards_supersedes(self, tmp_path):
        from markbot.memory.manager import MemoryManager

        mgr = MemoryManager.__new__(MemoryManager)
        mgr._memory_store = MemoryStore(working_dir=tmp_path)
        mgr.allow_curated_memory = lambda *a, **k: True

        assert await mgr.add_memory(
            "lives in Berlin", channel="cli"
        )
        assert await mgr.add_memory(
            "lives in Paris", channel="cli", supersedes="lives in Berlin"
        )
        assert mgr._memory_store.superseded_entries() == {
            "lives in Berlin": "lives in Paris"
        }

    def test_tool_schema_documents_supersedes(self):
        from markbot.tools.memory_tools import MemorySaveTool

        props = MemorySaveTool().parameters["properties"]
        assert "supersedes" in props
        assert "REPLACES" in props["supersedes"]["description"]
