import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bridge.jukebox import (
    Jukebox,
    JukeboxError,
    add_jukebox_routes,
    error_envelope,
    ok_envelope,
    track_from_song,
)
from bridge.player import PlayerError, PlayerStatus, Track
from bridge.soap import UpnpError

SONGS: dict[str, dict[str, Any]] = {
    "a": {"id": "a", "title": "A", "artist": "Art", "album": "Alb", "duration": 100},
    "b": {"id": "b", "title": "B", "duration": 200},
    "c": {"id": "c", "title": "C"},
}


class FakePlayer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.idx: int | None = None
        self.is_playing = False
        self.override: PlayerStatus | None = None
        self.failing: dict[str, Exception] = {}
        self.volume: int | None = None
        self.muted: bool | None = None
        self.ceiling = 100

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.failing:
            raise self.failing[name]

    async def load(self, tracks: Sequence[Track], start: int = 0, play: bool = True) -> None:
        self._rec("load", list(tracks), start, play)
        self.idx, self.is_playing = start, play

    async def append(self, tracks: Sequence[Track]) -> None:
        self._rec("append", list(tracks))

    async def remove(self, index: int) -> None:
        self._rec("remove", index)

    async def play_index(self, index: int) -> None:
        self._rec("play_index", index)
        self.idx, self.is_playing = index, True

    async def seek(self, seconds: int) -> None:
        self._rec("seek", seconds)

    async def pause(self) -> None:
        self._rec("pause")
        self.is_playing = False

    async def resume(self) -> None:
        self._rec("resume")
        self.is_playing = True

    async def set_mute(self, muted: bool) -> None:
        self._rec("set_mute", muted)

    async def set_volume(self, volume: int) -> int:
        clamped = min(max(volume, 0), self.ceiling)
        self._rec("set_volume", clamped)
        return clamped

    async def clear(self) -> None:
        self._rec("clear")
        self.idx, self.is_playing = None, False

    async def refresh(self) -> PlayerStatus:
        if self.override is not None:
            return self.override
        return PlayerStatus(self.idx, self.is_playing, 0.0, True, None, self.volume, self.muted)


async def lookup(song_id: str) -> Mapping[str, Any] | None:
    return SONGS.get(song_id)


def run(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(guarded())


def make() -> tuple[Jukebox, FakePlayer]:
    player = FakePlayer()
    return Jukebox(player, lookup), player  # type: ignore[arg-type]


def do(jb: Jukebox, action: str, ids: Sequence[str] = (), **kw: Any) -> dict[str, Any]:
    return run(  # type: ignore[no-any-return]
        jb.handle(action, ids, kw.get("index"), kw.get("offset"), kw.get("gain"), kw.get("mute"))
    )


def names(player: FakePlayer) -> list[str]:
    return [n for n, _ in player.calls]


def test_set_returns_status_and_touches_no_device() -> None:
    jb, player = make()
    payload = do(jb, "set", ["a", "b"])
    assert payload == {
        "jukeboxStatus": {"currentIndex": 0, "playing": False, "gain": 0.5, "position": 0, "muted": False}
    }
    assert player.calls == []


def test_set_errors_keep_playlist() -> None:
    jb, _ = make()
    do(jb, "set", ["a"])
    with pytest.raises(JukeboxError) as err:
        do(jb, "set", ["b", "zzz"])
    assert err.value.code == 70
    assert "zzz" in err.value.message
    assert [s["id"] for s in jb.songs] == ["a"]
    with pytest.raises(JukeboxError) as err:
        do(jb, "set")
    assert err.value.code == 10


def test_feishin_start_sequence_loads_once() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "skip", index=0, offset=0)
    do(jb, "start")
    assert len(player.calls) == 1
    name, (tracks, start, play) = player.calls[0]
    assert name == "load"
    assert [t.song_id for t in tracks] == ["a", "b"]
    assert (start, play) == (0, True)
    assert do(jb, "status")["jukeboxStatus"]["playing"] is True


def test_skip_before_start_sets_load_index_and_pending_seek() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "skip", index=1)
    do(jb, "start")
    assert player.calls[0][1][1] == 1
    assert names(player) == ["load"]

    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "skip", index=1, offset=30)
    do(jb, "start")
    assert player.calls[0][1][1] == 1
    assert player.calls[1] == ("seek", (30,))


def test_loaded_skip_seeks_or_jumps() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.calls.clear()
    do(jb, "skip", index=0, offset=42)
    assert player.calls == [("seek", (42,))]
    player.calls.clear()
    do(jb, "skip", index=1, offset=0)
    assert player.calls == [("play_index", (1,))]


def test_loaded_skip_while_stopped_stays_paused() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    do(jb, "stop")
    player.calls.clear()
    do(jb, "skip", index=1)
    assert player.calls == [("play_index", (1,)), ("pause", ())]


def test_stop_pauses_and_start_resumes_loaded() -> None:
    jb, player = make()
    do(jb, "set", ["a"])
    do(jb, "start")
    player.calls.clear()
    assert do(jb, "stop")["jukeboxStatus"]["playing"] is False
    do(jb, "start")
    assert names(player) == ["pause", "resume"]


def test_roll_forward_after_device_advance() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.override = PlayerStatus(1, True, 0.0, True, "PLAYING")
    assert do(jb, "get")["jukeboxPlaylist"]["currentIndex"] == 1
    player.calls.clear()
    status = do(jb, "remove", index=0)["jukeboxStatus"]
    assert player.calls == [("remove", (0,))]
    assert status["currentIndex"] == 0
    assert [s["id"] for s in jb.songs] == ["b"]
    player.override = PlayerStatus(0, True, 0.0, True, "PLAYING")
    player.calls.clear()
    do(jb, "add", ["c"])
    assert player.calls[0][0] == "append"
    assert [t.song_id for t in player.calls[0][1][0]] == ["c"]
    entries = do(jb, "get")["jukeboxPlaylist"]["entry"]
    assert [e["id"] for e in entries] == ["b", "c"]


def test_remove_current_while_loaded_clears_device() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.calls.clear()
    do(jb, "remove", index=0)
    assert player.calls == [("clear", ())]
    assert not jb.loaded
    assert not jb.playing
    assert [s["id"] for s in jb.songs] == ["b"]


def test_lost_control_unloads() -> None:
    jb, player = make()
    do(jb, "set", ["a"])
    do(jb, "start")
    player.override = PlayerStatus(None, False, 0.0, False, "PLAYING")
    assert do(jb, "status")["jukeboxStatus"]["playing"] is False
    player.calls.clear()
    do(jb, "start")
    assert names(player) == ["load"]


def test_clear_only_touches_device_when_loaded() -> None:
    jb, player = make()
    do(jb, "set", ["a"])
    do(jb, "clear")
    assert player.calls == []
    assert jb.songs == []
    do(jb, "set", ["a"])
    do(jb, "start")
    player.calls.clear()
    do(jb, "clear")
    assert player.calls == [("clear", ())]
    assert jb.songs == []
    assert not jb.loaded


def test_gain_and_shuffle() -> None:
    jb, _ = make()
    assert do(jb, "setGain", gain=0.3)["jukeboxStatus"]["gain"] == 0.3
    assert do(jb, "setGain", gain=7.0)["jukeboxStatus"]["gain"] == 1.0
    with pytest.raises(JukeboxError) as err:
        do(jb, "setGain")
    assert err.value.code == 10
    with pytest.raises(JukeboxError) as err:
        do(jb, "shuffle")
    assert err.value.code == 0


@pytest.mark.parametrize("exc", [PlayerError("boom"), UpnpError(701, "x")])
def test_device_errors_become_generic_errors(exc: Exception) -> None:
    jb, player = make()
    do(jb, "set", ["a"])
    player.failing["load"] = exc
    with pytest.raises(JukeboxError) as err:
        do(jb, "start")
    assert err.value.code == 0


def test_get_returns_raw_song_objects() -> None:
    jb, _ = make()
    do(jb, "set", ["b", "a"])
    entries = do(jb, "get")["jukeboxPlaylist"]["entry"]
    assert entries == [SONGS["b"], SONGS["a"]]


def test_track_from_song_and_envelopes() -> None:
    assert track_from_song(SONGS["a"]) == Track("a", "A", "Art", "Alb", 100)
    assert track_from_song({"id": 5}) == Track("5", "")
    assert ok_envelope({"x": 1})["subsonic-response"]["status"] == "ok"
    assert error_envelope(70, "m")["subsonic-response"]["error"] == {"code": 70, "message": "m"}


async def _allow(params: Mapping[str, str]) -> bool:
    return True


def serve(
    jb: Jukebox, authenticate: Any = _allow
) -> TestClient[Any, Any]:
    app = web.Application()
    add_jukebox_routes(app, jb, authenticate)
    return TestClient(TestServer(app))


def test_http_ok_and_auth_failure() -> None:
    seen: list[Mapping[str, str]] = []

    async def deny(params: Mapping[str, str]) -> bool:
        seen.append(params)
        return False

    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            resp = await client.get("/rest/jukeboxControl.view?action=status&u=x")
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            body = (await resp.json())["subsonic-response"]
            assert body["status"] == "ok"
            assert body["jukeboxStatus"]["currentIndex"] == 0
        async with serve(jb, deny) as client:
            resp = await client.get("/rest/jukeboxControl.view?action=status&u=x")
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            body = (await resp.json())["subsonic-response"]
            assert body["status"] == "failed"
            assert body["error"]["code"] == 40
        assert seen[0]["u"] == "x"
        assert seen[0]["action"] == "status"

    run(scenario())


def test_http_repeated_ids_on_all_variants() -> None:
    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            await client.get("/rest/jukeboxControl.view?action=set&id=b&id=a&id=c")
            assert [s["id"] for s in jb.songs] == ["b", "a", "c"]
            await client.get("/rest/jukeboxControl?action=set&id=c&id=a")
            assert [s["id"] for s in jb.songs] == ["c", "a"]
            resp = await client.post("/rest/jukeboxControl", data=[("action", "set"), ("id", "a"), ("id", "b")])
            assert (await resp.json())["subsonic-response"]["status"] == "ok"
            assert [s["id"] for s in jb.songs] == ["a", "b"]

    run(scenario())


def test_http_parameter_errors_and_options() -> None:
    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            resp = await client.get("/rest/jukeboxControl?u=x")
            assert (await resp.json())["subsonic-response"]["error"]["code"] == 10
            resp = await client.get("/rest/jukeboxControl?action=skip&index=abc")
            error = (await resp.json())["subsonic-response"]["error"]
            assert error["code"] == 10
            assert "index" in error["message"]
            for path in ("/rest/jukeboxControl", "/rest/jukeboxControl.view"):
                resp = await client.options(path)
                assert resp.status == 204
                assert resp.headers["Access-Control-Allow-Origin"] == "*"
                assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
                assert resp.headers["Access-Control-Allow-Headers"] == "*"

    run(scenario())


def test_set_gain_sets_device_volume() -> None:
    jb, player = make()
    status = do(jb, "setGain", gain=0.3)["jukeboxStatus"]
    assert ("set_volume", (30,)) in player.calls
    assert status["gain"] == 0.3


def test_set_gain_reports_the_clamped_volume() -> None:
    jb, player = make()
    player.ceiling = 60
    assert do(jb, "setGain", gain=0.9)["jukeboxStatus"]["gain"] == 0.6


def test_status_picks_up_volume_changed_on_the_remote() -> None:
    jb, player = make()
    do(jb, "set", ["a"])
    player.volume = 41
    status = do(jb, "status")["jukeboxStatus"]
    assert status["gain"] == 0.41
    assert status["playing"] is False
    assert "load" not in names(player)


def test_status_includes_muted_and_follows_refresh() -> None:
    jb, player = make()
    assert do(jb, "status")["jukeboxStatus"]["muted"] is False
    player.muted = True
    assert do(jb, "status")["jukeboxStatus"]["muted"] is True
    player.muted = False
    assert do(jb, "get")["jukeboxPlaylist"]["muted"] is False


def test_set_mute_calls_player_and_keeps_gain() -> None:
    jb, player = make()
    status = do(jb, "setMute", mute=True)["jukeboxStatus"]
    assert ("set_mute", (True,)) in player.calls
    assert status["muted"] is True
    assert status["gain"] == 0.5


def test_set_mute_requires_a_value() -> None:
    jb, _ = make()
    with pytest.raises(JukeboxError) as err:
        do(jb, "setMute")
    assert err.value.code == 10


def test_http_set_mute() -> None:
    async def scenario() -> None:
        jb, player = make()
        async with serve(jb) as client:
            resp = await client.get("/rest/jukeboxControl?action=setMute&mute=false")
            body = (await resp.json())["subsonic-response"]
            assert body["jukeboxStatus"]["muted"] is False
            assert ("set_mute", (False,)) in player.calls
            resp = await client.get("/rest/jukeboxControl?action=setMute&mute=TRUE")
            assert (await resp.json())["subsonic-response"]["jukeboxStatus"]["muted"] is True
            resp = await client.get("/rest/jukeboxControl?action=setMute&mute=maybe")
            error = (await resp.json())["subsonic-response"]["error"]
            assert error["code"] == 10
            assert "mute" in error["message"]
            resp = await client.get("/rest/jukeboxControl?action=status")
            assert "muted" in (await resp.json())["subsonic-response"]["jukeboxStatus"]

    run(scenario())
