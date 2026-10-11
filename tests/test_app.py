import asyncio
import dataclasses
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

from bridge.subsonic import ConfigError
from bridge.proxy import RequestLog
from bridge.app import (
    JUKEBOX_KEY,
    PLAYER_KEY,
    REMOTE_FILES,
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
        assert [name for name, _ in wiim.calls] == ["stop", "queue_delete"]

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
        "--secret", "S3cret-value",
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
    assert info["base_url"] == "http://10.0.0.5:9001/s/S3cret-value"
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

    monkeypatch.setattr(poc_proxy, "ping_navidrome", accepted)
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Unreachable(FakeWiim):
        async def stop(self) -> str:
            raise aiohttp.ClientConnectionError("device down")

    assert _run_started(tmp_path, Unreachable()) == 200
    err = capsys.readouterr().err
    assert "warning: could not clear the device queue at start: device down" in err


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

    monkeypatch.setattr(poc_proxy, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server, "make_app_factory", fake_factory)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda coro, **kw: coro.close())
    base = server_args(tmp_path, creds)
    assert bridge_server.main(base) == 0
    assert bridge_server.main([*base, "--keep-queue-on-start"]) == 0
    assert [c.clear_on_start for c in captured] == [True, False]


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
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(poc_proxy, "ping_navidrome", accepted)
    monkeypatch.setattr(bridge_server.web, "run_app", lambda app, **kw: app.close())
    monkeypatch.setenv("BRIDGE_NAVIDROME_USER", "envuser")
    monkeypatch.setenv("BRIDGE_NAVIDROME_PASSWORD", "envpw-value")
    monkeypatch.setenv("BRIDGE_SECRET", "env-secret-99")
    argv = [
        *server_args(tmp_path, tmp_path / "missing.json"),
        "--no-info-file",
    ]
    argv[argv.index("--secret") : argv.index("--secret") + 2] = []
    assert bridge_server.main(argv) == 0
    out = capsys.readouterr().out
    assert "base URL: http://10.0.0.5:9001/s/<hidden>" in out
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
