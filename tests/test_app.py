import asyncio
import gzip
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import bridge_server
import poc_proxy
import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_player import FakeWiim

from bridge.app import (
    JUKEBOX_KEY,
    PLAYER_KEY,
    BridgeConfig,
    NavidromeClient,
    build_app,
    stream_url,
)

PAYLOAD = bytes(i % 251 for i in range(5000))
GZ_BODY = gzip.compress(b"hello gzip " * 50)


def song(song_id: str) -> dict[str, Any]:
    return {"id": song_id, "title": song_id.upper(), "artist": "Art", "duration": 60}


class FakeNavidrome:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.app = web.Application()
        for path in ("/rest/ping", "/rest/ping.view"):
            self.app.router.add_get(path, self.ping)
        self.app.router.add_get("/rest/getSong", self.get_song)
        self.app.router.add_get("/rest/stream", self.stream)
        self.app.router.add_route("*", "/api/thing", self.thing)
        self.app.router.add_get("/gz", self.gz)

    async def _record(self, request: web.Request) -> bytes:
        body = await request.read()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
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

    async def ping(self, request: web.Request) -> web.Response:
        await self._record(request)
        q = request.query
        good = (q.get("u") == "dan" and q.get("p") == "pw") or q.get("t") == "good"
        return web.json_response(self.envelope("ok" if good else "failed"))

    async def get_song(self, request: web.Request) -> web.Response:
        await self._record(request)
        song_id = request.query.get("id", "")
        if song_id == "junk":
            return web.Response(text="not json")
        if song_id in ("a", "b"):
            return web.json_response(self.envelope("ok", song=song(song_id)))
        return web.json_response(self.envelope("failed"))

    async def stream(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.Response(body=PAYLOAD, content_type="audio/flac")

    async def thing(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        reply = {"method": request.method, "body": body.decode()}
        return web.json_response(reply, status=201, headers={"X-Test": "1"})

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


def test_check_caller_and_cache() -> None:
    now = [0.0]
    good = {"u": "dan", "p": "pw"}

    async def scenario(client: NavidromeClient, fake: FakeNavidrome) -> None:
        assert await client.check_caller(good) is True
        assert fake.count("/rest/ping") == 1
        assert fake.requests[0]["query"]["c"] == "wiim-bridge-auth"
        assert await client.check_caller(good) is True
        assert fake.count("/rest/ping") == 1
        now[0] = 61.0
        assert await client.check_caller(good) is True
        assert fake.count("/rest/ping") == 2
        assert await client.check_caller({"u": "dan", "p": "bad"}) is False
        assert await client.check_caller({"u": "dan", "p": "bad"}) is False
        assert fake.count("/rest/ping") == 4
        before = len(fake.requests)
        assert await client.check_caller({}) is False
        assert len(fake.requests) == before

    with_client(scenario, clock=lambda: now[0])


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
        assert wiim.calls == []

    with_bridge(tmp_path, scenario)


def test_passthrough_keeps_compressed_bytes(tmp_path: Path) -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome, wiim: FakeWiim) -> None:
        resp = await client.get("/gz")
        assert resp.status == 200
        assert resp.headers["Content-Encoding"] == "gzip"
        assert await resp.read() == GZ_BODY

    with_bridge(tmp_path, scenario, auto_decompress=False)


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
        "--secret", "S3",
        "--port", "9001",
        "--advertise-host", "10.0.0.5",
    ]  # fmt: skip


def test_bridge_server_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
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

    monkeypatch.setattr(poc_proxy, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server.web, "run_app", fake_run_app)
    assert bridge_server.main(server_args(tmp_path, creds)) == 0
    info = json.loads((tmp_path / "info.json").read_text())
    assert info["base_url"] == "http://10.0.0.5:9001/s/S3"
    assert started["port"] == 9001
    out = capsys.readouterr().out
    assert any(line.startswith("Feishin server URL: http://") for line in out.splitlines())
    assert "hunter2-pw" not in out


def test_bridge_server_rejects_open_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"user": "dan", "password": "pw"}))
    os.chmod(creds, 0o644)
    assert bridge_server.main(server_args(tmp_path, creds)) == 2
    assert "error:" in capsys.readouterr().err
