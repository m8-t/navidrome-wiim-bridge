"""Logging setup shared by the bridge modules."""

import logging
import re
import sys

from bridge.subsonic import ConfigError

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_handler: logging.Handler | None = None
_QUERY = re.compile(r"\?\S*")


def redact(text: str) -> str:
    return _QUERY.sub("?<redacted>", text)


def parse_level(value: str) -> int:
    try:
        return LEVELS[value.strip().lower()]
    except KeyError:
        raise ConfigError("log level must be one of debug, info, warn, error") from None


def setup_logging(level: int) -> None:
    global _handler
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(_handler)
    root.setLevel(level)
    logging.getLogger("aiohttp.access").setLevel(max(level, logging.WARNING))
