import asyncio
import dataclasses
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import bridge_server
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_app import good_creds, server_args
from test_player import FakeWiim

from bridge import radio as radio_module
from bridge import startup
from bridge.app import BridgeConfig, build_app
from bridge.radio import (
    Radio,
    RadioSettings,
    SettingsError,
    annotate_line,
    load_settings,
    parse_settings,
    save_settings,
)
from bridge.subsonic import ConfigError

TOKEN = "radio-token-1234"
LOGIN = {"u": "dan", "t": "good", "s": "salt"}
GENRES = [
    {"value": "Rock", "songCount": 30},
    {"value": "Hip-Hop", "songCount": 20},
    {"value": "Jazz", "songCount": 10},
]
MP3 = b"ID3" + bytes(range(200))


def run(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guarded())


def make_song(n: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"s{n}",
        "title": f"Song {n}",
        "artist": "Art",
        "album": "Alb",
        "duration": 200,
        **extra,
    }


class FakeNavidrome:
    def __init__(self, library: int = 400) -> None:
        self.library = [make_song(n, genre="Rock" if n % 2 else "Jazz") for n in range(library)]
        self.random_calls = 0
        self.stream_queries: list[dict[str, str]] = []
        self.random_fails = False
        self.genres = list(GENRES)
        self.app = web.Application()
        self.app.router.add_get("/rest/ping", self.ping)
        self.app.router.add_get("/rest/getRandomSongs", self.random_songs)
        self.app.router.add_get("/rest/getGenres", self.get_genres)
        self.app.router.add_get("/rest/stream", self.stream)

    @staticmethod
    def envelope(status: str, **extra: Any) -> dict[str, Any]:
        return {"subsonic-response": {"status": status, "version": "1.16.1", **extra}}

    async def ping(self, request: web.Request) -> web.Response:
        good = request.query.get("t") == "good"
        return web.json_response(self.envelope("ok" if good else "failed"))

    async def random_songs(self, request: web.Request) -> web.Response:
        if self.random_fails:
            return web.Response(status=503, text="down")
        self.random_calls += 1
        size = int(request.query["size"])
        genre = request.query.get("genre")
        pool = [s for s in self.library if genre is None or s.get("genre") == genre]
        start = (self.random_calls * 7) % max(len(pool), 1)
        picked = (pool[start:] + pool[:start])[:size]
        return web.json_response(self.envelope("ok", randomSongs={"song": picked}))

    async def get_genres(self, request: web.Request) -> web.Response:
        return web.json_response(self.envelope("ok", genres={"genre": self.genres}))

    async def stream(self, request: web.Request) -> web.Response:
        self.stream_queries.append(dict(request.query))
        return web.Response(body=MP3, content_type="audio/mpeg")


def radio_config(**overrides: Any) -> BridgeConfig:
    base = BridgeConfig(
        navidrome_url="http://n.invalid",
        user="dan",
        password="pw",
        salt="abcd",
        secret="SECRET",
        advertise_host="10.0.0.5",
        port=9001,
        device_base="http://device.invalid",
        status_url="http://device.invalid/status",
        radio=True,
        radio_token=TOKEN,
    )
    return dataclasses.replace(base, **overrides)


def with_radio(
    scenario: Callable[[TestClient[Any, Any], FakeNavidrome], Awaitable[None]],
    fake: FakeNavidrome | None = None,
    **config: Any,
) -> None:
    async def go() -> None:
        nd = fake or FakeNavidrome()
        async with TestServer(nd.app) as server:
            cfg = radio_config(navidrome_url=str(server.make_url("")).rstrip("/"), **config)
            async with TestClient(TestServer(build_app(cfg, FakeWiim()))) as client:
                await scenario(client, nd)

    run(go())


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# configuration


def resolve(argv: list[str] | None = None, env: dict[str, str] | None = None) -> Any:
    args = bridge_server.parse_args(
        ["--advertise-host", "192.0.2.10", "--device-host", "192.0.2.50", *(argv or [])]
    )
    return bridge_server.resolve_settings(args, env or {})


def test_radio_is_off_by_default() -> None:
    settings = resolve()
    assert settings.radio is False
    assert settings.radio_token == ""
    assert settings.radio_state_path is None
    assert settings.radio_stream_url is None
    assert settings.radio_format == "raw"


def test_radio_needs_a_token() -> None:
    with pytest.raises(ConfigError, match="BRIDGE_RADIO_TOKEN"):
        resolve(env={"BRIDGE_RADIO": "true"})
    with pytest.raises(ConfigError, match="BRIDGE_RADIO_TOKEN"):
        resolve(["--radio"])


def test_radio_token_without_the_switch_is_ignored() -> None:
    assert resolve(env={"BRIDGE_RADIO_TOKEN": "x"}).radio is False


def test_radio_settings_from_env() -> None:
    settings = resolve(
        env={
            "BRIDGE_RADIO": "yes",
            "BRIDGE_RADIO_TOKEN": "tok",
            "BRIDGE_RADIO_STATE_PATH": "/data/radio.json",
            "BRIDGE_RADIO_STREAM_URL": "http://radio.example:8000/radio.mp3",
            "BRIDGE_RADIO_FORMAT": "mp3",
        }
    )
    assert settings.radio is True
    assert settings.radio_token == "tok"
    assert settings.radio_state_path == Path("/data/radio.json")
    assert settings.radio_stream_url == "http://radio.example:8000/radio.mp3"
    assert settings.radio_format == "mp3"


def test_radio_options_override_env() -> None:
    settings = resolve(
        ["--radio", "--radio-token", "cli", "--radio-format", "flac"],
        {"BRIDGE_RADIO_TOKEN": "env"},
    )
    assert (settings.radio, settings.radio_token, settings.radio_format) == (True, "cli", "flac")


def test_radio_config_reaches_the_bridge_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in [n for n in os.environ if n.startswith("BRIDGE_")]:
        monkeypatch.delenv(name)
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
    argv = [*server_args(tmp_path, good_creds(tmp_path)), "--radio", "--radio-token", "tok"]
    assert bridge_server.main(argv) == 0
    config = captured["config"]
    assert (config.radio, config.radio_token, config.radio_format) == (True, "tok", "raw")
    assert "tok" not in repr(config)


def test_main_fails_without_a_radio_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in [n for n in os.environ if n.startswith("BRIDGE_")]:
        monkeypatch.delenv(name)
    argv = [*server_args(tmp_path, good_creds(tmp_path)), "--radio"]
    assert bridge_server.main(argv) == 2
    assert "error: set BRIDGE_RADIO_TOKEN" in capsys.readouterr().err


# routes absent when disabled


def test_routes_absent_when_disabled() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        resp = await client.get("/radio/next", headers=bearer())
        assert resp.status == 404
        assert await resp.text() != "no songs available"
        resp = await client.put("/radio/settings", json={})
        assert resp.status == 404
        resp = await client.get("/remote/config.json")
        assert await resp.json() == {"radio": False}
        assert fake.random_calls == 0

    with_radio(scenario, radio=False)


def test_capability_flag_when_enabled() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        resp = await client.get("/remote/config.json")
        assert resp.status == 200
        assert await resp.json() == {"radio": True}
        assert "default-src 'none'" in resp.headers["Content-Security-Policy"]

    with_radio(scenario)


# /radio/next


def test_next_requires_the_token() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        assert (await client.get("/radio/next")).status == 401
        assert (await client.get("/radio/next", headers=bearer("nope"))).status == 401
        assert (await client.get("/radio/next?token=nope")).status == 401
        assert (await client.get("/radio/next", headers={"Authorization": TOKEN})).status == 401
        assert (await client.get("/radio/next", headers=bearer())).status == 200
        assert (await client.get(f"/radio/next?token={TOKEN}")).status == 200

    with_radio(scenario)


def test_next_returns_an_annotate_line_for_this_host() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        resp = await client.get("/radio/next", headers=bearer())
        assert resp.content_type == "text/plain"
        line = await resp.text()
        assert "\n" not in line
        head, _, url = line.partition('":http')
        url = "http" + url
        assert head.startswith('annotate:title="Song ')
        assert 'artist="Art",album="Alb' in line
        host = f"{client.host}:{client.port}"
        assert url.startswith(f"http://{host}/radio/track/s")
        assert url.endswith(f"?token={TOKEN}")
        status = await (await client.get("/radio/settings", params=LOGIN)).json()
        assert status["nowPlaying"]["artist"] == "Art"
        assert f"annotate:title=\"{status['nowPlaying']['title']}\"" in line

    with_radio(scenario)


def test_next_is_503_when_navidrome_has_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(radio_module, "NEXT_WAIT_S", 0.2)
    fake = FakeNavidrome()
    fake.random_fails = True

    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        assert (await client.get("/radio/next", headers=bearer())).status == 503
        fake.random_fails = False

    with_radio(scenario, fake)


def test_annotate_escaping() -> None:
    song = {"title": 'He said "hi" \\ back', "artist": "A\nB\tC", "album": "x#{y}"}
    line = annotate_line(song, "http://h/radio/track/1?token=t")
    assert line == (
        'annotate:title="He said \\"hi\\" \\\\ back",artist="A B C",album="x# {y}":'
        "http://h/radio/track/1?token=t"
    )
    assert annotate_line({}, "http://h/x") == 'annotate:title="",artist="",album="":http://h/x'


def test_token_is_quoted_in_the_url() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        resp = await client.get("/radio/next", params={"token": "a b&c"})
        line = await resp.text()
        assert resp.status == 200
        assert line.endswith("?token=a%20b%26c")

    with_radio(scenario, radio_token="a b&c")


# /radio/track


def test_track_streams_only_handed_out_ids() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        assert (await client.get("/radio/track/s1", headers=bearer())).status == 404
        line = await (await client.get("/radio/next", headers=bearer())).text()
        url = line.rsplit(":http", 1)[1]
        path = "/" + url.split("/", 3)[3]
        assert (await client.get(path.split("?")[0])).status == 401
        assert (await client.get(path.split("?")[0], headers=bearer("bad"))).status == 401
        resp = await client.get(path)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "audio/mpeg"
        assert await resp.read() == MP3
        assert fake.stream_queries[-1]["format"] == "raw"
        assert fake.stream_queries[-1]["id"] in path
        head = await client.head(path)
        assert head.status == 200
        assert (await client.get("/radio/track/s399", headers=bearer())).status == 404
        assert (await client.get("/radio/track/..%2Fx", headers=bearer())).status == 404

    with_radio(scenario)


def test_track_uses_the_configured_format() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        line = await (await client.get("/radio/next", headers=bearer())).text()
        path = "/" + line.rsplit(":http", 1)[1].split("/", 3)[3]
        assert (await client.get(path)).status == 200
        assert fake.stream_queries[-1]["format"] == "mp3"

    with_radio(scenario, radio_format="mp3")


def test_radio_routes_are_hidden_on_the_device_port() -> None:
    async def go() -> None:
        fake = FakeNavidrome()
        main_sock = socket.create_server(("127.0.0.1", 0))
        device_sock = socket.create_server(("127.0.0.1", 0))
        main_port, device_port = (s.getsockname()[1] for s in (main_sock, device_sock))
        async with TestServer(fake.app) as navidrome:
            cfg = radio_config(
                navidrome_url=str(navidrome.make_url("")).rstrip("/"),
                port=main_port,
                device_port=device_port,
            )
            runner = web.AppRunner(build_app(cfg, FakeWiim()))
            await runner.setup()
            try:
                for sock in (main_sock, device_sock):
                    await web.SockSite(runner, sock).start()
                async with aiohttp.ClientSession() as http:
                    for path in ("/radio/next", "/radio/track/s1", "/remote/config.json"):
                        resp = await http.get(
                            f"http://127.0.0.1:{device_port}{path}", headers=bearer()
                        )
                        assert resp.status == 404
                    resp = await http.get(
                        f"http://127.0.0.1:{main_port}/radio/next", headers=bearer()
                    )
                    assert resp.status == 200
            finally:
                await runner.cleanup()

    run(go())


# /radio/settings


def test_settings_need_the_navidrome_login() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        assert (await client.get("/radio/settings")).status == 401
        assert (await client.get("/radio/settings", headers=bearer())).status == 401
        assert (await client.get("/radio/settings", params={**LOGIN, "t": "bad"})).status == 401
        assert (await client.put("/radio/settings", json={})).status == 401
        assert (await client.get("/radio/settings", params=LOGIN)).status == 200

    with_radio(scenario)


def test_get_settings() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        body = await (await client.get("/radio/settings", params=LOGIN)).json()
        assert body["settings"] == {
            "buckets": [],
            "skipInterludes": True,
            "skipInstrumentals": True,
        }
        assert body["buckets"] == [
            {"name": "Rock", "songCount": 30},
            {"name": "Hip Hop", "songCount": 20},
            {"name": "Jazz", "songCount": 10},
        ]
        assert body["nowPlaying"] is None
        assert body["streamUrl"] == "http://radio.example:8000/radio.mp3"

    with_radio(scenario, radio_stream_url="http://radio.example:8000/radio.mp3")


def test_put_settings_validates() -> None:
    async def scenario(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        async def put(payload: Any) -> web.Response:
            return await client.put("/radio/settings", params=LOGIN, json=payload)  # type: ignore[no-any-return]

        assert (await put({"buckets": ["Polka"]})).status == 400
        assert (await put({"buckets": "Rock"})).status == 400
        assert (await put({"buckets": [5]})).status == 400
        assert (await put({"skipInterludes": "yes"})).status == 400
        assert (await put([])).status == 400
        raw = await client.put("/radio/settings", params=LOGIN, data="{nope")
        assert raw.status == 400
        resp = await put({"buckets": ["Rock", "Rock"], "skipInstrumentals": False})
        assert resp.status == 200
        body = await resp.json()
        assert body["settings"] == {
            "buckets": ["Rock"],
            "skipInterludes": True,
            "skipInstrumentals": False,
        }

    with_radio(scenario)


def test_settings_persist_and_reload(tmp_path: Path) -> None:
    state = tmp_path / "sub" / "radio.json"

    async def change(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        resp = await client.put(
            "/radio/settings",
            params=LOGIN,
            json={"buckets": ["Jazz"], "skipInterludes": False, "skipInstrumentals": False},
        )
        assert resp.status == 200

    async def check(client: TestClient[Any, Any], fake: FakeNavidrome) -> None:
        body = await (await client.get("/radio/settings", params=LOGIN)).json()
        assert body["settings"] == {
            "buckets": ["Jazz"],
            "skipInterludes": False,
            "skipInstrumentals": False,
        }

    with_radio(change, radio_state_path=state)
    assert json.loads(state.read_text())["buckets"] == ["Jazz"]
    assert [p.name for p in state.parent.iterdir()] == ["radio.json"]
    with_radio(check, radio_state_path=state)


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"buckets": "Rock"}', '{"skipInterludes": 1}', "\xff"],
)
def test_corrupt_state_file_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    path = tmp_path / "radio.json"
    path.write_text(content, encoding="latin-1")
    with caplog.at_level(logging.WARNING, logger="bridge.radio"):
        assert load_settings(path) == RadioSettings()
    assert "using defaults" in caplog.text


def test_missing_state_file_falls_back_to_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="bridge.radio"):
        assert load_settings(tmp_path / "none.json") == RadioSettings()
    assert "not found" in caplog.text
    assert load_settings(None) == RadioSettings()


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "radio.json"
    settings = RadioSettings(("Rock", "Pop"), False, True)
    save_settings(path, settings)
    assert load_settings(path) == settings


def test_parse_settings_partial_update() -> None:
    base = RadioSettings(("Rock",), True, False)
    assert parse_settings({}, base, ["Rock"]) == base
    assert parse_settings({"buckets": []}, base, ["Rock"]).buckets == ()
    with pytest.raises(SettingsError, match="unknown bucket: Pop"):
        parse_settings({"buckets": ["Pop"]}, base, ["Rock"])


# buffer behaviour


class Library:
    def __init__(self, size: int, step: int = 30, window: int = 50) -> None:
        self.size = size
        self.step = step
        self.window = window
        self.calls: list[tuple[int, str | None]] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.fail = False

    async def random(self, size: int, genre: str | None) -> list[dict[str, Any]]:
        self.calls.append((size, genre))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise ConnectionError("down")
        start = (len(self.calls) - 1) * self.step
        return [
            make_song((start + i) % self.size, genre=genre or "all")
            for i in range(min(size, self.window))
        ]

    async def genres(self) -> list[dict[str, Any]]:
        return GENRES


def make_radio(lib: Library, **kwargs: Any) -> Radio:
    kwargs.setdefault("wait", 1)
    return Radio(lib.random, lib.genres, None, **kwargs)


def test_buffer_refills_in_the_background_below_ten() -> None:
    async def go() -> None:
        lib = Library(1000, step=50)
        radio = make_radio(lib)
        radio.start()
        first = await radio.next_song()
        assert first is not None
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(radio._buffer) > 40
        before = len(lib.calls)
        for _ in range(45):
            assert await radio.next_song() is not None
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert len(lib.calls) > before
        assert all(size == 50 for size, _ in lib.calls)
        await radio.close()

    run(go())


def test_recent_songs_are_not_repeated() -> None:
    async def go() -> None:
        lib = Library(100)
        radio = make_radio(lib)
        radio.start()
        ids = [(await radio.next_song())["id"] for _ in range(90)]  # type: ignore[index]
        assert len(set(ids)) == 90
        await radio.close()

    run(go())


def test_small_library_repeats_instead_of_stalling() -> None:
    async def go() -> None:
        lib = Library(5, step=0)
        radio = make_radio(lib)
        radio.start()
        ids = [(await radio.next_song())["id"] for _ in range(20)]  # type: ignore[index]
        assert len(ids) == 20
        assert set(ids) == {f"s{n}" for n in range(5)}
        await radio.close()

    run(go())


def test_history_ring_is_bounded() -> None:
    async def go() -> None:
        radio = make_radio(Library(1000, step=50), history=3)
        radio.start()
        for _ in range(10):
            await radio.next_song()
        assert len(radio._history) == 3
        assert sum(radio._recent.values()) == 3
        await radio.close()

    run(go())


def test_changing_settings_flushes_the_buffer() -> None:
    async def go() -> None:
        lib = Library(1000, step=50)
        radio = make_radio(lib)
        radio.start()
        old = await radio.next_song()
        assert old is not None and old["genre"] == "all"
        assert len(radio._buffer) > 0
        await radio.update(RadioSettings(("Rock",), True, True))
        assert len(radio._buffer) == 0
        song = await radio.next_song()
        assert song is not None
        assert song["genre"] == "Rock"
        assert lib.calls[-1][1] == "Rock"
        await radio.close()

    run(go())


def test_in_flight_fetch_for_old_settings_is_discarded() -> None:
    async def go() -> None:
        lib = Library(1000, step=50)
        lib.gate = asyncio.Event()
        radio = make_radio(lib)
        radio.start()
        await lib.entered.wait()
        await radio.update(RadioSettings(("Jazz",), True, True))
        old_gate, lib.gate = lib.gate, None
        old_gate.set()
        song = await radio.next_song()
        assert song is not None
        assert song["genre"] == "Jazz"
        assert {s["genre"] for s in radio._buffer} == {"Jazz"}
        await radio.close()

    run(go())


def test_unknown_buckets_yield_nothing_and_back_off() -> None:
    async def go() -> None:
        sleeps: list[float] = []
        gate = asyncio.Event()

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                gate.set()
                await asyncio.sleep(3600)

        lib = Library(100)
        radio = make_radio(lib, sleep=fake_sleep, wait=0.05)
        await radio.update(RadioSettings(("Polka",), True, True))
        radio.start()
        assert await radio.next_song() is None
        await asyncio.wait_for(gate.wait(), 5)
        assert sleeps[:3] == [2, 4, 8]
        assert lib.calls == []
        await radio.close()

    run(go())


def test_navidrome_errors_never_crash_and_recover() -> None:
    async def go() -> None:
        lib = Library(1000, step=50)
        lib.fail = True
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await asyncio.sleep(0.01)

        radio = make_radio(lib, sleep=fake_sleep, wait=0.05)
        radio.start()
        assert await radio.next_song() is None
        assert sleeps and sleeps[0] == 2
        lib.fail = False
        song = await radio.next_song()
        assert song is not None
        await radio.close()

    run(go())


def test_handed_out_ids_expire() -> None:
    async def go() -> None:
        now = [0.0]
        radio = make_radio(Library(1000, step=50), clock=lambda: now[0], grant_ttl=100)
        radio.start()
        song = await radio.next_song()
        assert song is not None
        assert radio.granted_label(song["id"]) == song["title"]
        assert radio.granted_label("other") is None
        now[0] = 101
        assert radio.granted_label(song["id"]) is None
        await radio.close()

    run(go())


def test_genre_list_is_cached_for_ten_minutes() -> None:
    async def go() -> None:
        now = [0.0]
        calls = []

        async def genres() -> list[dict[str, Any]]:
            calls.append(1)
            return GENRES

        async def random(size: int, genre: str | None) -> list[dict[str, Any]]:
            return []

        radio = Radio(random, genres, None, clock=lambda: now[0])
        assert len(await radio.available_buckets()) == 3
        assert len(await radio.available_buckets()) == 3
        assert len(calls) == 1
        now[0] = 601
        await radio.available_buckets()
        assert len(calls) == 2

    run(go())


def test_filters_apply_to_the_buffer() -> None:
    async def go() -> None:
        async def random(size: int, genre: str | None) -> list[dict[str, Any]]:
            return [
                make_song(1, title="Intro", duration=30),
                make_song(2, title="Real (Instrumental)"),
                *[make_song(n) for n in range(10, 70)],
            ]

        async def genres() -> list[Any]:
            return []

        radio = Radio(random, genres, None, wait=1)
        radio.start()
        await radio.next_song()
        titles = {s["title"] for s in radio._buffer}
        assert "Intro" not in titles
        assert "Real (Instrumental)" not in titles
        await radio.close()

    run(go())
