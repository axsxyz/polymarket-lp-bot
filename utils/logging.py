"""
Loguru configuration.

Sets up:
  - Coloured stderr output (for interactive use / journald)
  - Rotating file logs under logs/ (50 MB per file, 30 days retention)
  - Structured JSON file for machine parsing (optional)

Call `setup_logging(level)` once at startup.
"""

from __future__ import annotations

import sys

from loguru import logger


def setup_logging(level: str = "INFO") -> None:
    """
    Remove default loguru handler and replace with our own.
    Must be called before any log statements.
    """
    logger.remove()

    # Console handler — human-readable with colour
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # Rotating file handler — plain text
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="50 MB",
        retention="30 days",
        compression="gz",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} — {message}"
        ),
        enqueue=True,  # thread-safe
    )

    # JSON file — machine-parseable (useful for log aggregation)
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.jsonl",
        level="DEBUG",
        rotation="50 MB",
        retention="30 days",
        serialize=True,
        enqueue=True,
    )

    logger.info("Logging initialised at level {}", level.upper())
