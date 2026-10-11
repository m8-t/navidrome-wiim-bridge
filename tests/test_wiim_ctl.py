import asyncio
import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bridge.soap import (
    UpnpError,
    build_didl,
    build_envelope,
    escape_metadata,
    escape_xml,
    format_hms,
    parse_fault,
    parse_hms,
    parse_values,
    soap_headers,
)
from bridge.wiim import (
    CONTROL_PATH,
    PLAYQUEUE_CONTROL_PATH,
    PLAYQUEUE_SERVICE,
    DeviceState,
    QueueTrack,
    Wiim,
    build_queue_context,
    describe_change,
    parse_queue_context,
    parse_queue_index,
    parse_queue_tracks,
    redact_uri,
    tracks_from_album,
)
from wiim_ctl import (
    InputError,
    build_parser,
    cmd_queue_create,
    queue_context_from_args,
)

MEDIA_INFO = r"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>
<u:GetMediaInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<NrTracks>1</NrTracks>
<MediaDuration>00:03:16</MediaDuration>
<CurrentURI>http://172.16.20.30:4533/rest/stream.view?id=5gFSmk6J99a0AZx7u5uYMP&amp;v=1.13.0&amp;c=XXX&amp;u=XXX&amp;s=XXX&amp;t=XXX</CurrentURI>
<CurrentURIMetaData>&lt;DIDL-Lite&gt;...&lt;/DIDL-Lite&gt;</CurrentURIMetaData>
<NextURI>http://172.16.20.30:4533/rest/stream.view?id=67dH1IcXCSk65j1V5Ajn7X&amp;v=1.13.0&amp;c=XXX&amp;u=XXX&amp;s=XXX&amp;t=XXX</NextURI>
<NextURIMetaData>&lt;DIDL-Lite&gt;...&lt;/DIDL-Lite&gt;</NextURIMetaData>
<TrackSource></TrackSource>
<PlayMedium>THIRD-DLNA</PlayMedium>
<PreSwitchMode>DLNA</PreSwitchMode>
<RecordMedium>NOT_IMPLEMENTED</RecordMedium>
<WriteStatus>NOT_IMPLEMENTED</WriteStatus>
</u:GetMediaInfoResponse>
</s:Body> </s:Envelope>"""

TRANSPORT_INFO = r"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>
<u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
<CurrentTransportState>STOPPED</CurrentTransportState>
<CurrentTransportStatus>OK</CurrentTransportStatus>
<CurrentSpeed>1</CurrentSpeed>
</u:GetTransportInfoResponse>
</s:Body> </s:Envelope>"""

FAULT = r"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>701</errorCode><errorDescription>Transition not available</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>"""

QUEUE_INDEX = r"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>
<u:GetQueueIndexResponse xmlns:u="urn:schemas-wiimu-com:service:PlayQueue:1">
<CurrentIndex>2</CurrentIndex>
<PreloadingIndex>2</PreloadingIndex>
<CurrentPage>1</CurrentPage>
<TrackNums>3</TrackNums>
</u:GetQueueIndexResponse>
</s:Body> </s:Envelope>"""

FAULT_501 = FAULT.replace("<errorCode>701</errorCode>", "<errorCode>501</errorCode>").replace(
    "Transition not available", "GetLastIndexByQueue failed"
)

BROWSE_CONTEXT = r"""<?xml version="1.0"?>
<PlayList>
<ListName>bridge_poc</ListName>
<ListInfo>
<QueueVersion>2.0</QueueVersion>
<SourceName>UPnPServer</SourceName>
<PicUrl></PicUrl>
<TotalNumber>3</TotalNumber>
<TrackNumber>3</TrackNumber>
<LastPlayIndex>1</LastPlayIndex>
</ListInfo>
<Tracks>
<Track1>
<Id>1</Id>
<URL>http://172.16.20.30:8098/s/SECRET/range/0zyJc2ogSZBFHR1lz0d7yq.flac</URL>
<Metadata>&lt;?xml version=&quot;1.0&quot; encoding=&quot;UTF-8&quot;?&gt;
&lt;DIDL-Lite xmlns:dc=&quot;http://purl.org/dc/elements/1.1/&quot;&gt;
&lt;dc:title&gt;You Can&apos;t Hide, You Can&apos;t Run&lt;/dc:title&gt;
&lt;upnp:artist&gt;Dilated Peoples&lt;/upnp:artist&gt;
&lt;/DIDL-Lite&gt;
</Metadata>
<Source>UPnPServer</Source>
</Track1>
<Track2>
<Id>2</Id>
<URL>http://172.16.20.30:8098/s/SECRET/range/5gFSmk6J99a0AZx7u5uYMP.flac</URL>
<Metadata>&lt;dc:title&gt;PoC track 2&lt;/dc:title&gt;</Metadata>
<Source>UPnPServer</Source>
</Track2>
</Tracks>
</PlayList>"""

BROWSE_BODY = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
    '<u:BrowseQueueResponse xmlns:u="urn:schemas-wiimu-com:service:PlayQueue:1">'
    f"<QueueContext>{html.escape(BROWSE_CONTEXT)}</QueueContext>"
    "</u:BrowseQueueResponse></s:Body></s:Envelope>"
)

LOOP_MODE_BODY = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
    '<u:GetQueueLoopModeResponse xmlns:u="urn:schemas-wiimu-com:service:PlayQueue:1">'
    "<LoopMode>3</LoopMode></u:GetQueueLoopModeResponse></s:Body></s:Envelope>"
)

PLAYER_STATUS = '{"type":"0","ch":"0","mode":"2","loop":"3","eq":"0","vendor":"","status":"play","curpos":"29564","offset_pts":"29564","totlen":"0","Title":"21446F6E6E657277657474657221","Artist":"5072696E7A205069","Album":"21446F6E6E657277657474657221","alarmflag":"0","plicount":"0","plicurr":"0","vol":"27","mute":"0"}'

DIDL_OPEN = (
    '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">'
)

AVT = "urn:schemas-upnp-org:service:AVTransport:1"

T = TypeVar("T")


def test_escape_xml() -> None:
    assert escape_xml('a&b<c>"d"') == "a&amp;b&lt;c&gt;&quot;d&quot;"


def test_build_envelope() -> None:
    expected = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body><u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        "<InstanceID>0</InstanceID><Speed>1</Speed></u:Play></s:Body></s:Envelope>"
    )
    assert build_envelope("Play", {"InstanceID": "0", "Speed": "1"}) == expected
    assert "<Name>a&amp;b</Name>" in build_envelope("Play", {"Name": "a&b"})


def test_soap_headers() -> None:
    assert soap_headers("Play") == {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
    }


def test_escape_metadata() -> None:
    assert escape_metadata("Bjørk & Co") == "Bj&#248;rk &amp; Co"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(194, "00:03:14"), (3725, "01:02:05"), (0, "00:00:00")],
)
def test_format_hms(seconds: int, text: str) -> None:
    assert format_hms(seconds) == text


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("00:03:16", 196),
        ("01:02:05", 3725),
        ("NOT_IMPLEMENTED", None),
        ("", None),
        (None, None),
        ("3:16", None),
        ("00:03:16\n", None),
    ],
)
def test_parse_hms(text: str | None, seconds: int | None) -> None:
    assert parse_hms(text) == seconds


def test_build_didl() -> None:
    url = "http://h:1/s/x/range/abc.flac?a=1&b=2"
    didl = build_didl(url, "Bjørk & Co", "Art", "Alb", 194, "abc")
    ET.fromstring(didl)
    assert didl.startswith(DIDL_OPEN)
    assert "<dc:title>Bj&#248;rk &amp; Co</dc:title>" in didl
    assert (
        '<res duration="00:03:14" protocolInfo="http-get:*:audio/flac:DLNA.ORG_OP=01;'
        "DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000"
        '">http://h:1/s/x/range/abc.flac?a=1&amp;b=2</res>'
    ) in didl
    assert "albumArtURI" not in didl

    with_art = build_didl(url, "T", "Art", "Alb", 194, "abc", art_url="http://h/art.jpg")
    ET.fromstring(with_art)
    assert (
        "<upnp:albumArtURI>http://h/art.jpg</upnp:albumArtURI><upnp:class>" in with_art
    )


def test_parse_values() -> None:
    values = parse_values(
        MEDIA_INFO,
        ("CurrentURI", "NextURI", "TrackSource", "Missing", "CurrentURIMetaData"),
    )
    assert values["CurrentURI"] == (
        "http://172.16.20.30:4533/rest/stream.view?id=5gFSmk6J99a0AZx7u5uYMP"
        "&v=1.13.0&c=XXX&u=XXX&s=XXX&t=XXX"
    )
    assert values["NextURI"] == (
        "http://172.16.20.30:4533/rest/stream.view?id=67dH1IcXCSk65j1V5Ajn7X"
        "&v=1.13.0&c=XXX&u=XXX&s=XXX&t=XXX"
    )
    assert values["TrackSource"] == ""
    assert values["Missing"] is None
    assert values["CurrentURIMetaData"] is not None
    assert values["CurrentURIMetaData"].startswith("<DIDL-Lite>")


def test_parse_fault() -> None:
    fault = parse_fault(FAULT)
    assert fault.code == 701
    assert fault.description == "Transition not available"
    garbage = parse_fault("garbage")
    assert garbage.code is None
    assert garbage.description == ""


def test_redact_uri() -> None:
    uri = "http://h/rest/stream.view?id=abc&v=1.13.0&c=Feishin&u=dan&s=salt&t=tok"
    assert redact_uri(uri) == (
        "http://h/rest/stream.view?id=abc&v=1.13.0&c=***&u=***&s=***&t=***"
    )
    assert redact_uri(None) is None
    assert redact_uri("http://h/a.flac") == "http://h/a.flac"
    assert redact_uri("http://h/a?U=dan&Token=x&id=a%20b") == "http://h/a?U=***&Token=***&id=a%20b"


def _state(
    t: float = 100.0,
    transport: str | None = "PLAYING",
    api_status: str | None = "play",
    curpos_ms: int | None = 1000,
    queue_index: int | None = None,
) -> DeviceState:
    return DeviceState(
        t=t,
        transport=transport,
        current_uri="http://h/a",
        next_uri="http://h/b",
        api_status=api_status,
        curpos_ms=curpos_ms,
        queue_index=queue_index,
    )


def test_describe_change() -> None:
    assert describe_change(None, _state()) == "initial"

    assert describe_change(_state(100.0, curpos_ms=1000), _state(100.3, curpos_ms=1300)) is None

    stopped = describe_change(_state(), _state(transport="STOPPED"))
    assert stopped is not None
    assert "transport PLAYING->STOPPED" in stopped

    jump = describe_change(_state(100.0, curpos_ms=92298), _state(100.3, curpos_ms=123))
    assert jump is not None
    assert "position jump 92298->123" in jump

    no_jump = describe_change(
        _state(100.0, curpos_ms=92298),
        _state(100.3, api_status="stop", curpos_ms=123),
    )
    assert no_jump is not None
    assert "position jump" not in no_jump


def _fake_device_app(
    calls: list[tuple[str, str]],
    content_types: list[str],
    queue_calls: list[tuple[str, str]] | None = None,
    queue_index_fails: bool = False,
) -> web.Application:
    recorded_queue = queue_calls if queue_calls is not None else []

    async def control(request: web.Request) -> web.Response:
        header = request.headers["SOAPACTION"]
        calls.append((header, await request.text()))
        content_types.append(request.headers["Content-Type"])
        action = header.strip('"').split("#", 1)[1]
        if action == "GetTransportInfo":
            return web.Response(text=TRANSPORT_INFO, content_type="text/xml")
        if action == "GetMediaInfo":
            return web.Response(text=MEDIA_INFO, content_type="text/xml")
        if action == "Seek":
            return web.Response(status=500, text=FAULT, content_type="text/xml")
        return web.Response(text="<s:Envelope/>", content_type="text/xml")

    async def queue_control(request: web.Request) -> web.Response:
        header = request.headers["SOAPACTION"]
        recorded_queue.append((header, await request.text()))
        action = header.strip('"').split("#", 1)[1]
        if action == "GetQueueIndex":
            if queue_index_fails:
                return web.Response(status=500, text=FAULT_501, content_type="text/xml")
            return web.Response(text=QUEUE_INDEX, content_type="text/xml")
        if action == "GetQueueLoopMode":
            return web.Response(text=LOOP_MODE_BODY, content_type="text/xml")
        if action == "BrowseQueue":
            return web.Response(text=BROWSE_BODY, content_type="text/xml")
        return web.Response(text="<s:Envelope/>", content_type="text/xml")

    async def status(request: web.Request) -> web.Response:
        return web.Response(text=PLAYER_STATUS, content_type="text/plain")

    app = web.Application()
    app.router.add_post(CONTROL_PATH, control)
    app.router.add_post(PLAYQUEUE_CONTROL_PATH, queue_control)
    app.router.add_get("/httpapi.asp", status)
    return app


def _run_against_fake_device(
    scenario: Callable[[Wiim], Awaitable[T]],
    queue_calls: list[tuple[str, str]] | None = None,
    queue_index_fails: bool = False,
) -> tuple[T, list[tuple[str, str]], list[str]]:
    calls: list[tuple[str, str]] = []
    content_types: list[str] = []

    async def go() -> T:
        async with TestServer(_fake_device_app(calls, content_types, queue_calls, queue_index_fails)) as server:
            base = f"http://{server.host}:{server.port}"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                wiim = Wiim(
                    session,
                    session,
                    device_base=base,
                    status_url=f"{base}/httpapi.asp?command=getPlayerStatus",
                )
                return await scenario(wiim)

    return asyncio.run(go()), calls, content_types


def test_wiim_set_uri_then_play() -> None:
    url = "http://h:1/s/x/range/abc.flac?a=1&b=2"
    didl = build_didl(url, "Title", "Art", "Alb", 194, "abc")

    async def scenario(wiim: Wiim) -> None:
        await wiim.set_uri(url, didl)
        await wiim.play()

    _, calls, content_types = _run_against_fake_device(scenario)

    assert [header for header, _ in calls] == [
        f'"{AVT}#SetAVTransportURI"',
        f'"{AVT}#Play"',
    ]
    first_body = calls[0][1]
    assert "<CurrentURI>http://h:1/s/x/range/abc.flac?a=1&amp;b=2</CurrentURI>" in first_body
    assert "&lt;DIDL-Lite" in first_body
    assert set(content_types) == {'text/xml; charset="utf-8"'}


def test_wiim_state() -> None:
    async def scenario(wiim: Wiim) -> DeviceState:
        return await wiim.state()

    state, _, _ = _run_against_fake_device(scenario)

    assert state.transport == "STOPPED"
    assert state.current_uri is not None
    assert "id=5gFSmk6J99a0AZx7u5uYMP&v=1.13.0" in state.current_uri
    assert state.api_status == "play"
    assert state.curpos_ms == 29564


def test_wiim_seek_raises_upnp_error() -> None:
    async def scenario(wiim: Wiim) -> UpnpError:
        with pytest.raises(UpnpError) as excinfo:
            await wiim.seek(10)
        return excinfo.value

    error, calls, _ = _run_against_fake_device(scenario)

    assert error.code == 701
    assert calls[0][0] == f'"{AVT}#Seek"'
    assert "<Target>00:00:10</Target>" in calls[0][1]


class _ClosingDevice:
    """Raw HTTP server that announces keep-alive but closes after every response."""

    def __init__(self) -> None:
        self.accepted = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            await reader.readexactly(length)
            body = TRANSPORT_INFO.encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\nCONNECTION: keep-alive\r\n"
                b'CONTENT-TYPE: text/xml; charset="utf-8"\r\n'
                b"CONTENT-LENGTH: %d\r\n\r\n%s" % (len(body), body)
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def test_wiim_call_uses_fresh_connection_per_request() -> None:
    device = _ClosingDevice()

    async def scenario() -> list[str]:
        server = await asyncio.start_server(device.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5),
                    connector=aiohttp.TCPConnector(force_close=True),
                ) as soap_session,
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as status_session,
            ):
                wiim = Wiim(
                    soap_session,
                    status_session,
                    f"http://127.0.0.1:{port}",
                    status_url="http://127.0.0.1:9/unused",
                )
                return [await wiim.call("GetTransportInfo", {"InstanceID": "0"}) for _ in range(3)]
        finally:
            server.close()
            await server.wait_closed()

    bodies = asyncio.run(scenario())

    assert all("STOPPED" in body for body in bodies)
    assert device.accepted == 3


PQ_CONTEXT_HEAD = (
    '<?xml version="1.0"?><PlayList><ListName>q &amp; a</ListName><ListInfo>'
    "<SourceName>UPnPServer</SourceName><TrackNumber>2</TrackNumber><SearchUrl></SearchUrl>"
    "<Quality>0</Quality><UpdateTime>0</UpdateTime><LastPlayIndex>1</LastPlayIndex>"
    "<SwitchPageMode>0</SwitchPageMode><CurrentPage>0</CurrentPage><TotalPages>0</TotalPages>"
    "</ListInfo><Tracks>"
)

TWO_TRACKS = [
    QueueTrack("http://h:1/s/x/range/t1.flac", "Bjørk & Co", "Art", "Alb", 194, "t1"),
    QueueTrack("http://h:1/s/x/range/t2.flac?a=1&b=2", "Two", "", "", 0, "t2"),
]


def test_playqueue_service_in_envelope_and_headers() -> None:
    body = build_envelope("GetQueueLoopMode", {}, PLAYQUEUE_SERVICE)
    assert body.endswith(
        '<s:Body><u:GetQueueLoopMode xmlns:u="urn:schemas-wiimu-com:service:PlayQueue:1">'
        "</u:GetQueueLoopMode></s:Body></s:Envelope>"
    )
    assert soap_headers("CreateQueue", PLAYQUEUE_SERVICE)["SOAPACTION"] == (
        '"urn:schemas-wiimu-com:service:PlayQueue:1#CreateQueue"'
    )
    assert build_envelope("Play", {}) == build_envelope("Play", {}, AVT)
    assert soap_headers("Play") == soap_headers("Play", AVT)


def test_build_queue_context() -> None:
    first = build_didl("http://h:1/s/x/range/t1.flac", "Bjørk & Co", "Art", "Alb", 194, "t1")
    second = build_didl("http://h:1/s/x/range/t2.flac?a=1&b=2", "Two", "", "", 0, "t2")
    expected = (
        PQ_CONTEXT_HEAD
        + f"<Track1><URL>http://h:1/s/x/range/t1.flac</URL><Metadata>{escape_xml(first)}"
        "</Metadata><Id>1</Id></Track1>"
        + "<Track2><URL>http://h:1/s/x/range/t2.flac?a=1&amp;b=2</URL>"
        f"<Metadata>{escape_xml(second)}</Metadata><Id>2</Id></Track2>"
        "</Tracks></PlayList>"
    )
    context = build_queue_context("q & a", TWO_TRACKS)
    assert context == expected
    root = ET.fromstring(context)
    assert root.findtext("ListName") == "q & a"
    assert root.findtext("ListInfo/TrackNumber") == "2"
    assert "<LastPlayIndex>2</LastPlayIndex>" in build_queue_context("q", TWO_TRACKS, 2)
    with pytest.raises(ValueError):
        build_queue_context("q", [])


def test_parse_queue_index() -> None:
    assert parse_queue_index(QUEUE_INDEX) == {
        "current_index": 2,
        "preloading_index": 2,
        "current_page": 1,
        "track_nums": 3,
    }
    assert parse_queue_index(FAULT_501) == {
        "current_index": None,
        "preloading_index": None,
        "current_page": None,
        "track_nums": None,
    }


def test_parse_queue_context_and_tracks() -> None:
    context = parse_queue_context(BROWSE_BODY)
    assert context is not None
    assert context.startswith('<?xml version="1.0"?>')
    assert parse_queue_context("<x/>") is None
    assert parse_queue_tracks(context) == [
        {
            "index": "1",
            "id": "1",
            "url": "http://172.16.20.30:8098/s/SECRET/range/0zyJc2ogSZBFHR1lz0d7yq.flac",
            "title": "You Can't Hide, You Can't Run",
        },
        {
            "index": "2",
            "id": "2",
            "url": "http://172.16.20.30:8098/s/SECRET/range/5gFSmk6J99a0AZx7u5uYMP.flac",
            "title": "PoC track 2",
        },
    ]


def test_tracks_from_album() -> None:
    album = {
        "tracks": [
            {"id": "a1", "title": "One", "artist": "Art", "album": "Alb", "duration": 196},
            {"id": "b2", "title": "Two", "album": None, "duration": None},
        ]
    }
    tracks = tracks_from_album(album, "B", "range")
    assert [t.url for t in tracks] == ["B/range/a1.flac", "B/range/b2.flac"]
    assert tracks[0] == QueueTrack("B/range/a1.flac", "One", "Art", "Alb", 196, "a1")
    assert tracks[1].duration_s == 0
    assert tracks[1].artist == ""
    assert tracks[1].album == ""
    assert tracks[1].item_id == "b2"
    for bad in ({}, {"tracks": []}, {"tracks": [{"title": "x"}]}, {"tracks": [{"id": "x"}]}):
        with pytest.raises(ValueError):
            tracks_from_album(bad, "B", "range")


def test_describe_change_queue_index() -> None:
    change = describe_change(_state(queue_index=1), _state(queue_index=2))
    assert change == "queue_index 1->2"
    assert describe_change(_state(), _state()) is None


def test_wiim_queue_create_and_play() -> None:
    queue_calls: list[tuple[str, str]] = []
    context = build_queue_context("q", TWO_TRACKS)

    async def scenario(wiim: Wiim) -> None:
        await wiim.queue_create(context)
        await wiim.queue_play("q", 2)

    _, calls, _ = _run_against_fake_device(scenario, queue_calls=queue_calls)

    assert calls == []
    assert queue_calls[0][0] == '"urn:schemas-wiimu-com:service:PlayQueue:1#CreateQueue"'
    assert "&lt;PlayList&gt;" in queue_calls[0][1]
    assert "<QueueName>q</QueueName><Index>2</Index>" in queue_calls[1][1]


def test_wiim_queue_index_loop_mode_and_browse() -> None:
    async def scenario(wiim: Wiim) -> tuple[dict[str, int | None], int | None, str | None]:
        return await wiim.queue_index(), await wiim.queue_loop_mode(), await wiim.queue_browse("q")

    (index, loop_mode, context), _, _ = _run_against_fake_device(scenario)

    assert index["current_index"] == 2
    assert loop_mode == 3
    assert context is not None
    assert len(parse_queue_tracks(context)) == 2


def test_wiim_state_includes_queue_index() -> None:
    async def scenario(wiim: Wiim) -> DeviceState:
        return await wiim.state()

    state, _, _ = _run_against_fake_device(scenario)
    assert state.queue_index == 2


def test_wiim_state_survives_queue_index_fault() -> None:
    async def scenario(wiim: Wiim) -> DeviceState:
        return await wiim.state()

    state, _, _ = _run_against_fake_device(scenario, queue_index_fails=True)
    assert state.queue_index is None
    assert state.transport == "STOPPED"
    assert state.api_status == "play"


def _album_file(tmp_path: Path) -> Path:
    path = tmp_path / "album.json"
    path.write_text(
        json.dumps(
            {
                "tracks": [
                    {"id": "t1", "title": "One", "artist": "A", "album": "B", "duration": 10},
                    {"id": "t2", "title": "Two", "duration": None},
                ]
            }
        )
    )
    return path


def test_queue_create_command_sends_both_urls(tmp_path: Path) -> None:
    queue_calls: list[tuple[str, str]] = []
    args = build_parser().parse_args(
        [
            "queue-create", "--name", "q", "--album-file", str(_album_file(tmp_path)),
            "--base-url", "http://h:1/s/x",
        ]
    )  # fmt: skip

    async def scenario(wiim: Wiim) -> dict[str, object]:
        return await cmd_queue_create(wiim, args)

    result, _, _ = _run_against_fake_device(scenario, queue_calls=queue_calls)

    assert len(queue_calls) == 1
    header, body = queue_calls[0]
    assert header.endswith("#CreateQueue\"")
    assert "http://h:1/s/x/range/t1.flac" in body
    assert "http://h:1/s/x/range/t2.flac" in body
    assert result["action"] == "queue-create"
    assert result["tracks"] == 2


def test_queue_create_input_errors(tmp_path: Path) -> None:
    parser = build_parser()
    base = ["queue-create", "--name", "q"]
    missing = parser.parse_args(
        [*base, "--album-file", str(tmp_path / "none.json"), "--base-url", "http://h"]
    )
    with pytest.raises(InputError):
        queue_context_from_args(missing)

    no_base = parser.parse_args(
        [*base, "--album-file", str(_album_file(tmp_path)), "--proxy-info", str(tmp_path / "x")]
    )
    with pytest.raises(InputError):
        queue_context_from_args(no_base)

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    malformed = parser.parse_args([*base, "--album-file", str(bad), "--base-url", "http://h"])
    with pytest.raises(InputError):
        queue_context_from_args(malformed)


def _queue_call_body(call: Callable[[Wiim], Awaitable[object]]) -> tuple[str, str]:
    queue_calls: list[tuple[str, str]] = []
    _run_against_fake_device(call, queue_calls=queue_calls)
    assert len(queue_calls) == 1
    return queue_calls[0]


def test_wiim_queue_remove() -> None:
    header, body = _queue_call_body(lambda w: w.queue_remove("q", 3, 3))
    assert header == '"urn:schemas-wiimu-com:service:PlayQueue:1#RemoveTracksInQueue"'
    assert (
        "<QueueName>q</QueueName><RangStart>3</RangStart><RangEnd>3</RangEnd><Action></Action>"
        in body
    )


def test_wiim_queue_move() -> None:
    header, body = _queue_call_body(lambda w: w.queue_move("q", 4, 1))
    assert header == '"urn:schemas-wiimu-com:service:PlayQueue:1#MoveTracksInQueue"'
    assert "<QueueName>q</QueueName><IndexList>4</IndexList><ToIndex>1</ToIndex>" in body


def test_wiim_queue_insert() -> None:
    context = build_queue_context("q", TWO_TRACKS)
    header, body = _queue_call_body(lambda w: w.queue_insert(context, 2))
    assert header == '"urn:schemas-wiimu-com:service:PlayQueue:1#AppendTracksInQueueEx"'
    assert "&lt;PlayList&gt;" in body
    assert (
        "<Direction>1</Direction><StartIndex>2</StartIndex><Play>0</Play><Action></Action>"
        in body
    )
    _, playing = _queue_call_body(lambda w: w.queue_insert(context, 2, play=True))
    assert "<Play>1</Play>" in playing
