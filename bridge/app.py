"""Combined bridge service: jukebox endpoint, audio proxy and Navidrome pass-through."""

import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from yarl import URL

from bridge.jukebox import Jukebox, add_jukebox_routes
from bridge.player import Player
from bridge.proxy import ProxyConfig, add_proxy_routes
from bridge.subsonic import UPSTREAM_HEADERS, subsonic_params

logger = logging.getLogger("bridge.app")

AUTH_PARAMS = ("u", "p", "t", "s", "apiKey")
AUTH_CACHE_SECONDS = 60
CHUNK_SIZE = 65536
REQUEST_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
        "te",
        "trailer",
    }
)
RESPONSE_HOP_HEADERS = frozenset({"transfer-encoding", "connection", "keep-alive"})


@dataclass(frozen=True)
class BridgeConfig:
    navidrome_url: str
    user: str
    password: str = field(repr=False)
    salt: str = field(repr=False)
    secret: str = field(repr=False)
    advertise_host: str
    port: int
    log_path: Path
    device_base: str
    status_url: str
    queue_name: str = "bridge"
    profile: str = "range"
    max_volume: int = 100
    clear_on_start: bool = True


def stream_url(config: BridgeConfig, song_id: str) -> str:
    return (
        f"http://{config.advertise_host}:{config.port}"
        f"/s/{config.secret}/{config.profile}/{song_id}.flac"
    )


class NavidromeClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        navidrome_url: str,
        user: str,
        password: str,
        salt: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._url = navidrome_url
        self._user = user
        self._password = password
        self._salt = salt
        self._clock = clock
        self._verified: dict[tuple[str | None, ...], float] = {}

    async def _get_json(self, path: str, params: Mapping[str, str]) -> Any:
        async with self._session.get(
            f"{self._url}{path}", params=params, headers=UPSTREAM_HEADERS
        ) as resp:
            text = await resp.text(errors="replace")
        try:
            body = json.loads(text)["subsonic-response"]
        except (ValueError, KeyError, TypeError):
            return None
        return body if isinstance(body, dict) else None

    async def get_song(self, song_id: str) -> Mapping[str, Any] | None:
        params = subsonic_params(self._user, self._password, self._salt)
        params["id"] = song_id
        body = await self._get_json("/rest/getSong", params)
        if body is None or body.get("status") != "ok":
            return None
        song = body.get("song")
        return song if isinstance(song, dict) else None

    async def check_caller(self, params: Mapping[str, str]) -> bool:
        if "u" not in params and "apiKey" not in params:
            return False
        key = tuple(params.get(name) for name in AUTH_PARAMS)
        cached_at = self._verified.get(key)
        if cached_at is not None and self._clock() - cached_at < AUTH_CACHE_SECONDS:
            return True
        query = {name: params[name] for name in AUTH_PARAMS if name in params}
        query |= {"v": "1.16.1", "c": "wiim-bridge-auth", "f": "json"}
        try:
            body = await self._get_json("/rest/ping", query)
        except (aiohttp.ClientError, TimeoutError):
            return False
        if body is None or body.get("status") != "ok":
            return False
        self._verified[key] = self._clock()
        return True


CONFIG_KEY = web.AppKey("bridge_config", BridgeConfig)
NAVIDROME_KEY = web.AppKey("navidrome_client", NavidromeClient)
PASSTHROUGH_SESSION_KEY = web.AppKey("passthrough_session", aiohttp.ClientSession)
PLAYER_KEY = web.AppKey("player", Player)
JUKEBOX_KEY = web.AppKey("jukebox", Jukebox)


async def passthrough(request: web.Request) -> web.StreamResponse:
    base = request.app[CONFIG_KEY].navidrome_url
    url = URL(base + request.raw_path, encoded=True)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in REQUEST_HOP_HEADERS}
    body = await request.read()
    session = request.app[PASSTHROUGH_SESSION_KEY]
    resp: web.StreamResponse | None = None
    try:
        async with session.request(
            request.method, url, headers=headers, data=body or None, allow_redirects=False
        ) as upstream:
            resp = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for name, value in upstream.headers.items():
                if name.lower() not in RESPONSE_HOP_HEADERS:
                    resp.headers.add(name, value)
            await resp.prepare(request)
            async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except (ConnectionError, aiohttp.ClientError, TimeoutError) as exc:
        if resp is None or not resp.prepared:
            logger.warning(
                "passthrough %s %s failed: %s", request.method, request.path, type(exc).__name__
            )
            return web.Response(status=502, text="upstream error")
        if request.transport is not None:
            request.transport.close()
        return resp


async def _passthrough_session(app: web.Application) -> AsyncIterator[None]:
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
        app[PASSTHROUGH_SESSION_KEY] = session
        yield


REMOTE_DIR = Path(__file__).resolve().parent / "remote"
REMOTE_FILES = ("index.html", "remote.css", "remote.js", "logic.js", "md5.js")
REMOTE_HEADERS = {"Cache-Control": "no-cache"}


def add_remote_routes(app: web.Application) -> None:
    async def redirect(request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/remote/", headers=REMOTE_HEADERS)

    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(REMOTE_DIR / "index.html", headers=REMOTE_HEADERS)

    async def asset(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in REMOTE_FILES:
            return web.Response(status=404, text="not found", headers=REMOTE_HEADERS)
        return web.FileResponse(REMOTE_DIR / name, headers=REMOTE_HEADERS)

    app.router.add_get("/remote", redirect)
    app.router.add_get("/remote/", index)
    app.router.add_get("/remote/{name:.+}", asset)


def build_app(
    config: BridgeConfig, wiim: Any, navidrome: NavidromeClient | None = None
) -> web.Application:
    app = web.Application()
    app[CONFIG_KEY] = config
    if navidrome is not None:
        app[NAVIDROME_KEY] = navidrome
    else:

        async def navidrome_client(app: web.Application) -> AsyncIterator[None]:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                app[NAVIDROME_KEY] = NavidromeClient(
                    session, config.navidrome_url, config.user, config.password, config.salt
                )
                yield

        app.cleanup_ctx.append(navidrome_client)

    async def lookup(song_id: str) -> Mapping[str, Any] | None:
        return await app[NAVIDROME_KEY].get_song(song_id)

    async def authenticate(params: Mapping[str, str]) -> bool:
        return await app[NAVIDROME_KEY].check_caller(params)

    player = Player(
        wiim, lambda sid: stream_url(config, sid), config.queue_name, config.max_volume
    )
    jukebox = Jukebox(player, lookup)
    add_proxy_routes(
        app,
        ProxyConfig(
            navidrome_url=config.navidrome_url,
            user=config.user,
            password=config.password,
            salt=config.salt,
            secret=config.secret,
            log_path=config.log_path,
        ),
        describe=jukebox.describe,
    )
    if config.clear_on_start:

        async def clear_device_queue(app: web.Application) -> None:
            try:
                await player.clear()
            except (aiohttp.ClientError, TimeoutError, ConnectionError) as exc:
                message = str(exc) or type(exc).__name__
                logger.warning("could not clear the device queue at start: %s", message)

        app.on_startup.append(clear_device_queue)
    app[PLAYER_KEY] = player
    app[JUKEBOX_KEY] = jukebox
    add_jukebox_routes(app, jukebox, authenticate)
    app.cleanup_ctx.append(_passthrough_session)
    add_remote_routes(app)

    async def healthz(request: web.Request) -> web.StreamResponse:
        return web.Response(text="ok")

    app.router.add_get("/healthz", healthz)
    app.router.add_route("*", "/{tail:.*}", passthrough)
    return app
