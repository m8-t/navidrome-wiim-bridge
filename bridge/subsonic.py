"""Subsonic API helpers: credentials, request signing and the ping check."""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_CREDENTIALS = "~/.config/navidrome-wiim-bridge/credentials.json"
SUBSONIC_VERSION = "1.16.1"
SUBSONIC_CLIENT = "wiim-bridge-poc"
UPSTREAM_HEADERS = {"Accept-Encoding": "identity"}


class ConfigError(Exception):
    """Raised for unusable credentials."""


def load_credentials(path: Path) -> tuple[str, str]:
    """Return (user, password) from a JSON file that only its owner may access."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ConfigError(f"credentials file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read credentials file {path}: {exc.strerror}") from None
    if mode & 0o077:
        raise ConfigError(
            f"credentials file {path} is accessible by group or others, run: chmod 600 {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read credentials file {path}: {type(exc).__name__}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"credentials file {path} is not valid JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"credentials file {path} must contain a JSON object")
    return _credential_field(data, "user", path), _credential_field(data, "password", path)


def _credential_field(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"credentials file {path}: '{key}' must be a non-empty string")
    return value


def subsonic_params(user: str, password: str, salt: str) -> dict[str, str]:
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": SUBSONIC_VERSION,
        "c": SUBSONIC_CLIENT,
        "f": "json",
    }


def parse_ping(text: str) -> tuple[bool, str | None]:
    """Return (accepted, error message) for a Subsonic ping response body."""
    try:
        body = json.loads(text)["subsonic-response"]
        status = body["status"]
    except (ValueError, KeyError, TypeError):
        return False, None
    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return status == "ok", message if isinstance(message, str) else None


async def ping_navidrome(navidrome_url: str, params: Mapping[str, str]) -> tuple[bool, str | None]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(f"{navidrome_url}/rest/ping", params=params, headers=UPSTREAM_HEADERS) as resp,
    ):
        text = await resp.text(errors="replace")
    return parse_ping(text)


def describe_error(exc: Exception, hidden: Iterable[str]) -> str:
    """Describe exc without any request URL or credential value."""
    text = str(exc) or type(exc).__name__
    # aiohttp puts the full request URL, query included, into several messages.
    text = re.sub(r"\?\S*", "?<redacted>", text)
    for value in hidden:
        text = text.replace(value, "<redacted>")
    return text
