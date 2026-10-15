"""Bridge-side play queue mirrored onto the WiiM device queue."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from bridge.soap import UpnpError
from bridge.wiim import QueueTrack, Wiim, build_queue_context

DEVICE_QUEUE_URIS = (None, "", "SONGLIST-NETWORK")
PLAYING_STATES = ("PLAYING", "TRANSITIONING")


class PlayerError(Exception):
    """Raised when an operation does not fit the player's current state."""


@dataclass(frozen=True)
class Track:
    song_id: str
    title: str
    artist: str = ""
    album: str = ""
    duration_s: int = 0


@dataclass(frozen=True)
class PlayerStatus:
    index: int | None
    playing: bool
    position_s: float
    in_control: bool
    transport: str | None
    volume: int | None = None
    muted: bool | None = None


class Player:
    def __init__(
        self,
        wiim: Wiim,
        url_for: Callable[[str], str],
        queue_name: str = "bridge",
        max_volume: int = 100,
    ) -> None:
        if not 0 <= max_volume <= 100:
            raise ValueError("max_volume must be between 0 and 100")
        self._max_volume = max_volume
        self._wiim = wiim
        self._url_for = url_for
        self._queue_name = queue_name
        self._tracks: list[Track] = []
        self._index: int | None = None
        self._in_control = False

    @property
    def tracks(self) -> tuple[Track, ...]:
        return tuple(self._tracks)

    @property
    def index(self) -> int | None:
        return self._index

    @property
    def in_control(self) -> bool:
        return self._in_control

    def _queue_tracks(self, tracks: Sequence[Track]) -> list[QueueTrack]:
        return [
            QueueTrack(
                url=self._url_for(t.song_id),
                title=t.title,
                artist=t.artist,
                album=t.album,
                duration_s=t.duration_s,
                item_id=t.song_id,
            )
            for t in tracks
        ]

    def _check_index(self, index: int) -> None:
        if not 0 <= index < len(self._tracks):
            raise ValueError(f"index {index} out of range")

    def _require_queue(self) -> None:
        if not self._tracks:
            raise PlayerError("no queue loaded")

    async def load(self, tracks: Sequence[Track], start: int = 0, play: bool = True) -> None:
        if not tracks:
            raise ValueError("no tracks to load")
        if not 0 <= start < len(tracks):
            raise ValueError(f"start {start} out of range")
        try:
            await self._wiim.queue_delete(self._queue_name)
        except UpnpError:
            pass
        context = build_queue_context(
            self._queue_name, self._queue_tracks(tracks), last_play_index=start + 1
        )
        try:
            await self._wiim.queue_create(context)
            if not play:
                await self._wiim.pause()
        except Exception:
            self._tracks = []
            self._index = None
            self._in_control = False
            raise
        self._tracks = list(tracks)
        self._index = start
        self._in_control = True

    async def play_index(self, index: int) -> None:
        self._require_queue()
        self._check_index(index)
        await self._wiim.queue_play(self._queue_name, index + 1)
        self._index = index

    async def append(self, tracks: Sequence[Track]) -> None:
        self._require_queue()
        if not tracks:
            return
        context = build_queue_context(self._queue_name, self._queue_tracks(tracks))
        await self._wiim.queue_append(context, start_index=len(self._tracks))
        self._tracks.extend(tracks)

    async def remove(self, index: int) -> None:
        self._check_index(index)
        if index == self._index:
            raise PlayerError("cannot remove the playing track")
        await self._wiim.queue_remove(self._queue_name, index + 1, index + 1)
        del self._tracks[index]
        if self._index is not None and index < self._index:
            self._index -= 1

    async def move(self, index: int, to: int) -> None:
        self._check_index(index)
        self._check_index(to)
        if index == to:
            return
        if index == self._index:
            raise PlayerError("cannot move the playing track")
        await self._wiim.queue_move(self._queue_name, index + 1, to + 1)
        current = self._tracks[self._index] if self._index is not None else None
        self._tracks.insert(to, self._tracks.pop(index))
        if current is not None:
            self._index = next(i for i, t in enumerate(self._tracks) if t is current)

    async def pause(self) -> None:
        await self._wiim.pause()

    async def resume(self) -> None:
        await self._wiim.play()

    async def stop(self) -> None:
        await self._wiim.stop()

    async def next(self) -> None:
        await self._wiim.next()

    async def previous(self) -> None:
        await self._wiim.previous()

    async def seek(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        await self._wiim.seek(seconds)

    async def set_volume(self, volume: int) -> int:
        clamped = min(max(volume, 0), self._max_volume)
        await self._wiim.set_volume(clamped)
        return clamped

    async def set_mute(self, muted: bool) -> None:
        await self._wiim.set_mute(muted)

    async def clear(self) -> None:
        try:
            await self._wiim.stop()
        except UpnpError:
            pass
        try:
            await self._wiim.queue_delete(self._queue_name)
        except UpnpError:
            pass
        self._tracks = []
        self._index = None
        self._in_control = False

    async def refresh(self) -> PlayerStatus:
        s = await self._wiim.state()
        ours = (
            self._in_control
            and s.current_uri in DEVICE_QUEUE_URIS
            and s.queue_index is not None
            and 1 <= s.queue_index <= len(self._tracks)
        )
        if ours:
            assert s.queue_index is not None
            self._index = s.queue_index - 1
        elif self._in_control:
            self._in_control = False
            self._index = None
        return PlayerStatus(
            index=self._index if ours else None,
            playing=ours and s.transport in PLAYING_STATES,
            position_s=(s.curpos_ms or 0) / 1000 if ours else 0.0,
            in_control=self._in_control,
            transport=s.transport,
            volume=s.volume,
            muted=s.muted,
        )
