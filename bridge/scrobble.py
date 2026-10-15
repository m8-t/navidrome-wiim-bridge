"""Report plays to Navidrome: a pure play tracker, a delivery queue and a device watcher."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from bridge.log import redact

logger = logging.getLogger("bridge.scrobble")

POLL_INTERVAL_S = 5.0
MAX_LISTEN_S = 240.0
MAX_GAP_S = 15.0
LOOP_WINDOW_S = 10.0
UNKNOWN_DURATION_DROP_S = 60.0
END_SLACK_S = 3.0
QUEUE_LIMIT = 100

ScrobbleSender = Callable[[str, int, bool], Awaitable[bool]]


@dataclass(frozen=True)
class Scrobble:
    song_id: str
    time_ms: int
    submission: bool


@dataclass
class _Play:
    entry: Hashable
    song_id: str
    duration_s: int
    start_ms: int
    last_wall: float
    last_position: float
    last_playing: bool
    listened_s: float = 0.0


def listen_threshold(duration_s: int) -> float:
    return min(duration_s / 2, MAX_LISTEN_S) if duration_s > 0 else MAX_LISTEN_S


class PlayTracker:
    """Turn device samples into Navidrome scrobbles; knows nothing about the device.

    A position that wraps to the start of the same entry is a new play only with repeat one
    or when the device has stopped; otherwise it is the device lagging behind a track change.
    """

    def __init__(self) -> None:
        self._play: _Play | None = None

    def feed(
        self,
        entry: Hashable | None,
        song: Mapping[str, Any] | None,
        playing: bool,
        position_s: float,
        now: float,
        repeat_one: bool = False,
    ) -> list[Scrobble]:
        events: list[Scrobble] = []
        play = self._play
        if play is not None and (
            entry != play.entry
            or (self._wrapped(play, position_s) and (repeat_one or not playing))
        ):
            events += self._finish()
            play = None
        if entry is None or song is None:
            return events
        if play is None:
            if not playing:
                return events
            start_ms = round((now - max(position_s, 0.0)) * 1000)
            play = _Play(
                entry,
                str(song["id"]),
                int(song.get("duration") or 0),
                start_ms,
                now,
                position_s,
                playing,
            )
            self._play = play
            events.append(Scrobble(play.song_id, start_ms, False))
            return events
        if play.last_playing and playing:
            play.listened_s += min(max(now - play.last_wall, 0.0), MAX_GAP_S)
        if not playing and self._at_end(play, position_s):
            events += self._finish()
            return events
        play.last_wall, play.last_position, play.last_playing = now, position_s, playing
        return events

    def end(self, now: float) -> list[Scrobble]:
        """Close the running play because the queue was cleared or replaced."""
        play = self._play
        if play is not None and play.last_playing:
            play.listened_s += min(max(now - play.last_wall, 0.0), MAX_GAP_S)
        return self._finish()

    def _finish(self) -> list[Scrobble]:
        play, self._play = self._play, None
        if play is None or play.listened_s < listen_threshold(play.duration_s):
            return []
        return [Scrobble(play.song_id, play.start_ms, True)]

    @staticmethod
    def _wrapped(play: _Play, position_s: float) -> bool:
        if position_s > LOOP_WINDOW_S or play.last_position <= position_s:
            return False
        if play.duration_s > 0:
            return play.last_position >= play.duration_s - LOOP_WINDOW_S
        return play.last_position - position_s >= UNKNOWN_DURATION_DROP_S

    @staticmethod
    def _at_end(play: _Play, position_s: float) -> bool:
        return play.duration_s > 0 and position_s >= play.duration_s - END_SLACK_S


class Scrobbler:
    """Deliver scrobbles one at a time, in order, without ever blocking the caller."""

    def __init__(self, send: ScrobbleSender) -> None:
        self._send = send
        self._queue: asyncio.Queue[Scrobble] = asyncio.Queue(QUEUE_LIMIT)

    def submit(self, events: Sequence[Scrobble]) -> None:
        for event in events:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("scrobble queue is full, dropping a scrobble")

    async def run(self) -> None:
        while True:
            event = await self._queue.get()
            kind = "play" if event.submission else "now playing"
            try:
                accepted = await self._send(event.song_id, event.time_ms, event.submission)
            except Exception as exc:  # noqa: BLE001
                message = redact(str(exc) or type(exc).__name__)
                logger.warning("scrobble of %s %s failed: %s", kind, event.song_id, message)
                continue
            if accepted:
                logger.info("scrobbled %s %s", kind, event.song_id)
            else:
                logger.warning("scrobble of %s %s was not accepted", kind, event.song_id)


async def watch(
    poll: Callable[[], Awaitable[object]],
    interval: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    while True:
        await sleep(interval)
        try:
            await poll()
        except Exception as exc:  # noqa: BLE001
            logger.warning("device poll failed: %s", redact(str(exc) or type(exc).__name__))
