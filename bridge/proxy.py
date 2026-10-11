"""Pass-through FLAC proxy: request handling, logging and app wiring."""

import hashlib
import hmac
import itertools
import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from aiohttp import web

from bridge.subsonic import UPSTREAM_HEADERS, subsonic_params

PROFILES = ("range", "norange")
CHUNK_SIZE = 65536
ERROR_BODY_CHARS = 300
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
_RANGE_RE = re.compile(r"\s*bytes=([0-9]+)-([0-9]*)\s*", re.IGNORECASE)


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


def parse_range_start(header: str | None) -> int | None:
    if header is None:
        return None
    match = _RANGE_RE.fullmatch(header)
    return int(match.group(1)) if match else None


def response_headers(profile: str, upstream: Mapping[str, str]) -> dict[str, str]:
    lowered = {key.lower(): value for key, value in upstream.items()}
    if profile == "range":
        headers = {
            name: lowered[name.lower()] for name in COPIED_HEADERS if name.lower() in lowered
        }
        headers["transferMode.dlna.org"] = "Streaming"
        headers["contentFeatures.dlna.org"] = (
            "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000"
        )
        return headers
    if profile == "norange":
        return {
            "Content-Type": lowered.get("content-type", "audio/flac"),
            "transferMode.dlna.org": "Streaming",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Accept-Ranges": "none",
            "Connection": "close",
            "contentFeatures.dlna.org": (
                "DLNA.ORG_OP=00;DLNA.ORG_FLAGS=01700000000000000000000000000000"
            ),
        }
    raise ValueError(f"unknown profile: {profile!r}")


@dataclass(frozen=True)
class ProxyConfig:
    navidrome_url: str
    user: str
    password: str = field(repr=False)
    salt: str = field(repr=False)
    secret: str = field(repr=False)
    log_path: Path


def utc_iso(timestamp: float) -> str:
    stamp = datetime.fromtimestamp(timestamp, UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RequestLog:
    """Append-only JSON lines file, flushed after every line."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
            handle.flush()


@dataclass(frozen=True)
class RequestInfo:
    req: int
    method: str
    profile: str
    song_id: str
    range_header: str | None
    started: float


@dataclass
class Outcome:
    status: int = 500
    body_bytes: int = 0
    complete: bool = False
    sha256: str | None = None
    resp_headers: dict[str, str] | None = None
    upstream_status: int | None = None
    upstream_content_type: str | None = None
    upstream_body: str | None = None


CONFIG_KEY = web.AppKey("config", ProxyConfig)
LOG_KEY = web.AppKey("log", RequestLog)
COUNTER_KEY = web.AppKey("counter", itertools.count)
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)


def start_event(info: RequestInfo, request: web.Request) -> dict[str, Any]:
    now = time.time()
    return {
        "ev": "start",
        "req": info.req,
        "t": now,
        "iso": utc_iso(now),
        "method": info.method,
        "profile": info.profile,
        "song_id": info.song_id,
        "remote": request.remote,
        "range": info.range_header,
        "range_start": parse_range_start(info.range_header),
        "headers": dict(request.headers),
    }


def end_event(info: RequestInfo, outcome: Outcome) -> dict[str, Any]:
    now = time.time()
    return {
        "ev": "end",
        "req": info.req,
        "t": now,
        "iso": utc_iso(now),
        "status": outcome.status,
        "bytes": outcome.body_bytes,
        "complete": outcome.complete,
        "dur_s": round(time.monotonic() - info.started, 6),
        "sha256": outcome.sha256,
        "resp_headers": outcome.resp_headers,
        "upstream_status": outcome.upstream_status,
        "upstream_content_type": outcome.upstream_content_type,
        "upstream_body": outcome.upstream_body,
    }


def print_end(info: RequestInfo, outcome: Outcome) -> None:
    print(
        f"#{info.req} {info.method} {info.profile} {info.song_id} range={info.range_header}"
        f" status={outcome.status} bytes={outcome.body_bytes} complete={outcome.complete}",
        flush=True,
    )


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
    now = time.time()
    event = {
        "ev": "reject",
        "t": now,
        "iso": utc_iso(now),
        "reason": reason,
        "remote": request.remote,
    }
    request.app[LOG_KEY].write(event)
    print(f"reject reason={reason} remote={request.remote}", flush=True)
    return web.Response(status=404)


async def read_text_prefix(content: aiohttp.StreamReader, chars: int) -> str:
    limit = chars * 4
    buffer = bytearray()
    while len(buffer) < limit:
        chunk = await content.read(limit - len(buffer))
        if not chunk:
            break
        buffer += chunk
    return buffer.decode("utf-8", errors="replace")[:chars]


async def send(
    request: web.Request,
    resp: web.StreamResponse,
    outcome: Outcome,
    chunks: AsyncIterator[bytes] | None,
) -> web.StreamResponse:
    """Prepare resp, copy chunks (None means no body) and record the result in outcome."""
    outcome.status = resp.status
    digest: _Digest | None = hashlib.sha256() if chunks is not None and resp.status == 200 else None
    try:
        await resp.prepare(request)
        outcome.resp_headers = dict(resp.headers)
        if chunks is not None:
            async for chunk in chunks:
                await resp.write(chunk)
                outcome.body_bytes += len(chunk)
                if digest is not None:
                    digest.update(chunk)
        await resp.write_eof()
        outcome.complete = True
        if digest is not None:
            outcome.sha256 = digest.hexdigest()
    except (ConnectionResetError, aiohttp.ClientError, TimeoutError):
        if request.transport is not None:
            request.transport.close()
    return resp


def plain_response(outcome: Outcome, status: int, **kwargs: Any) -> web.Response:
    outcome.status = status
    return web.Response(status=status, **kwargs)


async def relay(
    request: web.Request, upstream: aiohttp.ClientResponse, profile: str, outcome: Outcome
) -> web.StreamResponse:
    status = upstream.status if profile == "range" else 200
    resp = web.StreamResponse(status=status, headers=response_headers(profile, upstream.headers))
    if profile == "norange" and request.version == aiohttp.HttpVersion11:
        resp.enable_chunked_encoding()
    chunks = upstream.content.iter_chunked(CHUNK_SIZE) if request.method == "GET" else None
    return await send(request, resp, outcome, chunks)


async def serve(
    request: web.Request, info: RequestInfo, outcome: Outcome
) -> web.StreamResponse | web.Response:
    config = request.app[CONFIG_KEY]
    if info.profile == "norange" and info.method == "HEAD":
        resp = web.StreamResponse(status=200, headers=response_headers("norange", {}))
        return await send(request, resp, outcome, None)
    params = subsonic_params(config.user, config.password, config.salt)
    params.update(id=info.song_id, format="raw")
    headers = dict(UPSTREAM_HEADERS)
    if info.profile == "range":
        headers |= {
            n: request.headers[n] for n in FORWARDED_REQUEST_HEADERS if n in request.headers
        }
    try:
        async with request.app[SESSION_KEY].request(
            info.method, f"{config.navidrome_url}/rest/stream", params=params, headers=headers
        ) as upstream:
            outcome.upstream_status = upstream.status
            outcome.upstream_content_type = upstream.headers.get("Content-Type")
            content_type = outcome.upstream_content_type or ""
            if upstream.status in (200, 206) and content_type.startswith("audio/"):
                return await relay(request, upstream, info.profile, outcome)
            if upstream.status == 416:
                extra = {}
                if "Content-Range" in upstream.headers:
                    extra = {"Content-Range": upstream.headers["Content-Range"]}
                return plain_response(outcome, 416, headers=extra)
            outcome.upstream_body = await read_text_prefix(upstream.content, ERROR_BODY_CHARS)
            return plain_response(outcome, 502, text="upstream error")
    except (aiohttp.ClientError, TimeoutError):
        return plain_response(outcome, 502, text="upstream error")


async def handle_stream(request: web.Request) -> web.StreamResponse | web.Response:
    config = request.app[CONFIG_KEY]
    reason = reject_reason(config, request.match_info)
    if reason is not None:
        return reject(request, reason)
    log = request.app[LOG_KEY]
    info = RequestInfo(
        req=next(request.app[COUNTER_KEY]),
        method=request.method,
        profile=request.match_info["profile"],
        song_id=request.match_info["name"].removesuffix(".flac"),
        range_header=request.headers.get("Range"),
        started=time.monotonic(),
    )
    log.write(start_event(info, request))
    outcome = Outcome()
    try:
        return await serve(request, info, outcome)
    finally:
        log.write(end_event(info, outcome))
        print_end(info, outcome)


async def upstream_session(app: web.Application) -> AsyncIterator[None]:
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
        app[SESSION_KEY] = session
        yield


def add_proxy_routes(app: web.Application, config: ProxyConfig) -> None:
    app[CONFIG_KEY] = config
    app[LOG_KEY] = RequestLog(config.log_path)
    app[COUNTER_KEY] = itertools.count(1)
    app.cleanup_ctx.append(upstream_session)
    app.router.add_get("/s/{secret}/{profile}/{name}", handle_stream)


def create_app(config: ProxyConfig) -> web.Application:
    app = web.Application()
    add_proxy_routes(app, config)
    return app
