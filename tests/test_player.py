import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from bridge.player import Player, PlayerError, PlayerStatus, Track
from bridge.soap import UpnpError
from bridge.wiim import DeviceState


def url_for(song_id: str) -> str:
    return f"http://b/s/x/range/{song_id}.flac"


class FakeWiim:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.failing: set[str] = set()
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
        if name in self.failing:
            raise UpnpError(701, "x")
        return ""

    async def queue_delete(self, name: str) -> str:
        return await self._record("queue_delete", name)

    async def queue_create(self, context: str) -> str:
        return await self._record("queue_create", context)

    async def queue_play(self, name: str, index: int) -> str:
        return await self._record("queue_play", name, index)

    async def queue_insert(self, context: str, after_index: int, play: bool = False) -> str:
        return await self._record("queue_insert", context, after_index)

    async def queue_remove(self, name: str, start: int, end: int) -> str:
        return await self._record("queue_remove", name, start, end)

    async def queue_move(self, name: str, index: int, to_index: int) -> str:
        return await self._record("queue_move", name, index, to_index)

    async def set_volume(self, volume: int) -> str:
        return await self._record("set_volume", volume)

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
    assert names(fake) == ["queue_delete", "queue_create"]
    assert fake.calls[0][1] == ("bridge",)
    context = fake.calls[1][1][0]
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
    assert names(fake) == ["queue_delete", "queue_create", "pause"]


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


def test_insert_before_current_shifts_index() -> None:
    player, fake = loaded(start=2)
    run(player.insert([tr("n1"), tr("n2")], after=0))
    name, args = fake.calls[0]
    assert name == "queue_insert"
    assert args[1] == 1
    assert url_for("n1") in args[0] and url_for("n2") in args[0]
    assert url_for("t0") not in args[0]
    assert [t.song_id for t in player.tracks] == ["t0", "n1", "n2", "t1", "t2"]
    assert player.index == 4


def test_insert_after_current_and_append() -> None:
    player, fake = loaded(start=1)
    run(player.insert([tr("n1")], after=1))
    assert player.index == 1
    assert fake.calls[0][1][1] == 2
    run(player.append([tr("n2")]))
    assert fake.calls[1][1][1] == 4
    assert [t.song_id for t in player.tracks][-1] == "n2"
    with pytest.raises(ValueError):
        run(player.insert([tr("x")], after=-1))
    empty = Player(FakeWiim(), url_for)  # type: ignore[arg-type]
    with pytest.raises(PlayerError):
        run(empty.append([tr("x")]))


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
