"""Start-up steps shared by the entrypoints: Navidrome credential check and info file."""

import asyncio
import json
import time
from pathlib import Path

import aiohttp

from bridge.proxy import PROFILES, utc_iso
from bridge.subsonic import AuthResult, describe_error, ping_navidrome, subsonic_params


def check_navidrome(
    navidrome_url: str, user: str, password: str, salt: str
) -> tuple[AuthResult, str | None]:
    """Return the verdict and an error message, None when Navidrome accepted the credentials.

    A reply that is not a Subsonic answer with a reason counts as unreachable, not as rejected.
    """
    params = subsonic_params(user, password, salt)
    try:
        accepted, message = asyncio.run(ping_navidrome(navidrome_url, params))
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        reason = describe_error(exc, (password, salt, params["t"]))
        return AuthResult.UNREACHABLE, f"cannot reach Navidrome at {navidrome_url}: {reason}"
    if accepted:
        return AuthResult.OK, None
    if message is None:
        return AuthResult.UNREACHABLE, f"Navidrome at {navidrome_url} sent an unusable reply"
    suffix = f": {message}" if message else ""
    return AuthResult.REJECTED, f"Navidrome rejected the credentials{suffix}"


def write_info_file(path: Path, base_url: str, navidrome_url: str, log_path: Path) -> None:
    info = {
        "base_url": base_url,
        "url_template": f"{base_url}/<profile>/<song_id>.flac",
        "profiles": list(PROFILES),
        "navidrome_url": navidrome_url,
        "started_iso": utc_iso(time.time()),
        "log": str(log_path),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
