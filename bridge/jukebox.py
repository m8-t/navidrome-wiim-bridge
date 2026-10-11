"""Subsonic jukeboxControl endpoint driving the bridge Player."""

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from aiohttp import web

from bridge.player import Player, PlayerError, Track
from bridge.soap import UpnpError
from bridge.wiim import REQUEST_ERRORS

SongLookup = Callable[[str], Awaitable[Mapping[str, Any] | None]]
Authenticator = Callable[[Mapping[str, str]], Awaitable[bool]]

ENVELOPE_BASE = {
    "version": "1.16.1",
    "type": "wiim-bridge",
    "serverVersion": "0.1",
    "openSubsonic": True,
}
PATHS = ("/rest/jukeboxControl", "/rest/jukeboxControl.view")
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
    def __init__(self, player: Player, lookup: SongLookup) -> None:
        self._player = player
        self._lookup = lookup
        self.songs: list[Mapping[str, Any]] = []
        self.current = 0
        self.gain = 0.5
        self.loaded = False
        self.playing = False
        self.position = 0
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
        }

    async def handle(
        self,
        action: str,
        ids: Sequence[str],
        index: int | None,
        offset: int | None,
        gain: float | None,
    ) -> dict[str, Any]:
        if action not in self._actions:
            raise JukeboxError(0, "unknown action")
        try:
            if action not in ("set", "clear"):
                await self._sync()
            await self._actions[action](ids, index, offset, gain)
        except JukeboxError:
            raise
        except (UpnpError, PlayerError, ValueError, *REQUEST_ERRORS) as exc:
            raise JukeboxError(0, str(exc) or type(exc).__name__) from exc
        return self._payload(action)

    def _status(self) -> dict[str, Any]:
        return {
            "currentIndex": self.current,
            "playing": self.playing,
            "gain": self.gain,
            "position": self.position,
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
        if not self.loaded:
            return
        if not status.in_control:
            self.loaded = False
            self.playing = False
            self.position = 0
            return
        if status.index is not None:
            self.current = status.index
        self.playing = status.playing
        self.position = int(status.position_s)

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
        if self.loaded:
            await self._player.clear()
        self.songs = songs
        self.current = 0
        self.loaded = False
        self.playing = False
        self.position = 0

    async def _add(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        songs = await self._lookup_all(ids)
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
                await self._player.seek(self.position)
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
                    await self._player.seek(seconds)
                if not self.playing:
                    await self._player.pause()
            else:
                await self._player.seek(seconds)
        self.current = target
        self.position = seconds

    async def _remove(self, ids: Sequence[str], index: int | None, offset: Any, gain: Any) -> None:
        target = self._require_index(index)
        if self.loaded and target == self.current:
            await self._player.clear()
            self.loaded = False
            self.playing = False
            self.position = 0
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
        self.position = 0

    async def _shuffle(self, ids: Sequence[str], index: Any, offset: Any, gain: Any) -> None:
        raise JukeboxError(0, "shuffle is not supported")

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
            return reply(error_envelope(40, "Wrong username or password"))
        try:
            action = params.get("action")
            if not action:
                raise JukeboxError(10, "Required parameter is missing: action")
            index = _parse_int(params, "index")
            offset = _parse_int(params, "offset")
            gain = _parse_gain(params)
            async with lock:
                payload = await jukebox.handle(action, ids, index, offset, gain)
        except JukeboxError as exc:
            return reply(error_envelope(exc.code, exc.message))
        return reply(ok_envelope(payload))

    async def preflight(request: web.Request) -> web.Response:
        return web.Response(status=204, headers=CORS_PREFLIGHT)

    for path in PATHS:
        app.router.add_get(path, run)
        app.router.add_post(path, run)
        app.router.add_route("OPTIONS", path, preflight)
