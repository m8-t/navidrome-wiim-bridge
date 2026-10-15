"""WiiM device access over UPnP AVTransport, PlayQueue and the status API."""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiohttp

from bridge.soap import (
    SERVICE,
    UpnpError,
    build_didl,
    build_envelope,
    escape_xml,
    format_hms,
    parse_fault,
    parse_values,
    soap_headers,
)

CONTROL_PATH = "/upnp/control/rendertransport1"
PLAYQUEUE_CONTROL_PATH = "/upnp/control/PlayQueue1"
PLAYQUEUE_SERVICE = "urn:schemas-wiimu-com:service:PlayQueue:1"
RENDERING_CONTROL_PATH = "/upnp/control/rendercontrol1"
RENDERING_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"


def device_urls(host: str) -> tuple[str, str]:
    """Return (UPnP base URL, status API URL) for a device host name or IP address."""
    if "://" in host or "/" in host:
        raise ValueError("device host must be a host name or IP address, not a URL")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:49152", f"https://{host}/httpapi.asp?command=getPlayerStatus"


SENSITIVE_PARAMS = frozenset({"u", "t", "s", "p", "c", "apikey", "token"})
JUMP_TOLERANCE_MS = 1500
REQUEST_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)


def redact_uri(uri: str | None) -> str | None:
    if uri is None:
        return None
    rest, hash_sep, fragment = uri.partition("#")
    base, query_sep, query = rest.partition("?")
    if not query_sep:
        return uri
    pieces = [_redact_param(piece) for piece in query.split("&")]
    return base + query_sep + "&".join(pieces) + hash_sep + fragment


def _redact_param(piece: str) -> str:
    name, eq, _ = piece.partition("=")
    if eq and name.lower() in SENSITIVE_PARAMS:
        return f"{name}=***"
    return piece


@dataclass(frozen=True)
class QueueTrack:
    url: str
    title: str
    artist: str
    album: str
    duration_s: int
    item_id: str


def _queue_track_xml(index: int, track: QueueTrack) -> str:
    didl = build_didl(
        track.url, track.title, track.artist, track.album, track.duration_s, track.item_id
    )
    return (
        f"<Track{index}><URL>{escape_xml(track.url)}</URL>"
        f"<Metadata>{escape_xml(didl)}</Metadata><Id>{index}</Id></Track{index}>"
    )


def build_queue_context(name: str, tracks: Sequence[QueueTrack], last_play_index: int = 1) -> str:
    if not tracks:
        raise ValueError("queue needs at least one track")
    items = "".join(_queue_track_xml(i, track) for i, track in enumerate(tracks, start=1))
    return (
        f'<?xml version="1.0"?><PlayList><ListName>{escape_xml(name)}</ListName>'
        f"<ListInfo><SourceName>UPnPServer</SourceName><TrackNumber>{len(tracks)}</TrackNumber>"
        "<SearchUrl></SearchUrl><Quality>0</Quality><UpdateTime>0</UpdateTime>"
        f"<LastPlayIndex>{last_play_index}</LastPlayIndex><SwitchPageMode>0</SwitchPageMode>"
        "<CurrentPage>0</CurrentPage><TotalPages>0</TotalPages></ListInfo>"
        f"<Tracks>{items}</Tracks></PlayList>"
    )


def _int_or_none(text: str | None) -> int | None:
    if text is not None and re.fullmatch(r"-?[0-9]+", text.strip()):
        return int(text)
    return None


def parse_queue_index(xml_text: str) -> dict[str, int | None]:
    found = parse_values(
        xml_text, ("CurrentIndex", "PreloadingIndex", "CurrentPage", "TrackNums")
    )
    return {
        "current_index": _int_or_none(found["CurrentIndex"]),
        "preloading_index": _int_or_none(found["PreloadingIndex"]),
        "current_page": _int_or_none(found["CurrentPage"]),
        "track_nums": _int_or_none(found["TrackNums"]),
    }


def parse_queue_context(xml_text: str) -> str | None:
    return parse_values(xml_text, ("QueueContext",))["QueueContext"]


def _queue_track_title(block: str) -> str | None:
    metadata = parse_values(block, ("Metadata",))["Metadata"]
    if metadata is None:
        return None
    return parse_values(metadata, ("dc:title",))["dc:title"]


def parse_queue_tracks(context: str) -> list[dict[str, str | None]]:
    tracks: list[dict[str, str | None]] = []
    for match in re.finditer(r"<Track([0-9]+)>(.*?)</Track\1>", context, re.DOTALL):
        block = match.group(2)
        found = parse_values(block, ("Id", "URL"))
        tracks.append(
            {
                "index": match.group(1),
                "id": found["Id"],
                "url": found["URL"],
                "title": _queue_track_title(block),
            }
        )
    return tracks


def _text_field(track: Mapping[str, Any], key: str) -> str:
    value = track.get(key)
    return "" if value is None else str(value)


def _queue_track_from_album(track: object, base_url: str, profile: str) -> QueueTrack:
    if not isinstance(track, Mapping):
        raise ValueError("album track is not an object")
    if track.get("id") is None or track.get("title") is None:
        raise ValueError("album track lacks id or title")
    track_id = str(track["id"])
    try:
        duration_s = int(track.get("duration") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"track {track_id}: bad duration") from exc
    return QueueTrack(
        url=f"{base_url}/{profile}/{track_id}.flac",
        title=str(track["title"]),
        artist=_text_field(track, "artist"),
        album=_text_field(track, "album"),
        duration_s=duration_s,
        item_id=track_id,
    )


def tracks_from_album(album: Mapping[str, Any], base_url: str, profile: str) -> list[QueueTrack]:
    tracks = album.get("tracks") if isinstance(album, Mapping) else None
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("album has no tracks")
    return [_queue_track_from_album(track, base_url, profile) for track in tracks]


@dataclass(frozen=True)
class DeviceState:
    t: float
    transport: str | None
    current_uri: str | None
    next_uri: str | None
    api_status: str | None
    curpos_ms: int | None
    queue_index: int | None = None
    volume: int | None = None
    muted: bool | None = None


def _position_jumped(prev: DeviceState, cur: DeviceState) -> bool:
    if prev.api_status != "play" or cur.api_status != "play":
        return False
    if prev.curpos_ms is None or cur.curpos_ms is None:
        return False
    expected = prev.curpos_ms + (cur.t - prev.t) * 1000
    return abs(cur.curpos_ms - expected) > JUMP_TOLERANCE_MS


def describe_change(prev: DeviceState | None, cur: DeviceState) -> str | None:
    if prev is None:
        return "initial"
    parts: list[str] = []
    if prev.transport != cur.transport:
        parts.append(f"transport {prev.transport}->{cur.transport}")
    if prev.current_uri != cur.current_uri:
        parts.append("current_uri changed")
    if prev.next_uri != cur.next_uri:
        parts.append("next_uri changed")
    if prev.queue_index != cur.queue_index:
        parts.append(f"queue_index {prev.queue_index}->{cur.queue_index}")
    if prev.volume != cur.volume:
        parts.append(f"volume {prev.volume}->{cur.volume}")
    if prev.api_status != cur.api_status:
        parts.append(f"api_status {prev.api_status}->{cur.api_status}")
    if _position_jumped(prev, cur):
        parts.append(f"position jump {prev.curpos_ms}->{cur.curpos_ms}")
    return "; ".join(parts) or None


@dataclass(frozen=True)
class ApiStatus:
    status: str | None = None
    curpos_ms: int | None = None
    volume: int | None = None
    muted: bool | None = None


def _parse_volume(raw: object) -> int | None:
    try:
        return int(str(raw))
    except ValueError:
        return None


def _blank_to_none(value: str | None) -> str | None:
    return value or None


def _parse_curpos(raw: object) -> int | None:
    try:
        return int(str(raw))
    except ValueError:
        return None


class Wiim:
    def __init__(
        self,
        soap_session: aiohttp.ClientSession,
        status_session: aiohttp.ClientSession,
        device_base: str,
        status_url: str,
    ) -> None:
        self._soap_session = soap_session
        self._status_session = status_session
        self._device_base = device_base
        self._status_url = status_url

    async def call(
        self,
        action: str,
        args: Mapping[str, str],
        service: str = SERVICE,
        control_path: str = CONTROL_PATH,
    ) -> str:
        url = self._device_base + control_path
        data = build_envelope(action, args, service).encode("utf-8")
        async with self._soap_session.post(
            url, data=data, headers=soap_headers(action, service)
        ) as resp:
            body = await resp.text(errors="replace")
            status = resp.status
        if status == 200:
            return body
        fault = parse_fault(body)
        if fault.code is None:
            raise UpnpError(None, f"HTTP {status}")
        raise fault

    async def set_uri(self, url: str, metadata: str) -> str:
        return await self.call(
            "SetAVTransportURI",
            {"InstanceID": "0", "CurrentURI": url, "CurrentURIMetaData": metadata},
        )

    async def set_next(self, url: str, metadata: str) -> str:
        return await self.call(
            "SetNextAVTransportURI",
            {"InstanceID": "0", "NextURI": url, "NextURIMetaData": metadata},
        )

    async def play(self) -> str:
        return await self.call("Play", {"InstanceID": "0", "Speed": "1"})

    async def pause(self) -> str:
        return await self.call("Pause", {"InstanceID": "0"})

    async def stop(self) -> str:
        return await self.call("Stop", {"InstanceID": "0"})

    async def next(self) -> str:
        return await self.call("Next", {"InstanceID": "0"})

    async def previous(self) -> str:
        return await self.call("Previous", {"InstanceID": "0"})

    async def seek(self, seconds: int) -> str:
        return await self.call(
            "Seek",
            {"InstanceID": "0", "Unit": "REL_TIME", "Target": format_hms(seconds)},
        )

    async def _queue_call(self, action: str, args: Mapping[str, str]) -> str:
        return await self.call(action, args, PLAYQUEUE_SERVICE, PLAYQUEUE_CONTROL_PATH)

    async def queue_create(self, context: str) -> str:
        return await self._queue_call("CreateQueue", {"QueueContext": context})

    async def queue_play(self, name: str, index: int) -> str:
        return await self._queue_call(
            "PlayQueueWithIndex", {"QueueName": name, "Index": str(index)}
        )

    async def queue_index(self, name: str = "CurrentQueue") -> dict[str, int | None]:
        return parse_queue_index(await self._queue_call("GetQueueIndex", {"QueueName": name}))

    async def queue_browse(self, name: str) -> str | None:
        return parse_queue_context(await self._queue_call("BrowseQueue", {"QueueName": name}))

    async def queue_delete(self, name: str) -> str:
        return await self._queue_call("DeleteQueue", {"QueueName": name})

    async def queue_loop_mode(self) -> int | None:
        body = await self._queue_call("GetQueueLoopMode", {})
        return _int_or_none(parse_values(body, ("LoopMode",))["LoopMode"])

    async def queue_set_loop_mode(self, mode: int) -> str:
        return await self._queue_call("SetQueueLoopMode", {"LoopMode": str(mode)})

    async def queue_remove(self, name: str, start: int, end: int) -> str:
        return await self._queue_call(
            "RemoveTracksInQueue",
            {
                "QueueName": name,
                "RangStart": str(start),
                "RangEnd": str(end),
                "Action": "",
            },
        )

    async def queue_move(self, name: str, index: int, to_index: int) -> str:
        return await self._queue_call(
            "MoveTracksInQueue",
            {"QueueName": name, "IndexList": str(index), "ToIndex": str(to_index)},
        )

    async def queue_insert(self, context: str, after_index: int, play: bool = False) -> str:
        """Insert after the playing track on the device; StartIndex is ignored."""
        return await self._queue_call(
            "AppendTracksInQueueEx",
            {
                "QueueContext": context,
                "Direction": "1",
                "StartIndex": str(after_index),
                "Play": "1" if play else "0",
                "Action": "",
            },
        )

    async def queue_append(self, context: str, start_index: int) -> str:
        return await self._queue_call(
            "AppendTracksInQueueEx",
            {
                "QueueContext": context,
                "Direction": "0",
                "StartIndex": str(start_index),
                "Play": "0",
                "Action": "",
            },
        )

    async def _current_queue_index(self) -> int | None:
        try:
            return (await self.queue_index())["current_index"]
        except (UpnpError, *REQUEST_ERRORS):
            return None

    async def set_volume(self, volume: int) -> str:
        if not 0 <= volume <= 100:
            raise ValueError("volume must be between 0 and 100")
        return await self.call(
            "SetVolume",
            {"InstanceID": "0", "Channel": "Master", "DesiredVolume": str(volume)},
            RENDERING_SERVICE,
            RENDERING_CONTROL_PATH,
        )

    async def set_mute(self, muted: bool) -> str:
        return await self.call(
            "SetMute",
            {"InstanceID": "0", "Channel": "Master", "DesiredMute": "1" if muted else "0"},
            RENDERING_SERVICE,
            RENDERING_CONTROL_PATH,
        )

    async def _api_status(self) -> ApiStatus:
        try:
            async with self._status_session.get(self._status_url, ssl=False) as resp:
                resp.raise_for_status()
                payload = json.loads(await resp.text())
        except (*REQUEST_ERRORS, ValueError):
            return ApiStatus()
        if not isinstance(payload, dict):
            return ApiStatus()
        status = payload.get("status")
        return ApiStatus(
            status=str(status) if status is not None else None,
            curpos_ms=_parse_curpos(payload.get("curpos")),
            volume=_parse_volume(payload.get("vol")),
            muted={"1": True, "0": False}.get(str(payload.get("mute"))),
        )

    async def state(self) -> DeviceState:
        transport = parse_values(
            await self.call("GetTransportInfo", {"InstanceID": "0"}),
            ("CurrentTransportState",),
        )
        media = parse_values(
            await self.call("GetMediaInfo", {"InstanceID": "0"}),
            ("CurrentURI", "NextURI"),
        )
        queue_index = await self._current_queue_index()
        api = await self._api_status()
        return DeviceState(
            t=time.time(),
            transport=transport["CurrentTransportState"],
            current_uri=_blank_to_none(media["CurrentURI"]),
            next_uri=_blank_to_none(media["NextURI"]),
            api_status=api.status,
            curpos_ms=api.curpos_ms,
            queue_index=queue_index,
            volume=api.volume,
            muted=api.muted,
        )

    async def position(self) -> dict[str, str | None]:
        found = parse_values(
            await self.call("GetPositionInfo", {"InstanceID": "0"}),
            ("RelTime", "TrackDuration"),
        )
        return {"rel_time": found["RelTime"], "track_duration": found["TrackDuration"]}


@asynccontextmanager
async def open_sessions(
    timeout_s: float = 5,
) -> AsyncIterator[tuple[aiohttp.ClientSession, aiohttp.ClientSession]]:
    """Yield (soap_session, status_session); SOAP connections are closed after each call."""
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    soap_connector = aiohttp.TCPConnector(force_close=True)
    async with (
        aiohttp.ClientSession(timeout=timeout, connector=soap_connector) as soap_session,
        aiohttp.ClientSession(timeout=timeout) as status_session,
    ):
        yield soap_session, status_session
