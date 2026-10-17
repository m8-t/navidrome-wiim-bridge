"""Combined bridge service: jukebox endpoint, audio proxy and Navidrome pass-through."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.typedefs import Handler, Middleware
from yarl import URL

from bridge.jukebox import Jukebox, add_jukebox_routes
from bridge.player import Player
from bridge.proxy import ProxyConfig, add_proxy_routes
from bridge.scrobble import POLL_INTERVAL_S, Scrobbler, watch
from bridge.subsonic import UPSTREAM_HEADERS, AuthResult, subsonic_params

logger = logging.getLogger("bridge.app")

AUTH_PARAMS = ("u", "p", "t", "s", "apiKey")
AUTH_CACHE_SECONDS = 60
AUTH_STALE_SECONDS = 3600
AUTH_CACHE_MAX = 256
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
REQUEST_DROPPED_HEADERS = REQUEST_HOP_HEADERS | {"remote-user"}
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
    scrobble: bool = False
    device_port: int | None = None


def stream_url(config: BridgeConfig, song_id: str) -> str:
    port = config.port if config.device_port is None else config.device_port
    return (
        f"http://{config.advertise_host}:{port}"
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
        self._reachable = True

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

    async def scrobble(self, song_id: str, time_ms: int, submission: bool) -> bool:
        params = subsonic_params(self._user, self._password, self._salt)
        params |= {
            "id": song_id,
            "time": str(time_ms),
            "submission": "true" if submission else "false",
        }
        body = await self._get_json("/rest/scrobble", params)
        return body is not None and body.get("status") == "ok"

    async def check_caller(self, params: Mapping[str, str]) -> AuthResult:
        if "u" not in params and "apiKey" not in params:
            return AuthResult.REJECTED
        key = tuple(params.get(name) for name in AUTH_PARAMS)
        if self._age(key) < AUTH_CACHE_SECONDS:
            return AuthResult.OK
        query = {name: params[name] for name in AUTH_PARAMS if name in params}
        query |= {"v": "1.16.1", "c": "wiim-bridge-auth", "f": "json"}
        try:
            body = await self._get_json("/rest/ping", query)
        except (aiohttp.ClientError, TimeoutError) as exc:
            return self._no_answer(key, type(exc).__name__)
        status = body.get("status") if body is not None else None
        if status == "ok":
            self._answered()
            self._remember(key)
            return AuthResult.OK
        if status == "failed":
            self._answered()
            self._verified.pop(key, None)
            return AuthResult.REJECTED
        return self._no_answer(key, "unusable reply")

    def _age(self, key: tuple[str | None, ...]) -> float:
        verified_at = self._verified.get(key)
        return float("inf") if verified_at is None else self._clock() - verified_at

    def _remember(self, key: tuple[str | None, ...]) -> None:
        now = self._clock()
        self._verified.pop(key, None)
        self._verified[key] = now
        for stale in [k for k, at in self._verified.items() if now - at >= AUTH_STALE_SECONDS]:
            del self._verified[stale]
        while len(self._verified) > AUTH_CACHE_MAX:
            del self._verified[next(iter(self._verified))]

    def _no_answer(self, key: tuple[str | None, ...], reason: str) -> AuthResult:
        if self._reachable:
            self._reachable = False
            logger.warning("navidrome is unreachable: %s", reason)
        if self._age(key) < AUTH_STALE_SECONDS:
            return AuthResult.OK
        return AuthResult.UNREACHABLE

    def _answered(self) -> None:
        if not self._reachable:
            self._reachable = True
            logger.info("navidrome answers again")


CONFIG_KEY = web.AppKey("bridge_config", BridgeConfig)
NAVIDROME_KEY = web.AppKey("navidrome_client", NavidromeClient)
PASSTHROUGH_SESSION_KEY = web.AppKey("passthrough_session", aiohttp.ClientSession)
PLAYER_KEY = web.AppKey("player", Player)
JUKEBOX_KEY = web.AppKey("jukebox", Jukebox)


async def passthrough(request: web.Request) -> web.StreamResponse:
    target = request.raw_path
    if not target.startswith("/") or target.startswith("//"):
        return web.Response(status=400, text="bad request target")
    base = request.app[CONFIG_KEY].navidrome_url
    url = URL(base + target, encoded=True)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in REQUEST_DROPPED_HEADERS}
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


DEVICE_ROUTES = frozenset({"stream", "healthz"})


def device_port_only(device_port: int) -> Middleware:
    """Answer 404 on the device listener for everything but the audio and health routes."""

    @web.middleware
    async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
        transport = request.transport
        sockname = transport.get_extra_info("sockname") if transport is not None else None
        local_port = sockname[1] if isinstance(sockname, tuple) else None
        on_device_port = local_port is None or local_port == device_port
        if on_device_port and request.match_info.route.name not in DEVICE_ROUTES:
            return web.Response(status=404)
        return await handler(request)

    return guard


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
    middlewares = [] if config.device_port is None else [device_port_only(config.device_port)]
    app = web.Application(middlewares=middlewares)
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

    async def authenticate(params: Mapping[str, str]) -> AuthResult:
        return await app[NAVIDROME_KEY].check_caller(params)

    player = Player(
        wiim, lambda sid: stream_url(config, sid), config.queue_name, config.max_volume
    )
    scrobbler = None
    if config.scrobble:

        async def send_scrobble(song_id: str, time_ms: int, submission: bool) -> bool:
            return await app[NAVIDROME_KEY].scrobble(song_id, time_ms, submission)

        scrobbler = Scrobbler(send_scrobble)
    jukebox = Jukebox(player, lookup, scrobbler=scrobbler)
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
    if scrobbler is not None:

        async def scrobbling(app: web.Application) -> AsyncIterator[None]:
            tasks = [
                asyncio.create_task(scrobbler.run()),
                asyncio.create_task(watch(lambda: jukebox.poll(POLL_INTERVAL_S), POLL_INTERVAL_S)),
            ]
            yield
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        app.cleanup_ctx.append(scrobbling)
    add_remote_routes(app)

    async def healthz(request: web.Request) -> web.StreamResponse:
        return web.Response(text="ok")

    app.router.add_get("/healthz", healthz, name="healthz")
    app.router.add_route("*", "/{tail:.*}", passthrough)
    return app
