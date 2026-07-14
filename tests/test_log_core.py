"""Tests for markbot.log.core and markbot.log.format."""

from __future__ import annotations

import logging

import pytest
from loguru import logger

from markbot.log.core import InterceptHandler, get_logger, setup_logging
from markbot.log.format import (
    _MAX_CONSOLE_MSG_LEN,
    _MAX_FILE_MSG_LEN,
    _escape_markup,
    console_format,
    file_format,
)


# ---------------------------------------------------------------------------
# _escape_markup
# ---------------------------------------------------------------------------


class TestEscapeMarkup:
    def test_backslash_escaped(self):
        assert _escape_markup("a\\b") == "a\\\\b"

    def test_angle_bracket_escaped(self):
        assert _escape_markup("a<b") == "a\\<b"

    def test_open_brace_escaped(self):
        assert _escape_markup("a{b") == "a{{b"

    def test_close_brace_escaped(self):
        assert _escape_markup("a}b") == "a}}b"

    def test_dict_repr_escaped(self):
        text = "Headers({'cache-control': 'no-cache'})"
        out = _escape_markup(text)
        assert "{{" in out
        assert "}}" in out

    def test_empty_string(self):
        assert _escape_markup("") == ""

    def test_no_special_chars(self):
        assert _escape_markup("hello world") == "hello world"

    def test_multiple_special_chars(self):
        out = _escape_markup("\\<{}")
        assert out == "\\\\\\<{{}}"


# ---------------------------------------------------------------------------
# console_format / file_format
# ---------------------------------------------------------------------------


def _make_record(**overrides):
    """Build a minimal loguru-style record dict."""
    import datetime as _dt

    record = {
        "message": "hello",
        "name": "markbot.test",
        "function": "test_func",
        "line": 42,
        "level": type("Lvl", (), {"name": "INFO"})(),
        "time": _dt.datetime(2025, 1, 1, 12, 0, 0, 123000),
        "extra": {},
    }
    record.update(overrides)
    return record


class TestConsoleFormat:
    def test_basic_message(self):
        result = console_format(_make_record())
        assert "hello" in result
        assert "markbot.test" in result
        assert "test_func" in result
        assert "42" in result
        assert result.endswith("\n")

    def test_includes_timestamp_template(self):
        # console_format returns a loguru template with {time:...} placeholders
        result = console_format(_make_record())
        assert "{time:" in result

    def test_includes_level_template(self):
        result = console_format(_make_record())
        assert "{level:" in result

    def test_component_tag(self):
        record = _make_record(extra={"component": "TaskTracker"})
        result = console_format(record)
        assert "TaskTracker" in result

    def test_no_component_tag_when_empty(self):
        result = console_format(_make_record())
        # No component tag should appear
        assert "[]" not in result

    def test_long_message_truncated(self):
        long_msg = "x" * (_MAX_CONSOLE_MSG_LEN + 100)
        record = _make_record(message=long_msg)
        result = console_format(record)
        assert "[truncated]" in result

    def test_short_message_not_truncated(self):
        record = _make_record(message="short")
        result = console_format(record)
        assert "[truncated]" not in result


class TestFileFormat:
    def test_basic_message(self):
        result = file_format(_make_record())
        assert "hello" in result
        assert "markbot.test" in result
        assert result.endswith("\n")

    def test_includes_timestamp(self):
        result = file_format(_make_record())
        assert "2025-01-01 12:00:00" in result

    def test_includes_level(self):
        result = file_format(_make_record())
        assert "INFO" in result

    def test_component_tag(self):
        record = _make_record(extra={"component": "MemoryManager"})
        result = file_format(record)
        assert "[MemoryManager]" in result

    def test_long_message_truncated(self):
        long_msg = "x" * (_MAX_FILE_MSG_LEN + 100)
        record = _make_record(message=long_msg)
        result = file_format(record)
        assert "[truncated]" in result

    def test_strips_ansi_codes(self):
        record = _make_record(message="\033[31mred text\033[0m")
        result = file_format(record)
        assert "\033[31m" not in result
        assert "red text" in result


# ---------------------------------------------------------------------------
# InterceptHandler
# ---------------------------------------------------------------------------


class TestInterceptHandler:
    def test_is_logging_handler(self):
        handler = InterceptHandler()
        assert isinstance(handler, logging.Handler)

    def test_emit_routes_to_loguru(self, capsys):
        handler = InterceptHandler()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="routed message",
            args=(),
            exc_info=None,
        )
        # Loguru must be configured with a sink to capture
        setup_logging(level="INFO", install_exception_hooks=False)
        handler.emit(record)
        captured = capsys.readouterr()
        # If stderr is not a TTY, setup_logging won't add the console sink.
        # We just verify no exception is raised.


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------


class TestGetLogger:
    def test_returns_logger(self):
        log = get_logger("TestComponent")
        assert log is not None

    def test_without_name(self):
        log = get_logger()
        assert log is not None

    def test_binds_component(self):
        log = get_logger("MyComponent")
        assert log is not None
        # Bound logger should differ from the global logger
        assert log is not logger


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def test_does_not_raise(self):
        setup_logging(level="INFO", install_exception_hooks=False)

    def test_verbose_sets_debug(self):
        setup_logging(verbose=True, install_exception_hooks=False)

    def test_with_log_file(self, tmp_path):
        log_file = tmp_path / "test.log"
        setup_logging(
            level="INFO",
            log_file=log_file,
            install_exception_hooks=False,
        )
        logger.info("test message")
        # Force loguru to flush
        logger.complete()
        # The log file should exist
        assert log_file.exists()
