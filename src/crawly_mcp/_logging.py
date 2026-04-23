from __future__ import annotations

import logging
import os
import sys
from typing import IO, Any

from loguru import logger

CRAWLY_LOG_LEVEL_ENV_VAR = "CRAWLY_LOG_LEVEL"
_DEFAULT_LEVEL = "INFO"
_ALLOWED_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}</cyan> "
    "{message}"
)


class _StdlibInterceptHandler(logging.Handler):
    """Route stdlib `logging` records through loguru so uvicorn / mcp logs
    share the same sink, format, and level as crawly's own logs."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging(
    level: str | None = None,
    *,
    sink: IO[str] | Any = None,
) -> None:
    """Install the crawly log sink on stderr (or a provided sink).

    Stdio MCP transport uses stdout for the JSON-RPC protocol, so all log
    output MUST go to stderr to keep the protocol stream clean. This function
    also redirects stdlib `logging` through loguru so messages from uvicorn,
    the MCP SDK, and other libraries share the same format and level.
    """

    resolved = (level or os.environ.get(CRAWLY_LOG_LEVEL_ENV_VAR, _DEFAULT_LEVEL)).upper()
    if resolved not in _ALLOWED_LEVELS:
        allowed = ", ".join(_ALLOWED_LEVELS)
        raise ValueError(
            f"{CRAWLY_LOG_LEVEL_ENV_VAR} must be one of: {allowed}; got {resolved!r}"
        )

    logger.remove()
    logger.add(
        sink if sink is not None else sys.stderr, # type: ignore
        level=resolved,
        format=_LOG_FORMAT,
        colorize=sink is None,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )

    logging.basicConfig(handlers=[_StdlibInterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp"):
        logging.getLogger(name).handlers = [_StdlibInterceptHandler()]
        logging.getLogger(name).propagate = False
