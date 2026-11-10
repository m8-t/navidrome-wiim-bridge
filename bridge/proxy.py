"""Pass-through FLAC proxy: request handling, logging and app wiring."""

import hmac
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp
from aiohttp import web

from bridge.log import clean, human_size
from bridge.subsonic import UPSTREAM_HEADERS, subsonic_params

logger = logging.getLogger("bridge.proxy")

PROFILES = ("range",)
CHUNK_SIZE = 65536
COPIED_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "Last-Modified",
    "ETag",
)
FORWARDED_REQUEST_HEADERS = ("Range", "If-Range")
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def response_headers(upstream: Mapping[str, str]) -> dict[str, str]:
    lowered = {key.lower(): value for key, value in upstream.items()}
    headers = {name: lowered[name.lower()] for name in COPIED_HEADERS if name.lower() in lowered}
    headers["transferMode.dlna.org"] = "Streaming"
    headers["contentFeatures.dlna.org"] = (
        "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000"
    )
    return headers


@dataclass(frozen=True)
class ProxyConfig:
    navidrome_url: str
    user: str
    password: str = field(repr=False)
    salt: str = field(repr=False)
    secret: str = field(repr=False)


def utc_iso(timestamp: float) -> str:
    stamp = datetime.fromtimestamp(timestamp, UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RequestInfo:
    method: str
    song_id: str
    range_header: str | None


@dataclass
class Outcome:
    status: int = 500
    body_bytes: int = 0
    complete: bool = False
    upstream_status: int | None = None


CONFIG_KEY = web.AppKey("config", ProxyConfig)
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
Describe = Callable[[str], tuple[str, str] | None]
DESCRIBE_KEY = web.AppKey("describe", Callable)


def song_ref(app: web.Application, song_id: str) -> tuple[str, str]:
    described = app[DESCRIBE_KEY](song_id)
    if described is None:
        return f"song {song_id}", ""
    label, album = described
    return f'"{label}"', album


def log_start(app: web.Application, info: RequestInfo) -> str:
    ref, album = song_ref(app, info.song_id)
    text = f"device fetches {ref}"
    if album:
        text += f' from "{album}"'
    if info.range_header is not None and info.range_header != "bytes=0-":
        text += f" (range {clean(info.range_header.removeprefix('bytes='))})"
    logger.info("%s", text)
    return ref


def log_end(ref: str, outcome: Outcome) -> None:
    if not 200 <= outcome.status < 300:
        failed = outcome.upstream_status
        if failed is None or 200 <= failed < 300:
            failed = outcome.status
        logger.warning("could not send %s: upstream answered %d", ref, failed)
        return
    state = "complete" if outcome.complete else "aborted"
    logger.info("sent %s: %s, %s", ref, human_size(outcome.body_bytes), state)


def reject_reason(config: ProxyConfig, match: Mapping[str, str]) -> str | None:
    if not hmac.compare_digest(match["secret"].encode(), config.secret.encode()):
        return "secret"
    if match["profile"] not in PROFILES:
        return "profile"
    name = match["name"]
    if not name.endswith(".flac") or not _NAME_RE.fullmatch(name.removesuffix(".flac")):
        return "name"
    return None


def reject(request: web.Request, reason: str) -> web.Response:
    logger.warning("reject reason=%s remote=%s", reason, request.remote)
    return web.Response(status=404)


async def send(
    request: web.Request,
    resp: web.StreamResponse,
    outcome: Outcome,
    chunks: AsyncIterator[bytes] | None,
) -> web.StreamResponse:
    """Prepare resp, copy chunks (None means no body) and record the result in outcome."""
    outcome.status = resp.status
    try:
        await resp.prepare(request)
        if chunks is not None:
            async for chunk in chunks:
                await resp.write(chunk)
                outcome.body_bytes += len(chunk)
        await resp.write_eof()
        outcome.complete = True
    except (ConnectionError, aiohttp.ClientError, TimeoutError):
        if request.transport is not None:
            request.transport.close()
    return resp


def plain_response(outcome: Outcome, status: int, **kwargs: Any) -> web.Response:
    outcome.status = status
    return web.Response(status=status, **kwargs)


async def relay(
    request: web.Request, upstream: aiohttp.ClientResponse, outcome: Outcome
) -> web.StreamResponse:
    resp = web.StreamResponse(status=upstream.status, headers=response_headers(upstream.headers))
    chunks = upstream.content.iter_chunked(CHUNK_SIZE) if request.method == "GET" else None
    return await send(request, resp, outcome, chunks)


async def serve(
    request: web.Request, info: RequestInfo, outcome: Outcome
) -> web.StreamResponse | web.Response:
    config = request.app[CONFIG_KEY]
    params = subsonic_params(config.user, config.password, config.salt)
    params.update(id=info.song_id, format="raw")
    headers = dict(UPSTREAM_HEADERS)
    headers |= {n: request.headers[n] for n in FORWARDED_REQUEST_HEADERS if n in request.headers}
    try:
        async with request.app[SESSION_KEY].request(
            info.method, f"{config.navidrome_url}/rest/stream", params=params, headers=headers
        ) as upstream:
            outcome.upstream_status = upstream.status
            content_type = upstream.headers.get("Content-Type") or ""
            if upstream.status in (200, 206) and content_type.startswith("audio/"):
                return await relay(request, upstream, outcome)
            if upstream.status == 416:
                extra = {}
                if "Content-Range" in upstream.headers:
                    extra = {"Content-Range": upstream.headers["Content-Range"]}
                return plain_response(outcome, 416, headers=extra)
            return plain_response(outcome, 502, text="upstream error")
    except (aiohttp.ClientError, TimeoutError):
        return plain_response(outcome, 502, text="upstream error")


async def handle_stream(request: web.Request) -> web.StreamResponse | web.Response:
    config = request.app[CONFIG_KEY]
    reason = reject_reason(config, request.match_info)
    if reason is not None:
        return reject(request, reason)
    info = RequestInfo(
        method=request.method,
        song_id=request.match_info["name"].removesuffix(".flac"),
        range_header=request.headers.get("Range"),
    )
    ref = log_start(request.app, info)
    outcome = Outcome()
    try:
        return await serve(request, info, outcome)
    finally:
        log_end(ref, outcome)


async def upstream_session(app: web.Application) -> AsyncIterator[None]:
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
        app[SESSION_KEY] = session
        yield


def add_proxy_routes(
    app: web.Application, config: ProxyConfig, *, describe: Describe | None = None
) -> None:
    app[CONFIG_KEY] = config
    app[DESCRIBE_KEY] = describe or (lambda song_id: None)
    app.cleanup_ctx.append(upstream_session)
    app.router.add_get("/s/{secret}/{profile}/{name}", handle_stream, name="stream")


def create_app(config: ProxyConfig) -> web.Application:
    app = web.Application()
    add_proxy_routes(app, config)
    return app
