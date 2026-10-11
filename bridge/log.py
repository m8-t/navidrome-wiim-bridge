"""Logging setup shared by the bridge modules."""

import logging
import re
import sys
import unicodedata
from collections.abc import Mapping
from typing import Any

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


def clean(text: str, limit: int = 120) -> str:
    """Make text safe for one log line: no control characters, collapsed spaces, capped."""
    spaced = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    squeezed = " ".join(spaced.split())
    if len(squeezed) > limit:
        return squeezed[:limit] + "..."
    return squeezed


def song_label(song: Mapping[str, Any]) -> str:
    artist = clean(str(song.get("artist") or ""))
    title = clean(str(song.get("title") or ""))
    if artist and title:
        return f"{artist} - {title}"
    return title or artist or "(untitled)"


def human_size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{n // 1000} kB"


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
