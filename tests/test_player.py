import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

import pytest

from bridge.player import (
    APPEND_BATCH,
    LOOP_MODES,
    RETRY_701_S,
    Player,
    PlayerError,
    PlayerStatus,
    QueueDesyncError,
    Track,
)
from bridge.soap import UpnpError
from bridge.wiim import DeviceState, parse_queue_tracks


def url_for(song_id: str) -> str:
    return f"http://b/s/x/range/{song_id}.flac"


class FakeWiim:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.failing: set[str] = set()
        self.timing_out: set[str] = set()
        self.fail_after: dict[str, int] = {}
        self.errors: dict[str, list[Exception]] = {}
        self.device_state = DeviceState(
            t=0.0,
            transport="PLAYING",
            current_uri=None,
            next_uri=None,
            api_status="play",
            curpos_ms=0,
            queue_index=1,
        )

    async def _record(self, name: str, *args: Any) -> str:
        self.calls.append((name, args))
        if self.errors.get(name):
            raise self.errors[name].pop(0)
        if name in self.failing:
            raise UpnpError(701, "x")
        if name in self.fail_after and names(self).count(name) > self.fail_after[name]:
            raise UpnpError(701, "x")
        if name in self.timing_out:
            raise TimeoutError
        return ""

    async def queue_delete(self, name: str) -> str:
        return await self._record("queue_delete", name)

    async def queue_create(self, context: str) -> str:
        return await self._record("queue_create", context)

    async def queue_play(self, name: str, index: int) -> str:
        return await self._record("queue_play", name, index)

    async def queue_set_loop_mode(self, mode: int) -> str:
        return await self._record("queue_set_loop_mode", mode)

    async def queue_insert(self, context: str, after_index: int, play: bool = False) -> str:
        return await self._record("queue_insert", context, after_index, play)

    async def queue_append(self, context: str, start_index: int) -> str:
        return await self._record("queue_append", context, start_index)

    async def queue_remove(self, name: str, start: int, end: int) -> str:
        return await self._record("queue_remove", name, start, end)

    async def queue_move(self, name: str, index: int, to_index: int) -> str:
        return await self._record("queue_move", name, index, to_index)

    async def set_volume(self, volume: int) -> str:
        return await self._record("set_volume", volume)

    async def set_mute(self, muted: bool) -> str:
        return await self._record("set_mute", muted)

    async def pause(self) -> str:
        return await self._record("pause")

    async def play(self) -> str:
        return await self._record("play")

    async def stop(self) -> str:
        return await self._record("stop")

    async def next(self) -> str:
        return await self._record("next")

    async def previous(self) -> str:
        return await self._record("previous")

    async def seek(self, seconds: int) -> str:
        return await self._record("seek", seconds)

    async def state(self) -> DeviceState:
        self.calls.append(("state", ()))
        return self.device_state


def run(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(guarded())


def tr(name: str) -> Track:
    return Track(song_id=name, title=name.upper())


def names(fake: FakeWiim) -> list[str]:
    return [name for name, _ in fake.calls]


def loaded(start: int = 0) -> tuple[Player, FakeWiim]:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr("t0"), tr("t1"), tr("t2")], start=start))
    fake.calls.clear()
    return player, fake


def test_load_creates_queue_at_start_index() -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr("a"), tr("b"), tr("c")], start=1))
    assert names(fake) == ["queue_set_loop_mode", "queue_delete", "queue_create"]
    assert fake.calls[1][1] == ("bridge",)
    context = fake.calls[2][1][0]
    assert "<LastPlayIndex>2</LastPlayIndex>" in context
    assert "<TrackNumber>3</TrackNumber>" in context
    positions = [context.index(url_for(s)) for s in ("a", "b", "c")]
    assert positions == sorted(positions)
    assert player.index == 1
    assert player.in_control
    assert len(player.tracks) == 3


def test_load_without_play_pauses_after_create() -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr("a")], play=False))
    assert names(fake) == ["queue_set_loop_mode", "queue_delete", "queue_create", "pause"]


def test_load_error_handling() -> None:
    fake = FakeWiim()
    fake.failing = {"queue_delete"}
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr("a")]))
    assert player.in_control

    fake2 = FakeWiim()
    fake2.failing = {"queue_create"}
    player2 = Player(fake2, url_for)  # type: ignore[arg-type]
    with pytest.raises(UpnpError):
        run(player2.load([tr("a")]))
    assert player2.tracks == ()
    assert not player2.in_control
    assert player2.index is None


def test_load_validation_makes_no_device_call() -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        run(player.load([]))
    with pytest.raises(ValueError):
        run(player.load([tr("a")], start=5))
    assert fake.calls == []


def test_play_index() -> None:
    player, fake = loaded()
    run(player.play_index(2))
    assert fake.calls == [("queue_play", ("bridge", 3))]
    assert player.index == 2
    with pytest.raises(ValueError):
        run(player.play_index(3))
    empty = Player(FakeWiim(), url_for)  # type: ignore[arg-type]
    with pytest.raises(PlayerError):
        run(empty.play_index(0))


def test_append_goes_to_end_and_keeps_index() -> None:
    player, fake = loaded(start=2)
    run(player.append([tr("n1"), tr("n2")]))
    assert len(fake.calls) == 1
    name, args = fake.calls[0]
    assert name == "queue_append"
    assert args[1] == 3
    assert url_for("n1") in args[0] and url_for("n2") in args[0]
    assert url_for("t0") not in args[0]
    assert [t.song_id for t in player.tracks] == ["t0", "t1", "t2", "n1", "n2"]
    assert player.index == 2
    run(player.append([tr("n3")]))
    assert fake.calls[1][1][1] == 5
    assert all(c[0] != "queue_insert" for c in fake.calls)


def test_append_empty_and_unloaded() -> None:
    player, fake = loaded(start=1)
    fake.calls.clear()
    run(player.append([]))
    assert fake.calls == []
    empty = Player(FakeWiim(), url_for)  # type: ignore[arg-type]
    with pytest.raises(PlayerError):
        run(empty.append([tr("x")]))


def test_insert_next_goes_after_the_playing_track() -> None:
    player, fake = loaded(start=1)
    run(player.insert_next([tr("n1"), tr("n2")]))
    assert len(fake.calls) == 1
    name, (context, after_index, play) = fake.calls[0]
    assert name == "queue_insert"
    assert (after_index, play) == (2, False)
    assert [t["url"] for t in parse_queue_tracks(context)] == [url_for("n1"), url_for("n2")]
    assert [t.song_id for t in player.tracks] == ["t0", "t1", "n1", "n2", "t2"]
    assert player.index == 1


def test_insert_next_empty_unloaded_and_failure() -> None:
    player, fake = loaded(start=1)
    run(player.insert_next([]))
    assert fake.calls == []
    fake.failing = {"queue_insert"}
    with pytest.raises(UpnpError):
        run(player.insert_next([tr("x")]))
    assert [t.song_id for t in player.tracks] == ["t0", "t1", "t2"]
    empty = Player(FakeWiim(), url_for)  # type: ignore[arg-type]
    with pytest.raises(PlayerError):
        run(empty.insert_next([tr("x")]))


def test_insert_next_needs_a_playing_track() -> None:
    player, fake = loaded()
    state_with(fake, current_uri="http://x/y", queue_index=None)
    run(player.refresh())
    fake.calls.clear()
    with pytest.raises(PlayerError):
        run(player.insert_next([tr("x")]))
    with pytest.raises(PlayerError):
        run(player.replace_upcoming([tr("x")]))
    assert fake.calls == []


def many(count: int, start: int) -> tuple[Player, FakeWiim]:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr(f"t{n}") for n in range(count)], start=start))
    fake.calls.clear()
    return player, fake


def test_replace_upcoming_removes_one_track_at_a_time_then_appends_in_batches() -> None:
    # RemoveTracksInQueue ignores RangEnd on the device, so each upcoming track goes alone
    assert APPEND_BATCH == 50
    player, fake = many(124, start=3)
    reordered = [tr(f"t{n}") for n in range(123, 3, -1)]
    run(player.replace_upcoming(reordered))
    removes = [args for name, args in fake.calls if name == "queue_remove"]
    assert removes == [("bridge", n, n) for n in range(124, 4, -1)]
    assert len(removes) == 120
    assert names(fake) == ["queue_remove"] * 120 + ["queue_append"] * 3
    appends = fake.calls[120:]
    assert [args[1] for _, args in appends] == [4, 54, 104]
    sizes = [len(parse_queue_tracks(args[0])) for _, args in appends]
    assert sizes == [50, 50, 20]
    sent = [t["url"] for _, args in appends for t in parse_queue_tracks(args[0])]
    assert sent == [url_for(t.song_id) for t in reordered]
    assert [t.song_id for t in player.tracks] == [f"t{n}" for n in range(4)] + [
        t.song_id for t in reordered
    ]
    assert player.index == 3


def test_replace_upcoming_with_nothing_upcoming_sends_nothing() -> None:
    player, fake = loaded(start=2)
    run(player.replace_upcoming([]))
    assert fake.calls == []
    assert len(player.tracks) == 3


def test_replace_upcoming_rejects_a_different_length() -> None:
    player, fake = loaded(start=0)
    with pytest.raises(ValueError):
        run(player.replace_upcoming([tr("x")]))
    assert fake.calls == []


def test_replace_upcoming_failed_remove_leaves_state_unchanged() -> None:
    player, fake = loaded(start=0)
    fake.failing = {"queue_remove"}
    with pytest.raises(UpnpError) as err:
        run(player.replace_upcoming([tr("t2"), tr("t1")]))
    assert not isinstance(err.value, QueueDesyncError)
    assert fake.calls == [("queue_remove", ("bridge", 3, 3))]
    assert [t.song_id for t in player.tracks] == ["t0", "t1", "t2"]


def test_replace_upcoming_failure_on_a_later_remove_is_a_desync() -> None:
    player, fake = many(10, start=1)
    fake.fail_after = {"queue_remove": 2}
    with pytest.raises(QueueDesyncError) as err:
        run(player.replace_upcoming([tr(f"u{n}") for n in range(8)]))
    assert isinstance(err.value.__cause__, UpnpError)
    assert [args for name, args in fake.calls] == [("bridge", 10, 10), ("bridge", 9, 9), ("bridge", 8, 8)]
    assert [t.song_id for t in player.tracks] == [f"t{n}" for n in range(8)]


def test_replace_upcoming_failure_after_the_removes_is_a_desync() -> None:
    player, fake = many(5, start=0)
    fake.failing = {"queue_append"}
    with pytest.raises(QueueDesyncError) as err:
        run(player.replace_upcoming([tr(f"u{n}") for n in range(4)]))
    assert isinstance(err.value.__cause__, UpnpError)
    assert names(fake) == ["queue_remove"] * 4 + ["queue_append"]
    assert [t.song_id for t in player.tracks] == ["t0"]


def test_replace_upcoming_timeout_on_the_remove_is_a_desync() -> None:
    player, fake = loaded(start=0)
    fake.timing_out = {"queue_remove"}
    with pytest.raises(QueueDesyncError):
        run(player.replace_upcoming([tr("t2"), tr("t1")]))
    assert names(fake) == ["queue_remove"]


def test_remove() -> None:
    player, fake = loaded(start=2)
    run(player.remove(0))
    assert fake.calls == [("queue_remove", ("bridge", 1, 1))]
    assert [t.song_id for t in player.tracks] == ["t1", "t2"]
    assert player.index == 1
    fake.calls.clear()
    with pytest.raises(PlayerError):
        run(player.remove(1))
    assert fake.calls == []


def test_move() -> None:
    player, fake = loaded(start=1)
    run(player.move(0, 2))
    assert fake.calls == [("queue_move", ("bridge", 1, 3))]
    assert [t.song_id for t in player.tracks] == ["t1", "t2", "t0"]
    assert player.index == 0
    fake.calls.clear()
    run(player.move(1, 1))
    assert fake.calls == []
    with pytest.raises(PlayerError):
        run(player.move(0, 2))


def test_failed_device_call_leaves_state_unchanged() -> None:
    player, fake = loaded(start=2)
    fake.failing = {"queue_remove"}
    with pytest.raises(UpnpError):
        run(player.remove(0))
    assert [t.song_id for t in player.tracks] == ["t0", "t1", "t2"]
    assert player.index == 2


def state_with(fake: FakeWiim, **changes: Any) -> None:
    fields = {**fake.device_state.__dict__, **changes}
    fake.device_state = DeviceState(**fields)


def test_refresh_follows_device() -> None:
    player, fake = loaded()
    state_with(fake, current_uri=None, queue_index=3, transport="PLAYING", curpos_ms=4200)
    status = run(player.refresh())
    assert status == PlayerStatus(
        index=2, playing=True, position_s=4.2, in_control=True, transport="PLAYING"
    )
    assert player.index == 2


def test_refresh_paused() -> None:
    player, fake = loaded()
    state_with(fake, current_uri=None, queue_index=2, transport="PAUSED_PLAYBACK")
    status = run(player.refresh())
    assert not status.playing
    assert status.index == 1
    assert player.index == 1


def test_refresh_stopped_queue_is_still_ours() -> None:
    player, fake = loaded()
    state_with(fake, current_uri="SONGLIST-NETWORK", queue_index=2, transport="STOPPED")
    status = run(player.refresh())
    assert status.in_control
    assert status.index == 1
    assert not status.playing


def test_refresh_no_uri_and_no_queue_index_is_not_ours() -> None:
    player, fake = loaded()
    state_with(fake, current_uri=None, queue_index=None, transport="STOPPED")
    status = run(player.refresh())
    assert not status.in_control
    assert player.index is None


def test_refresh_other_source_drops_control() -> None:
    player, fake = loaded()
    state_with(fake, current_uri="http://x/y", queue_index=None)
    status = run(player.refresh())
    assert not status.in_control
    assert not status.playing
    assert player.index is None
    assert not player.in_control
    assert names(fake) == ["state"]


def test_refresh_queue_index_beyond_local_queue() -> None:
    player, fake = loaded()
    state_with(fake, queue_index=4)
    status = run(player.refresh())
    assert not status.in_control
    assert player.index is None


def test_transport_commands() -> None:
    player, fake = loaded()
    run(player.pause())
    run(player.resume())
    run(player.stop())
    run(player.next())
    run(player.previous())
    run(player.seek(30))
    assert fake.calls == [
        ("pause", ()),
        ("play", ()),
        ("stop", ()),
        ("next", ()),
        ("previous", ()),
        ("seek", (30,)),
    ]
    with pytest.raises(ValueError):
        run(player.seek(-1))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def retrying(start: int = 0) -> tuple[Player, FakeWiim, Sleeps]:
    fake = FakeWiim()
    sleeps = Sleeps()
    player = Player(fake, url_for, sleep=sleeps)  # type: ignore[arg-type]
    run(player.load([tr("t0"), tr("t1"), tr("t2")], start=start))
    fake.calls.clear()
    return player, fake, sleeps


RETRIED = [
    ("pause", (), "pause"),
    ("resume", (), "play"),
    ("play_index", (2,), "queue_play"),
    ("seek", (30,), "seek"),
]


def test_the_701_retry_waits_a_third_of_a_second() -> None:
    assert RETRY_701_S == 0.3


@pytest.mark.parametrize(("method", "args", "call"), RETRIED)
def test_a_701_is_retried_once_after_a_pause(
    method: str, args: tuple[Any, ...], call: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    player, fake, sleeps = retrying()
    fake.errors[call] = [UpnpError(701, "Transition not allowed")]
    run(getattr(player, method)(*args))
    assert names(fake) == [call, call]
    assert fake.calls[0][1] == fake.calls[1][1]
    assert sleeps.calls == [RETRY_701_S]
    (record,) = [r for r in caplog.records if r.name == "bridge.player"]
    assert record.levelno == logging.INFO
    assert method in record.getMessage()
    assert "retrying" in record.getMessage()


@pytest.mark.parametrize(("method", "args", "call"), RETRIED)
def test_a_second_701_propagates(method: str, args: tuple[Any, ...], call: str) -> None:
    player, fake, sleeps = retrying()
    fake.errors[call] = [UpnpError(701, "x"), UpnpError(701, "y")]
    with pytest.raises(UpnpError) as err:
        run(getattr(player, method)(*args))
    assert err.value.code == 701
    assert names(fake) == [call, call]
    assert sleeps.calls == [RETRY_701_S]
    assert player.index == 0


@pytest.mark.parametrize(("method", "args", "call"), RETRIED)
@pytest.mark.parametrize(
    "error", [UpnpError(500, "x"), UpnpError(None, "HTTP 500"), TimeoutError(), ValueError("v")]
)
def test_other_errors_are_not_retried(
    method: str, args: tuple[Any, ...], call: str, error: Exception
) -> None:
    player, fake, sleeps = retrying()
    fake.errors[call] = [error]
    with pytest.raises(type(error)):
        run(getattr(player, method)(*args))
    assert names(fake) == [call]
    assert sleeps.calls == []


def test_a_retried_play_index_moves_the_index_only_after_it_worked() -> None:
    player, fake, _ = retrying()
    fake.errors["queue_play"] = [UpnpError(701, "x")]
    run(player.play_index(2))
    assert player.index == 2
    assert fake.calls == [("queue_play", ("bridge", 3))] * 2


def test_the_default_sleep_is_asyncio_sleep() -> None:
    assert Player(FakeWiim(), url_for)._sleep is asyncio.sleep  # type: ignore[arg-type]


def test_other_device_calls_are_not_retried() -> None:
    player, fake, sleeps = retrying()
    for method in ("stop", "next", "previous"):
        fake.errors[method] = [UpnpError(701, "x")]
        with pytest.raises(UpnpError):
            run(getattr(player, method)())
    fake.errors["pause"] = [UpnpError(701, "x")]
    with pytest.raises(UpnpError):
        run(player.load([tr("x")], play=False))
    assert names(fake).count("pause") == 1
    assert sleeps.calls == []


def test_clear() -> None:
    player, fake = loaded()
    fake.failing = {"stop", "queue_delete"}
    run(player.clear())
    assert names(fake) == ["stop", "queue_delete"]
    assert player.tracks == ()
    assert player.index is None
    assert not player.in_control


def test_load_failure_resets_a_loaded_player() -> None:
    player, fake = loaded(start=1)
    fake.failing = {"queue_create"}
    with pytest.raises(UpnpError):
        run(player.load([tr("x")]))
    assert player.tracks == ()
    assert player.index is None
    assert not player.in_control

    player, fake = loaded()
    fake.failing = {"pause"}
    with pytest.raises(UpnpError):
        run(player.load([tr("x")], play=False))
    assert player.tracks == ()
    assert not player.in_control


def test_set_volume_clamps_to_ceiling() -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    assert run(player.set_volume(150)) == 100
    assert run(player.set_volume(-5)) == 0
    limited = Player(fake, url_for, max_volume=60)  # type: ignore[arg-type]
    assert run(limited.set_volume(80)) == 60
    assert [args for name, args in fake.calls if name == "set_volume"] == [(100,), (0,), (60,)]
    with pytest.raises(ValueError):
        Player(fake, url_for, max_volume=101)  # type: ignore[arg-type]


def test_refresh_reports_volume_without_control() -> None:
    fake = FakeWiim()
    state_with(fake, volume=33)
    player = Player(fake, url_for)  # type: ignore[arg-type]
    status = run(player.refresh())
    assert not status.in_control
    assert status.volume == 33


def test_refresh_reports_mute_and_set_mute_calls_device() -> None:
    fake = FakeWiim()
    state_with(fake, muted=True)
    player = Player(fake, url_for)  # type: ignore[arg-type]
    assert run(player.refresh()).muted is True
    run(player.set_mute(False))
    assert ("set_mute", (False,)) in fake.calls


def test_loop_modes_map_to_the_verified_device_modes() -> None:
    assert LOOP_MODES == {"off": 4, "all": 0, "one": 1}
    assert not set(LOOP_MODES.values()) & {2, 3, 5}


def test_set_repeat_sends_the_mapped_mode_at_once() -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    for name in ("all", "one", "off"):
        run(player.set_repeat(name))
    assert fake.calls == [
        ("queue_set_loop_mode", (0,)),
        ("queue_set_loop_mode", (1,)),
        ("queue_set_loop_mode", (4,)),
    ]


@pytest.mark.parametrize("name", ["shuffle", "ALL", "3", ""])
def test_set_repeat_rejects_unknown_modes_without_a_device_call(name: str) -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        run(player.set_repeat(name))
    assert fake.calls == []


def test_first_load_applies_the_loop_mode_once() -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr("a")]))
    assert fake.calls[0] == ("queue_set_loop_mode", (4,))
    run(player.load([tr("a")]))
    run(player.clear())
    run(player.load([tr("a")]))
    assert names(fake).count("queue_set_loop_mode") == 1


def test_a_mode_set_before_the_first_load_is_not_sent_again() -> None:
    fake = FakeWiim()
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.set_repeat("one"))
    run(player.load([tr("a")]))
    assert [args for name, args in fake.calls if name == "queue_set_loop_mode"] == [(1,)]


def test_loop_mode_failure_at_load_is_retried_at_the_next_load(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeWiim()
    fake.failing = {"queue_set_loop_mode"}
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr("a")]))
    assert player.in_control
    assert names(fake) == ["queue_set_loop_mode", "queue_delete", "queue_create"]
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "loop mode" in warning.getMessage()
    fake.failing = set()
    fake.timing_out = set()
    run(player.load([tr("a")]))
    run(player.load([tr("a")]))
    assert names(fake).count("queue_set_loop_mode") == 2


def test_loop_mode_timeout_at_load_does_not_fail_the_load() -> None:
    fake = FakeWiim()
    fake.timing_out = {"queue_set_loop_mode"}
    player = Player(fake, url_for)  # type: ignore[arg-type]
    run(player.load([tr("a")]))
    assert player.in_control


def test_failed_set_repeat_keeps_the_previous_mode() -> None:
    fake = FakeWiim()
    fake.failing = {"queue_set_loop_mode"}
    player = Player(fake, url_for)  # type: ignore[arg-type]
    with pytest.raises(UpnpError):
        run(player.set_repeat("one"))
    fake.failing = set()
    run(player.load([tr("a")]))
    assert [args for name, args in fake.calls if name == "queue_set_loop_mode"][-1] == (4,)
