import asyncio
import gzip
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_player import FakeWiim, url_for

import bridge.jukebox as jukebox_module
from bridge.jukebox import (
    LOOKUP_CONCURRENCY,
    MAX_IDS_PER_CALL,
    MAX_QUEUE,
    SEEK_ATTEMPTS,
    SEEK_FRESH_TIMEOUT,
    SEEK_TOLERANCE,
    SEEK_WAIT_STEP,
    SEEK_WAIT_TIMEOUT,
    SYNC_REUSE_S,
    Jukebox,
    JukeboxError,
    add_jukebox_routes,
    error_envelope,
    ok_envelope,
    track_from_song,
)
from bridge.player import (
    RETRY_701_S,
    Player,
    PlayerError,
    PlayerStatus,
    QueueDesyncError,
    Track,
)
from bridge.scrobble import Scrobble, Scrobbler
from bridge.soap import UpnpError
from bridge.subsonic import AuthResult
from bridge.wiim import parse_queue_tracks

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
        self.pos = 0.0
        self.seeks_to_take = 1
        self.landing_offset = 0
        self.lag_checks = 0
        self.lag_left = 0
        self.pending: float | None = None
        self.seek_count = 0
        self.pre_refreshes: list[tuple[int | None, bool]] = []
        self.last_ok = False
        self.seek_ok: list[bool] = []
        self.stale: list[float] = []
        self.stale_at_seek: list[int] = []
        self.sampled_at = 0.0
        self.refreshes = 0

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

    async def insert_next(self, tracks: Sequence[Track]) -> None:
        self._rec("insert_next", list(tracks))

    async def replace_upcoming(self, tracks: Sequence[Track]) -> None:
        self._rec("replace_upcoming", list(tracks))

    async def move(self, index: int, to: int) -> None:
        self._rec("move", index, to)
        if self.idx is not None:
            if index < self.idx <= to:
                self.idx -= 1
            elif to <= self.idx < index:
                self.idx += 1

    async def play_index(self, index: int) -> None:
        self._rec("play_index", index)
        self.idx, self.is_playing = index, True

    async def seek(self, seconds: int) -> None:
        self._rec("seek", seconds)
        self.seek_ok.append(self.last_ok)
        self.stale_at_seek.append(len(self.stale))
        self.seek_count += 1
        if self.seek_count >= self.seeks_to_take:
            if self.lag_checks:
                self.pending, self.lag_left = seconds + self.landing_offset, self.lag_checks
            else:
                self.pos = seconds + self.landing_offset

    async def pause(self) -> None:
        self._rec("pause")
        self.is_playing = False

    async def resume(self) -> None:
        self._rec("resume")
        self.is_playing = True

    async def set_mute(self, muted: bool) -> None:
        self._rec("set_mute", muted)

    async def set_repeat(self, repeat: str) -> None:
        self._rec("set_repeat", repeat)

    async def set_volume(self, volume: int) -> int:
        clamped = min(max(volume, 0), self.ceiling)
        self._rec("set_volume", clamped)
        return clamped

    async def clear(self) -> None:
        self._rec("clear")
        self.idx, self.is_playing = None, False

    async def refresh(self) -> PlayerStatus:
        self.refreshes += 1
        if self.override is not None:
            return self.override
        if self.pending is not None:
            self.lag_left -= 1
            if self.lag_left <= 0:
                self.pos, self.pending = self.pending, None
        if self.stale:
            return PlayerStatus(self.idx, self.is_playing, self.stale.pop(0), True, None, None, None, self.sampled_at)
        idx, playing = self.pre_refreshes.pop(0) if self.pre_refreshes else (self.idx, self.is_playing)
        self.last_ok = playing and idx == self.idx
        return PlayerStatus(idx, playing, self.pos, True, None, self.volume, self.muted, self.sampled_at)


@pytest.fixture(autouse=True)
def read_the_device_on_every_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    # most tests poll back to back and expect every poll to read the device
    monkeypatch.setattr(jukebox_module, "SYNC_REUSE_S", 0.0)


@pytest.fixture
def reuse_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jukebox_module, "SYNC_REUSE_S", SYNC_REUSE_S)


async def lookup(song_id: str) -> Mapping[str, Any] | None:
    return SONGS.get(song_id)


def run(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(guarded())


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds


def make() -> tuple[Jukebox, FakePlayer]:
    jb, player, _ = make_timed()
    return jb, player


def make_timed() -> tuple[Jukebox, FakePlayer, FakeTime]:
    player = FakePlayer()
    fake = FakeTime()
    jb = Jukebox(player, lookup, sleep=fake.sleep, clock=fake.clock)  # type: ignore[arg-type]
    return jb, player, fake


def do(jb: Jukebox, action: str, ids: Sequence[str] = (), **kw: Any) -> dict[str, Any]:
    return run(  # type: ignore[no-any-return]
        jb.handle(
            action,
            ids,
            kw.get("index"),
            kw.get("offset"),
            kw.get("gain"),
            kw.get("mute"),
            kw.get("to_index"),
            kw.get("shuffle"),
            kw.get("repeat"),
        )
    )


def names(player: FakePlayer) -> list[str]:
    return [n for n, _ in player.calls]


def test_set_returns_status_and_touches_no_device() -> None:
    jb, player = make()
    payload = do(jb, "set", ["a", "b"])
    version = payload["jukeboxStatus"].pop("queueVersion")
    assert re.fullmatch(r"[0-9a-f]+:1", version)
    assert payload == {
        "jukeboxStatus": {
            "currentIndex": 0,
            "playing": False,
            "gain": 0.5,
            "position": 0,
            "positionMs": 0,
            "muted": False,
            "shuffle": False,
            "repeat": "off",
        }
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


def seek_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "bridge.jukebox" and r.levelno == logging.WARNING
    ]


def test_start_waits_for_playing_before_seeking() -> None:
    jb, player, _ = make_timed()
    do(jb, "set", ["a", "b"])
    do(jb, "skip", index=0, offset=34)
    player.pre_refreshes = [(None, False), (None, False)]
    status = do(jb, "start")["jukeboxStatus"]
    assert names(player) == ["load", "seek"]
    assert player.seek_ok == [True]
    assert player.pos == 34
    assert status["playing"] is True
    assert status["position"] == 34


def test_seek_retried_when_first_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, _ = make_timed()
    player.seeks_to_take = 2
    do(jb, "set", ["a"])
    do(jb, "skip", index=0, offset=34)
    do(jb, "start")
    assert names(player) == ["load", "seek", "seek"]
    assert seek_warnings(caplog) == []


def test_seek_confirmed_on_third_check(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, _ = make_timed()
    player.lag_checks = 3
    do(jb, "set", ["a"])
    do(jb, "skip", index=0, offset=34)
    do(jb, "start")
    assert names(player).count("seek") == 1
    assert seek_warnings(caplog) == []


def test_seek_never_confirmed_still_starts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, _ = make_timed()
    player.seeks_to_take = 99
    do(jb, "set", ["a"])
    do(jb, "skip", index=0, offset=34)
    status = do(jb, "start")["jukeboxStatus"]
    assert names(player).count("seek") == SEEK_ATTEMPTS
    assert seek_warnings(caplog) == ["seek to 34s not confirmed by the device"]
    assert status["playing"] is True


def test_device_never_starts_sends_no_seek(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, fake = make_timed()
    player.pre_refreshes = [(0, False)] * 1000
    do(jb, "set", ["a"])
    do(jb, "skip", index=0, offset=34)
    status = do(jb, "start")["jukeboxStatus"]
    assert "seek" not in names(player)
    assert seek_warnings(caplog) == ["seek to 34s skipped: device did not start track 0 in time"]
    assert SEEK_FRESH_TIMEOUT - SEEK_WAIT_STEP < fake.slept <= SEEK_FRESH_TIMEOUT + SEEK_WAIT_STEP
    assert status["playing"] is True


def test_seek_waits_for_matching_index() -> None:
    jb, player, _ = make_timed()
    do(jb, "set", ["a", "b"])
    do(jb, "skip", index=1, offset=34)
    player.pre_refreshes = [(0, True), (0, True)]
    do(jb, "start")
    assert names(player) == ["load", "seek"]
    assert player.seek_ok == [True]


def test_seek_within_tolerance_is_confirmed() -> None:
    jb, player, _ = make_timed()
    player.landing_offset = -2
    do(jb, "set", ["a"])
    do(jb, "skip", index=0, offset=34)
    do(jb, "start")
    assert names(player).count("seek") == 1


def info_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "bridge.jukebox" and r.levelno == logging.INFO
    ]


def playing_fake(idx: int = 0) -> tuple[Jukebox, FakePlayer, FakeTime]:
    jb, player, fake = make_timed()
    player.idx, player.is_playing = idx, True
    return jb, player, fake


def test_stale_status_then_fresh_seeks_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, _ = playing_fake()
    player.stale = [34.0] * 3
    assert run(jb._seek_when_playing(0, 34)) is True
    assert names(player) == ["seek"]
    assert player.stale_at_seek == [0]
    assert info_messages(caplog) == ["resumed at 34s after 1 seek(s)"]
    assert seek_warnings(caplog) == []


def test_stale_then_fresh_with_dropped_first_seek_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, _ = playing_fake()
    player.stale = [34.0] * 3
    player.seeks_to_take = 2
    assert run(jb._seek_when_playing(0, 34)) is True
    assert names(player) == ["seek", "seek"]
    assert info_messages(caplog) == ["resumed at 34s after 2 seek(s)"]
    assert seek_warnings(caplog) == []


def test_stale_forever_at_target_sends_no_seek(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, fake = playing_fake()
    player.stale = [34.0] * 1000
    assert run(jb._seek_when_playing(0, 34)) is True
    assert "seek" not in names(player)
    assert info_messages(caplog) == ["resume at 34s: device already there"]
    assert seek_warnings(caplog) == []
    assert fake.slept <= SEEK_FRESH_TIMEOUT + SEEK_WAIT_STEP


@pytest.mark.parametrize("seconds", [0, 1, SEEK_TOLERANCE])
def test_seek_within_tolerance_of_start_is_skipped(seconds: int) -> None:
    jb, player, fake = playing_fake()
    assert run(jb._seek_when_playing(0, seconds)) is True
    assert player.calls == []
    assert fake.slept == 0


def test_seek_just_past_tolerance_takes_normal_path() -> None:
    jb, player, _ = playing_fake()
    assert run(jb._seek_when_playing(0, SEEK_TOLERANCE + 1)) is True
    assert names(player) == ["seek"]


def test_wrong_index_with_low_position_does_not_end_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player, _ = playing_fake(idx=1)
    assert run(jb._seek_when_playing(0, 34)) is False
    assert "seek" not in names(player)
    assert seek_warnings(caplog) == ["seek to 34s skipped: device did not start track 0 in time"]


def test_start_without_position_sends_no_seek_and_no_wait() -> None:
    jb, player, fake = make_timed()
    do(jb, "set", ["a"])
    do(jb, "start")
    assert names(player) == ["load"]
    assert fake.slept == 0


def test_skip_to_other_track_with_offset_waits_for_playing() -> None:
    jb, player, _ = make_timed()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.calls.clear()
    player.pre_refreshes = [(0, True), (1, False)]
    do(jb, "skip", index=1, offset=42)
    assert names(player) == ["play_index", "seek"]
    assert player.seek_ok == [True]


def test_skip_same_track_seeks_without_waiting() -> None:
    jb, player, fake = make_timed()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.calls.clear()
    do(jb, "skip", index=0, offset=42)
    assert player.calls == [("seek", (42,))]
    assert fake.slept == 0


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


def test_lost_control_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player = make()
    do(jb, "set", ["a"])
    do(jb, "start")
    caplog.clear()
    player.override = PlayerStatus(None, False, 0.0, False, "PLAYING")
    do(jb, "status")
    (record,) = [r for r in caplog.records if r.name == "bridge.jukebox"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "lost control of the device queue"
    do(jb, "status")
    assert len([r for r in caplog.records if r.name == "bridge.jukebox"]) == 1


def test_failure_warning_redacts_query_strings(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    text = "GET http://host/x?u=dan&t=abc123&s=salt failed"
    jb, player = make()
    do(jb, "set", ["a"])
    player.failing["load"] = PlayerError(text)

    async def scenario() -> dict[str, Any]:
        async with serve(jb) as client:
            resp = await client.get("/rest/jukeboxControl?action=start&u=dan&p=pw")
            return await resp.json()  # type: ignore[no-any-return]

    body = run(scenario())
    assert body["subsonic-response"]["error"] == {"code": 0, "message": text}
    (record,) = [r for r in caplog.records if r.name == "bridge.jukebox"]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "?<redacted>" in message
    for leaked in ("abc123", "salt", "u=dan"):
        assert leaked not in message


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


async def _allow(params: Mapping[str, str]) -> AuthResult:
    return AuthResult.OK


def serve(jb: Jukebox, authenticate: Any = _allow, **client_kwargs: Any) -> TestClient[Any, Any]:
    app = web.Application()
    add_jukebox_routes(app, jb, authenticate)
    return TestClient(TestServer(app), **client_kwargs)


def test_http_ok_and_auth_failure() -> None:
    seen: list[Mapping[str, str]] = []

    async def deny(params: Mapping[str, str]) -> AuthResult:
        seen.append(params)
        return AuthResult.REJECTED

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


def test_http_unreachable_navidrome_is_not_a_login_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def unreachable(params: Mapping[str, str]) -> AuthResult:
        return AuthResult.UNREACHABLE

    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb, unreachable) as client:
            resp = await client.get("/rest/jukeboxControl?action=set&id=a")
            assert resp.status == 200
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
            error = (await resp.json())["subsonic-response"]["error"]
            assert error == {"code": 0, "message": "Navidrome is unreachable, try again shortly"}
            assert jb.songs == []

    run(scenario())
    assert jukebox_lines(caplog) == []


def test_http_rejected_login_stays_code_40_and_runs_nothing() -> None:
    async def rejected(params: Mapping[str, str]) -> AuthResult:
        return AuthResult.REJECTED

    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb, rejected) as client:
            resp = await client.get("/rest/jukeboxControl?action=set&id=a")
            error = (await resp.json())["subsonic-response"]["error"]
            assert error == {"code": 40, "message": "Wrong username or password"}
            assert jb.songs == []

    run(scenario())


def test_http_get_reply_is_gzipped_for_clients_that_accept_it() -> None:
    jb, _ = make_numbered()
    do(jb, "set", numbers(500))

    async def scenario() -> None:
        async with serve(jb, auto_decompress=False) as client:
            url = "/rest/jukeboxControl?action=get"
            for accept in ("gzip", "gzip, deflate, br, zstd", "deflate, GZip;q=0.8"):
                resp = await client.get(url, headers={"Accept-Encoding": accept})
                assert resp.headers["Content-Encoding"] == "gzip", accept
                assert resp.headers["Access-Control-Allow-Origin"] == "*"
                wire = await resp.read()
                body = json.loads(gzip.decompress(wire))["subsonic-response"]
                assert [e["id"] for e in body["jukeboxPlaylist"]["entry"]] == numbers(500)
                assert len(wire) < len(gzip.decompress(wire)) / 2
            plain = await client.get(url, headers={"Accept-Encoding": "identity"})
            assert "Content-Encoding" not in plain.headers
            assert len((await plain.json())["subsonic-response"]["jukeboxPlaylist"]["entry"]) == 500

    run(scenario())


def test_http_only_the_get_reply_is_compressed() -> None:
    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb, auto_decompress=False) as client:
            gzip_header = {"Accept-Encoding": "gzip"}
            for query in (
                "action=status",
                "action=set&id=a",
                "action=setGain&gain=0.2",
                "action=skip&index=9",
                "action=nope",
                "action=get&index=abc",
            ):
                resp = await client.get(f"/rest/jukeboxControl?{query}", headers=gzip_header)
                assert "Content-Encoding" not in resp.headers, query
                assert "subsonic-response" in json.loads(await resp.read())
            resp = await client.get("/rest/jukeboxControl?action=get", headers=gzip_header)
            assert resp.headers["Content-Encoding"] == "gzip"

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


def test_describe_known_unknown_and_first_match() -> None:
    jb, _ = make()
    do(jb, "set", ["a", "b", "a"])
    assert jb.describe("a") == ("Art - A", "Alb")
    assert jb.describe("b") == ("B", "")
    assert jb.describe("zzz") is None


def test_describe_remembers_replaced_queue() -> None:
    jb, _ = make()
    do(jb, "set", ["a"])
    do(jb, "set", ["b"])
    assert jb.describe("a") == ("Art - A", "Alb")
    do(jb, "add", ["c"])
    do(jb, "clear")
    assert jb.describe("c") == ("C", "")


def test_describe_memory_drops_oldest_after_500() -> None:
    async def many(song_id: str) -> Mapping[str, Any] | None:
        return {"id": song_id, "title": f"T{song_id}"}

    jb = Jukebox(FakePlayer(), many)  # type: ignore[arg-type]
    ids = [str(n) for n in range(501)]
    run(jb.handle("set", ids[:500], None, None, None))
    run(jb.handle("add", ids[500:], None, None, None))
    run(jb.handle("clear", [], None, None, None))
    assert jb.describe("0") is None
    assert jb.describe("1") == ("T1", "")
    assert jb.describe("500") == ("T500", "")


def jukebox_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "bridge.jukebox"]


def test_http_action_lines_name_the_track(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            await client.get("/rest/jukeboxControl?action=start")
            await client.get("/rest/jukeboxControl?action=set&id=a&id=b")
            await client.get("/rest/jukeboxControl?action=skip&index=1")
            await client.get("/rest/jukeboxControl?action=add&id=c")
            await client.get("/rest/jukeboxControl?action=setGain&gain=0.2")

    run(scenario())
    lines = jukebox_lines(caplog)
    assert lines[0].endswith("mute=-")
    assert lines[1].endswith(' first="Art - A"')
    assert lines[2].endswith(' track="B"')
    assert lines[3].endswith(' first="C"')
    assert lines[4].endswith("mute=-")


def test_http_start_on_empty_queue_has_no_suffix(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            await client.get("/rest/jukeboxControl?action=start")

    run(scenario())
    (line,) = jukebox_lines(caplog)
    assert line == "jukebox action=start index=- offset=- ids=0 gain=- mute=-"


def make_walled() -> tuple[Jukebox, FakePlayer, list[float]]:
    player = FakePlayer()
    wall = [1000.0]
    jb = Jukebox(player, lookup, wall=lambda: wall[0])  # type: ignore[arg-type]
    return jb, player, wall


def test_position_ms_adds_time_since_device_sample_while_playing() -> None:
    jb, player, wall = make_walled()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.pos, player.sampled_at = 12.345, 1000.0
    wall[0] = 1000.4
    status = do(jb, "get")["jukeboxPlaylist"]
    assert status["position"] == 12
    assert status["positionMs"] == 12745


def test_position_ms_frozen_while_paused() -> None:
    jb, player, wall = make_walled()
    do(jb, "set", ["a"])
    do(jb, "start")
    do(jb, "stop")
    player.pos, player.sampled_at = 7.5, 1000.0
    wall[0] = 1005.0
    status = do(jb, "get")["jukeboxPlaylist"]
    assert status["playing"] is False
    assert status["positionMs"] == 7500


def test_position_ms_skip_and_clear_use_whole_seconds() -> None:
    jb, player, wall = make_walled()
    do(jb, "set", ["a", "b"])
    wall[0] = 2000.0
    status = do(jb, "skip", index=1, offset=34)["jukeboxStatus"]
    assert (status["position"], status["positionMs"]) == (34, 34000)
    do(jb, "clear")
    assert do(jb, "get")["jukeboxPlaylist"]["positionMs"] == 0


def test_position_ms_resume_does_not_count_paused_time() -> None:
    jb, player, wall = make_walled()
    do(jb, "set", ["a"])
    do(jb, "start")
    do(jb, "stop")
    player.pos, player.sampled_at = 10.0, 1000.0
    wall[0] = 1060.0
    status = do(jb, "start")["jukeboxStatus"]
    assert status["positionMs"] == 10000
    wall[0] = 1060.5
    player.sampled_at = 1060.2
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 10300


def version_of(jb: Jukebox) -> str:
    return str(do(jb, "status")["jukeboxStatus"]["queueVersion"])


def test_queue_version_bumps_only_when_songs_change() -> None:
    jb, player = make()
    first = version_of(jb)
    assert first == version_of(jb)
    seen = [first]
    for action, kw in (
        ("set", {"ids": ["a", "b"]}),
        ("add", {"ids": ["c"]}),
        ("remove", {"index": 2}),
        ("clear", {}),
    ):
        do(jb, action, **kw)
        seen.append(version_of(jb))
    assert len(set(seen)) == len(seen)
    do(jb, "set", ["a", "b"])
    before = version_of(jb)
    do(jb, "skip", index=1, offset=0)
    do(jb, "start")
    do(jb, "stop")
    assert version_of(jb) == before
    assert do(jb, "get")["jukeboxPlaylist"]["queueVersion"] == before


def test_queue_version_boot_id_differs_between_instances() -> None:
    assert version_of(make()[0]).split(":")[0] != version_of(make()[0]).split(":")[0]


def test_add_does_not_sync_the_device() -> None:
    jb, player = make()
    do(jb, "set", ["a"])
    do(jb, "start")
    player.pre_refreshes = [(5, True)]
    do(jb, "add", ["b"])
    assert player.pre_refreshes == [(5, True)]
    assert names(player)[-1] == "append"


def seeking_setup() -> tuple[Jukebox, FakePlayer, list[float]]:
    jb, player, wall = make_walled()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.pos, player.sampled_at = 50.0, wall[0]
    do(jb, "skip", index=0, offset=100)
    return jb, player, wall


def test_seek_hold_masks_stale_position() -> None:
    jb, player, wall = seeking_setup()
    wall[0] += 1.0
    player.pos, player.sampled_at = 51.0, wall[0]
    status = do(jb, "status")["jukeboxStatus"]
    assert status["position"] == 101
    assert status["positionMs"] == 101000


def test_seek_hold_releases_when_device_reaches_target() -> None:
    jb, player, wall = seeking_setup()
    wall[0] += 1.2
    player.pos, player.sampled_at = 101.0, wall[0]
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 101000
    wall[0] += 0.5
    player.pos, player.sampled_at = 100.0, wall[0]
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 100000


def test_seek_hold_releases_on_index_change() -> None:
    jb, player, wall = seeking_setup()
    wall[0] += 1.0
    player.idx, player.pos, player.sampled_at = 1, 3.0, wall[0]
    status = do(jb, "status")["jukeboxStatus"]
    assert (status["currentIndex"], status["positionMs"]) == (1, 3000)
    player.idx, player.pos, player.sampled_at = 0, 4.0, wall[0]
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 4000


def test_seek_hold_releases_on_pause() -> None:
    jb, player, wall = seeking_setup()
    wall[0] += 1.0
    player.is_playing, player.pos, player.sampled_at = False, 51.0, wall[0]
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 51000
    player.is_playing, player.sampled_at = True, wall[0]
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 51000


def test_seek_hold_expires_after_three_seconds() -> None:
    jb, player, wall = seeking_setup()
    wall[0] += 3.5
    player.pos, player.sampled_at = 52.0, wall[0]
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 52000


def test_seek_hold_dropped_by_new_seek() -> None:
    jb, player, wall = seeking_setup()
    wall[0] += 1.0
    do(jb, "skip", index=0, offset=10)
    player.pos, player.sampled_at = 10.0, wall[0]
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 10000


async def numbered(song_id: str) -> Mapping[str, Any] | None:
    return {"id": song_id, "title": f"T{song_id}"}


def make_numbered(seed: int = 7) -> tuple[Jukebox, FakePlayer]:
    player = FakePlayer()
    fake = FakeTime()
    jb = Jukebox(
        player,  # type: ignore[arg-type]
        numbered,
        sleep=fake.sleep,
        clock=fake.clock,
        rng=random.Random(seed),
    )
    return jb, player


def numbers(count: int) -> list[str]:
    return [str(n) for n in range(count)]


def queue_ids(jb: Jukebox) -> list[str]:
    assert len(jb._keys) == len(jb.songs)
    return [str(song["id"]) for song in jb.songs]


def shuffled(seed: int, ids: Sequence[str]) -> list[str]:
    order = list(ids)
    random.Random(seed).shuffle(order)
    assert order != list(ids)
    return order


def numbered_queue(count: int, current: int = 0, *, load: bool = False) -> tuple[Jukebox, FakePlayer]:
    jb, player = make_numbered()
    do(jb, "set", numbers(count))
    do(jb, "skip", index=current)
    if load:
        do(jb, "start")
        player.calls.clear()
    return jb, player


def test_insert_on_an_empty_queue_acts_like_add() -> None:
    jb, player = make()
    before = version_of(jb)
    do(jb, "insert", ["b", "a"])
    assert queue_ids(jb) == ["b", "a"]
    assert player.calls == []
    assert version_of(jb) != before


def test_insert_not_loaded_goes_after_current_without_device_calls() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b", "c"])
    do(jb, "skip", index=1)
    status = do(jb, "insert", ["c", "a"])["jukeboxStatus"]
    assert queue_ids(jb) == ["a", "b", "c", "a", "c"]
    assert status["currentIndex"] == 1
    assert player.calls == []


def test_insert_loaded_sends_one_device_call_and_keeps_current() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.calls.clear()
    status = do(jb, "insert", ["c", "a"])["jukeboxStatus"]
    assert player.calls == [
        ("insert_next", ([track_from_song(SONGS["c"]), track_from_song(SONGS["a"])],))
    ]
    assert queue_ids(jb) == ["a", "c", "a", "b"]
    assert status["currentIndex"] == 0


def test_insert_follows_the_track_the_device_is_playing() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b", "c"])
    do(jb, "start")
    player.idx = 1
    do(jb, "insert", ["a"])
    assert queue_ids(jb) == ["a", "b", "a", "c"]
    assert jb.current == 1


def test_insert_unknown_id_changes_nothing() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.calls.clear()
    before = version_of(jb)
    with pytest.raises(JukeboxError) as err:
        do(jb, "insert", ["c", "zzz"])
    assert err.value.code == 70
    with pytest.raises(JukeboxError) as err:
        do(jb, "insert")
    assert err.value.code == 10
    assert queue_ids(jb) == ["a", "b"]
    assert player.calls == []
    assert version_of(jb) == before


def test_insert_device_failure_leaves_the_queue_alone() -> None:
    jb, player = make()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.failing["insert_next"] = UpnpError(701, "x")
    with pytest.raises(JukeboxError) as err:
        do(jb, "insert", ["c"])
    assert err.value.code == 0
    assert queue_ids(jb) == ["a", "b"]


def test_insert_remembers_the_songs() -> None:
    jb, _ = make()
    do(jb, "set", ["a", "b"])
    do(jb, "insert", ["c"])
    do(jb, "clear")
    assert jb.describe("c") == ("C", "")


@pytest.mark.parametrize("load", [False, True])
@pytest.mark.parametrize(
    ("current", "source", "target", "expected", "new_current"),
    [
        (0, 3, 1, ["0", "3", "1", "2", "4"], 0),
        (0, 1, 3, ["0", "2", "3", "1", "4"], 0),
        (2, 0, 3, ["1", "2", "3", "0", "4"], 1),
        (2, 4, 0, ["4", "0", "1", "2", "3"], 3),
        (2, 0, 2, ["1", "2", "0", "3", "4"], 1),
        (2, 4, 2, ["0", "1", "4", "2", "3"], 3),
        (2, 0, 1, ["1", "0", "2", "3", "4"], 2),
        (2, 3, 4, ["0", "1", "2", "4", "3"], 2),
    ],
)
def test_move_reorders_and_keeps_pointing_at_the_current_entry(
    load: bool, current: int, source: int, target: int, expected: list[str], new_current: int
) -> None:
    jb, player = numbered_queue(5, current, load=load)
    before = version_of(jb)
    status = do(jb, "move", index=source, to_index=target)["jukeboxStatus"]
    assert queue_ids(jb) == expected
    assert status["currentIndex"] == new_current
    assert expected[new_current] == str(current)
    assert player.calls == ([("move", (source, target))] if load else [])
    assert version_of(jb) != before
    assert do(jb, "status")["jukeboxStatus"]["currentIndex"] == new_current


@pytest.mark.parametrize("load", [False, True])
def test_move_keeps_the_playing_entry_when_a_song_id_repeats(load: bool) -> None:
    jb, _ = make()
    do(jb, "set", ["a", "b", "a", "c"])
    do(jb, "skip", index=2)
    if load:
        do(jb, "start")
    assert jb.songs[0] is jb.songs[2]
    status = do(jb, "move", index=1, to_index=3)["jukeboxStatus"]
    assert queue_ids(jb) == ["a", "a", "c", "b"]
    assert status["currentIndex"] == 1


@pytest.mark.parametrize("load", [False, True])
def test_move_refuses_the_playing_track(load: bool) -> None:
    jb, player = numbered_queue(4, 2, load=load)
    before = version_of(jb)
    with pytest.raises(JukeboxError) as err:
        do(jb, "move", index=2, to_index=0)
    assert (err.value.code, err.value.message) == (0, "cannot move the playing track")
    assert queue_ids(jb) == numbers(4)
    assert player.calls == []
    assert version_of(jb) == before


@pytest.mark.parametrize(("index", "to_index"), [(5, 0), (0, 5), (-1, 0), (0, -1)])
def test_move_out_of_range(index: int, to_index: int) -> None:
    jb, _ = numbered_queue(5, 1)
    with pytest.raises(JukeboxError) as err:
        do(jb, "move", index=index, to_index=to_index)
    assert (err.value.code, err.value.message) == (0, "index out of range")
    assert queue_ids(jb) == numbers(5)


def test_move_needs_both_indexes() -> None:
    jb, _ = numbered_queue(5)
    for kw, name in (({"index": 1}, "toIndex"), ({"to_index": 1}, "index"), ({"index": 9}, "toIndex")):
        with pytest.raises(JukeboxError) as err:
            do(jb, "move", **kw)
        assert err.value.code == 10
        assert err.value.message.endswith(name)


@pytest.mark.parametrize("load", [False, True])
def test_move_to_the_same_slot_is_a_noop(load: bool) -> None:
    jb, player = numbered_queue(5, 1, load=load)
    before = version_of(jb)
    do(jb, "move", index=3, to_index=3)
    do(jb, "move", index=1, to_index=1)
    assert queue_ids(jb) == numbers(5)
    assert player.calls == []
    assert version_of(jb) == before


def test_move_device_failure_leaves_the_queue_alone() -> None:
    jb, player = numbered_queue(5, 1, load=True)
    player.failing["move"] = UpnpError(701, "x")
    with pytest.raises(JukeboxError) as err:
        do(jb, "move", index=3, to_index=0)
    assert err.value.code == 0
    assert queue_ids(jb) == numbers(5)
    assert jb.current == 1


def test_status_carries_shuffle() -> None:
    jb, _ = make_numbered()
    do(jb, "set", numbers(4))
    assert do(jb, "status")["jukeboxStatus"]["shuffle"] is False
    assert do(jb, "setShuffle", shuffle=True)["jukeboxStatus"]["shuffle"] is True
    assert do(jb, "get")["jukeboxPlaylist"]["shuffle"] is True
    assert do(jb, "setShuffle", shuffle=False)["jukeboxStatus"]["shuffle"] is False


def test_shuffle_needs_a_value() -> None:
    jb, _ = make_numbered()
    with pytest.raises(JukeboxError) as err:
        do(jb, "setShuffle")
    assert err.value.code == 10


def test_shuffle_reorders_only_the_upcoming_part() -> None:
    jb, player = numbered_queue(8, 2)
    before = version_of(jb)
    do(jb, "setShuffle", shuffle=True)
    assert queue_ids(jb) == ["0", "1", "2", *shuffled(7, numbers(8)[3:])]
    assert jb.current == 2
    assert player.calls == []
    assert version_of(jb) != before


def test_shuffle_again_while_on_reshuffles() -> None:
    jb, _ = numbered_queue(6)
    do(jb, "setShuffle", shuffle=True)
    first = queue_ids(jb)
    do(jb, "setShuffle", shuffle=True)
    second = queue_ids(jb)
    rng = random.Random(7)
    expected = numbers(6)
    for _ in range(2):
        tail = expected[1:]
        rng.shuffle(tail)
        expected[1:] = tail
    assert second == expected
    assert second != first
    assert jb.shuffle


def test_shuffle_off_restores_add_order_including_later_songs() -> None:
    jb, _ = numbered_queue(6, 1)
    do(jb, "setShuffle", shuffle=True)
    assert queue_ids(jb) != numbers(6)
    do(jb, "add", ["6", "7"])
    do(jb, "insert", ["8"])
    assert len(queue_ids(jb)) == 9
    assert jb.current == 1
    do(jb, "setShuffle", shuffle=False)
    assert queue_ids(jb) == numbers(9)
    assert not jb.shuffle


def test_shuffle_off_when_already_off_does_nothing() -> None:
    jb, player = numbered_queue(4, load=True)
    do(jb, "insert", ["9"])
    before = version_of(jb)
    player.calls.clear()
    do(jb, "setShuffle", shuffle=False)
    assert queue_ids(jb) == ["0", "9", "1", "2", "3"]
    assert player.calls == []
    assert version_of(jb) == before


def test_moves_and_removals_while_shuffled_do_not_disturb_the_order_keys() -> None:
    jb, _ = numbered_queue(6)
    do(jb, "setShuffle", shuffle=True)
    do(jb, "move", index=1, to_index=4)
    victim = queue_ids(jb)[2]
    do(jb, "remove", index=2)
    do(jb, "setShuffle", shuffle=False)
    assert queue_ids(jb) == [n for n in numbers(6) if n != victim]


def test_set_and_clear_reset_shuffle() -> None:
    for action, kw in (("set", {"ids": ["a", "b"]}), ("clear", {})):
        jb, _ = make_numbered()
        do(jb, "set", numbers(5))
        do(jb, "setShuffle", shuffle=True)
        assert jb.shuffle
        do(jb, action, **kw)
        assert not jb.shuffle
        assert do(jb, "status")["jukeboxStatus"]["shuffle"] is False
    assert jb._keys == []
    do(jb, "set", numbers(3))
    assert jb._keys == [0, 1, 2]


@pytest.mark.parametrize(("count", "current"), [(1, 0), (2, 1), (5, 4)])
def test_shuffle_with_nothing_to_reorder_sends_nothing(count: int, current: int) -> None:
    jb, player = numbered_queue(count, current, load=True)
    before = version_of(jb)
    status = do(jb, "setShuffle", shuffle=True)["jukeboxStatus"]
    assert status["shuffle"] is True
    assert player.calls == []
    assert version_of(jb) == before


def test_loaded_shuffle_replaces_the_upcoming_tracks_on_the_device() -> None:
    jb, player = numbered_queue(8, 2, load=True)
    before = version_of(jb)
    status = do(jb, "setShuffle", shuffle=True)["jukeboxStatus"]
    expected = shuffled(7, numbers(8)[3:])
    assert player.calls == [("replace_upcoming", ([Track(i, f"T{i}") for i in expected],))]
    assert queue_ids(jb) == ["0", "1", "2", *expected]
    assert (status["currentIndex"], status["playing"], status["shuffle"]) == (2, True, True)
    assert version_of(jb) != before
    player.calls.clear()
    do(jb, "setShuffle", shuffle=False)
    assert player.calls == [("replace_upcoming", ([Track(i, f"T{i}") for i in numbers(8)[3:]],))]
    assert queue_ids(jb) == numbers(8)


def test_loaded_shuffle_follows_the_track_the_device_is_playing() -> None:
    jb, player = numbered_queue(8, 0, load=True)
    player.idx = 4
    do(jb, "setShuffle", shuffle=True)
    expected = shuffled(7, numbers(8)[5:])
    assert player.calls == [("replace_upcoming", ([Track(i, f"T{i}") for i in expected],))]
    assert queue_ids(jb) == [*numbers(5), *expected]


def test_shuffle_failure_after_the_remove_reloads_the_queue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    jb, player = numbered_queue(6, load=True)
    player.failing["replace_upcoming"] = QueueDesyncError("incomplete")
    status = do(jb, "setShuffle", shuffle=True)["jukeboxStatus"]
    expected = ["0", *shuffled(7, numbers(6)[1:])]
    assert names(player) == ["replace_upcoming", "load"]
    _, (tracks, start, play) = player.calls[1]
    assert [t.song_id for t in tracks] == expected
    assert (start, play) == (0, True)
    assert queue_ids(jb) == expected
    assert (status["shuffle"], status["playing"]) == (True, True)
    assert jb.loaded
    (warning,) = seek_warnings(caplog)
    assert warning.startswith("reorder left the device queue out of step")


def test_shuffle_reload_resumes_the_position_and_keeps_a_pause() -> None:
    jb, player = numbered_queue(6, load=True)
    player.failing["replace_upcoming"] = QueueDesyncError("incomplete")
    player.stale = [34.0, 0.0]
    do(jb, "setShuffle", shuffle=True)
    assert names(player) == ["replace_upcoming", "load", "seek"]
    assert player.calls[2] == ("seek", (34,))

    jb, player = numbered_queue(6, load=True)
    do(jb, "stop")
    player.calls.clear()
    player.failing["replace_upcoming"] = QueueDesyncError("incomplete")
    do(jb, "setShuffle", shuffle=True)
    assert names(player) == ["replace_upcoming", "load"]
    assert player.calls[1][1][1:] == (0, False)


def test_shuffle_reload_failure_unloads_so_the_next_start_reloads() -> None:
    jb, player = numbered_queue(6, load=True)
    player.failing["replace_upcoming"] = QueueDesyncError("incomplete")
    player.failing["load"] = UpnpError(701, "x")
    before = version_of(jb)
    with pytest.raises(JukeboxError) as err:
        do(jb, "setShuffle", shuffle=True)
    assert err.value.code == 0
    expected = ["0", *shuffled(7, numbers(6)[1:])]
    assert queue_ids(jb) == expected
    assert jb.shuffle
    assert not jb.loaded
    assert not jb.playing
    assert version_of(jb) != before
    del player.failing["load"]
    player.calls.clear()
    do(jb, "start")
    assert names(player) == ["load"]
    assert [t.song_id for t in player.calls[0][1][0]] == expected


def test_shuffle_failed_remove_keeps_order_and_device() -> None:
    jb, player = numbered_queue(6, load=True)
    player.failing["replace_upcoming"] = UpnpError(701, "x")
    before = version_of(jb)
    with pytest.raises(JukeboxError) as err:
        do(jb, "setShuffle", shuffle=True)
    assert err.value.code == 0
    assert names(player) == ["replace_upcoming"]
    assert queue_ids(jb) == numbers(6)
    assert not jb.shuffle
    assert jb.loaded
    assert version_of(jb) == before


def test_queue_version_bumps_for_insert_move_and_shuffle() -> None:
    jb, _ = numbered_queue(5)
    seen = [version_of(jb)]
    for action, kw in (
        ("insert", {"ids": ["9"]}),
        ("move", {"index": 3, "to_index": 1}),
        ("setShuffle", {"shuffle": True}),
        ("setShuffle", {"shuffle": False}),
    ):
        do(jb, action, **kw)
        seen.append(version_of(jb))
    assert len(set(seen)) == len(seen)


def test_http_to_index_and_shuffle_parameters() -> None:
    async def get(client: TestClient[Any, Any], query: str) -> dict[str, Any]:
        resp = await client.get(f"/rest/jukeboxControl?{query}")
        return (await resp.json())["subsonic-response"]  # type: ignore[no-any-return]

    async def scenario() -> None:
        jb, _ = make_numbered()
        async with serve(jb) as client:
            await get(client, "action=set&id=0&id=1&id=2&id=3")
            assert (await get(client, "action=move&index=3&toIndex=1"))["status"] == "ok"
            assert queue_ids(jb) == ["0", "3", "1", "2"]
            for query in ("action=move&index=1&toIndex=abc", "action=move&index=1"):
                error = (await get(client, query))["error"]
                assert error["code"] == 10
                assert "toIndex" in error["message"]
            assert (await get(client, "action=move&index=1&toIndex=9"))["error"]["code"] == 0
            for value, expected in (("true", True), ("0", False), ("1", True), ("FALSE", False)):
                body = await get(client, f"action=setShuffle&shuffle={value}")
                assert body["jukeboxStatus"]["shuffle"] is expected
            error = (await get(client, "action=setShuffle&shuffle=maybe"))["error"]
            assert error["code"] == 10
            assert "shuffle" in error["message"]
            assert (await get(client, "action=setShuffle"))["error"]["code"] == 10
            refused = (await get(client, "action=shuffle&shuffle=true"))["error"]
            assert (refused["code"], refused["message"]) == (0, "shuffle is not supported")

    run(scenario())


def test_http_insert_takes_ids_from_the_form_too() -> None:
    async def scenario() -> None:
        jb, _ = make_numbered()
        async with serve(jb) as client:
            await client.get("/rest/jukeboxControl?action=set&id=0&id=1")
            data = [("action", "insert"), ("id", "5"), ("id", "6")]
            resp = await client.post("/rest/jukeboxControl", data=data)
            assert (await resp.json())["subsonic-response"]["status"] == "ok"
            assert queue_ids(jb) == ["0", "5", "6", "1"]
            await client.get("/rest/jukeboxControl?action=insert&id=7")
            assert queue_ids(jb) == ["0", "7", "5", "6", "1"]

    run(scenario())


def test_http_extension_action_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            await client.get("/rest/jukeboxControl?action=set&id=a&id=b")
            await client.get("/rest/jukeboxControl?action=insert&id=c")
            await client.get("/rest/jukeboxControl?action=move&index=1&toIndex=2")
            await client.get("/rest/jukeboxControl?action=setShuffle&shuffle=true")
            await client.get("/rest/jukeboxControl?action=setShuffle&shuffle=0")

    run(scenario())
    lines = jukebox_lines(caplog)
    assert lines[1] == 'jukebox action=insert index=- offset=- ids=1 gain=- mute=- first="C"'
    assert lines[2] == "jukebox action=move index=1 offset=- ids=0 gain=- mute=- toIndex=2"
    assert lines[3] == "jukebox action=setShuffle index=- offset=- ids=0 gain=- mute=- shuffle=True"
    assert lines[4].endswith("mute=- shuffle=False")


def test_status_carries_repeat_and_set_repeat_reaches_the_device_at_once() -> None:
    jb, player = make()
    assert do(jb, "status")["jukeboxStatus"]["repeat"] == "off"
    for mode in ("all", "one", "off"):
        assert do(jb, "setRepeat", repeat=mode)["jukeboxStatus"]["repeat"] == mode
    assert do(jb, "get")["jukeboxPlaylist"]["repeat"] == "off"
    assert [args for name, args in player.calls if name == "set_repeat"] == [
        ("all",),
        ("one",),
        ("off",),
    ]


def test_set_repeat_needs_a_known_mode() -> None:
    jb, player = make()
    with pytest.raises(JukeboxError) as err:
        do(jb, "setRepeat")
    assert err.value.code == 10
    for bad in ("loop", "shuffle", ""):
        with pytest.raises(JukeboxError) as err:
            do(jb, "setRepeat", repeat=bad)
        assert err.value.code == 10
    assert "set_repeat" not in names(player)
    assert jb.repeat == "off"


def test_set_repeat_failure_keeps_the_mode() -> None:
    jb, player = make()
    player.failing["set_repeat"] = UpnpError(701, "x")
    with pytest.raises(JukeboxError) as err:
        do(jb, "setRepeat", repeat="all")
    assert err.value.code == 0
    assert jb.repeat == "off"


def test_repeat_survives_set_and_clear_and_leaves_the_queue_version_alone() -> None:
    jb, _ = make()
    do(jb, "set", ["a", "b"])
    before = version_of(jb)
    do(jb, "setRepeat", repeat="one")
    assert version_of(jb) == before
    do(jb, "set", ["c"])
    assert do(jb, "status")["jukeboxStatus"]["repeat"] == "one"
    do(jb, "clear")
    assert do(jb, "status")["jukeboxStatus"]["repeat"] == "one"


def test_http_set_repeat_parameter_and_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    async def get(client: TestClient[Any, Any], query: str) -> dict[str, Any]:
        resp = await client.get(f"/rest/jukeboxControl?{query}")
        return (await resp.json())["subsonic-response"]  # type: ignore[no-any-return]

    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            body = await get(client, "action=setRepeat&repeat=all")
            assert body["jukeboxStatus"]["repeat"] == "all"
            body = await get(client, "action=setRepeat&repeat=ONE")
            assert body["jukeboxStatus"]["repeat"] == "one"
            for query in ("action=setRepeat&repeat=loop", "action=setRepeat", "action=status&repeat=x"):
                error = (await get(client, query))["error"]
                assert error["code"] == 10
            assert "repeat" in (await get(client, "action=setRepeat&repeat=2"))["error"]["message"]

    run(scenario())
    lines = jukebox_lines(caplog)
    assert lines[0] == "jukebox action=setRepeat index=- offset=- ids=0 gain=- mute=- repeat=all"
    assert lines[1].endswith("mute=- repeat=one")


class RecordingScrobbler(Scrobbler):
    def __init__(self) -> None:
        async def never(song_id: str, time_ms: int, submission: bool) -> bool:
            raise AssertionError("delivery is not under test")

        super().__init__(never)
        self.events: list[Scrobble] = []

    def submit(self, events: Sequence[Scrobble]) -> None:
        self.events.extend(events)

    def take(self) -> list[Scrobble]:
        taken, self.events = self.events, []
        return taken


def make_scrobbling(
    lookup_fn: Any = lookup,
) -> tuple[Jukebox, FakePlayer, RecordingScrobbler, list[float]]:
    player = FakePlayer()
    sink = RecordingScrobbler()
    wall = [1000.0]
    jb = Jukebox(player, lookup_fn, wall=lambda: wall[0], scrobbler=sink)  # type: ignore[arg-type]
    return jb, player, sink, wall


def sample(
    jb: Jukebox, player: FakePlayer, wall: list[float], at: float, pos: float, idx: int = 0
) -> None:
    wall[0], player.pos, player.idx, player.sampled_at = at, pos, idx, at
    do(jb, "status")


def play_for(
    jb: Jukebox, player: FakePlayer, wall: list[float], seconds: int, idx: int = 0
) -> None:
    """Sample every 5 s, continuing from the last sampled time and position."""
    for _ in range(seconds // 5):
        sample(jb, player, wall, wall[0] + 5, player.pos + 5, idx)


def test_a_sync_reports_now_playing_and_the_finished_play() -> None:
    jb, player, sink, wall = make_scrobbling()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    assert sink.take() == []
    sample(jb, player, wall, 1000.0, 0.0)
    assert sink.take() == [Scrobble("a", 1_000_000, False)]
    play_for(jb, player, wall, 50)
    assert sink.take() == []
    sample(jb, player, wall, 1055.0, 0.0, idx=1)
    assert sink.take() == [Scrobble("a", 1_000_000, True), Scrobble("b", 1_055_000, False)]


def test_skipping_early_reports_no_play() -> None:
    jb, player, sink, wall = make_scrobbling()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    sample(jb, player, wall, 1000.0, 0.0)
    play_for(jb, player, wall, 10)
    sample(jb, player, wall, 1012.0, 0.0, idx=1)
    assert [e.submission for e in sink.take()] == [False, False]


def test_the_same_song_twice_in_the_queue_is_two_plays() -> None:
    jb, player, sink, wall = make_scrobbling()
    do(jb, "set", ["a", "a"])
    do(jb, "start")
    sample(jb, player, wall, 1000.0, 0.0)
    play_for(jb, player, wall, 60)
    sample(jb, player, wall, 1065.0, 0.0, idx=1)
    assert sink.take() == [
        Scrobble("a", 1_000_000, False),
        Scrobble("a", 1_000_000, True),
        Scrobble("a", 1_065_000, False),
    ]


def test_repeat_one_loop_counts_as_another_play() -> None:
    jb, player, sink, wall = make_scrobbling()
    do(jb, "set", ["a"])
    do(jb, "setRepeat", repeat="one")
    do(jb, "start")
    sample(jb, player, wall, 1000.0, 0.0)
    play_for(jb, player, wall, 95)
    sample(jb, player, wall, 1100.0, 1.0)
    assert sink.take() == [
        Scrobble("a", 1_000_000, False),
        Scrobble("a", 1_000_000, True),
        Scrobble("a", 1_099_000, False),
    ]


def test_without_repeat_one_the_wrap_before_a_track_change_is_not_a_play() -> None:
    jb, player, sink, wall = make_scrobbling()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    sample(jb, player, wall, 1000.0, 0.0)
    play_for(jb, player, wall, 95)
    sink.take()
    sample(jb, player, wall, 1097.0, 0.0)
    assert sink.take() == []
    sample(jb, player, wall, 1098.0, 0.0, idx=1)
    assert sink.take() == [Scrobble("a", 1_000_000, True), Scrobble("b", 1_098_000, False)]


def test_the_jukebox_feeds_the_tracker_its_repeat_mode() -> None:
    jb, *_ = make_scrobbling()
    assert jb._plays is not None
    seen: list[bool] = []
    feed = jb._plays.feed

    def spy(*args: Any) -> list[Scrobble]:
        if len(args) > 5:
            seen.append(args[5])
        return feed(*args)  # type: ignore[no-any-return]

    jb._plays.feed = spy  # type: ignore[method-assign]
    do(jb, "set", ["a"])
    do(jb, "start")
    do(jb, "status")
    assert seen[-1] is False
    do(jb, "setRepeat", repeat="one")
    do(jb, "status")
    assert seen[-1] is True
    do(jb, "setRepeat", repeat="all")
    do(jb, "status")
    assert seen[-1] is False


def test_set_clear_and_removing_the_playing_entry_end_the_play() -> None:
    for action, kw in (
        ("set", {"ids": ["b"]}),
        ("clear", {}),
        ("remove", {"index": 0}),
    ):
        jb, player, sink, wall = make_scrobbling()
        do(jb, "set", ["a", "b"])
        do(jb, "start")
        sample(jb, player, wall, 1000.0, 0.0)
        play_for(jb, player, wall, 50)
        sink.take()
        wall[0] = 1055.0
        do(jb, action, **kw)
        assert sink.take() == [Scrobble("a", 1_000_000, True)], action
        do(jb, "status")
        assert sink.take() == []


def test_lost_control_ends_the_play() -> None:
    jb, player, sink, wall = make_scrobbling()
    do(jb, "set", ["a"])
    do(jb, "start")
    sample(jb, player, wall, 1000.0, 0.0)
    play_for(jb, player, wall, 60)
    sink.take()
    player.override = PlayerStatus(None, False, 0.0, False, "PLAYING")
    do(jb, "status")
    assert sink.take() == [Scrobble("a", 1_000_000, True)]


def test_moving_and_shuffling_do_not_end_the_play() -> None:
    async def numbered_long(song_id: str) -> Mapping[str, Any] | None:
        return {"id": song_id, "title": song_id, "duration": 200}

    jb, player, sink, wall = make_scrobbling(numbered_long)
    do(jb, "set", numbers(5))
    do(jb, "start")
    sample(jb, player, wall, 1000.0, 0.0)
    sink.take()
    do(jb, "move", index=3, to_index=1)
    do(jb, "setShuffle", shuffle=True)
    sample(jb, player, wall, 1005.0, 5.0)
    assert sink.take() == []


def test_no_scrobbler_means_no_tracking() -> None:
    jb, _ = make()
    do(jb, "set", ["a"])
    do(jb, "start")
    do(jb, "status")
    do(jb, "clear")
    assert jb._plays is None


def test_poll_syncs_only_when_loaded_and_stale() -> None:
    jb, player, fake = make_timed()
    assert run(jb.poll(5.0)) is False
    do(jb, "set", ["a"])
    do(jb, "start")
    assert run(jb.poll(5.0)) is False
    fake.now += 6.0
    player.pre_refreshes = [(0, True)]
    assert run(jb.poll(5.0)) is True
    assert player.pre_refreshes == []
    fake.now += 2.0
    player.pre_refreshes = [(0, True)]
    assert run(jb.poll(5.0)) is False
    assert player.pre_refreshes == [(0, True)]


def test_poll_picks_up_what_the_device_is_doing() -> None:
    jb, player, fake = make_timed()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    fake.now += 6.0
    player.idx = 1
    assert run(jb.poll(5.0)) is True
    assert jb.current == 1


def test_poll_waits_for_the_lock_the_requests_use() -> None:
    jb, player, fake = make_timed()
    do(jb, "set", ["a"])
    do(jb, "start")
    fake.now += 6.0
    player.pre_refreshes = [(0, True)]

    async def scenario() -> None:
        async with jb.lock:
            task = asyncio.create_task(jb.poll(5.0))
            await asyncio.sleep(0.02)
            assert not task.done()
            assert player.pre_refreshes == [(0, True)]
        assert await task is True

    run(scenario())
    assert player.pre_refreshes == []


def test_http_requests_use_the_same_lock_as_the_watcher() -> None:
    async def scenario() -> None:
        jb, _ = make()
        async with serve(jb) as client:
            async with jb.lock:
                task = asyncio.create_task(client.get("/rest/jukeboxControl?action=status"))
                await asyncio.sleep(0.05)
                assert not task.done()
            resp = await task
            assert resp.status == 200

    run(scenario())


def names_of(wiim: FakeWiim) -> list[str]:
    return [name for name, _ in wiim.calls]


def make_on_device(seed: int = 7) -> tuple[Jukebox, FakeWiim]:
    wiim = FakeWiim()
    fake = FakeTime()
    jb = Jukebox(
        Player(wiim, url_for),  # type: ignore[arg-type]
        numbered,
        sleep=fake.sleep,
        clock=fake.clock,
        rng=random.Random(seed),
    )
    do(jb, "set", numbers(6))
    do(jb, "start")
    wiim.calls.clear()
    return jb, wiim


def test_shuffle_on_the_device_removes_track_by_track_and_appends_once() -> None:
    jb, wiim = make_on_device()
    do(jb, "setShuffle", shuffle=True)
    calls = [(n, a) for n, a in wiim.calls if n in ("queue_remove", "queue_append")]
    assert [n for n, _ in calls] == ["queue_remove"] * 5 + ["queue_append"]
    assert [a for n, a in calls[:5]] == [("bridge", i, i) for i in range(6, 1, -1)]
    assert calls[5][1][1] == 1
    assert queue_ids(jb) == ["0", *shuffled(7, numbers(6)[1:])]


def test_a_failed_third_remove_reloads_the_whole_queue() -> None:
    jb, wiim = make_on_device()
    wiim.fail_after = {"queue_remove": 2}
    status = do(jb, "setShuffle", shuffle=True)["jukeboxStatus"]
    expected = ["0", *shuffled(7, numbers(6)[1:])]
    assert [n for n, _ in wiim.calls if n == "queue_remove"] == ["queue_remove"] * 3
    assert "queue_create" in names_of(wiim)
    context = [a for n, a in wiim.calls if n == "queue_create"][-1][0]
    assert [t["url"] for t in parse_queue_tracks(context)] == [url_for(i) for i in expected]
    assert queue_ids(jb) == expected
    assert (status["shuffle"], jb.loaded) == (True, True)


def test_a_failed_first_remove_changes_nothing_and_does_not_reload() -> None:
    jb, wiim = make_on_device()
    wiim.failing = {"queue_remove"}
    with pytest.raises(JukeboxError) as err:
        do(jb, "setShuffle", shuffle=True)
    assert err.value.code == 0
    assert names_of(wiim).count("queue_remove") == 1
    assert "queue_create" not in names_of(wiim)
    assert queue_ids(jb) == numbers(6)
    assert not jb.shuffle
    assert jb.loaded


def test_a_701_on_skip_is_retried_once_by_the_player() -> None:
    fake = FakeTime()
    wiim = FakeWiim()
    player = Player(wiim, url_for, sleep=fake.sleep)  # type: ignore[arg-type]
    jb = Jukebox(player, numbered, sleep=fake.sleep, clock=fake.clock)
    do(jb, "set", numbers(3))
    do(jb, "start")
    wiim.errors["queue_play"] = [UpnpError(701, "Transition not allowed")]
    status = do(jb, "skip", index=2)["jukeboxStatus"]
    assert status["currentIndex"] == 2
    assert names_of(wiim).count("queue_play") == 2
    assert fake.slept == RETRY_701_S
    wiim.errors["queue_play"] = [UpnpError(701, "x"), UpnpError(701, "y")]
    with pytest.raises(JukeboxError) as err:
        do(jb, "skip", index=1)
    assert err.value.code == 0
    assert names_of(wiim).count("queue_play") == 4
    assert fake.slept == 2 * RETRY_701_S


def test_the_sync_reuse_window_is_one_second() -> None:
    assert SYNC_REUSE_S == 1.0


@pytest.mark.parametrize("action", ["get", "status"])
def test_polls_reuse_a_device_reading_younger_than_the_window(
    reuse_window: None, action: str
) -> None:
    jb, player, fake = make_timed()
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    base = player.refreshes
    assert base == 1
    for now in (0.0, 0.5, SYNC_REUSE_S - 0.001):
        fake.now = now
        do(jb, action)
        assert player.refreshes == base
    fake.now = SYNC_REUSE_S
    do(jb, action)
    assert player.refreshes == base + 1
    fake.now = SYNC_REUSE_S + 0.9
    do(jb, action)
    assert player.refreshes == base + 1
    fake.now = 2 * SYNC_REUSE_S
    do(jb, action)
    assert player.refreshes == base + 2


def test_get_and_status_share_one_reading(reuse_window: None) -> None:
    jb, player, fake = make_timed()
    do(jb, "status")
    assert player.refreshes == 1
    fake.now = 0.4
    do(jb, "get")
    do(jb, "status")
    assert player.refreshes == 1


@pytest.mark.parametrize(
    ("action", "kw"),
    [
        ("skip", {"index": 1}),
        ("start", {}),
        ("stop", {}),
        ("setGain", {"gain": 0.3}),
        ("setMute", {"mute": True}),
        ("setRepeat", {"repeat": "all"}),
        ("move", {"index": 3, "to_index": 2}),
        ("setShuffle", {"shuffle": True}),
        ("insert", {"ids": ["9"]}),
        ("remove", {"index": 2}),
    ],
)
def test_every_other_action_reads_the_device_each_time(
    reuse_window: None, action: str, kw: dict[str, Any]
) -> None:
    jb, player = numbered_queue(5, 0, load=True)
    do(jb, "status")
    before = player.refreshes
    do(jb, action, **kw)
    assert player.refreshes == before + 1


def test_set_add_and_clear_still_skip_the_device_read(reuse_window: None) -> None:
    jb, player = numbered_queue(3, 0, load=True)
    do(jb, "status")
    before = player.refreshes
    do(jb, "add", ["9"])
    do(jb, "set", ["a"])
    do(jb, "clear")
    assert player.refreshes == before


def test_a_reused_reading_keeps_position_ms_moving(reuse_window: None) -> None:
    player = FakePlayer()
    fake = FakeTime()
    wall = [1000.0]
    jb = Jukebox(
        player,  # type: ignore[arg-type]
        lookup,
        sleep=fake.sleep,
        clock=fake.clock,
        wall=lambda: wall[0],
    )
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.pos, player.sampled_at = 12.0, 1000.0
    fake.now = SYNC_REUSE_S
    first = do(jb, "status")["jukeboxStatus"]
    assert first["positionMs"] == 12000
    base = player.refreshes
    wall[0], fake.now = 1000.4, SYNC_REUSE_S + 0.4
    reused = do(jb, "get")["jukeboxPlaylist"]
    assert player.refreshes == base
    assert (reused["position"], reused["positionMs"]) == (12, 12400)
    wall[0] = 1000.9
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 12900
    assert player.refreshes == base


def test_a_stopped_player_keeps_its_position_on_a_reused_reading(reuse_window: None) -> None:
    jb, player, fake = make_timed()
    do(jb, "set", ["a"])
    do(jb, "start")
    do(jb, "stop")
    player.pos, player.sampled_at = 7.5, 0.0
    fake.now = SYNC_REUSE_S
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 7500
    fake.now += 0.3
    assert do(jb, "status")["jukeboxStatus"]["positionMs"] == 7500


class GatedLookup:
    """Song lookups that wait on a per-id event and record how many run at once."""

    def __init__(self, gated: Sequence[str] = ()) -> None:
        self.gates = {song_id: asyncio.Event() for song_id in gated}
        self.calls: list[str] = []
        self.in_flight = 0
        self.peak = 0

    def open(self, *song_ids: str) -> None:
        for song_id in song_ids:
            self.gates[song_id].set()

    async def __call__(self, song_id: str) -> Mapping[str, Any] | None:
        self.calls.append(song_id)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if song_id in self.gates:
                await self.gates[song_id].wait()
            else:
                await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        if song_id.startswith("missing"):
            return None
        if song_id.startswith("boom"):
            raise aiohttp.ClientConnectionError("navidrome down")
        return {"id": song_id, "title": f"T{song_id}"}


def gated_jukebox(*gated: str) -> tuple[Jukebox, FakePlayer, GatedLookup]:
    songs = GatedLookup(gated)
    player = FakePlayer()
    jb = Jukebox(player, songs, clock=FakeTime().clock)  # type: ignore[arg-type]
    return jb, player, songs


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


async def submit(jb: Jukebox, action: str, ids: Sequence[str] = (), **kw: Any) -> dict[str, Any]:
    return await jb.execute(
        action,
        list(ids),
        kw.get("index"),
        kw.get("offset"),
        kw.get("gain"),
        kw.get("mute"),
        kw.get("to_index"),
        kw.get("shuffle"),
        kw.get("repeat"),
    )


async def idle() -> None:
    await asyncio.sleep(0.05)


def test_a_later_edit_that_resolves_first_still_applies_second() -> None:
    jb, _, songs = gated_jukebox("slow1", "slow2")

    async def scenario() -> None:
        first = asyncio.create_task(submit(jb, "add", ["slow1", "slow2"]))
        await until(lambda: songs.in_flight == 2)
        second = asyncio.create_task(submit(jb, "add", ["fast1", "fast2"]))
        await until(lambda: "fast2" in songs.calls)
        await idle()
        assert not first.done()
        assert not second.done()
        assert queue_ids(jb) == []
        songs.open("slow1", "slow2")
        await asyncio.gather(first, second)
        assert queue_ids(jb) == ["slow1", "slow2", "fast1", "fast2"]

    run(scenario())


def test_an_edit_without_lookups_waits_behind_an_earlier_slow_add() -> None:
    jb, _, songs = gated_jukebox("slow1")
    do(jb, "set", ["a"])

    async def scenario() -> None:
        add = asyncio.create_task(submit(jb, "add", ["slow1"]))
        await until(lambda: songs.in_flight == 1)
        clear = asyncio.create_task(submit(jb, "clear"))
        await idle()
        assert not clear.done()
        assert queue_ids(jb) == ["a"]
        songs.open("slow1")
        await asyncio.gather(add, clear)
        assert queue_ids(jb) == []

    run(scenario())


def test_a_failed_edit_in_between_does_not_let_the_next_one_overtake() -> None:
    jb, _, songs = gated_jukebox("slow1")

    async def scenario() -> None:
        slow = asyncio.create_task(submit(jb, "add", ["slow1"]))
        await until(lambda: songs.in_flight == 1)
        with pytest.raises(JukeboxError) as err:
            await submit(jb, "add", ["missing1"])
        assert (err.value.code, err.value.message) == (70, "song not found: missing1")
        with pytest.raises(JukeboxError) as err:
            await submit(jb, "add")
        assert err.value.code == 10
        fast = asyncio.create_task(submit(jb, "add", ["fast1"]))
        await until(lambda: "fast1" in songs.calls)
        await idle()
        assert not slow.done()
        assert not fast.done()
        assert queue_ids(jb) == []
        songs.open("slow1")
        await asyncio.gather(slow, fast)
        assert queue_ids(jb) == ["slow1", "fast1"]

    run(scenario())


def test_cancelling_a_waiting_edit_does_not_cancel_the_one_it_waits_for() -> None:
    jb, _, songs = gated_jukebox("slow1")

    async def scenario() -> None:
        slow = asyncio.create_task(submit(jb, "add", ["slow1"]))
        await until(lambda: songs.in_flight == 1)
        waiting = asyncio.create_task(submit(jb, "add", ["fast1"]))
        await until(lambda: "fast1" in songs.calls)
        await idle()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        later = asyncio.create_task(submit(jb, "add", ["fast2"]))
        await until(lambda: "fast2" in songs.calls)
        await idle()
        assert not slow.done()
        assert not later.done()
        assert queue_ids(jb) == []
        songs.open("slow1")
        await asyncio.gather(slow, later)
        assert queue_ids(jb) == ["slow1", "fast2"]

    run(scenario())


def test_cancelling_an_edit_during_its_lookups_keeps_the_order_of_the_others() -> None:
    jb, _, songs = gated_jukebox("slowA", "slowB")

    async def scenario() -> None:
        first = asyncio.create_task(submit(jb, "add", ["slowA"]))
        await until(lambda: songs.in_flight == 1)
        doomed = asyncio.create_task(submit(jb, "add", ["slowB"]))
        await until(lambda: songs.in_flight == 2)
        doomed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await doomed
        last = asyncio.create_task(submit(jb, "add", ["fast1"]))
        await until(lambda: "fast1" in songs.calls)
        await idle()
        assert not last.done()
        assert queue_ids(jb) == []
        songs.open("slowA")
        await asyncio.gather(first, last)
        assert queue_ids(jb) == ["slowA", "fast1"]

    run(scenario())


def test_an_edit_that_fails_in_the_queue_hands_the_turn_on() -> None:
    jb, player, songs = gated_jukebox("slow1")
    do(jb, "set", ["a", "b"])
    do(jb, "start")
    player.failing["clear"] = RuntimeError("boom")

    async def scenario() -> None:
        slow = asyncio.create_task(submit(jb, "add", ["slow1", "missing1"]))
        await until(lambda: songs.in_flight == 1 and "missing1" in songs.calls)
        after = asyncio.create_task(submit(jb, "add", ["fast1"]))
        await until(lambda: "fast1" in songs.calls)
        await idle()
        assert not after.done()
        songs.open("slow1")
        with pytest.raises(JukeboxError) as err:
            await slow
        assert err.value.code == 70
        await after
        assert queue_ids(jb) == ["a", "b", "fast1"]
        with pytest.raises(JukeboxError) as err:
            await submit(jb, "remove", index=9)
        assert (err.value.code, err.value.message) == (0, "index out of range")
        with pytest.raises(RuntimeError):
            await submit(jb, "clear")
        await asyncio.wait_for(submit(jb, "add", ["fast2"]), 1)
        assert queue_ids(jb) == ["a", "b", "fast1", "fast2"]

    run(scenario())


def test_unknown_actions_skip_the_queue() -> None:
    jb, _, songs = gated_jukebox("slow1")

    async def scenario() -> None:
        slow = asyncio.create_task(submit(jb, "add", ["slow1"]))
        await until(lambda: songs.in_flight == 1)
        with pytest.raises(JukeboxError) as err:
            await asyncio.wait_for(submit(jb, "nope"), 1)
        assert (err.value.code, err.value.message) == (0, "unknown action")
        songs.open("slow1")
        await slow

    run(scenario())


def test_many_edits_arriving_together_apply_in_arrival_order() -> None:
    delays = {f"s{n}": 0.004 * (12 - n) for n in range(12)}

    async def slower_first(song_id: str) -> Mapping[str, Any] | None:
        await asyncio.sleep(delays[song_id])
        return {"id": song_id, "title": song_id}

    jb = Jukebox(FakePlayer(), slower_first)  # type: ignore[arg-type]

    async def scenario() -> None:
        tasks = []
        for n in range(12):
            tasks.append(asyncio.create_task(submit(jb, "add", [f"s{n}"])))
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        assert queue_ids(jb) == [f"s{n}" for n in range(12)]

    run(scenario())


def test_skip_and_status_are_answered_while_an_add_waits_for_its_lookups() -> None:
    jb, _, songs = gated_jukebox("slow1")
    do(jb, "set", ["a", "b"])

    async def scenario() -> None:
        async with serve(jb) as client:
            add = asyncio.create_task(client.get("/rest/jukeboxControl?action=add&id=slow1"))
            await until(lambda: songs.in_flight == 1)
            skip = await asyncio.wait_for(client.get("/rest/jukeboxControl?action=skip&index=1"), 2)
            status = await asyncio.wait_for(client.get("/rest/jukeboxControl?action=status"), 2)
            assert (await skip.json())["subsonic-response"]["jukeboxStatus"]["currentIndex"] == 1
            assert (await status.json())["subsonic-response"]["status"] == "ok"
            assert not add.done()
            songs.open("slow1")
            body = (await (await add).json())["subsonic-response"]
            assert body["status"] == "ok"
            assert queue_ids(jb) == ["a", "b", "slow1"]

    run(scenario())


def test_http_slow_add_then_fast_add_keeps_arrival_order() -> None:
    jb, _, songs = gated_jukebox("slow1")

    async def scenario() -> None:
        async with serve(jb) as client:
            first = asyncio.create_task(client.get("/rest/jukeboxControl?action=add&id=slow1"))
            await until(lambda: songs.in_flight == 1)
            second = asyncio.create_task(client.get("/rest/jukeboxControl?action=add&id=fast1"))
            await until(lambda: "fast1" in songs.calls)
            await idle()
            assert not second.done()
            songs.open("slow1")
            for response in await asyncio.gather(first, second):
                assert (await response.json())["subsonic-response"]["status"] == "ok"
            assert queue_ids(jb) == ["slow1", "fast1"]

    run(scenario())


def test_resolve_runs_at_most_eight_lookups_at_once_and_keeps_input_order() -> None:
    assert LOOKUP_CONCURRENCY == 8
    ids = [f"s{n}" for n in range(40)]
    jb, _, songs = gated_jukebox(*ids)

    async def scenario() -> None:
        task = asyncio.create_task(jb.resolve(ids))
        await until(lambda: songs.in_flight == LOOKUP_CONCURRENCY)
        await idle()
        assert songs.in_flight == LOOKUP_CONCURRENCY
        assert len(songs.calls) == LOOKUP_CONCURRENCY
        released: list[str] = []
        while len(released) < len(ids):
            await until(lambda: any(i not in released for i in songs.calls))
            newest = [i for i in songs.calls if i not in released][-1]
            released.append(newest)
            songs.open(newest)
            await asyncio.sleep(0)
        found = await task
        assert [song["id"] for song in found] == ids
        assert released != ids
        assert songs.peak == LOOKUP_CONCURRENCY

    run(scenario())


def test_resolve_takes_remembered_songs_without_a_lookup() -> None:
    jb, _, songs = gated_jukebox()
    do(jb, "set", ["a"])
    songs.calls.clear()
    assert [s["id"] for s in run(jb.resolve(["a"]))] == ["a"]
    assert songs.calls == []
    found = run(jb.resolve(["new", "a", "new"]))
    assert [s["id"] for s in found] == ["new", "a", "new"]
    assert songs.calls == ["new"]
    assert found[0] is found[2]


def test_resolve_needs_ids_and_caps_them_per_call() -> None:
    jb, _, songs = gated_jukebox()
    with pytest.raises(JukeboxError) as err:
        run(jb.resolve([]))
    assert (err.value.code, err.value.message) == (10, "Required parameter is missing: id")
    assert MAX_IDS_PER_CALL == 500
    with pytest.raises(JukeboxError) as err:
        run(jb.resolve([str(n) for n in range(501)]))
    assert (err.value.code, err.value.message) == (0, "too many ids in one call (max 500)")
    assert songs.calls == []
    assert len(run(jb.resolve([str(n) for n in range(500)]))) == 500


def test_a_call_with_too_many_ids_changes_nothing() -> None:
    jb, player, songs = gated_jukebox()
    do(jb, "set", ["a"])
    do(jb, "start")
    player.calls.clear()
    songs.calls.clear()
    before = version_of(jb)
    for action in ("set", "add", "insert"):
        with pytest.raises(JukeboxError) as err:
            do(jb, action, [str(n) for n in range(501)])
        assert err.value.code == 0
        with pytest.raises(JukeboxError):
            run(submit(jb, action, [str(n) for n in range(501)]))
    assert songs.calls == []
    assert player.calls == []
    assert queue_ids(jb) == ["a"]
    assert version_of(jb) == before


@pytest.mark.parametrize(
    ("ids", "code", "message"),
    [
        (["ok", "missing2", "missing1"], 70, "song not found: missing2"),
        (["ok", "boom", "missing1"], 0, "navidrome down"),
        (["missing1", "boom"], 70, "song not found: missing1"),
        (["boom", "boom2"], 0, "navidrome down"),
    ],
)
def test_resolve_reports_the_first_problem_in_input_order(
    ids: list[str], code: int, message: str
) -> None:
    jb, _, _ = gated_jukebox()
    with pytest.raises(JukeboxError) as err:
        run(jb.resolve(ids))
    assert (err.value.code, err.value.message) == (code, message)


def test_resolve_names_a_lookup_error_without_a_message_by_its_type() -> None:
    async def timing_out(song_id: str) -> Mapping[str, Any] | None:
        raise TimeoutError

    jb = Jukebox(FakePlayer(), timing_out)  # type: ignore[arg-type]
    with pytest.raises(JukeboxError) as err:
        run(jb.resolve(["a"]))
    assert (err.value.code, err.value.message) == (0, "TimeoutError")


def test_resolve_passes_unexpected_errors_through() -> None:
    async def broken(song_id: str) -> Mapping[str, Any] | None:
        raise RuntimeError("bug")

    jb = Jukebox(FakePlayer(), broken)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="bug"):
        run(jb.resolve(["a", "b"]))


def test_resolve_looks_each_distinct_id_up_once() -> None:
    jb, _, songs = gated_jukebox()
    found = run(jb.resolve(["x", "y", "x", "x"]))
    assert sorted(songs.calls) == ["x", "y"]
    assert [s["id"] for s in found] == ["x", "y", "x", "x"]


def test_a_lookup_failure_in_a_set_through_handle_is_a_generic_error() -> None:
    jb, _, _ = gated_jukebox()
    with pytest.raises(JukeboxError) as err:
        do(jb, "set", ["boom"])
    assert (err.value.code, err.value.message) == (0, "navidrome down")


@pytest.mark.parametrize("action", ["set", "add", "insert"])
def test_handle_uses_given_songs_instead_of_looking_them_up(action: str) -> None:
    jb, _, songs = gated_jukebox()
    given = [{"id": "g1", "title": "G1"}, {"id": "g2", "title": "G2"}]
    run(jb.handle(action, ["ignored"], None, None, None, songs=given))
    assert songs.calls == []
    assert queue_ids(jb) == ["g1", "g2"]
    assert jb.songs is not given
    assert jb.describe("g1") == ("G1", "")
    assert [s["id"] for s in jb._seen.values()] == ["g1", "g2"]


def test_handle_with_empty_given_songs_is_a_missing_id() -> None:
    jb, _, _ = gated_jukebox()
    for action in ("set", "add", "insert"):
        with pytest.raises(JukeboxError) as err:
            run(jb.handle(action, ["a"], None, None, None, songs=[]))
        assert err.value.code == 10


def test_queue_cap_refuses_without_touching_the_queue_or_the_device() -> None:
    assert MAX_QUEUE == 1000
    jb, player = make_numbered()
    do(jb, "set", numbers(500))
    do(jb, "add", [str(n) for n in range(500, 1000)])
    do(jb, "start")
    assert len(jb.songs) == MAX_QUEUE
    player.calls.clear()
    before = version_of(jb)
    for action in ("add", "insert"):
        with pytest.raises(JukeboxError) as err:
            do(jb, action, ["1000"])
        assert (err.value.code, err.value.message) == (0, "queue is full (max 1000 tracks)")
    assert len(jb.songs) == MAX_QUEUE
    assert player.calls == []
    assert version_of(jb) == before
    assert jb.describe("1000") is None


def test_queue_cap_counts_the_whole_call() -> None:
    jb, player = make_numbered()
    do(jb, "set", numbers(500))
    do(jb, "add", [str(n) for n in range(500, 999)])
    do(jb, "start")
    player.calls.clear()
    with pytest.raises(JukeboxError):
        do(jb, "insert", ["a", "b"])
    assert len(jb.songs) == 999
    assert player.calls == []
    do(jb, "insert", ["a"])
    assert len(jb.songs) == MAX_QUEUE


def test_queue_cap_applies_to_a_set_too() -> None:
    jb, player = make_numbered()
    do(jb, "set", ["0", "1"])
    do(jb, "start")
    player.calls.clear()
    too_many = [{"id": str(n), "title": str(n)} for n in range(MAX_QUEUE + 1)]
    with pytest.raises(JukeboxError) as err:
        run(jb.handle("set", [], None, None, None, songs=too_many))
    assert err.value.message == "queue is full (max 1000 tracks)"
    assert queue_ids(jb) == ["0", "1"]
    assert player.calls == []


def test_http_queue_cap_and_id_cap_errors() -> None:
    jb, _ = make_numbered()
    do(jb, "set", numbers(500))
    do(jb, "add", [str(n) for n in range(500, 1000)])

    async def scenario() -> None:
        async with serve(jb) as client:
            resp = await client.get("/rest/jukeboxControl?action=add&id=x")
            error = (await resp.json())["subsonic-response"]["error"]
            assert error == {"code": 0, "message": "queue is full (max 1000 tracks)"}
            ids = "&".join(f"id={n}" for n in range(501))
            resp = await client.get(f"/rest/jukeboxControl?action=set&{ids}")
            error = (await resp.json())["subsonic-response"]["error"]
            assert error == {"code": 0, "message": "too many ids in one call (max 500)"}
            assert len(jb.songs) == MAX_QUEUE

    run(scenario())
