"""Centralized logging configuration."""

import logging
import sys
from pathlib import Path
from typing import Optional


# Global flag to track whether logging has been initialized.
_log_initialized: bool = False


def setup_logging(
    log_level: str = "INFO",
    log_format: Optional[str] = None,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """Initialize the root logger for the multi-agent system.

    Args:
        log_level: Python log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: Custom format string. Falls back to a sensible default.
        log_file: Path to a log file. If provided, logs are written there too.

    Returns:
        The root logger for the project, ready to use.
    """
    global _log_initialized

    if _log_initialized:
        return logging.getLogger("dungeon_crusher")

    if log_format is None:
        log_format = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"

    root_logger = logging.getLogger("dungeon_crusher")
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Clear any existing handlers (idempotency).
    root_logger.handlers.clear()

    # Console handler.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)

    # File handler (optional).
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        file_handler.setFormatter(logging.Formatter(log_format))
        root_logger.addHandler(file_handler)

    _log_initialized = True
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the 'dungeon_crusher' namespace.

    Args:
        name: Usually __name__ of the calling module.

    Returns:
        A logger instance, e.g. ``dungeon_crusher.src.capture.window_capturer``.
    """
    if name.startswith("dungeon_crusher."):
        return logging.getLogger(name)
    return logging.getLogger(f"dungeon_crusher.{name}")
