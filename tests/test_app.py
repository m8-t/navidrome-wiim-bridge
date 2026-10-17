import asyncio
import dataclasses
import gzip
import json
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import bridge_server
import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_player import FakeWiim

from bridge import log as bridge_log
from bridge import startup
from bridge.log import parse_level, setup_logging
from bridge.subsonic import AuthResult, ConfigError
from bridge.wiim import device_urls
from bridge.proxy import RequestLog
from bridge.app import (
    AUTH_CACHE_MAX,
    AUTH_CACHE_SECONDS,
    AUTH_STALE_SECONDS,
    JUKEBOX_KEY,
    PLAYER_KEY,
    REMOTE_FILES,
    BridgeConfig,
    NavidromeClient,
    build_app,
    stream_url,
)

@pytest.fixture(autouse=True)
def restore_logging() -> Any:
    root = logging.getLogger()
    access = logging.getLogger("aiohttp.access")
    saved = (list(root.handlers), root.level, access.level, bridge_log._handler)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    access.setLevel(saved[2])
    bridge_log._handler = saved[3]


PAYLOAD = bytes(i % 251 for i in range(5000))
GZ_BODY = gzip.compress(b"hello gzip " * 50)


def song(song_id: str) -> dict[str, Any]:
    return {
        "id": song_id,
        "title": song_id.upper(),
        "artist": "Art",
        "album": "Alb",
        "duration": 60,
    }


class FakeNavidrome:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.down = False
        self.revoked = False
        self.ping_reply: Any = None
        self.app = web.Application()
        for path in ("/rest/ping", "/rest/ping.view"):
            self.app.router.add_get(path, self.ping)
        self.app.router.add_get("/rest/getSong", self.get_song)
        self.app.router.add_get("/rest/scrobble", self.scrobble)
        self.app.router.add_get("/rest/stream", self.stream)
        self.app.router.add_route("*", "/api/thing", self.thing)
        self.app.router.add_get("/echo/{tail:.*}", self.echo)
        self.app.router.add_get("/gz", self.gz)

    async def _record(self, request: web.Request) -> bytes:
        body = await request.read()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "raw_path": request.raw_path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        return body

    def count(self, path: str) -> int:
        return len([r for r in self.requests if r["path"] == path])

    @staticmethod
    def envelope(status: str, **extra: Any) -> dict[str, Any]:
        return {"subsonic-response": {"status": status, "version": "1.16.1", **extra}}

    @staticmethod
    def outage() -> web.Response:
        return web.Response(status=503, text="<html>bad gateway</html>", content_type="text/html")

    async def ping(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.down:
            return self.outage()
        if self.ping_reply is not None:
            return web.json_response(self.ping_reply)
        q = request.query
        good = (q.get("u") == "dan" and q.get("p") == "pw") or q.get("t") == "good"
        return web.json_response(self.envelope("ok" if good and not self.revoked else "failed"))

    async def get_song(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.down:
            return self.outage()
        song_id = request.query.get("id", "")
        if song_id == "junk":
            return web.Response(text="not json")
        if song_id in ("a", "b"):
            return web.json_response(self.envelope("ok", song=song(song_id)))
        return web.json_response(self.envelope("failed"))

    async def scrobble(self, request: web.Request) -> web.Response:
        await self._record(request)
        good = request.query.get("id") != "bad"
        return web.json_response(self.envelope("ok" if good else "failed"))

    async def stream(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.Response(body=PAYLOAD, content_type="audio/flac")

    async def thing(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        reply = {"method": request.method, "body": body.decode()}
        return web.json_response(reply, status=201, headers={"X-Test": "1"})

    async def echo(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.Response(text="echo")

    async def gz(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.Response(
            body=GZ_BODY, headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"}
        )


def run(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guarded())


def make_config(navidrome_url: str, tmp_path: Path) -> BridgeConfig:
    return BridgeConfig(
        navidrome_url=navidrome_url,
        user="dan",
        password="pw",
        salt="abcd",
        secret="SECRET",
        advertise_host="10.0.0.5",
        port=9001,
        log_path=tmp_path / "log.jsonl",
        device_base="http://device.invalid",
        status_url="http://device.invalid/status",
    )


def with_bridge(
    tmp_path: Path,
    scenario: Callable[[TestClient[Any, Any], FakeNavidrome, FakeWiim], Awaitable[None]],
    **client_kwargs: Any,
) -> FakeWiim:
    wiim = FakeWiim()

    async def go() -> None:
        fake = FakeNavidrome()
        async with TestServer(fake.app) as server:
            config = make_config(str(server.make_url("")).rstrip("/"), tmp_path)
            app = build_app(config, wiim)
            async with TestClient(TestServer(app), **client_kwargs) as client:
                await scenario(client, fake, wiim)

    run(go())
    return wiim


def with_client(
    scenario: Callable[[NavidromeClient, FakeNavidrome], Awaitable[None]],
    clock: Callable[[], float] = lambda: 0.0,
) -> None:
    async def go() -> None:
        fake = FakeNavidrome()
        async with TestServer(fake.app) as server, aiohttp.ClientSession() as session:
            client = NavidromeClient(
                session, str(server.make_url("")).rstrip("/"), "dan", "pw", "abcd", clock
            )
            await scenario(client, fake)

    run(go())


def test_stream_url(tmp_path: Path) -> None:
    config = make_config("http://n", tmp_path)
    assert stream_url(config, "a") == "http://10.0.0.5:9001/s/SECRET/range/a.flac"


def test_stream_url_uses_the_device_port_when_there_is_one(tmp_path: Path) -> None:
    config = dataclasses.replace(make_config("http://n", tmp_path), device_port=9002)
    assert stream_url(config, "a") == "http://10.0.0.5:9002/s/SECRET/range/a.flac"


def test_get_song() -> None:
    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        assert await client.get_song("a") == song("a")
        assert await client.get_song("zzz") is None
        assert await client.get_song("junk") is None
        first = fake.requests[0]
        assert first["query"]["u"] == "dan"
        assert first["query"]["id"] == "a"
        assert first["query"]["s"] == "abcd"
        assert first["headers"]["Accept-Encoding"] == "identity"

    with_client(scenario)


def test_scrobble_sends_id_time_and_submission() -> None:
    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        assert await client.scrobble("a", 1700000000123, True) is True
        assert await client.scrobble("a", 1700000000456, False) is True
        assert await client.scrobble("bad", 1, True) is False
        first, second, _ = fake.requests
        assert first["path"] == "/rest/scrobble"
        assert first["query"]["id"] == "a"
        assert first["query"]["time"] == "1700000000123"
        assert first["query"]["submission"] == "true"
        assert first["query"]["u"] == "dan"
        assert first["query"]["s"] == "abcd"
        assert second["query"]["submission"] == "false"

    with_client(scenario)


def test_check_caller_and_cache() -> None:
    now = [0.0]
    good = {"u": "dan", "p": "pw"}

    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        assert await client.check_caller(good) is AuthResult.OK
        assert fake.count("/rest/ping") == 1
        assert fake.requests[0]["query"]["c"] == "wiim-bridge-auth"
        assert await client.check_caller(good) is AuthResult.OK
        assert fake.count("/rest/ping") == 1
        now[0] = 61.0
        assert await client.check_caller(good) is AuthResult.OK
        assert fake.count("/rest/ping") == 2
        assert await client.check_caller({"u": "dan", "p": "bad"}) is AuthResult.REJECTED
        assert await client.check_caller({"u": "dan", "p": "bad"}) is AuthResult.REJECTED
        assert fake.count("/rest/ping") == 4
        before = len(fake.requests)
        assert await client.check_caller({}) is AuthResult.REJECTED
        assert len(fake.requests) == before

    with_client(scenario, clock=lambda: now[0])


async def dead_url() -> str:
    async with TestServer(web.Application()) as server:
        return str(server.make_url("")).rstrip("/")


def test_check_caller_rejection_forgets_the_cached_login() -> None:
    now = [0.0]
    good = {"u": "dan", "p": "pw"}

    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        assert await client.check_caller(good) is AuthResult.OK
        fake.revoked = True
        now[0] = AUTH_CACHE_SECONDS
        assert await client.check_caller(good) is AuthResult.REJECTED
        assert client._verified == {}
        fake.down = True
        assert await client.check_caller(good) is AuthResult.UNREACHABLE

    with_client(scenario, clock=lambda: now[0])


def test_check_caller_without_history_is_unreachable_not_rejected() -> None:
    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        fake.down = True
        assert await client.check_caller({"u": "dan", "p": "pw"}) is AuthResult.UNREACHABLE
        assert await client.check_caller({"u": "dan", "p": "bad"}) is AuthResult.UNREACHABLE
        assert client._verified == {}

    with_client(scenario)


@pytest.mark.parametrize(
    "reply",
    [
        {"subsonic-response": {"status": "weird"}},
        {"subsonic-response": {}},
        {"subsonic-response": "ok"},
        {"other": 1},
        [],
    ],
)
def test_check_caller_reply_without_a_verdict_is_unreachable(reply: Any) -> None:
    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        fake.ping_reply = reply
        assert await client.check_caller({"u": "dan", "p": "pw"}) is AuthResult.UNREACHABLE
        assert client._verified == {}

    with_client(scenario)


def test_check_caller_connection_failure_is_unreachable() -> None:
    async def go() -> None:
        url = await dead_url()
        async with aiohttp.ClientSession() as session:
            client = NavidromeClient(session, url, "dan", "pw", "abcd")
            result = await client.check_caller({"u": "dan", "p": "pw"})
            assert result is AuthResult.UNREACHABLE

    run(go())


def test_check_caller_serves_a_known_login_for_an_hour_while_navidrome_is_down() -> None:
    now = [0.0]
    good = {"u": "dan", "p": "pw"}
    stranger = {"u": "dan", "t": "good", "s": "other"}

    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        assert await client.check_caller(good) is AuthResult.OK
        fake.down = True
        now[0] = AUTH_CACHE_SECONDS
        pings = fake.count("/rest/ping")
        assert await client.check_caller(good) is AuthResult.OK
        assert fake.count("/rest/ping") == pings + 1
        assert await client.check_caller(stranger) is AuthResult.UNREACHABLE
        now[0] = AUTH_STALE_SECONDS - 1
        assert await client.check_caller(good) is AuthResult.OK
        now[0] = AUTH_STALE_SECONDS
        assert await client.check_caller(good) is AuthResult.UNREACHABLE
        fake.down = False
        assert await client.check_caller(good) is AuthResult.OK
        assert await client.check_caller(stranger) is AuthResult.OK

    with_client(scenario, clock=lambda: now[0])


def test_check_caller_connection_failure_also_serves_a_known_login() -> None:
    now = [0.0]
    good = {"u": "dan", "p": "pw"}

    async def go() -> None:
        fake = FakeNavidrome()
        async with aiohttp.ClientSession() as session:
            async with TestServer(fake.app) as server:
                url = str(server.make_url("")).rstrip("/")
                client = NavidromeClient(session, url, "dan", "pw", "abcd", lambda: now[0])
                assert await client.check_caller(good) is AuthResult.OK
            now[0] = AUTH_CACHE_SECONDS + 1
            assert await client.check_caller(good) is AuthResult.OK
            assert await client.check_caller({"u": "dan", "p": "bad"}) is AuthResult.UNREACHABLE

    run(go())


def test_check_caller_cache_is_bounded() -> None:
    now = [0.0]

    def login(n: int) -> dict[str, str]:
        return {"u": "dan", "t": "good", "s": f"salt{n}"}

    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        for n in range(AUTH_CACHE_MAX + 5):
            now[0] += 0.001
            assert await client.check_caller(login(n)) is AuthResult.OK
        assert len(client._verified) == AUTH_CACHE_MAX
        pings = fake.count("/rest/ping")
        assert await client.check_caller(login(AUTH_CACHE_MAX + 4)) is AuthResult.OK
        assert fake.count("/rest/ping") == pings
        assert await client.check_caller(login(0)) is AuthResult.OK
        assert fake.count("/rest/ping") == pings + 1
        assert len(client._verified) == AUTH_CACHE_MAX
        now[0] += AUTH_STALE_SECONDS
        assert await client.check_caller(login(1)) is AuthResult.OK
        assert len(client._verified) == 1

    with_client(scenario, clock=lambda: now[0])


def test_check_caller_logs_navidrome_availability_on_changes_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        for down in (False, True, True, False, False):
            fake.down = down
            await client.check_caller({"u": "dan", "t": "good", "s": f"s{len(fake.requests)}"})

    with_client(scenario)
    assert [(r.levelno, r.getMessage()) for r in records_of(caplog, "bridge.app")] == [
        (logging.WARNING, "navidrome is unreachable: unusable reply"),
        (logging.INFO, "navidrome answers again"),
    ]


def test_check_caller_connection_failure_is_logged_without_the_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def go() -> None:
        url = await dead_url()
        async with aiohttp.ClientSession() as session:
            client = NavidromeClient(session, url, "dan", "pw-secret", "abcd")
            await client.check_caller({"u": "dan", "p": "pw-secret"})

    run(go())
    (record,) = records_of(caplog, "bridge.app")
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("navidrome is unreachable: ")
    assert "pw-secret" not in caplog.text


def test_jukebox_start_sequence_through_app(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        base = "/rest/jukeboxControl.view"
        for query in (
            "action=set&id=a&id=b&u=dan&p=pw",
            "action=skip&index=0&offset=0&u=dan&p=pw",
            "action=start&u=dan&p=pw",
        ):
            resp = await client.get(f"{base}?{query}")
            body = (await resp.json())["subsonic-response"]
            assert body["status"] == "ok"
            assert "jukeboxStatus" in body

    wiim = with_bridge(tmp_path, scenario)
    creates = [args for name, args in wiim.calls if name == "queue_create"]
    assert len(creates) == 1
    context = creates[0][0]
    config = make_config("http://n", tmp_path)
    first, second = stream_url(config, "a"), stream_url(config, "b")
    assert 0 <= context.index(first) < context.index(second)


def test_jukebox_bad_credentials(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get("/rest/jukeboxControl?action=set&id=a&u=dan&p=wrong")
        body = (await resp.json())["subsonic-response"]
        assert body["status"] == "failed"
        assert body["error"]["code"] == 40
        assert fake.count("/rest/getSong") == 0

    with_bridge(tmp_path, scenario)


def test_jukebox_navidrome_down_is_not_a_wrong_login(tmp_path: Path) -> None:
    async def go() -> None:
        app = build_app(make_config(await dead_url(), tmp_path), FakeWiim())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/rest/jukeboxControl?action=status&u=dan&p=pw")
            body = (await resp.json())["subsonic-response"]
            assert body["status"] == "failed"
            assert body["error"] == {
                "code": 0,
                "message": "Navidrome is unreachable, try again shortly",
            }

    run(go())


def test_jukebox_keeps_a_known_login_through_a_navidrome_outage(tmp_path: Path) -> None:
    now = [0.0]

    async def go() -> None:
        fake = FakeNavidrome()
        async with aiohttp.ClientSession() as session, TestServer(fake.app) as server:
            url = str(server.make_url("")).rstrip("/")
            navidrome = NavidromeClient(session, url, "dan", "pw", "abcd", lambda: now[0])
            app = build_app(make_config(url, tmp_path), FakeWiim(), navidrome)
            async with TestClient(TestServer(app)) as client:

                async def status(login: str) -> dict[str, Any]:
                    resp = await client.get(f"/rest/jukeboxControl?action=status&{login}")
                    return (await resp.json())["subsonic-response"]  # type: ignore[no-any-return]

                assert (await status("u=dan&p=pw"))["status"] == "ok"
                fake.down = True
                now[0] = AUTH_CACHE_SECONDS + 1
                assert (await status("u=dan&p=pw"))["status"] == "ok"
                assert (await status("u=dan&t=good&s=x"))["error"]["code"] == 0
                fake.down = False
                assert (await status("u=dan&p=bad"))["error"]["code"] == 40

    run(go())


def test_proxy_route_still_works(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get("/s/SECRET/range/a.flac")
        assert resp.status == 200
        assert await resp.read() == PAYLOAD

    with_bridge(tmp_path, scenario)


def test_passthrough_forwards_request_and_response(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.post("/api/thing?x=1", json={"k": "v"})
        assert resp.status == 201
        assert resp.headers["X-Test"] == "1"
        assert (await resp.json()) == {"method": "POST", "body": '{"k": "v"}'}
        seen = fake.requests[-1]
        assert (seen["method"], seen["path"], seen["query"]) == ("POST", "/api/thing", {"x": "1"})
        assert json.loads(seen["body"]) == {"k": "v"}

        resp = await client.get("/rest/ping.view?u=dan&p=pw")
        assert (await resp.json())["subsonic-response"]["status"] == "ok"
        assert fake.requests[-1]["path"] == "/rest/ping.view"
        assert [name for name, _ in wiim.calls] == ["stop", "queue_delete"]

    with_bridge(tmp_path, scenario)


def test_passthrough_keeps_compressed_bytes(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get("/gz")
        assert resp.status == 200
        assert resp.headers["Content-Encoding"] == "gzip"
        assert await resp.read() == GZ_BODY

    with_bridge(tmp_path, scenario, auto_decompress=False)


async def raw_exchange(port: int, request_line: str, *headers: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [request_line, "Host: bridge", "Connection: close", *headers, "", ""]
    writer.write("\r\n".join(lines).encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


@pytest.mark.parametrize(
    "target",
    [
        "/echo/x?id=1&id=2&id=3",
        "/echo/x?q=a+b&plus=%2B&space=%20&slash=%2F&pct=%25&mixed=%2f%2F%5b",
        "/echo/a%20b/c%2Fd/e%25f?id=a&id=b",
        "/echo/x?empty=&flag&=v&a=1&a=2&a=3",
        "/echo//double/slash?x=//y",
    ],
)
def test_passthrough_sends_path_and_query_byte_for_byte(tmp_path: Path, target: str) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        reply = await raw_exchange(client.server.port, f"GET {target} HTTP/1.1")
        assert reply.startswith(b"HTTP/1.1 200")
        assert [r["raw_path"] for r in fake.requests] == [target]

    with_bridge(tmp_path, scenario)


def test_passthrough_client_request_keeps_repeated_ids_and_encoding(tmp_path: Path) -> None:
    query = "id=1&id=2&q=a+b&plus=%2B&s=%20z&pct=%25"

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get(f"/echo/x?{query}")
        assert resp.status == 200
        assert fake.requests[-1]["raw_path"] == f"/echo/x?{query}"
        assert fake.requests[-1]["query"]["q"] == "a b"

    with_bridge(tmp_path, scenario)


@pytest.mark.parametrize(
    "target",
    [
        "http://evil.example/echo/x?u=dan",
        "HTTP://evil.example/rest/ping",
        "//evil.example/echo/x",
        "//x",
        "///",
    ],
)
def test_passthrough_rejects_targets_that_are_not_a_path(tmp_path: Path, target: str) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        reply = await raw_exchange(client.server.port, f"GET {target} HTTP/1.1")
        head, _, body = reply.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 400"), head
        assert body == b"bad request target"
        assert fake.requests == []

    with_bridge(tmp_path, scenario)


@pytest.mark.parametrize("target", ["http://evil.example", "*"])
def test_targets_the_router_cannot_match_never_reach_navidrome(tmp_path: Path, target: str) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        reply = await raw_exchange(client.server.port, f"GET {target} HTTP/1.1")
        assert reply.startswith(b"HTTP/1.1 404")
        assert fake.requests == []

    with_bridge(tmp_path, scenario)


def test_passthrough_does_not_forward_remote_user(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        headers = {
            "Remote-User": "admin",
            "x-nd-authorization": "Bearer t",
            "Authorization": "Basic eDp5",
            "X-Other": "kept",
        }
        resp = await client.get("/api/thing", headers=headers)
        assert resp.status == 201
        reply = await raw_exchange(
            client.server.port, "GET /api/thing HTTP/1.1", "REMOTE-USER: admin", "remote-user: x"
        )
        assert reply.startswith(b"HTTP/1.1 201")
        assert len(fake.requests) == 2
        for seen in fake.requests:
            assert "remote-user" not in {name.lower() for name in seen["headers"]}
        first = {name.lower(): value for name, value in fake.requests[0]["headers"].items()}
        assert first["x-nd-authorization"] == "Bearer t"
        assert first["authorization"] == "Basic eDp5"
        assert first["x-other"] == "kept"

    with_bridge(tmp_path, scenario)


def test_passthrough_connection_lost_during_write_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing_write(self: web.StreamResponse, data: bytes) -> None:
        raise ConnectionError("Connection lost")

    monkeypatch.setattr(web.StreamResponse, "write", failing_write)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        with pytest.raises(aiohttp.ClientError):
            resp = await client.get("/rest/stream?id=a")
            await resp.read()
        await asyncio.sleep(0.2)

    with_bridge(tmp_path, scenario)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_passthrough_upstream_unreachable(tmp_path: Path) -> None:
    async def go() -> None:
        app = build_app(make_config("http://127.0.0.1:1", tmp_path), FakeWiim())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/thing")
            assert resp.status == 502
            assert await resp.text() == "upstream error"

    run(go())


@dataclasses.dataclass
class TwoPorts:
    main_port: int
    device_port: int
    fake: FakeNavidrome
    wiim: FakeWiim

    @property
    def main(self) -> str:
        return f"http://127.0.0.1:{self.main_port}"

    @property
    def device(self) -> str:
        return f"http://127.0.0.1:{self.device_port}"


@asynccontextmanager
async def two_ports(tmp_path: Path) -> AsyncIterator[TwoPorts]:
    """Serve the bridge on a main and a device socket, the way run_app does."""
    fake, wiim = FakeNavidrome(), FakeWiim()
    main_sock = socket.create_server(("127.0.0.1", 0))
    device_sock = socket.create_server(("127.0.0.1", 0))
    ports = TwoPorts(main_sock.getsockname()[1], device_sock.getsockname()[1], fake, wiim)
    async with TestServer(fake.app) as navidrome:
        base = make_config(str(navidrome.make_url("")).rstrip("/"), tmp_path)
        config = dataclasses.replace(base, port=ports.main_port, device_port=ports.device_port)
        runner = web.AppRunner(build_app(config, wiim))
        await runner.setup()
        try:
            for sock in (main_sock, device_sock):
                await web.SockSite(runner, sock).start()
            yield ports
        finally:
            await runner.cleanup()


DEVICE_PORT_BLOCKED = [
    ("GET", "/rest/ping?u=dan&p=pw"),
    ("GET", "/rest/ping.view?u=dan&p=pw"),
    ("GET", "/rest/jukeboxControl?action=status&u=dan&p=pw"),
    ("POST", "/rest/jukeboxControl?action=status&u=dan&p=pw"),
    ("OPTIONS", "/rest/jukeboxControl"),
    ("GET", "/rest/getSong?id=a&u=dan&p=pw"),
    ("GET", "/rest/stream?id=a&u=dan&p=pw"),
    ("GET", "/remote"),
    ("GET", "/remote/"),
    ("GET", "/remote/remote.js"),
    ("GET", "/api/thing"),
    ("POST", "/api/thing"),
    ("DELETE", "/api/thing"),
    ("GET", "/"),
    ("GET", "/s"),
    ("GET", "/s/SECRET/range"),
    ("POST", "/s/SECRET/range/a.flac"),
    ("GET", "/healthz/extra"),
]


def test_the_device_port_serves_only_audio_and_health(tmp_path: Path) -> None:
    async def go() -> None:
        async with two_ports(tmp_path) as ports, aiohttp.ClientSession() as http:
            device = ports.device
            resp = await http.get(f"{device}/healthz")
            assert (resp.status, await resp.text()) == (200, "ok")
            resp = await http.get(f"{device}/s/SECRET/range/a.flac")
            assert resp.status == 200
            assert await resp.read() == PAYLOAD
            resp = await http.head(f"{device}/s/SECRET/range/a.flac")
            assert resp.status == 200
            resp = await http.head(f"{device}/healthz")
            assert resp.status == 200
            resp = await http.get(f"{device}/s/SECRET/norange/a.flac")
            assert resp.status == 200
            resp = await http.get(f"{device}/s/WRONG/range/a.flac")
            assert resp.status == 404
            served = len(ports.fake.requests)
            calls = list(ports.wiim.calls)
            for method, target in DEVICE_PORT_BLOCKED:
                resp = await http.request(method, device + target, data=b"x=1")
                assert resp.status == 404, (method, target)
            for target in ("*", "http://evil.example/rest/ping", "//evil.example/x"):
                reply = await raw_exchange(ports.device_port, f"GET {target} HTTP/1.1")
                assert reply.startswith((b"HTTP/1.1 404", b"HTTP/1.1 400")), target
            assert len(ports.fake.requests) == served
            assert {r["path"] for r in ports.fake.requests} == {"/rest/stream"}
            assert ports.wiim.calls == calls

    run(go())


def test_the_main_port_keeps_serving_everything_with_a_device_port(tmp_path: Path) -> None:
    async def go() -> None:
        async with two_ports(tmp_path) as ports, aiohttp.ClientSession() as http:
            main = ports.main
            resp = await http.get(f"{main}/healthz")
            assert (resp.status, await resp.text()) == (200, "ok")
            resp = await http.get(f"{main}/s/SECRET/range/a.flac")
            assert (resp.status, await resp.read()) == (200, PAYLOAD)
            resp = await http.get(f"{main}/rest/ping?u=dan&p=pw")
            assert (await resp.json())["subsonic-response"]["status"] == "ok"
            resp = await http.get(f"{main}/rest/jukeboxControl?action=status&u=dan&p=pw")
            assert (await resp.json())["subsonic-response"]["status"] == "ok"
            resp = await http.post(f"{main}/api/thing", data=b"{}")
            assert resp.status == 201
            for target in ("/remote", "/remote/", "/remote/remote.js"):
                resp = await http.get(main + target, allow_redirects=False)
                assert resp.status in (200, 302), target
            resp = await http.options(f"{main}/rest/jukeboxControl")
            assert resp.status == 204

    run(go())


def test_the_amplifier_is_given_the_device_port(tmp_path: Path) -> None:
    async def go() -> None:
        async with two_ports(tmp_path) as ports, aiohttp.ClientSession() as http:
            for query in ("action=set&id=a&id=b", "action=start"):
                resp = await http.get(f"{ports.main}/rest/jukeboxControl?{query}&u=dan&p=pw")
                assert (await resp.json())["subsonic-response"]["status"] == "ok"
            (context,) = [a[0] for name, a in ports.wiim.calls if name == "queue_create"]
            assert f"http://10.0.0.5:{ports.device_port}/s/SECRET/range/a.flac" in context
            assert f":{ports.main_port}/" not in context

    run(go())


def test_without_a_device_port_there_is_no_guard(tmp_path: Path) -> None:
    config = make_config("http://n", tmp_path)
    assert len(build_app(config, FakeWiim()).middlewares) == 0
    guarded = dataclasses.replace(config, device_port=9002)
    assert len(build_app(guarded, FakeWiim()).middlewares) == 1


def test_app_keys_are_set(tmp_path: Path) -> None:
    app = build_app(make_config("http://n", tmp_path), FakeWiim())
    assert PLAYER_KEY in app
    assert JUKEBOX_KEY in app


def server_args(tmp_path: Path, credentials: Path) -> list[str]:
    return [
        "--credentials", str(credentials),
        "--navidrome-url", "http://navidrome.invalid:4533",
        "--log", str(tmp_path / "out" / "requests.jsonl"),
        "--info-file", str(tmp_path / "info.json"),
        "--secret", "S3cret-value",
        "--port", "9001",
        "--advertise-host", "10.0.0.5",
        "--device-host", "192.0.2.50",
    ]  # fmt: skip


@pytest.fixture(autouse=True)
def clean_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("BRIDGE_"):
            monkeypatch.delenv(name)


def test_bridge_server_main(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "hunter2-pw"}))
    creds.chmod(0o600)
    started: dict[str, Any] = {}

    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    def fake_run_app(app: Any, **kwargs: Any) -> None:
        app.close()
        started.update(kwargs)

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server.web, "run_app", fake_run_app)
    assert bridge_server.main(server_args(tmp_path, creds)) == 0
    info = json.loads((tmp_path / "info.json").read_text())
    assert info["base_url"] == "http://10.0.0.5:9001/s/S3cret-value"
    assert started["port"] == 9001
    records = [r for r in caplog.records if r.name == "bridge.server"]
    assert all(r.levelno == logging.INFO for r in records)
    messages = [r.getMessage() for r in records]
    assert "listening on 0.0.0.0:9001, advertised as http://10.0.0.5:9001" in messages
    assert "navidrome at http://navidrome.invalid:4533" in messages
    assert "device at http://192.0.2.50:49152" in messages
    assert "audio URLs under /s/<hidden>" in messages
    assert "S3cret-value" not in "\n".join(messages)
    assert "hunter2-pw" not in "\n".join(messages)


def test_bridge_server_rejects_open_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "pw"}))
    os.chmod(creds, 0o644)
    assert bridge_server.main(server_args(tmp_path, creds)) == 2
    assert "error:" in capsys.readouterr().err


def no_server(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("server started")


def test_bridge_server_exits_when_navidrome_rejects_the_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def rejected(url: str, params: Any) -> tuple[bool, str | None]:
        return False, "Wrong username or password"

    monkeypatch.setattr(startup, "ping_navidrome", rejected)
    monkeypatch.setattr(bridge_server.web, "run_app", no_server)
    assert bridge_server.main(server_args(tmp_path, good_creds(tmp_path))) == 2
    assert capsys.readouterr().err == (
        "error: Navidrome rejected the credentials: Wrong username or password\n"
    )
    assert not (tmp_path / "info.json").exists()


def test_bridge_server_starts_anyway_when_navidrome_is_unreachable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "hunter2-pw"}))
    creds.chmod(0o600)
    sent: list[dict[str, str]] = []
    started: list[dict[str, Any]] = []

    async def unreachable(url: str, params: Any) -> tuple[bool, str | None]:
        sent.append(dict(params))
        raise aiohttp.ClientConnectionError(
            "Cannot connect to host navidrome.invalid:4533 ssl:default "
            f"[{url}/rest/ping?u=dan&t={params['t']}&s={params['s']}]"
        )

    def fake_run_app(app: Any, **kwargs: Any) -> None:
        app.close()
        started.append(kwargs)

    monkeypatch.setattr(startup, "ping_navidrome", unreachable)
    monkeypatch.setattr(bridge_server.web, "run_app", fake_run_app)
    assert bridge_server.main(server_args(tmp_path, creds)) == 0
    assert len(started) == 1
    assert capsys.readouterr().err == ""
    assert (tmp_path / "info.json").exists()
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.name == "bridge.server"
    message = warning.getMessage()
    assert message.startswith("cannot reach Navidrome at http://navidrome.invalid:4533: ")
    assert message.endswith("starting anyway, requests that need Navidrome fail until it is back")
    for hidden in ("hunter2-pw", sent[0]["t"], sent[0]["s"], "S3cret-value"):
        assert hidden not in caplog.text


def test_bridge_server_starts_anyway_on_a_reply_that_is_not_subsonic(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def html(url: str, params: Any) -> tuple[bool, str | None]:
        return False, None

    monkeypatch.setattr(startup, "ping_navidrome", html)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda app, **kw: app.close())
    assert bridge_server.main(server_args(tmp_path, good_creds(tmp_path))) == 0
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage().startswith(
        "Navidrome at http://navidrome.invalid:4533 sent an unusable reply; starting anyway"
    )


def test_max_volume_reaches_the_player(tmp_path: Path) -> None:
    wiim = FakeWiim()

    async def go() -> None:
        fake = FakeNavidrome()
        async with TestServer(fake.app) as server:
            config = dataclasses.replace(
                make_config(str(server.make_url("")).rstrip("/"), tmp_path), max_volume=70
            )
            async with TestClient(TestServer(build_app(config, wiim))) as client:
                resp = await client.get("/rest/jukeboxControl?action=setGain&gain=0.9&u=dan&p=pw")
                body = (await resp.json())["subsonic-response"]
                assert body["jukeboxStatus"]["gain"] == 0.7

    run(go())
    assert ("set_volume", (70,)) in wiim.calls


def test_bridge_server_max_volume_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "pw"}))
    creds.chmod(0o600)
    captured: dict[str, Any] = {}

    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    def fake_factory(config: BridgeConfig) -> Callable[[], Awaitable[None]]:
        captured["config"] = config

        async def factory() -> None:
            return None

        return factory

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server, "make_app_factory", fake_factory)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda coro, **kw: coro.close())
    argv = [*server_args(tmp_path, creds), "--max-volume", "70"]
    assert bridge_server.main(argv) == 0
    assert captured["config"].max_volume == 70


async def _start_and_ping(config: BridgeConfig, wiim: FakeWiim) -> int:
    async with TestClient(TestServer(build_app(config, wiim))) as client:
        resp = await client.get("/rest/jukeboxControl?action=status&u=dan&p=pw")
        return resp.status


def _run_started(tmp_path: Path, wiim: FakeWiim, **changes: Any) -> int:
    async def go() -> int:
        fake = FakeNavidrome()
        async with TestServer(fake.app) as server:
            base = make_config(str(server.make_url("")).rstrip("/"), tmp_path)
            return await _start_and_ping(dataclasses.replace(base, **changes), wiim)

    return int(run(go()))


def test_startup_clears_device_queue_once(tmp_path: Path) -> None:
    wiim = FakeWiim()
    assert _run_started(tmp_path, wiim) == 200
    names = [name for name, _ in wiim.calls]
    assert names[:2] == ["stop", "queue_delete"]
    assert names.count("stop") == 1
    assert names.count("queue_delete") == 1


def test_clear_on_start_can_be_disabled(tmp_path: Path) -> None:
    wiim = FakeWiim()
    _run_started(tmp_path, wiim, clear_on_start=False)
    names = [name for name, _ in wiim.calls]
    assert "stop" not in names
    assert "queue_delete" not in names


def test_unreachable_device_does_not_block_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Unreachable(FakeWiim):
        async def stop(self) -> str:
            raise aiohttp.ClientConnectionError("device down")

    assert _run_started(tmp_path, Unreachable()) == 200
    record = next(r for r in caplog.records if r.name == "bridge.app")
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "could not clear the device queue at start: device down"


def test_bridge_server_keep_queue_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "pw"}))
    creds.chmod(0o600)
    captured: list[BridgeConfig] = []

    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    def fake_factory(config: BridgeConfig) -> Callable[[], Awaitable[None]]:
        captured.append(config)

        async def factory() -> None:
            return None

        return factory

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server, "make_app_factory", fake_factory)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda coro, **kw: coro.close())
    base = server_args(tmp_path, creds)
    assert bridge_server.main(base) == 0
    assert bridge_server.main([*base, "--keep-queue-on-start"]) == 0
    assert [c.clear_on_start for c in captured] == [True, False]
    assert bridge_server.main([*base, "--scrobble"]) == 0
    assert [c.scrobble for c in captured] == [False, False, True]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_bridge_server_opens_the_device_port(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    captured: list[BridgeConfig] = []
    started: dict[str, Any] = {}

    def fake_factory(config: BridgeConfig) -> Callable[[], Awaitable[None]]:
        captured.append(config)

        async def factory() -> None:
            return None

        return factory

    def fake_run_app(coro: Any, **kwargs: Any) -> None:
        coro.close()
        started.update(kwargs)

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server, "make_app_factory", fake_factory)
    monkeypatch.setattr(bridge_server.web, "run_app", fake_run_app)
    port = free_port()
    argv = [*server_args(tmp_path, good_creds(tmp_path)), "--bind", "127.0.0.1"]
    assert bridge_server.main([*argv, "--device-port", str(port)]) == 0
    with started["sock"] as sock:
        assert sock.getsockname()[:2] == ("127.0.0.1", port)
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert (started["host"], started["port"]) == ("127.0.0.1", 9001)
    assert captured[0].device_port == port
    assert captured[0].port == 9001
    info = json.loads((tmp_path / "info.json").read_text())
    assert info["base_url"] == f"http://10.0.0.5:{port}/s/S3cret-value"
    messages = [r.getMessage() for r in caplog.records if r.name == "bridge.server"]
    assert "listening on 127.0.0.1:9001, advertised as http://10.0.0.5:9001" in messages
    expected = f"audio-only device port 127.0.0.1:{port}, advertised as http://10.0.0.5:{port}"
    assert expected in messages
    listening = messages.index("listening on 127.0.0.1:9001, advertised as http://10.0.0.5:9001")
    assert messages.index(expected) == listening + 1


def test_bridge_server_without_a_device_port_passes_no_socket(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    captured: list[BridgeConfig] = []
    started: dict[str, Any] = {}

    def fake_factory(config: BridgeConfig) -> Callable[[], Awaitable[None]]:
        captured.append(config)

        async def factory() -> None:
            return None

        return factory

    def fake_run_app(coro: Any, **kwargs: Any) -> None:
        coro.close()
        started.update(kwargs)

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server, "make_app_factory", fake_factory)
    monkeypatch.setattr(bridge_server.web, "run_app", fake_run_app)
    assert bridge_server.main(server_args(tmp_path, good_creds(tmp_path))) == 0
    assert captured[0].device_port is None
    assert started == {"host": "0.0.0.0", "port": 9001, "print": None}
    assert not [r for r in caplog.records if "device port" in r.getMessage()]


def test_bridge_server_reports_a_device_port_that_cannot_be_opened(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server.web, "run_app", no_server)
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        argv = [*server_args(tmp_path, good_creds(tmp_path)), "--bind", "127.0.0.1"]
        assert bridge_server.main([*argv, "--device-port", str(port)]) == 2
    assert capsys.readouterr().err.startswith(f"error: cannot listen on 127.0.0.1:{port}: ")
    assert not (tmp_path / "info.json").exists()


def test_listen_on_picks_the_address_family(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, int]] = []

    def fake_create_server(address: Any, *, family: int) -> str:
        seen.append((address, family))
        return "socket"

    monkeypatch.setattr(bridge_server.socket, "create_server", fake_create_server)
    for bind in ("::", "::1", "0.0.0.0", "localhost"):
        assert bridge_server.listen_on(bind, 9002) == "socket"
    assert seen == [
        (("::", 9002), socket.AF_INET6),
        (("::1", 9002), socket.AF_INET6),
        (("0.0.0.0", 9002), socket.AF_INET),
        (("localhost", 9002), socket.AF_INET),
    ]


def scrobble_bridge(
    tmp_path: Path,
    scenario: Callable[[TestClient[Any, Any], FakeNavidrome, FakeWiim], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
    **changes: Any,
) -> None:
    monkeypatch.setattr("bridge.app.POLL_INTERVAL_S", 0.05)
    wiim = FakeWiim()

    async def go() -> None:
        fake = FakeNavidrome()
        async with TestServer(fake.app) as server:
            base = make_config(str(server.make_url("")).rstrip("/"), tmp_path)
            app = build_app(dataclasses.replace(base, **changes), wiim)
            async with TestClient(TestServer(app)) as client:
                await scenario(client, fake, wiim)

    run(go())


async def wait_for_scrobbles(fake: FakeNavidrome, count: int) -> list[dict[str, Any]]:
    for _ in range(300):
        found = [r for r in fake.requests if r["path"] == "/rest/scrobble"]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.01)
    raise AssertionError("scrobble requests never arrived")


def test_scrobbling_reports_now_playing_from_the_background_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        for query in ("action=set&id=a&id=b", "action=start"):
            await client.get(f"/rest/jukeboxControl?{query}&u=dan&p=pw")
        (request,) = await wait_for_scrobbles(fake, 1)
        assert request["query"]["id"] == "a"
        assert request["query"]["submission"] == "false"
        assert request["query"]["time"].isdigit()
        await asyncio.sleep(0.2)
        assert fake.count("/rest/scrobble") == 1

    scrobble_bridge(tmp_path, scenario, monkeypatch, scrobble=True)


def test_scrobbling_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        for query in ("action=set&id=a", "action=start", "action=status"):
            await client.get(f"/rest/jukeboxControl?{query}&u=dan&p=pw")
        polled = wiim.calls.count(("state", ()))
        await asyncio.sleep(0.2)
        assert fake.count("/rest/scrobble") == 0
        assert wiim.calls.count(("state", ())) == polled

    scrobble_bridge(tmp_path, scenario, monkeypatch)


def test_remote_redirect_and_pages(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get("/remote", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == "/remote/"

        resp = await client.get("/remote/")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        assert "<title>" in await resp.text()
        assert resp.headers["Cache-Control"] == "no-cache"

        for name in REMOTE_FILES:
            resp = await client.get(f"/remote/{name}")
            assert resp.status == 200, name
            assert resp.headers["Cache-Control"] == "no-cache"

        for bad in ("/remote/nope.js", "/remote/..%2Fapp.py"):
            resp = await client.get(bad)
            assert resp.status == 404, bad
        assert fake.requests == []

        resp = await client.get("/rest/ping.view?u=dan&p=pw")
        assert resp.status == 200
        assert fake.count("/rest/ping.view") == 1

    with_bridge(tmp_path, scenario)


def make_creds(tmp_path: Path) -> Path:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "fileuser", "password": "filepw"}))
    creds.chmod(0o600)
    return creds


def test_resolve_credentials_from_environment(tmp_path: Path) -> None:
    env = {"BRIDGE_NAVIDROME_USER": "u", "BRIDGE_NAVIDROME_PASSWORD": "p"}
    assert bridge_server.resolve_credentials(str(tmp_path / "missing.json"), env) == ("u", "p")


@pytest.mark.parametrize(
    "env",
    [
        {"BRIDGE_NAVIDROME_USER": "u"},
        {"BRIDGE_NAVIDROME_PASSWORD": "p"},
        {"BRIDGE_NAVIDROME_USER": "u", "BRIDGE_NAVIDROME_PASSWORD": ""},
    ],
)
def test_resolve_credentials_partial_environment(tmp_path: Path, env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="both BRIDGE_NAVIDROME_USER"):
        bridge_server.resolve_credentials(str(make_creds(tmp_path)), env)


def test_resolve_credentials_falls_back_to_file(tmp_path: Path) -> None:
    assert bridge_server.resolve_credentials(str(make_creds(tmp_path)), {}) == (
        "fileuser",
        "filepw",
    )


def test_resolve_secret_precedence() -> None:
    env = {"BRIDGE_SECRET": "from-env-123"}
    assert bridge_server.resolve_secret("from-arg-123", env) == "from-arg-123"
    assert bridge_server.resolve_secret(None, env) == "from-env-123"


def test_resolve_secret_random_default() -> None:
    secret = bridge_server.resolve_secret(None, {})
    assert bridge_server.SECRET_PATTERN.fullmatch(secret)
    assert secret != bridge_server.resolve_secret(None, {})


@pytest.mark.parametrize("bad", ["short", "has/slash", "has space12"])
def test_resolve_secret_rejects_invalid(bad: str) -> None:
    with pytest.raises(ConfigError, match="path secret"):
        bridge_server.resolve_secret(bad, {})
    with pytest.raises(ConfigError, match="path secret"):
        bridge_server.resolve_secret(None, {"BRIDGE_SECRET": bad})


def test_no_info_file_flag() -> None:
    assert bridge_server.parse_args([]).no_info_file is False
    assert bridge_server.parse_args(["--no-info-file"]).no_info_file is True


def test_main_hides_secret_and_skips_info_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda app, **kw: app.close())
    monkeypatch.setenv("BRIDGE_NAVIDROME_USER", "envuser")
    monkeypatch.setenv("BRIDGE_NAVIDROME_PASSWORD", "envpw-value")
    monkeypatch.setenv("BRIDGE_SECRET", "env-secret-99")
    argv = [
        *server_args(tmp_path, tmp_path / "missing.json"),
        "--no-info-file",
    ]
    argv[argv.index("--secret") : argv.index("--secret") + 2] = []
    argv[argv.index("--info-file") : argv.index("--info-file") + 2] = []
    assert bridge_server.main(argv) == 0
    out = "\n".join(r.getMessage() for r in caplog.records)
    assert "audio URLs under /s/<hidden>" in out
    assert "env-secret-99" not in out
    assert "envpw-value" not in out
    assert not (tmp_path / "info.json").exists()


def test_healthz_touches_nothing(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        before = len(fake.requests)
        wiim_before = list(wiim.calls)
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.text() == "ok"
        assert len(fake.requests) == before
        assert wiim.calls == wiim_before

    with_bridge(tmp_path, scenario)


def test_request_log_accepts_dev_null() -> None:
    RequestLog(Path("/dev/null")).write({"req": 1})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("debug", logging.DEBUG),
        ("info", logging.INFO),
        ("warn", logging.WARNING),
        ("warning", logging.WARNING),
        ("error", logging.ERROR),
        ("DEBUG", logging.DEBUG),
        ("Warn", logging.WARNING),
        ("  info\t", logging.INFO),
    ],
)
def test_parse_level(value: str, expected: int) -> None:
    assert parse_level(value) == expected


@pytest.mark.parametrize("bad", ["", "verbose", "critical", "10"])
def test_parse_level_rejects_unknown(bad: str) -> None:
    with pytest.raises(ConfigError, match="log level must be one of debug, info, warn, error"):
        parse_level(bad)


def test_setup_logging_replaces_its_handler(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    setup_logging(logging.INFO)
    setup_logging(logging.DEBUG)
    added = [h for h in root.handlers if getattr(h, "stream", None) is sys.stdout]
    assert len(root.handlers) == before + 1
    assert len(added) == 1
    assert root.level == logging.DEBUG
    logging.getLogger("bridge.test").info("hello")
    assert " INFO bridge.test: hello" in capsys.readouterr().out


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR])
def test_access_log_is_never_verbose(level: int) -> None:
    setup_logging(level)
    assert logging.getLogger("aiohttp.access").level >= logging.WARNING


def test_log_level_option_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIDGE_LOG_LEVEL", raising=False)
    assert bridge_server.parse_args([]).log_level == "info"
    monkeypatch.setenv("BRIDGE_LOG_LEVEL", "")
    assert bridge_server.parse_args([]).log_level == "info"
    monkeypatch.setenv("BRIDGE_LOG_LEVEL", "debug")
    assert bridge_server.parse_args([]).log_level == "debug"
    assert bridge_server.parse_args(["--log-level", "error"]).log_level == "error"


def test_main_invalid_log_level_fails_before_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "pw"}))
    creds.chmod(0o600)

    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(startup, "check_navidrome", no_network)
    argv = [*server_args(tmp_path, creds), "--log-level", "loud"]
    assert bridge_server.main(argv) == 2
    assert "error: log level must be one of" in capsys.readouterr().err
    monkeypatch.setenv("BRIDGE_LOG_LEVEL", "loud")
    assert bridge_server.main(server_args(tmp_path, creds)) == 2


def test_main_applies_log_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "pw"}))
    creds.chmod(0o600)

    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda app, **kw: app.close())
    assert bridge_server.main([*server_args(tmp_path, creds), "--log-level", "ERROR"]) == 0
    assert logging.getLogger().level == logging.ERROR


def records_of(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == name]


def test_audio_request_logs_at_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get("/s/SECRET/range/a.flac")
        assert await resp.read() == PAYLOAD

    with_bridge(tmp_path, scenario)
    records = records_of(caplog, "bridge.proxy")
    assert [r.levelno for r in records] == [logging.INFO, logging.INFO]
    assert [r.getMessage() for r in records] == [
        "device fetches song a",
        "sent song a: 5 kB, complete",
    ]


def test_queued_track_is_named_in_proxy_and_jukebox_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        await client.get("/rest/jukeboxControl?action=set&id=a&id=b&u=dan&p=pw")
        await client.get("/rest/jukeboxControl?action=start&u=dan&p=pw")
        resp = await client.get("/s/SECRET/range/a.flac", headers={"Range": "bytes=1000-"})
        await resp.read()

    with_bridge(tmp_path, scenario)
    messages = [
        r.getMessage() for r in caplog.records if r.name in ("bridge.proxy", "bridge.jukebox")
    ]
    assert messages == [
        'jukebox action=set index=- offset=- ids=2 gain=- mute=- first="Art - A"',
        'jukebox action=start index=- offset=- ids=0 gain=- mute=- track="Art - A"',
        'device fetches "Art - A" from "Alb" (range 1000-)',
        'sent "Art - A": 5 kB, complete',
    ]


def test_rejected_audio_request_logs_at_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get("/s/WRONG/range/a.flac")
        assert resp.status == 404

    with_bridge(tmp_path, scenario)
    (record,) = records_of(caplog, "bridge.proxy")
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("reject reason=secret remote=")
    assert "WRONG" not in record.getMessage()


def test_jukebox_skip_logs_at_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        await client.get("/rest/jukeboxControl?action=set&id=a&id=b&u=dan&p=pw")
        caplog.clear()
        await client.get("/rest/jukeboxControl?action=skip&index=1&offset=7&u=dan&p=pw")

    with_bridge(tmp_path, scenario)
    (record,) = records_of(caplog, "bridge.jukebox")
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        'jukebox action=skip index=1 offset=7 ids=0 gain=- mute=- track="Art - B"'
    )


def test_jukebox_get_and_status_log_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        for action in ("get", "status"):
            await client.get(f"/rest/jukeboxControl?action={action}&u=dan&p=pw")

    caplog.set_level(logging.INFO)
    with_bridge(tmp_path, scenario)
    assert not records_of(caplog, "bridge.jukebox")
    caplog.set_level(logging.DEBUG)
    with_bridge(tmp_path, scenario)
    records = records_of(caplog, "bridge.jukebox")
    assert [r.levelno for r in records] == [logging.DEBUG, logging.DEBUG]
    assert records[0].getMessage().startswith("jukebox action=get ")


def test_jukebox_failing_action_logs_at_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        await client.get("/rest/jukeboxControl?action=set&id=zzz&u=dan&p=pw")

    with_bridge(tmp_path, scenario)
    (record,) = records_of(caplog, "bridge.jukebox")
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "jukebox action=set failed code=70: song not found: zzz"


def test_jukebox_wrong_login_logs_at_info_without_user(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    setup_logging(logging.DEBUG)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        await client.get("/rest/jukeboxControl?action=set&id=a&u=mallory&p=wrong")

    with_bridge(tmp_path, scenario)
    (record,) = records_of(caplog, "bridge.jukebox")
    assert record.levelno == logging.INFO
    assert record.getMessage() == "jukebox rejected login"
    assert "mallory" not in caplog.text


def test_passthrough_failure_logs_without_query(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    setup_logging(logging.DEBUG)

    async def go() -> None:
        app = build_app(make_config("http://127.0.0.1:1", tmp_path), FakeWiim())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/thing?token=hunter2")
            assert resp.status == 502

    run(go())
    (record,) = [r for r in records_of(caplog, "bridge.app") if "passthrough" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("passthrough GET /api/thing failed: ")
    assert "hunter2" not in caplog.text


def test_round_trip_logs_contain_no_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    setup_logging(logging.DEBUG)

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        base = "/rest/jukeboxControl"
        for query in (
            "action=set&id=a&id=b&u=dan&p=pw",
            "action=start&u=dan&p=pw",
            "action=get&u=dan&t=good&s=tokensalt",
            "action=skip&index=1&u=dan&t=good&s=tokensalt",
            "action=set&id=a&u=dan&p=wrongpw",
        ):
            await client.get(f"{base}?{query}")
        for path in ("/s/SECRET/range/a.flac", "/s/BADSECRET/range/a.flac"):
            await (await client.get(path)).read()
        await client.get("/api/thing?t=good&s=tokensalt")

    with_bridge(tmp_path, scenario)
    assert caplog.records
    text = caplog.text
    assert 'device fetches "Art - A" from "Alb"' in text
    for forbidden in ("SECRET", "pw", "abcd", "good", "tokensalt"):
        assert forbidden not in text, forbidden


def settings_for(*argv: str, env: dict[str, str] | None = None) -> bridge_server.Settings:
    base = ["--advertise-host", "192.0.2.10", "--device-host", "192.0.2.50"]
    return bridge_server.resolve_settings(bridge_server.parse_args([*base, *argv]), env or {})


def test_string_setting_precedence() -> None:
    env = {"BRIDGE_NAVIDROME_URL": "http://env:1/"}
    assert settings_for().navidrome_url == "http://127.0.0.1:4533"
    assert settings_for(env=env).navidrome_url == "http://env:1"
    assert settings_for("--navidrome-url", "http://opt:2", env=env).navidrome_url == "http://opt:2"
    assert settings_for(env={"BRIDGE_NAVIDROME_URL": ""}).navidrome_url == "http://127.0.0.1:4533"


def test_integer_setting_precedence() -> None:
    assert settings_for().port == 8098
    assert settings_for(env={"BRIDGE_PORT": "9000"}).port == 9000
    assert settings_for("--port", "9100", env={"BRIDGE_PORT": "9000"}).port == 9100
    assert settings_for(env={"BRIDGE_PORT": ""}).port == 8098
    assert settings_for().max_volume == 100
    assert settings_for(env={"BRIDGE_MAX_VOLUME": "0"}).max_volume == 0


def test_device_port_setting_is_unset_by_default() -> None:
    assert settings_for().device_port is None
    assert settings_for(env={"BRIDGE_DEVICE_PORT": ""}).device_port is None
    assert settings_for(env={"BRIDGE_DEVICE_PORT": "9100"}).device_port == 9100
    opt = settings_for("--device-port", "9200", env={"BRIDGE_DEVICE_PORT": "9100"})
    assert opt.device_port == 9200
    assert settings_for("--device-port", " 1 ").device_port == 1
    assert settings_for("--device-port", "65535").device_port == 65535


@pytest.mark.parametrize("value", ["abc", "0", "-1", "65536", "70000", "1.5", "9100x"])
def test_device_port_must_be_a_port_number(value: str) -> None:
    with pytest.raises(ConfigError) as err:
        settings_for(env={"BRIDGE_DEVICE_PORT": value})
    assert str(err.value) == "BRIDGE_DEVICE_PORT must be an integer from 1 to 65535"
    with pytest.raises(ConfigError) as err:
        settings_for("--device-port", value)
    assert str(err.value) == "--device-port must be an integer from 1 to 65535"


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        (["--port", "9000", "--device-port", "9000"], {}, "--device-port must differ from --port"),
        (
            [],
            {"BRIDGE_PORT": "9000", "BRIDGE_DEVICE_PORT": "9000"},
            "BRIDGE_DEVICE_PORT must differ from BRIDGE_PORT",
        ),
        ([], {"BRIDGE_DEVICE_PORT": "8098"}, "BRIDGE_DEVICE_PORT must differ from BRIDGE_PORT"),
        (
            ["--device-port", "9000"],
            {"BRIDGE_PORT": "9000"},
            "--device-port must differ from BRIDGE_PORT",
        ),
        (
            ["--port", "9000"],
            {"BRIDGE_DEVICE_PORT": "9000"},
            "BRIDGE_DEVICE_PORT must differ from --port",
        ),
    ],
)
def test_device_port_must_differ_from_the_main_port(
    argv: list[str], env: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError) as err:
        settings_for(*argv, env=env)
    assert str(err.value) == message


def test_boolean_setting_precedence() -> None:
    assert settings_for().keep_queue_on_start is False
    assert settings_for("--keep-queue-on-start").keep_queue_on_start is True
    assert settings_for(env={"BRIDGE_KEEP_QUEUE_ON_START": ""}).keep_queue_on_start is False
    for value, expected in [("true", True), ("1", True), ("YES", True), ("False", False),
                            ("0", False), ("no", False)]:  # fmt: skip
        env = {"BRIDGE_KEEP_QUEUE_ON_START": value}
        assert settings_for(env=env).keep_queue_on_start is expected
    env = {"BRIDGE_KEEP_QUEUE_ON_START": "false"}
    assert settings_for("--keep-queue-on-start", env=env).keep_queue_on_start is True


def test_scrobble_setting_defaults_to_off() -> None:
    assert settings_for().scrobble is False
    assert settings_for("--scrobble").scrobble is True
    assert settings_for(env={"BRIDGE_SCROBBLE": ""}).scrobble is False
    for value, expected in [("true", True), ("1", True), ("YES", True), ("False", False),
                            ("0", False), ("no", False)]:  # fmt: skip
        assert settings_for(env={"BRIDGE_SCROBBLE": value}).scrobble is expected
    assert settings_for("--scrobble", env={"BRIDGE_SCROBBLE": "false"}).scrobble is True


def test_other_defaults_and_env() -> None:
    s = settings_for()
    assert (s.bind, s.queue_name, s.log, s.info_file) == (
        "0.0.0.0", "bridge", Path("/dev/null"), None,
    )  # fmt: skip
    assert s.credentials == "~/.config/navidrome-wiim-bridge/credentials.json"
    env = {
        "BRIDGE_BIND": "127.0.0.1",
        "BRIDGE_QUEUE_NAME": "q",
        "BRIDGE_REQUEST_LOG": "/tmp/x.jsonl",
        "BRIDGE_CREDENTIALS": "/c.json",
    }
    s = settings_for(env=env)
    assert (s.bind, s.queue_name, s.log, s.credentials) == (
        "127.0.0.1", "q", Path("/tmp/x.jsonl"), "/c.json",
    )  # fmt: skip
    assert settings_for("--credentials", "/o.json", env=env).credentials == "/o.json"


def test_device_host_derives_both_urls() -> None:
    s = settings_for()
    assert s.device_base == "http://192.0.2.50:49152"
    assert s.status_url == "https://192.0.2.50/httpapi.asp?command=getPlayerStatus"


def test_device_host_with_status_override() -> None:
    s = settings_for("--status-url", "https://other/status")
    assert s.device_base == "http://192.0.2.50:49152"
    assert s.status_url == "https://other/status"
    s = settings_for(env={"BRIDGE_DEVICE_BASE": "http://b:1"})
    assert s.device_base == "http://b:1"


def bare_settings(*argv: str, env: dict[str, str] | None = None) -> bridge_server.Settings:
    args = bridge_server.parse_args(["--advertise-host", "192.0.2.10", *argv])
    return bridge_server.resolve_settings(args, env or {})


def test_device_base_and_status_without_host() -> None:
    s = bare_settings("--device-base", "http://b:1", "--status-url", "https://s/x")
    assert (s.device_base, s.status_url) == ("http://b:1", "https://s/x")


@pytest.mark.parametrize("argv", [[], ["--device-base", "http://b:1"], ["--status-url", "https://s"]])
def test_device_address_required(argv: list[str]) -> None:
    with pytest.raises(ConfigError) as err:
        bare_settings(*argv)
    assert str(err.value) == "set BRIDGE_DEVICE_HOST (or --device-host) to the amplifier's address"


def test_device_host_that_is_a_url_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="not a URL"):
        settings_for("--device-host", "http://192.0.2.50")


def test_device_urls() -> None:
    assert device_urls("192.0.2.50") == (
        "http://192.0.2.50:49152",
        "https://192.0.2.50/httpapi.asp?command=getPlayerStatus",
    )
    assert device_urls("amp.example")[0] == "http://amp.example:49152"
    base, status = device_urls("2001:db8::1")
    assert base == "http://[2001:db8::1]:49152"
    assert status == "https://[2001:db8::1]/httpapi.asp?command=getPlayerStatus"
    for bad in ("http://192.0.2.50", "192.0.2.50/x"):
        with pytest.raises(ValueError, match="not a URL"):
            device_urls(bad)


def test_missing_advertise_host() -> None:
    args = bridge_server.parse_args(["--device-host", "192.0.2.50"])
    with pytest.raises(ConfigError) as err:
        bridge_server.resolve_settings(args, {})
    assert str(err.value) == (
        "set BRIDGE_ADVERTISE_HOST (or --advertise-host) to the address "
        "the amplifier uses to reach the bridge"
    )
    env = {"BRIDGE_ADVERTISE_HOST": "192.0.2.10"}
    assert bridge_server.resolve_settings(args, env).advertise_host == "192.0.2.10"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BRIDGE_PORT", "abc"), ("BRIDGE_PORT", "0"), ("BRIDGE_PORT", "70000"),
        ("BRIDGE_MAX_VOLUME", "-1"), ("BRIDGE_MAX_VOLUME", "101"),
        ("BRIDGE_KEEP_QUEUE_ON_START", "maybe"), ("BRIDGE_SCROBBLE", "maybe"),
    ],
)  # fmt: skip
def test_invalid_numeric_and_boolean_settings(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        settings_for(env={name: value})


def test_invalid_option_value_names_the_option() -> None:
    with pytest.raises(ConfigError, match="--port must be an integer from 1 to 65535"):
        settings_for("--port", "abc")


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(startup, "check_navidrome", no_network)
    monkeypatch.setattr(bridge_server.web, "run_app", no_network)


def good_creds(tmp_path: Path) -> Path:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "pw"}))
    creds.chmod(0o600)
    return creds


@pytest.mark.parametrize(
    ("drop", "env", "message"),
    [
        ("--advertise-host", {}, "error: set BRIDGE_ADVERTISE_HOST"),
        ("--device-host", {}, "error: set BRIDGE_DEVICE_HOST"),
        ("--port", {"BRIDGE_PORT": "0"}, "error: BRIDGE_PORT must be"),
        (None, {"BRIDGE_MAX_VOLUME": "101"}, "error: BRIDGE_MAX_VOLUME must be"),
        (None, {"BRIDGE_KEEP_QUEUE_ON_START": "maybe"}, "error: BRIDGE_KEEP_QUEUE_ON_START"),
    ],
)  # fmt: skip
def test_main_config_errors_before_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    drop: str | None,
    env: dict[str, str],
    message: str,
) -> None:
    forbid_network(monkeypatch)
    argv = server_args(tmp_path, good_creds(tmp_path))
    if drop is not None:
        i = argv.index(drop)
        argv[i : i + 2] = []
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert bridge_server.main(argv) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"BRIDGE_DEVICE_PORT": "0"}, "error: BRIDGE_DEVICE_PORT must be an integer from 1 to"),
        ({"BRIDGE_DEVICE_PORT": "9001"}, "error: BRIDGE_DEVICE_PORT must differ from --port"),
    ],
)
def test_main_device_port_errors_before_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    message: str,
) -> None:
    forbid_network(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert bridge_server.main(server_args(tmp_path, good_creds(tmp_path))) == 2
    assert message in capsys.readouterr().err


def test_main_config_error_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_network(monkeypatch)
    monkeypatch.setenv("BRIDGE_PORT", "0")
    argv = ["--credentials", str(tmp_path / "missing.json"), "--secret", "bad"]

    def err(*extra: str) -> str:
        assert bridge_server.main([*argv, *extra]) == 2
        return capsys.readouterr().err

    assert "log level" in err("--log-level", "loud")
    assert "BRIDGE_ADVERTISE_HOST" in err()
    argv += ["--advertise-host", "192.0.2.10"]
    assert "BRIDGE_DEVICE_HOST" in err()
    argv += ["--device-host", "192.0.2.50"]
    assert "BRIDGE_PORT" in err()
    monkeypatch.delenv("BRIDGE_PORT")
    assert "credentials file not found" in err()
    argv[argv.index("--credentials") + 1] = str(good_creds(tmp_path))
    assert "path secret" in err()


def test_main_defaults_write_no_files_and_log_to_dev_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[BridgeConfig] = []

    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    def fake_factory(config: BridgeConfig) -> Callable[[], Awaitable[None]]:
        captured.append(config)

        async def factory() -> None:
            return None

        return factory

    monkeypatch.setattr(startup, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server, "make_app_factory", fake_factory)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda coro, **kw: coro.close())
    monkeypatch.chdir(tmp_path)
    argv = [
        "--credentials", str(good_creds(tmp_path)),
        "--advertise-host", "192.0.2.10",
        "--device-host", "192.0.2.50",
    ]  # fmt: skip
    assert bridge_server.main(argv) == 0
    assert captured[0].log_path == Path("/dev/null")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["creds.json"]
    assert bridge_server.main([*argv, "--no-info-file"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["creds.json"]
    info = tmp_path / "sub" / "info.json"
    assert bridge_server.main([*argv, "--info-file", str(info)]) == 0
    assert json.loads(info.read_text())["base_url"].startswith("http://192.0.2.10:8098/s/")


def test_no_private_lan_addresses_in_source() -> None:
    root = Path(__file__).resolve().parent.parent
    offenders = [
        str(path)
        for folder in ("bridge", "tools")
        for path in (root / folder).rglob("*.py")
        if any(marker in path.read_text(encoding="utf-8") for marker in ("172.16.", "192.168."))
    ]
    assert offenders == []
