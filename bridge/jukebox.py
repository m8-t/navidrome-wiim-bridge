"""Subsonic jukeboxControl endpoint driving the bridge Player."""

import asyncio
import logging
import math
import random
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from aiohttp import web

from bridge.log import clean, redact, song_label
from bridge.player import LOOP_MODES, Player, PlayerError, QueueDesyncError, Track
from bridge.scrobble import PlayTracker, Scrobbler
from bridge.soap import UpnpError
from bridge.wiim import REQUEST_ERRORS

logger = logging.getLogger("bridge.jukebox")

SongLookup = Callable[[str], Awaitable[Mapping[str, Any] | None]]
Authenticator = Callable[[Mapping[str, str]], Awaitable[bool]]

ENVELOPE_BASE = {
    "version": "1.16.1",
    "type": "wiim-bridge",
    "serverVersion": "0.1",
    "openSubsonic": True,
}
WRONG_LOGIN = 40
QUIET_ACTIONS = frozenset({"get", "status"})
PATHS = ("/rest/jukeboxControl", "/rest/jukeboxControl.view")
MEMORY_SIZE = 500
SEEK_WAIT_STEP = 0.25
SEEK_WAIT_TIMEOUT = 8.0
SEEK_FRESH_TIMEOUT = 4.0
SEEK_ATTEMPTS = 4
SEEK_VERIFY_CHECKS = 4
SEEK_TOLERANCE = 3
SEEK_HOLD_S = 3.0
SEEK_HOLD_TOLERANCE = 2.0
CORS_ORIGIN = {"Access-Control-Allow-Origin": "*"}
CORS_PREFLIGHT = {
    **CORS_ORIGIN,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


class JukeboxError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def track_from_song(song: Mapping[str, Any]) -> Track:
    return Track(
        song_id=str(song["id"]),
        title=str(song.get("title") or ""),
        artist=str(song.get("artist") or ""),
        album=str(song.get("album") or ""),
        duration_s=int(song.get("duration") or 0),
    )


def ok_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"subsonic-response": {"status": "ok", **ENVELOPE_BASE, **payload}}


def error_envelope(code: int, message: str) -> dict[str, Any]:
    error = {"code": code, "message": message}
    return {"subsonic-response": {"status": "failed", **ENVELOPE_BASE, "error": error}}


class Jukebox:
    def __init__(
        self,
        player: Player,
        lookup: SongLookup,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        scrobbler: Scrobbler | None = None,
    ) -> None:
        self._player = player
        self._lookup = lookup
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._rng = rng if rng is not None else random.Random()
        self._scrobbler = scrobbler
        self._plays = PlayTracker() if scrobbler is not None else None
        self.lock = asyncio.Lock()
        self._synced_at = float("-inf")
        self._queue_id = 0
        self.songs: list[Mapping[str, Any]] = []
        self._keys: list[int] = []
        self._next_key = 0
        self.shuffle = False
        self.repeat = "off"
        self._seen: dict[str, Mapping[str, Any]] = {}
        self.current = 0
        self.gain = 0.5
        self.muted = False
        self.loaded = False
        self.playing = False
        self.position = 0
        self._position_ms = 0
        self._position_at = 0.0
        self._seek_hold: tuple[int, float, float] | None = None
        self._boot_id = secrets.token_hex(4)
        self._queue_version = 0
        self._actions: dict[str, Callable[..., Awaitable[None]]] = {
            "get": self._noop,
            "status": self._noop,
            "set": self._set,
            "start": self._start,
            "stop": self._stop,
            "skip": self._skip,
            "add": self._add,
            "insert": self._insert,
            "move": self._noop,
            "setShuffle": self._noop,
            "setRepeat": self._noop,
            "clear": self._clear,
            "remove": self._remove,
            "shuffle": self._shuffle,
            "setGain": self._set_gain,
            "setMute": self._noop,
        }

    async def handle(
        self,
        action: str,
        ids: Sequence[str],
        index: int | None,
        offset: int | None,
        gain: float | None,
        mute: bool | None = None,
        to_index: int | None = None,
        shuffle: bool | None = None,
        repeat: str | None = None,
    ) -> dict[str, Any]:
        if action not in self._actions:
            raise JukeboxError(0, "unknown action")
        try:
            if action not in ("set", "clear", "add"):
                await self._sync()
            if action == "setMute":
                await self._set_mute(mute)
            elif action == "move":
                await self._move(index, to_index)
            elif action == "setShuffle":
                await self._set_shuffle(shuffle)
            elif action == "setRepeat":
                await self._set_repeat(repeat)
            else:
                await self._actions[action](ids, index, offset, gain)
        except JukeboxError:
            raise
        except (UpnpError, PlayerError, ValueError, *REQUEST_ERRORS) as exc:
            raise JukeboxError(0, str(exc) or type(exc).__name__) from exc
        return self._payload(action)

    def _remember(self, songs: Sequence[Mapping[str, Any]]) -> None:
        for song in songs:
            key = str(song["id"])
            self._seen.pop(key, None)
            self._seen[key] = song
        while len(self._seen) > MEMORY_SIZE:
            del self._seen[next(iter(self._seen))]

    def describe(self, song_id: str) -> tuple[str, str] | None:
        song = next((s for s in self.songs if str(s["id"]) == song_id), None)
        if song is None:
            song = self._seen.get(song_id)
        if song is None:
            return None
        return song_label(song), clean(str(song.get("album") or ""))

    def current_label(self) -> str | None:
        if 0 <= self.current < len(self.songs):
            return song_label(self.songs[self.current])
        return None

    def _set_position(self, seconds: int) -> None:
        self.position = seconds
        self._position_ms = seconds * 1000
        self._position_at = self._wall()

    def _position_now_ms(self) -> int:
        if not self.playing:
            return self._position_ms
        return self._position_ms + round(max(self._wall() - self._position_at, 0.0) * 1000)

    def _songs_changed(self) -> None:
        self._queue_version += 1

    def _status(self) -> dict[str, Any]:
        return {
            "currentIndex": self.current,
            "playing": self.playing,
            "gain": self.gain,
            "position": self.position,
            "positionMs": self._position_now_ms(),
            "muted": self.muted,
            "shuffle": self.shuffle,
            "repeat": self.repeat,
            "queueVersion": f"{self._boot_id}:{self._queue_version}",
        }

    def _payload(self, action: str) -> dict[str, Any]:
        if action == "get":
            return {"jukeboxPlaylist": {**self._status(), "entry": list(self.songs)}}
        return {"jukeboxStatus": self._status()}

    def _tracks(self) -> list[Track]:
        return [track_from_song(song) for song in self.songs]

    def _new_keys(self, count: int) -> list[int]:
        keys = list(range(self._next_key, self._next_key + count))
        self._next_key += count
        return keys

    def _replace_queue(self, songs: list[Mapping[str, Any]]) -> None:
        self._queue_id += 1
        self.songs = songs
        self._next_key = 0
        self._keys = self._new_keys(len(songs))
        self.shuffle = False

    async def poll(self, max_age: float) -> bool:
        """Sync with the device unless a request did so less than max_age seconds ago."""
        async with self.lock:
            if not self.loaded or self._clock() - self._synced_at < max_age:
                return False
            await self._sync()
            return True

    def _feed_plays(self, position_s: float | None) -> None:
        if self._plays is None or self._scrobbler is None:
            return
        try:
            if position_s is None or not 0 <= self.current < len(self.songs):
                events = self._plays.feed(None, None, False, 0.0, self._wall())
            else:
                entry = (self._queue_id, self._keys[self.current])
                song = self.songs[self.current]
                events = self._plays.feed(entry, song, self.playing, position_s, self._wall())
            self._scrobbler.submit(events)
        except Exception as exc:  # noqa: BLE001
            logger.warning("play tracking failed: %s", type(exc).__name__)

    def _end_play(self) -> None:
        if self._plays is None or self._scrobbler is None:
            return
        try:
            self._scrobbler.submit(self._plays.end(self._wall()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("play tracking failed: %s", type(exc).__name__)

    async def _sync(self) -> None:
        status = await self._player.refresh()
        self._synced_at = self._clock()
        if status.volume is not None:
            self.gain = status.volume / 100
        if status.muted is not None:
            self.muted = status.muted
        if not self.loaded:
            self._feed_plays(None)
            return
        if not status.in_control:
            logger.warning("lost control of the device queue")
            self.loaded = False
            self.playing = False
            self._seek_hold = None
            self._set_position(0)
            self._feed_plays(None)
            return
        previous = self.current
        if status.index is not None:
            self.current = status.index
        self.playing = status.playing
        self.position = int(status.position_s)
        self._position_ms = round(status.position_s * 1000)
        self._position_at = status.sampled_at
        self._feed_plays(status.position_s)
        self._apply_seek_hold(previous)

    def _apply_seek_hold(self, previous_index: int) -> None:
        hold = self._seek_hold
        if hold is None:
            return
        index, target_s, set_at = hold
        elapsed = self._wall() - set_at
        if (
            self.current != index
            or previous_index != index
            or not self.playing
            or not 0 <= elapsed < SEEK_HOLD_S
        ):
            self._seek_hold = None
            return
        expected = target_s + elapsed
        if abs(self._position_ms / 1000 - expected) <= SEEK_HOLD_TOLERANCE:
            self._seek_hold = None
            return
        self.position = int(expected)
        self._position_ms = round(expected * 1000)
        self._position_at = self._wall()

    async def _seek_when_playing(self, index: int, seconds: int) -> bool:
        if seconds <= SEEK_TOLERANCE:
            logger.debug("seek to %ds skipped: within %ds of the start", seconds, SEEK_TOLERANCE)
            return True
        started = self._clock()
        deadline = started + SEEK_WAIT_TIMEOUT
        fresh_deadline = started + min(SEEK_FRESH_TIMEOUT, SEEK_WAIT_TIMEOUT)
        target = seconds - SEEK_TOLERANCE
        saw_at_target = False
        while True:
            status = await self._player.refresh()
            on_track = status.in_control and status.playing and status.index == index
            if on_track and status.position_s < target:
                break
            if on_track:
                saw_at_target = True
            if self._clock() >= fresh_deadline:
                if saw_at_target and on_track:
                    logger.info("resume at %ds: device already there", seconds)
                    return True
                logger.warning(
                    "seek to %ds skipped: device did not start track %d in time", seconds, index
                )
                return False
            await self._sleep(SEEK_WAIT_STEP)
        for attempt in range(1, SEEK_ATTEMPTS + 1):
            await self._player.seek(seconds)
            for _ in range(SEEK_VERIFY_CHECKS):
                await self._sleep(SEEK_WAIT_STEP)
                status = await self._player.refresh()
                if status.position_s >= target:
                    logger.info("resumed at %ds after %d seek(s)", seconds, attempt)
                    return True
                if self._clock() >= deadline:
                    break
            else:
                continue
            break
        logger.warning("seek to %ds not confirmed by the device", seconds)
        return False

    async def _lookup_all(self, ids: Sequence[str]) -> list[Mapping[str, Any]]:
        if not ids:
            raise JukeboxError(10, "Required parameter is missing: id")
        found = []
        for song_id in ids:
            song = await self._lookup(song_id)
            if song is None:
                raise JukeboxError(70, f"song not found: {song_id}")
            found.append(song)
        return found

    def _require_index(self, index: int | None) -> int:
        if index is None:
            raise JukeboxError(10, "Required parameter is missing: index")
        if not 0 <= index < len(self.songs):
            raise JukeboxError(0, "index out of range")
        return index

    async def _noop(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        return None

    async def _set(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        songs = await self._lookup_all(ids)
        self._remember(songs)
        if self.loaded:
            await self._player.clear()
        self._end_play()
        self._replace_queue(songs)
        self._songs_changed()
        self._seek_hold = None
        self.current = 0
        self.loaded = False
        self.playing = False
        self._set_position(0)

    async def _add(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        songs = await self._lookup_all(ids)
        self._remember(songs)
        await self._append_songs(songs)

    async def _append_songs(self, songs: list[Mapping[str, Any]]) -> None:
        if self.loaded:
            await self._player.append([track_from_song(song) for song in songs])
        self.songs.extend(songs)
        self._keys.extend(self._new_keys(len(songs)))
        self._songs_changed()

    async def _insert(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        songs = await self._lookup_all(ids)
        self._remember(songs)
        if not self.songs:
            await self._append_songs(songs)
            return
        after = self.current + 1
        if self.loaded:
            await self._player.insert_next([track_from_song(song) for song in songs])
        self.songs[after:after] = songs
        self._keys[after:after] = self._new_keys(len(songs))
        self._songs_changed()

    async def _start(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        if not self.songs:
            return
        if self.loaded:
            await self._player.resume()
        else:
            await self._player.load(self._tracks(), start=self.current, play=True)
            self.loaded = True
            if self.position > 0:
                await self._seek_when_playing(self.current, self.position)
        self._position_at = self._wall()
        self.playing = True

    async def _stop(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        self._seek_hold = None
        if self.loaded:
            await self._player.pause()
        self.playing = False

    async def _skip(
        self, ids: Sequence[str], index: int | None, offset: int | None, gain: Any
    ) -> None:
        target = self._require_index(index)
        seconds = offset or 0
        self._seek_hold = None
        if self.loaded:
            if target != self.current:
                await self._player.play_index(target)
                if seconds > 0:
                    await self._seek_when_playing(target, seconds)
                if not self.playing:
                    await self._player.pause()
            else:
                await self._player.seek(seconds)
                if self.playing:
                    self._seek_hold = (target, float(seconds), self._wall())
        self.current = target
        self._set_position(seconds)

    async def _remove(self, ids: Sequence[str], index: int | None, offset: Any, gain: Any) -> None:
        target = self._require_index(index)
        if self.loaded and target == self.current:
            await self._player.clear()
            self._end_play()
            self.loaded = False
            self.playing = False
            self._seek_hold = None
            self._set_position(0)
        elif self.loaded:
            await self._player.remove(target)
        del self.songs[target]
        del self._keys[target]
        self._songs_changed()
        if target < self.current:
            self.current -= 1
        self.current = min(self.current, max(len(self.songs) - 1, 0))

    async def _clear(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        if self.loaded:
            await self._player.clear()
        self._end_play()
        self._replace_queue([])
        self._songs_changed()
        self._seek_hold = None
        self.current = 0
        self.loaded = False
        self.playing = False
        self._set_position(0)

    async def _move(self, index: int | None, to_index: int | None) -> None:
        if index is None:
            raise JukeboxError(10, "Required parameter is missing: index")
        if to_index is None:
            raise JukeboxError(10, "Required parameter is missing: toIndex")
        source = self._require_index(index)
        target = self._require_index(to_index)
        if source == target:
            return
        if source == self.current:
            raise JukeboxError(0, "cannot move the playing track")
        playing_key = self._keys[self.current]
        if self.loaded:
            await self._player.move(source, target)
        self.songs.insert(target, self.songs.pop(source))
        self._keys.insert(target, self._keys.pop(source))
        self.current = self._keys.index(playing_key)
        self._songs_changed()

    async def _set_shuffle(self, enabled: bool | None) -> None:
        if enabled is None:
            raise JukeboxError(10, "Required parameter is missing: shuffle")
        if not enabled and not self.shuffle:
            return
        first = self.current + 1
        upcoming = list(range(first, len(self.songs)))
        order = list(upcoming)
        if enabled:
            self._rng.shuffle(order)
        else:
            order.sort(key=self._keys.__getitem__)
        if order == upcoming:
            self.shuffle = enabled
            return
        songs = [self.songs[i] for i in order]
        keys = [self._keys[i] for i in order]
        desynced = False
        if self.loaded:
            try:
                await self._player.replace_upcoming([track_from_song(song) for song in songs])
            except QueueDesyncError as exc:
                logger.warning(
                    "reorder left the device queue out of step, reloading it: %s",
                    redact(str(exc.__cause__ or exc)),
                )
                desynced = True
        self.songs[first:] = songs
        self._keys[first:] = keys
        self.shuffle = enabled
        self._songs_changed()
        if desynced:
            await self._reload_queue()

    async def _set_repeat(self, repeat: str | None) -> None:
        if repeat is None:
            raise JukeboxError(10, "Required parameter is missing: repeat")
        if repeat not in LOOP_MODES:
            raise JukeboxError(10, "Invalid parameter: repeat")
        await self._player.set_repeat(repeat)
        self.repeat = repeat

    async def _reload_queue(self) -> None:
        try:
            await self._player.load(self._tracks(), start=self.current, play=self.playing)
            if self.playing and self.position > 0:
                await self._seek_when_playing(self.current, self.position)
        except (UpnpError, PlayerError, ValueError, *REQUEST_ERRORS):
            self.loaded = False
            self.playing = False
            self._seek_hold = None
            raise

    async def _shuffle(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        raise JukeboxError(0, "shuffle is not supported")

    async def _set_mute(self, mute: bool | None) -> None:
        if mute is None:
            raise JukeboxError(10, "Required parameter is missing: mute")
        await self._player.set_mute(mute)
        self.muted = mute

    async def _set_gain(
        self, ids: Sequence[str], index: Any, offset: Any, gain: float | None
    ) -> None:
        if gain is None:
            raise JukeboxError(10, "Required parameter is missing: gain")
        clamped = min(max(gain, 0.0), 1.0)
        self.gain = await self._player.set_volume(round(clamped * 100)) / 100


def _parse_int(params: Mapping[str, str], name: str) -> int | None:
    if name not in params:
        return None
    try:
        return int(params[name])
    except ValueError:
        raise JukeboxError(10, f"Invalid parameter: {name}") from None


def _parse_bool(params: Mapping[str, str], name: str) -> bool | None:
    if name not in params:
        return None
    value = params[name].strip().lower()
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise JukeboxError(10, f"Invalid parameter: {name}")


def _parse_repeat(params: Mapping[str, str]) -> str | None:
    if "repeat" not in params:
        return None
    value = params["repeat"].strip().lower()
    if value not in LOOP_MODES:
        raise JukeboxError(10, "Invalid parameter: repeat")
    return value


def _parse_gain(params: Mapping[str, str]) -> float | None:
    if "gain" not in params:
        return None
    try:
        value = float(params["gain"])
    except ValueError:
        raise JukeboxError(10, "Invalid parameter: gain") from None
    if not math.isfinite(value):
        raise JukeboxError(10, "Invalid parameter: gain")
    return value


async def _collect(request: web.Request) -> tuple[dict[str, str], list[str]]:
    params: dict[str, str] = {}
    ids = request.query.getall("id", [])
    if request.method == "POST":
        form = await request.post()
        for key in form:
            value = form.get(key)
            if isinstance(value, str):
                params[key] = value
        if not ids:
            ids = [v for v in form.getall("id", []) if isinstance(v, str)]
    params.update(request.query)
    return params, list(ids)


def add_jukebox_routes(
    app: web.Application, jukebox: Jukebox, authenticate: Authenticator
) -> None:
    def reply(envelope: dict[str, Any]) -> web.Response:
        return web.json_response(envelope, headers=CORS_ORIGIN)

    async def run(request: web.Request) -> web.Response:
        params, ids = await _collect(request)
        if not await authenticate(params):
            logger.info("jukebox rejected login")
            return reply(error_envelope(WRONG_LOGIN, "Wrong username or password"))
        action = params.get("action")
        try:
            if not action:
                raise JukeboxError(10, "Required parameter is missing: action")
            index = _parse_int(params, "index")
            offset = _parse_int(params, "offset")
            gain = _parse_gain(params)
            to_index = _parse_int(params, "toIndex")
            mute = _parse_bool(params, "mute")
            shuffle = _parse_bool(params, "shuffle")
            repeat = _parse_repeat(params)
            async with jukebox.lock:
                payload = await jukebox.handle(
                    action, ids, index, offset, gain, mute, to_index, shuffle, repeat
                )
        except JukeboxError as exc:
            if exc.code == WRONG_LOGIN:
                logger.info("jukebox rejected login")
            else:
                logger.warning(
                    "jukebox action=%s failed code=%d: %s",
                    action or "-",
                    exc.code,
                    redact(exc.message),
                )
            return reply(error_envelope(exc.code, exc.message))
        suffix = "".join(
            f" {name}={value}"
            for name, value in (("toIndex", to_index), ("shuffle", shuffle), ("repeat", repeat))
            if value is not None
        )
        if action in ("skip", "start"):
            label = jukebox.current_label()
            suffix += f' track="{label}"' if label is not None else ""
        elif action in ("set", "add", "insert") and ids:
            described = jukebox.describe(ids[0])
            suffix += f' first="{described[0]}"' if described is not None else ""
        logger.log(
            logging.DEBUG if action in QUIET_ACTIONS else logging.INFO,
            "jukebox action=%s index=%s offset=%s ids=%d gain=%s mute=%s%s",
            action,
            *("-" if value is None else value for value in (index, offset)),
            len(ids),
            *("-" if value is None else value for value in (gain, mute)),
            suffix,
        )
        return reply(ok_envelope(payload))

    async def preflight(request: web.Request) -> web.Response:
        return web.Response(status=204, headers=CORS_PREFLIGHT)

    for path in PATHS:
        app.router.add_get(path, run)
        app.router.add_post(path, run)
        app.router.add_route("OPTIONS", path, preflight)
