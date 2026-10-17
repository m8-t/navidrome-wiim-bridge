"""Run the bridge: jukebox endpoint, audio proxy and Navidrome pass-through in one process."""

import argparse
import logging
import os
import re
import secrets
import socket
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bridge import startup  # noqa: E402
from bridge.app import BridgeConfig, build_app  # noqa: E402
from bridge.log import parse_level, setup_logging  # noqa: E402
from bridge.subsonic import (  # noqa: E402
    DEFAULT_CREDENTIALS,
    AuthResult,
    ConfigError,
    load_credentials,
)
from bridge.wiim import Wiim, device_urls, open_sessions  # noqa: E402


logger = logging.getLogger("bridge.server")

DEFAULT_NAVIDROME_URL = "http://127.0.0.1:4533"
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8098
DEFAULT_MAX_VOLUME = 100
DEFAULT_QUEUE_NAME = "bridge"
DEFAULT_REQUEST_LOG = "/dev/null"
TRUE_VALUES = frozenset({"true", "1", "yes"})
FALSE_VALUES = frozenset({"false", "0", "no"})


@dataclass(frozen=True)
class Settings:
    advertise_host: str
    device_base: str
    status_url: str
    navidrome_url: str
    bind: str
    port: int
    device_port: int | None
    max_volume: int
    queue_name: str
    keep_queue_on_start: bool
    scrobble: bool
    log: Path
    info_file: Path | None
    credentials: str


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WiiM bridge for Subsonic jukebox clients. Each option falls back to its "
        "environment variable, then to its default."
    )
    parser.add_argument(
        "--advertise-host",
        help="host or IP the amplifier uses to reach the bridge (env BRIDGE_ADVERTISE_HOST, required)",
    )
    parser.add_argument(
        "--device-host",
        help="host or IP of the amplifier (env BRIDGE_DEVICE_HOST, default: none; required unless "
        "both --device-base and --status-url are set)",
    )
    parser.add_argument(
        "--device-base",
        help="amplifier UPnP base URL (env BRIDGE_DEVICE_BASE, default: http://<device-host>:49152)",
    )
    parser.add_argument(
        "--status-url",
        help="amplifier status API URL (env BRIDGE_STATUS_URL, default: "
        "https://<device-host>/httpapi.asp?command=getPlayerStatus)",
    )
    parser.add_argument(
        "--navidrome-url",
        help=f"Navidrome base URL (env BRIDGE_NAVIDROME_URL, default: {DEFAULT_NAVIDROME_URL})",
    )
    parser.add_argument(
        "--bind", help=f"address to listen on (env BRIDGE_BIND, default: {DEFAULT_BIND})"
    )
    parser.add_argument(
        "--port",
        help=f"port to listen on, 1 to 65535 (env BRIDGE_PORT, default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--device-port",
        help="second port that serves only the audio URLs and /healthz, 1 to 65535, not the same "
        "as --port; the amplifier is told to use it (env BRIDGE_DEVICE_PORT, default: none, "
        "everything is served on --port)",
    )
    parser.add_argument(
        "--max-volume",
        help=f"volume ceiling, 0 to 100 (env BRIDGE_MAX_VOLUME, default: {DEFAULT_MAX_VOLUME})",
    )
    parser.add_argument(
        "--queue-name",
        help=f"name of the device queue (env BRIDGE_QUEUE_NAME, default: {DEFAULT_QUEUE_NAME})",
    )
    parser.add_argument(
        "--keep-queue-on-start",
        action="store_const",
        const=True,
        default=None,
        help="do not clear the device queue at start (env BRIDGE_KEEP_QUEUE_ON_START: "
        "true/1/yes or false/0/no, default: false)",
    )
    parser.add_argument(
        "--scrobble",
        action="store_const",
        const=True,
        default=None,
        help="report plays to Navidrome so its play history includes them (env BRIDGE_SCROBBLE: "
        "true/1/yes or false/0/no, default: false)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="JSON-lines log of the device's audio requests (env BRIDGE_REQUEST_LOG, "
        f"default: {DEFAULT_REQUEST_LOG})",
    )
    parser.add_argument(
        "--info-file",
        type=Path,
        help="write the base URL to this file (no env variable, default: no file is written)",
    )
    parser.add_argument(
        "--no-info-file", action="store_true", help="accepted for compatibility, does nothing"
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("BRIDGE_LOG_LEVEL") or "info",
        help="debug, info, warn or error (env BRIDGE_LOG_LEVEL, default: info)",
    )
    parser.add_argument(
        "--secret", default=None, help="path secret (env BRIDGE_SECRET, default: random)"
    )
    parser.add_argument(
        "--credentials",
        help="JSON file with user and password (env BRIDGE_CREDENTIALS, default: "
        f"{DEFAULT_CREDENTIALS}); BRIDGE_NAVIDROME_USER and BRIDGE_NAVIDROME_PASSWORD "
        "take precedence",
    )
    return parser.parse_args(argv)


def _pick(option: str | None, environ: Mapping[str, str], name: str) -> tuple[str | None, str]:
    """Return the option or the environment value, plus the name to use in error messages."""
    if option is not None and option != "":
        return option, f"--{name.removeprefix('BRIDGE_').lower().replace('_', '-')}"
    return (environ.get(name) or None), name


def _string(option: str | None, environ: Mapping[str, str], name: str, default: str) -> str:
    return _pick(option, environ, name)[0] or default


def _optional_integer(
    option: str | None, environ: Mapping[str, str], name: str, low: int, high: int
) -> int | None:
    value, label = _pick(option, environ, name)
    if value is None:
        return None
    try:
        number = int(value.strip())
    except ValueError:
        number = None
    if number is None or not low <= number <= high:
        raise ConfigError(f"{label} must be an integer from {low} to {high}")
    return number


def _integer(
    option: str | None, environ: Mapping[str, str], name: str, default: int, low: int, high: int
) -> int:
    number = _optional_integer(option, environ, name, low, high)
    return default if number is None else number


def _boolean(option: bool | None, environ: Mapping[str, str], name: str) -> bool:
    if option is not None:
        return option
    value = environ.get(name)
    if not value:
        return False
    lowered = value.strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be true, 1, yes, false, 0 or no")


def resolve_settings(args: argparse.Namespace, environ: Mapping[str, str]) -> Settings:
    advertise_host = _pick(args.advertise_host, environ, "BRIDGE_ADVERTISE_HOST")[0]
    if advertise_host is None:
        raise ConfigError(
            "set BRIDGE_ADVERTISE_HOST (or --advertise-host) to the address "
            "the amplifier uses to reach the bridge"
        )
    device_host = _pick(args.device_host, environ, "BRIDGE_DEVICE_HOST")[0]
    device_base = _pick(args.device_base, environ, "BRIDGE_DEVICE_BASE")[0]
    status_url = _pick(args.status_url, environ, "BRIDGE_STATUS_URL")[0]
    if device_host is not None:
        try:
            derived_base, derived_status = device_urls(device_host)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        device_base = device_base or derived_base
        status_url = status_url or derived_status
    elif device_base is None or status_url is None:
        raise ConfigError("set BRIDGE_DEVICE_HOST (or --device-host) to the amplifier's address")
    port = _integer(args.port, environ, "BRIDGE_PORT", DEFAULT_PORT, 1, 65535)
    device_port = _optional_integer(args.device_port, environ, "BRIDGE_DEVICE_PORT", 1, 65535)
    if device_port == port:
        device_label = _pick(args.device_port, environ, "BRIDGE_DEVICE_PORT")[1]
        port_label = _pick(args.port, environ, "BRIDGE_PORT")[1]
        raise ConfigError(f"{device_label} must differ from {port_label}")
    max_volume = _integer(
        args.max_volume, environ, "BRIDGE_MAX_VOLUME", DEFAULT_MAX_VOLUME, 0, 100
    )
    keep_queue = _boolean(args.keep_queue_on_start, environ, "BRIDGE_KEEP_QUEUE_ON_START")
    scrobble = _boolean(args.scrobble, environ, "BRIDGE_SCROBBLE")
    log = args.log or Path(environ.get("BRIDGE_REQUEST_LOG") or DEFAULT_REQUEST_LOG)
    return Settings(
        advertise_host=advertise_host,
        device_base=device_base,
        status_url=status_url,
        navidrome_url=_string(
            args.navidrome_url, environ, "BRIDGE_NAVIDROME_URL", DEFAULT_NAVIDROME_URL
        ).rstrip("/"),
        bind=_string(args.bind, environ, "BRIDGE_BIND", DEFAULT_BIND),
        port=port,
        device_port=device_port,
        max_volume=max_volume,
        queue_name=_string(args.queue_name, environ, "BRIDGE_QUEUE_NAME", DEFAULT_QUEUE_NAME),
        keep_queue_on_start=keep_queue,
        scrobble=scrobble,
        log=log,
        info_file=args.info_file,
        credentials=_string(args.credentials, environ, "BRIDGE_CREDENTIALS", DEFAULT_CREDENTIALS),
    )


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


def listen_on(bind: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    return socket.create_server((bind, port), family=family)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        setup_logging(parse_level(args.log_level))
        settings = resolve_settings(args, os.environ)
        user, password = resolve_credentials(settings.credentials, os.environ)
        secret = resolve_secret(args.secret, os.environ)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    salt = secrets.token_hex(8)
    verdict, problem = startup.check_navidrome(settings.navidrome_url, user, password, salt)
    if verdict is AuthResult.REJECTED:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    if verdict is AuthResult.UNREACHABLE:
        logger.warning(
            "%s; starting anyway, requests that need Navidrome fail until it is back", problem
        )
    run_options: dict[str, Any] = {}
    audio_port = settings.port
    if settings.device_port is not None:
        audio_port = settings.device_port
        try:
            run_options["sock"] = listen_on(settings.bind, settings.device_port)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            address = f"{settings.bind}:{audio_port}"
            print(f"error: cannot listen on {address}: {reason}", file=sys.stderr)
            return 2
    if settings.info_file is not None:
        base_url = f"http://{settings.advertise_host}:{audio_port}/s/{secret}"
        startup.write_info_file(settings.info_file, base_url, settings.navidrome_url, settings.log)
    logger.info(
        "listening on %s:%d, advertised as http://%s:%d",
        settings.bind,
        settings.port,
        settings.advertise_host,
        settings.port,
    )
    if settings.device_port is not None:
        logger.info(
            "audio-only device port %s:%d, advertised as http://%s:%d",
            settings.bind,
            settings.device_port,
            settings.advertise_host,
            settings.device_port,
        )
    logger.info("navidrome at %s", settings.navidrome_url)
    logger.info("device at %s", settings.device_base)
    logger.info("audio URLs under /s/<hidden>")
    if settings.scrobble:
        logger.info("scrobbling plays to navidrome")
    config = BridgeConfig(
        navidrome_url=settings.navidrome_url,
        user=user,
        password=password,
        salt=salt,
        secret=secret,
        advertise_host=settings.advertise_host,
        port=settings.port,
        log_path=settings.log,
        device_base=settings.device_base,
        status_url=settings.status_url,
        queue_name=settings.queue_name,
        max_volume=settings.max_volume,
        clear_on_start=not settings.keep_queue_on_start,
        scrobble=settings.scrobble,
        device_port=settings.device_port,
    )
    web.run_app(
        make_app_factory(config)(),
        host=settings.bind,
        port=settings.port,
        print=None,
        **run_options,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
