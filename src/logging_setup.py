"""Central logging configuration for the Naukri bot.

Every module logs through ``logging.getLogger("naukri_bot.<module>")``.
``setup_logging`` is called once from ``main.py``; library consumers (tests)
can skip it and still get namespaced, silent loggers.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

LOGGER_NAME = "naukri_bot"
DEFAULT_LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
DEFAULT_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

CONSOLE_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s [%(threadName)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Anything that looks like a credential is replaced before it reaches a handler.
_REDACTIONS = (
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|cookie)\b\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+"), r"\1 ***"),
    (re.compile(r"(?i)(NAUKRI_COOKIES\s*=\s*).*"), r"\1***"),
)


def redact(text: str) -> str:
    """Mask credentials and session cookies in a log line."""
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Filter that scrubs secrets from formatted log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 (stdlib API)
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def setup_logging(
    level: str | int = DEFAULT_LOG_LEVEL,
    log_dir: str | os.PathLike[str] | None = DEFAULT_LOG_DIR,
    run_id: str | None = None,
    quiet: bool = False,
) -> logging.Logger:
    """Configure console + rotating file logging and return the root logger.

    Args:
        level: Log level name ("DEBUG", "INFO", ...) or numeric level.
        log_dir: Directory for ``naukri_bot.log``; ``None`` disables file logging.
        run_id: Short identifier included in the startup banner.
        quiet: Suppress console output (useful for tests and CI artifacts).
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(level)
    root.propagate = False

    # Make reconfiguration idempotent (tests call this repeatedly).
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(CONSOLE_FORMAT, DATE_FORMAT)
    redaction = RedactionFilter()

    if not quiet:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        console.addFilter(redaction)
        root.addHandler(console)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            directory / "naukri_bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    if run_id:
        root.info("=" * 72)
        root.info("Naukri bot run %s started (pid=%s)", run_id, os.getpid())
        root.info("=" * 72)

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, e.g. ``get_logger("crawler")``."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
