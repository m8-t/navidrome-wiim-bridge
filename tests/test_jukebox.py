import asyncio
import logging
from collections.abc import Awaitable, Mapping, Sequence
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

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
        jb.handle(action, ids, kw.get("index"), kw.get("offset"), kw.get("gain"), kw.get("mute"))
    )


def names(player: FakePlayer) -> list[str]:
    return [n for n, _ in player.calls]


def test_set_returns_status_and_touches_no_device() -> None:
    jb, player = make()
    payload = do(jb, "set", ["a", "b"])
    assert payload == {
        "jukeboxStatus": {"currentIndex": 0, "playing": False, "gain": 0.5, "position": 0, "positionMs": 0, "muted": False}
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
