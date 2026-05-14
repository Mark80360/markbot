import logging
import sys
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

from markbot.log.filter import default_filter
from markbot.log.format import console_format, file_format


class InterceptHandler(logging.Handler):
    """Bridge stdlib logging records into loguru.

    Install via::

        logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    After this, *all* stdlib ``logging.getLogger(...)`` output is routed
    through loguru's sinks, filters and formatters.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(
    *,
    level: str = "INFO",
    log_file: Optional[Path] = None,
    verbose: bool = False,
    install_exception_hooks: bool = True,
) -> None:
    """Initialise the unified logging subsystem.

    Must be called once at application startup, **before** any other
    markbot module emits log messages.

    Parameters
    ----------
    level:
        Minimum log level for the *console* sink (e.g. ``"INFO"``,
        ``"DEBUG"``).  Ignored when *verbose* is ``True``.
    log_file:
        Optional path for the file sink.  When provided a second sink is
        added with ``level="DEBUG"`` and file-rotation settings.
    verbose:
        Shortcut for ``level="DEBUG"``.
    install_exception_hooks:
        Whether to install ``sys.excepthook`` / ``threading.excepthook``
        so that unhandled exceptions are captured by loguru.
    """
    logger.remove()

    effective_level = "DEBUG" if verbose else level

    logger.add(
        sys.stderr,
        level=effective_level,
        format=console_format,
        colorize=True,
        backtrace=True,
        diagnose=True,
        catch=True,
        filter=default_filter,
    )

    if log_file is not None:
        logger.add(
            log_file,
            level="DEBUG",
            format=file_format,
            rotation="10 MB",
            retention="7 days",
            backtrace=True,
            diagnose=True,
            catch=True,
            encoding="utf-8",
            filter=default_filter,
        )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    if install_exception_hooks:
        _install_exception_hooks()

    logger.info(
        "Logging configured: level={}, backtrace=True, diagnose=True",
        effective_level,
    )


def _install_exception_hooks() -> None:
    def handle_exception(exc_type, exc_value, exc_traceback):
        logger.exception(
            "Unhandled exception: {}:{}",
            exc_type.__name__,
            exc_value,
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def thread_exception_hook(args):
        logger.exception(
            "Unhandled exception in thread {}: {}:{}",
            args.thread.name if args.thread else "unknown",
            args.exc_type.__name__,
            args.exc_value,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = handle_exception
    threading.excepthook = thread_exception_hook
    logger.info("Global exception hooks installed")


def get_logger(name: str = ""):
    """Return a logger bound with a *component* tag.

    Usage::

        from markbot.log import get_logger
        logger = get_logger("TaskTracker")

        logger.info("Created task {}: {}", task.id, title)
        # → 2025-05-14 12:00:00 | INFO     | markbot.session.task_tracker:func:123 - Created task abc: Hello

    The ``component`` field is available in the loguru record as
    ``record["extra"]["component"]`` and can be used in custom formats
    or filters.
    """
    if name:
        return logger.bind(component=name)
    return logger
