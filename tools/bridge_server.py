"""Run the bridge: jukebox endpoint, audio proxy and Navidrome pass-through in one process."""

import argparse
import logging
import os
import re
import secrets
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import poc_proxy  # noqa: E402

from bridge.app import BridgeConfig, build_app  # noqa: E402
from bridge.log import parse_level, setup_logging  # noqa: E402
from bridge.subsonic import DEFAULT_CREDENTIALS, ConfigError, load_credentials  # noqa: E402
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
    max_volume: int
    queue_name: str
    keep_queue_on_start: bool
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


def _integer(
    option: str | None, environ: Mapping[str, str], name: str, default: int, low: int, high: int
) -> int:
    value, label = _pick(option, environ, name)
    if value is None:
        return default
    try:
        number = int(value.strip())
    except ValueError:
        number = None
    if number is None or not low <= number <= high:
        raise ConfigError(f"{label} must be an integer from {low} to {high}")
    return number


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
    max_volume = _integer(
        args.max_volume, environ, "BRIDGE_MAX_VOLUME", DEFAULT_MAX_VOLUME, 0, 100
    )
    keep_queue = _boolean(args.keep_queue_on_start, environ, "BRIDGE_KEEP_QUEUE_ON_START")
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
        max_volume=max_volume,
        queue_name=_string(args.queue_name, environ, "BRIDGE_QUEUE_NAME", DEFAULT_QUEUE_NAME),
        keep_queue_on_start=keep_queue,
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
    problem = poc_proxy.check_navidrome(settings.navidrome_url, user, password, salt)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    if settings.info_file is not None:
        base_url = f"http://{settings.advertise_host}:{settings.port}/s/{secret}"
        poc_proxy.write_info_file(settings.info_file, base_url, settings.navidrome_url, settings.log)
    logger.info(
        "listening on %s:%d, advertised as http://%s:%d",
        settings.bind,
        settings.port,
        settings.advertise_host,
        settings.port,
    )
    logger.info("navidrome at %s", settings.navidrome_url)
    logger.info("device at %s", settings.device_base)
    logger.info("audio URLs under /s/<hidden>")
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
    )
    web.run_app(make_app_factory(config)(), host=settings.bind, port=settings.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
