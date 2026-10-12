"""Tests for multimodal tool-result bounding.

A multimodal result (computer_use screenshots, vision output) was passed
straight into the context: the base64 blob dwarfs the character threshold, so
skipping the offload path for it also skipped the *text* bound. A tool
returning several screenshots, or a long ``text_summary`` beside them,
injected an unbounded amount of data and could push the turn past the context
window before compaction ran.
"""

from __future__ import annotations

import pytest

from markbot.agent.iteration import IterationRunner


class _FakeCompactorConfig:
    tool_output_inline_chars = 4000
    tool_output_preview_chars = 100


class _Stub:
    def __init__(self, name="computer_use"):
        self.name = name
        self.id = "c1"


def _runner(batch: int = 1) -> IterationRunner:
    r = object.__new__(IterationRunner)
    r.loop = type("L", (), {
        "compactor": type("C", (), {"config": _FakeCompactorConfig()})()
    })()
    r._batch_tool_count = batch
    return r


def _img(nbytes: int) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + "A" * nbytes},
    }


class TestTextBound:
    def test_long_summary_truncated(self):
        r = _runner()
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "x" * 40000, "content": [_img(10)]},
            _Stub(),
        )
        assert len(out["text_summary"]) < 40000
        assert "truncated" in out["text_summary"] or "offloaded" in out["text_summary"]

    def test_short_summary_untouched(self):
        r = _runner()
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "looked at the screen",
             "content": [_img(10)]},
            _Stub(),
        )
        assert out["text_summary"] == "looked at the screen"

    def test_budget_scales_with_fanout(self):
        """More tools in a batch means a smaller share each."""
        r1 = _runner(1)
        r8 = _runner(8)
        assert r1._inline_budget_for("", None) == 4000
        assert r8._inline_budget_for("", None) == 2000  # the floor


class TestImageBound:
    def test_oversized_image_replaced(self):
        r = _runner()
        big = r._MAX_INLINE_IMAGE_BYTES + 1000
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "shot", "content": [_img(big)]},
            _Stub(),
        )
        assert out["content"][0]["type"] == "text"
        assert "omitted" in out["content"][0]["text"]

    def test_image_count_capped(self):
        r = _runner()
        imgs = [_img(10) for _ in range(12)]
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "shots", "content": imgs},
            _Stub(),
        )
        assert len(out["content"]) == r._MAX_INLINE_IMAGES

    def test_normal_images_kept(self):
        r = _runner()
        imgs = [_img(100) for _ in range(3)]
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "s", "content": imgs},
            _Stub(),
        )
        assert len(out["content"]) == 3
        assert all(b["type"] == "image_url" for b in out["content"])

    def test_text_blocks_preserved(self):
        r = _runner()
        blocks = [{"type": "text", "text": "before"}, _img(10), {"type": "text", "text": "after"}]
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "s", "content": blocks},
            _Stub(),
        )
        assert out["content"][0]["text"] == "before"
        assert out["content"][-1]["text"] == "after"


class TestNoMutation:
    def test_original_dict_not_mutated(self):
        r = _runner()
        original = {
            "_multimodal": True,
            "text_summary": "x" * 40000,
            "content": [_img(10)],
        }
        snapshot = {k: (list(v) if isinstance(v, list) else v) for k, v in original.items()}
        r._bound_multimodal(original, _Stub())
        # The caller's dict is untouched; the loop may reuse tool output.
        assert original["text_summary"] == snapshot["text_summary"]
        assert len(original["content"]) == len(snapshot["content"])

    def test_multimodal_flag_preserved(self):
        r = _runner()
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "x" * 50000, "content": []},
            _Stub(),
        )
        assert out.get("_multimodal") is True


class TestRobustness:
    def test_non_list_content_survives(self):
        r = _runner()
        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "s", "content": "not a list"},
            _Stub(),
        )
        assert out["content"] == "not a list"

    def test_missing_fields(self):
        r = _runner()
        out = r._bound_multimodal({"_multimodal": True}, _Stub())
        assert out["_multimodal"] is True

    def test_never_raises(self):
        r = _runner()

        class Weird:
            def __len__(self):
                raise RuntimeError("boom")

        out = r._bound_multimodal(
            {"_multimodal": True, "text_summary": "s", "content": Weird()}, _Stub()
        )
        assert out is not None
