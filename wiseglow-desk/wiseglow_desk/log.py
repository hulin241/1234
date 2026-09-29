"""Logging setup: stdout plus a rotating file at ``<workspace>/logs/desk.log``."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

# Third-party loggers that are chatty at INFO; kept at WARNING unless DEBUG is requested.
# "Lark" is the logger lark-oapi actually uses (there is no "lark_oapi" logger): the SDK gives
# it a private stdout handler at import time and resets its level to INFO on every client build.
_NOISY_LOGGERS = ("Lark", "httpx", "httpcore", "websockets", "urllib3", "asyncio")

_MARKER = "_wiseglow_handler"


class _QuietFilter(logging.Filter):
    """Drop records below the quiet level even when a library resets its logger level.

    The threshold is read from the root logger at filter time, so a later
    ``setup_logging(..., "DEBUG")`` call lets everything through again.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= _quiet_level()


def _quiet_level() -> int:
    return logging.DEBUG if logging.getLogger().level <= logging.DEBUG else logging.WARNING


def _route_lark_logger() -> None:
    """Make the SDK's ``Lark`` logger flow only through the root handlers.

    Without this every Lark line is printed twice on stdout (its own handler
    plus propagation to ours) and once more in ``desk.log``.
    """
    try:
        import lark_oapi.core.log  # noqa: F401  (creates the "Lark" logger and its stdout handler)
    except ImportError:  # pragma: no cover - the SDK is a hard dependency
        pass
    lark_logger = logging.getLogger("Lark")
    for handler in list(lark_logger.handlers):
        lark_logger.removeHandler(handler)
    lark_logger.propagate = True


def _quiet_noisy_loggers() -> None:
    quiet_level = _quiet_level()
    for name in _NOISY_LOGGERS:
        noisy = logging.getLogger(name)
        noisy.setLevel(quiet_level)
        if not any(isinstance(f, _QuietFilter) for f in noisy.filters):
            noisy.addFilter(_QuietFilter())


def setup_logging(workspace: Path, level: str = "INFO") -> Path:
    """Configure the root logger once; return the log file path.

    Calling it again only adjusts the level (handlers are not duplicated).
    """
    log_dir = Path(workspace) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desk.log"

    root = logging.getLogger()
    numeric = logging.getLevelName(level.upper())
    root.setLevel(numeric if isinstance(numeric, int) else logging.INFO)

    if not any(getattr(h, _MARKER, False) for h in root.handlers):
        formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        setattr(stream, _MARKER, True)
        root.addHandler(stream)
        try:
            rotating = RotatingFileHandler(log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
            rotating.setFormatter(formatter)
            setattr(rotating, _MARKER, True)
            root.addHandler(rotating)
        except OSError as exc:  # unwritable folder must not stop the bot
            root.warning("Log su file non disponibile (%s): scrivo solo su stdout", exc)

    _route_lark_logger()
    _quiet_noisy_loggers()
    return log_path
