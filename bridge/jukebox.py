"""Subsonic jukeboxControl endpoint driving the bridge Player."""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from aiohttp import web

from bridge.log import clean, redact, song_label
from bridge.player import Player, PlayerError, Track
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
    ) -> None:
        self._player = player
        self._lookup = lookup
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self.songs: list[Mapping[str, Any]] = []
        self._seen: dict[str, Mapping[str, Any]] = {}
        self.current = 0
        self.gain = 0.5
        self.muted = False
        self.loaded = False
        self.playing = False
        self.position = 0
        self._position_ms = 0
        self._position_at = 0.0
        self._actions: dict[str, Callable[..., Awaitable[None]]] = {
            "get": self._noop,
            "status": self._noop,
            "set": self._set,
            "start": self._start,
            "stop": self._stop,
            "skip": self._skip,
            "add": self._add,
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
    ) -> dict[str, Any]:
        if action not in self._actions:
            raise JukeboxError(0, "unknown action")
        try:
            if action not in ("set", "clear"):
                await self._sync()
            if action == "setMute":
                await self._set_mute(mute)
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

    def _status(self) -> dict[str, Any]:
        return {
            "currentIndex": self.current,
            "playing": self.playing,
            "gain": self.gain,
            "position": self.position,
            "positionMs": self._position_now_ms(),
            "muted": self.muted,
        }

    def _payload(self, action: str) -> dict[str, Any]:
        if action == "get":
            return {"jukeboxPlaylist": {**self._status(), "entry": list(self.songs)}}
        return {"jukeboxStatus": self._status()}

    def _tracks(self) -> list[Track]:
        return [track_from_song(song) for song in self.songs]

    async def _sync(self) -> None:
        status = await self._player.refresh()
        if status.volume is not None:
            self.gain = status.volume / 100
        if status.muted is not None:
            self.muted = status.muted
        if not self.loaded:
            return
        if not status.in_control:
            logger.warning("lost control of the device queue")
            self.loaded = False
            self.playing = False
            self._set_position(0)
            return
        if status.index is not None:
            self.current = status.index
        self.playing = status.playing
        self.position = int(status.position_s)
        self._position_ms = round(status.position_s * 1000)
        self._position_at = status.sampled_at

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
        self.songs = songs
        self.current = 0
        self.loaded = False
        self.playing = False
        self._set_position(0)

    async def _add(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        songs = await self._lookup_all(ids)
        self._remember(songs)
        if self.loaded:
            await self._player.append([track_from_song(song) for song in songs])
        self.songs.extend(songs)

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
        if self.loaded:
            await self._player.pause()
        self.playing = False

    async def _skip(
        self, ids: Sequence[str], index: int | None, offset: int | None, gain: Any
    ) -> None:
        target = self._require_index(index)
        seconds = offset or 0
        if self.loaded:
            if target != self.current:
                await self._player.play_index(target)
                if seconds > 0:
                    await self._seek_when_playing(target, seconds)
                if not self.playing:
                    await self._player.pause()
            else:
                await self._player.seek(seconds)
        self.current = target
        self._set_position(seconds)

    async def _remove(self, ids: Sequence[str], index: int | None, offset: Any, gain: Any) -> None:
        target = self._require_index(index)
        if self.loaded and target == self.current:
            await self._player.clear()
            self.loaded = False
            self.playing = False
            self._set_position(0)
        elif self.loaded:
            await self._player.remove(target)
        del self.songs[target]
        if target < self.current:
            self.current -= 1
        self.current = min(self.current, max(len(self.songs) - 1, 0))

    async def _clear(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        if self.loaded:
            await self._player.clear()
        self.songs = []
        self.current = 0
        self.loaded = False
        self.playing = False
        self._set_position(0)

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


def _parse_mute(params: Mapping[str, str]) -> bool | None:
    if "mute" not in params:
        return None
    value = params["mute"].strip().lower()
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise JukeboxError(10, "Invalid parameter: mute")


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
    lock = asyncio.Lock()

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
            mute = _parse_mute(params)
            async with lock:
                payload = await jukebox.handle(action, ids, index, offset, gain, mute)
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
        suffix = ""
        if action in ("skip", "start"):
            label = jukebox.current_label()
            suffix = f' track="{label}"' if label is not None else ""
        elif action in ("set", "add") and ids:
            described = jukebox.describe(ids[0])
            suffix = f' first="{described[0]}"' if described is not None else ""
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
