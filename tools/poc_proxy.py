"""Proof-of-concept pass-through proxy between a WiiM amplifier and Navidrome.

The amplifier fetches /s/<secret>/<profile>/<song id>.flac from this proxy, the
proxy fetches the original FLAC bytes from the Subsonic API unchanged and logs
every request the amplifier makes as one JSON object per line.
"""

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bridge.proxy import PROFILES, ProxyConfig, create_app, utc_iso  # noqa: E402
from bridge.subsonic import (  # noqa: E402
    DEFAULT_CREDENTIALS,
    ConfigError,
    describe_error,
    load_credentials,
    ping_navidrome,
    subsonic_params,
)

def check_navidrome(navidrome_url: str, user: str, password: str, salt: str) -> str | None:
    """Return an error message, or None when Navidrome accepted the credentials."""
    params = subsonic_params(user, password, salt)
    try:
        accepted, message = asyncio.run(ping_navidrome(navidrome_url, params))
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        reason = describe_error(exc, (password, salt, params["t"]))
        return f"cannot reach Navidrome at {navidrome_url}: {reason}"
    if accepted:
        return None
    return "Navidrome rejected the credentials" + (f": {message}" if message else "")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pass-through FLAC proxy for WiiM experiments.")
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8098, help="port to listen on (default 8098)")
    parser.add_argument(
        "--advertise-host",
        default="172.16.20.30",
        help="host the amplifier uses to reach this proxy (default 172.16.20.30)",
    )
    parser.add_argument(
        "--navidrome-url",
        default="http://127.0.0.1:4533",
        help="Navidrome base URL (default http://127.0.0.1:4533)",
    )
    parser.add_argument(
        "--credentials",
        default=DEFAULT_CREDENTIALS,
        help=f"JSON file with user and password, mode 600 (default {DEFAULT_CREDENTIALS})",
    )
    parser.add_argument("--secret", default=None, help="path secret (default: random)")
    parser.add_argument(
        "--log",
        type=Path,
        default=PROJECT_DIR / "results" / "proxy_requests.jsonl",
        help="request log, JSON lines (default <project>/results/proxy_requests.jsonl)",
    )
    parser.add_argument(
        "--info-file",
        type=Path,
        default=PROJECT_DIR / "results" / "proxy_info.json",
        help="where to write the base URL (default <project>/results/proxy_info.json)",
    )
    return parser.parse_args(argv)


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        user, password = load_credentials(Path(args.credentials).expanduser())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    navidrome_url = args.navidrome_url.rstrip("/")
    salt = secrets.token_hex(8)
    problem = check_navidrome(navidrome_url, user, password, salt)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    secret = args.secret or secrets.token_urlsafe(9)
    base_url = f"http://{args.advertise_host}:{args.port}/s/{secret}"
    write_info_file(args.info_file, base_url, navidrome_url, args.log)
    print(f"base URL: {base_url}", flush=True)
    print(f"request log: {args.log}", flush=True)
    config = ProxyConfig(navidrome_url, user, password, salt, secret, args.log)
    web.run_app(create_app(config), host=args.bind, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
