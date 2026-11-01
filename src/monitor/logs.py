"""Structured logging: JSON lines in `logs/`, rotated at 50 MB × 5."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

MAX_BYTES = 50 * 1024 * 1024
BACKUPS = 5


def configure(process: str, log_dir: Path, level: int = logging.INFO) -> None:
    """Send structlog and stdlib logging to a rotating file and stderr."""
    log_dir.mkdir(parents=True, exist_ok=True)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared,
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    to_file = RotatingFileHandler(
        log_dir / f"{process}.jsonl",
        maxBytes=MAX_BYTES,
        backupCount=BACKUPS,
    )
    to_file.setFormatter(formatter)
    to_stderr = logging.StreamHandler()
    to_stderr.setFormatter(formatter)
    root.addHandler(to_file)
    root.addHandler(to_stderr)
    for noisy in ("httpx", "httpcore", "httpx2", "h2", "botocore", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    structlog.contextvars.bind_contextvars(process=process)


def get(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
