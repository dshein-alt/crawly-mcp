from __future__ import annotations

import io
import logging

import pytest
from loguru import logger

from crawly_mcp._logging import configure_logging


@pytest.fixture(autouse=True)
def _reset_loguru() -> None:
    logger.remove()
    yield
    logger.remove()


def test_configure_logging_writes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO")
    logger.info("hello stdio world")

    captured = capsys.readouterr()
    assert "hello stdio world" in captured.err
    # stdio MCP requires stdout to stay clean for the protocol.
    assert captured.out == ""


def test_configure_logging_respects_env_var(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CRAWLY_LOG_LEVEL", "WARNING")
    configure_logging()

    logger.info("info should be suppressed")
    logger.warning("warning should appear")

    captured = capsys.readouterr()
    assert "info should be suppressed" not in captured.err
    assert "warning should appear" in captured.err


def test_configure_logging_accepts_debug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="DEBUG")
    logger.debug("debug visible")

    captured = capsys.readouterr()
    assert "debug visible" in captured.err


def test_configure_logging_intercepts_stdlib_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO")

    # Simulate uvicorn / mcp library which use stdlib logging.
    stdlib_logger = logging.getLogger("uvicorn.test")
    stdlib_logger.info("stdlib message routed through loguru")

    captured = capsys.readouterr()
    assert "stdlib message routed through loguru" in captured.err


def test_configure_logging_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="CRAWLY_LOG_LEVEL"):
        configure_logging(level="BOGUS")


def test_configure_logging_writes_to_provided_sink() -> None:
    sink = io.StringIO()
    configure_logging(level="INFO", sink=sink)
    logger.info("captured in custom sink")

    assert "captured in custom sink" in sink.getvalue()
