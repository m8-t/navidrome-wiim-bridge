"""Run the bridge: jukebox endpoint, audio proxy and Navidrome pass-through in one process."""

import argparse
import secrets
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path

from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import poc_proxy  # noqa: E402

from bridge.app import BridgeConfig, build_app  # noqa: E402
from bridge.subsonic import ConfigError, load_credentials  # noqa: E402
from bridge.wiim import DEFAULT_DEVICE_BASE, DEFAULT_STATUS_URL, Wiim, open_sessions  # noqa: E402


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
    parser.add_argument("--device-base", default=DEFAULT_DEVICE_BASE, help="WiiM UPnP base URL")
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL, help="WiiM status API URL")
    parser.add_argument("--max-volume", type=int, default=100, help="volume ceiling, 0 to 100")
    parser.add_argument("--queue-name", default="bridge", help="name of the device queue")
    return parser.parse_args(argv)


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
        user, password = load_credentials(Path(args.credentials).expanduser())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    navidrome_url = args.navidrome_url.rstrip("/")
    salt = secrets.token_hex(8)
    problem = poc_proxy.check_navidrome(navidrome_url, user, password, salt)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    secret = args.secret or secrets.token_urlsafe(9)
    base_url = f"http://{args.advertise_host}:{args.port}/s/{secret}"
    poc_proxy.write_info_file(args.info_file, base_url, navidrome_url, args.log)
    print(f"base URL: {base_url}", flush=True)
    print(f"request log: {args.log}", flush=True)
    print(f"Feishin server URL: http://{args.advertise_host}:{args.port}", flush=True)
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
    )
    web.run_app(make_app_factory(config)(), host=args.bind, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
