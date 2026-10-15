import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_player import FakeWiim, url_for

from bridge.jukebox import (
    SEEK_ATTEMPTS,
    SEEK_FRESH_TIMEOUT,
    SEEK_TOLERANCE,
    SEEK_WAIT_STEP,
    SEEK_WAIT_TIMEOUT,
    Jukebox,
    JukeboxError,
    add_jukebox_routes,
    error_envelope,
    ok_envelope,
    track_from_song,
)
from bridge.player import Player, PlayerError, PlayerStatus, QueueDesyncError, Track
from bridge.scrobble import Scrobble, Scrobbler
from bridge.soap import UpnpError
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
    run(jb.handle("set", ids, None, None, None))
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
    do(jb, "start")
    sample(jb, player, wall, 1000.0, 0.0)
    play_for(jb, player, wall, 95)
    sample(jb, player, wall, 1100.0, 1.0)
    assert sink.take() == [
        Scrobble("a", 1_000_000, False),
        Scrobble("a", 1_000_000, True),
        Scrobble("a", 1_099_000, False),
    ]


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
