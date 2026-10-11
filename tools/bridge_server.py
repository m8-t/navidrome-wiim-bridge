"""Run the bridge: jukebox endpoint, audio proxy and Navidrome pass-through in one process."""

import argparse
import logging
import os
import re
import secrets
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path

from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import poc_proxy  # noqa: E402

from bridge.app import BridgeConfig, build_app  # noqa: E402
from bridge.log import parse_level, setup_logging  # noqa: E402
from bridge.subsonic import ConfigError, load_credentials  # noqa: E402
from bridge.wiim import DEFAULT_DEVICE_BASE, DEFAULT_STATUS_URL, Wiim, open_sessions  # noqa: E402


logger = logging.getLogger("bridge.server")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    defaults = poc_proxy.parse_args([])
    parser = argparse.ArgumentParser(description="WiiM bridge for Subsonic jukebox clients.")
    parser.add_argument("--bind", default=defaults.bind, help="address to listen on")
    parser.add_argument("--port", type=int, default=defaults.port, help="port to listen on")
    parser.add_argument(
        "--advertise-host",
        default=defaults.advertise_host,
        help="host the amplifier uses to reach this server",
    )
    parser.add_argument("--navidrome-url", default=defaults.navidrome_url, help="Navidrome base URL")
    parser.add_argument(
        "--credentials", default=defaults.credentials, help="JSON file with user and password"
    )
    parser.add_argument("--secret", default=None, help="path secret (default: random)")
    parser.add_argument("--log", type=Path, default=defaults.log, help="request log, JSON lines")
    parser.add_argument(
        "--info-file", type=Path, default=defaults.info_file, help="where to write the base URL"
    )
    parser.add_argument(
        "--no-info-file", action="store_true", help="do not write the info file (containers)"
    )
    parser.add_argument("--device-base", default=DEFAULT_DEVICE_BASE, help="WiiM UPnP base URL")
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL, help="WiiM status API URL")
    parser.add_argument("--max-volume", type=int, default=100, help="volume ceiling, 0 to 100")
    parser.add_argument(
        "--keep-queue-on-start", action="store_true", help="do not clear the device queue at start"
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("BRIDGE_LOG_LEVEL") or "info",
        help="debug, info, warn or error (default: $BRIDGE_LOG_LEVEL, else info)",
    )
    parser.add_argument("--queue-name", default="bridge", help="name of the device queue")
    return parser.parse_args(argv)


SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,}")


def resolve_credentials(credentials_path: str, environ: Mapping[str, str]) -> tuple[str, str]:
    user = environ.get("BRIDGE_NAVIDROME_USER", "")
    password = environ.get("BRIDGE_NAVIDROME_PASSWORD", "")
    if user and password:
        return user, password
    if user or password or "BRIDGE_NAVIDROME_USER" in environ or "BRIDGE_NAVIDROME_PASSWORD" in environ:
        raise ConfigError("set both BRIDGE_NAVIDROME_USER and BRIDGE_NAVIDROME_PASSWORD, or neither")
    return load_credentials(Path(credentials_path).expanduser())


def resolve_secret(arg: str | None, environ: Mapping[str, str]) -> str:
    secret = arg or environ.get("BRIDGE_SECRET") or None
    if secret is None:
        return secrets.token_urlsafe(9)
    if not SECRET_PATTERN.fullmatch(secret):
        raise ConfigError("path secret must be at least 8 characters of A-Z, a-z, 0-9, _ or -")
    return secret


def hidden_base_url(advertise_host: str, port: int) -> str:
    return f"http://{advertise_host}:{port}/s/<hidden>"


def make_app_factory(config: BridgeConfig) -> Callable[[], Awaitable[web.Application]]:
    async def factory() -> web.Application:
        stack = AsyncExitStack()
        soap_session, status_session = await stack.enter_async_context(open_sessions())
        wiim = Wiim(soap_session, status_session, config.device_base, config.status_url)
        app = build_app(config, wiim)

        async def close_device_sessions(app: web.Application) -> None:
            await stack.aclose()

        app.on_cleanup.append(close_device_sessions)
        return app

    return factory


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        setup_logging(parse_level(args.log_level))
        user, password = resolve_credentials(args.credentials, os.environ)
        secret = resolve_secret(args.secret, os.environ)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    navidrome_url = args.navidrome_url.rstrip("/")
    salt = secrets.token_hex(8)
    problem = poc_proxy.check_navidrome(navidrome_url, user, password, salt)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    base_url = f"http://{args.advertise_host}:{args.port}/s/{secret}"
    if not args.no_info_file:
        poc_proxy.write_info_file(args.info_file, base_url, navidrome_url, args.log)
    logger.info("base URL: %s", hidden_base_url(args.advertise_host, args.port))
    logger.info("request log: %s", args.log)
    logger.info("Feishin server URL: http://%s:%d", args.advertise_host, args.port)
    config = BridgeConfig(
        navidrome_url=navidrome_url,
        user=user,
        password=password,
        salt=salt,
        secret=secret,
        advertise_host=args.advertise_host,
        port=args.port,
        log_path=args.log,
        device_base=args.device_base,
        status_url=args.status_url,
        queue_name=args.queue_name,
        max_volume=args.max_volume,
        clear_on_start=not args.keep_queue_on_start,
    )
    web.run_app(make_app_factory(config)(), host=args.bind, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
