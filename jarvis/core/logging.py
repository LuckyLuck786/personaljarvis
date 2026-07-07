"""Structured logging. JSON to stderr by default (journald-friendly on the
hub), pretty console for dev. Deliberately no in-process log files — systemd
journald owns retention, which keeps the daemon's RAM/IO footprint minimal.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def setup_logging(level: str = "INFO", pretty: bool | None = None) -> None:
    if pretty is None:
        pretty = os.environ.get("JARVIS_LOG_PRETTY", "0") == "1"
    level = os.environ.get("JARVIS_LOG_LEVEL", level).upper()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
    )
    # uvicorn's own loggers stay, but drop their access spam to WARNING
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    renderer = (
        structlog.dev.ConsoleRenderer()
        if pretty
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
