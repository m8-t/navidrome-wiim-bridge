"""SOAP envelope building and parsing helpers for UPnP calls."""

import html
import re
from collections.abc import Mapping, Sequence

SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"


def escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_envelope(action: str, args: Mapping[str, str], service: str = SERVICE) -> str:
    params = "".join(f"<{name}>{escape_xml(value)}</{name}>" for name, value in args.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{params}</u:{action}></s:Body></s:Envelope>'
    )


def soap_headers(action: str, service: str = SERVICE) -> dict[str, str]:
    return {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{service}#{action}"',
    }


def escape_metadata(value: str) -> str:
    return "".join(
        char if ord(char) < 128 else f"&#{ord(char)};" for char in escape_xml(value)
    )


def format_hms(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_hms(text: str | None) -> int | None:
    if text is None:
        return None
    match = re.fullmatch(r"([0-9]+):([0-9]{2}):([0-9]{2})", text)
    if match is None:
        return None
    hours, minutes, secs = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + secs


def build_didl(
    url: str,
    title: str,
    artist: str,
    album: str,
    duration_s: int,
    item_id: str,
    art_url: str | None = None,
    mime: str = "audio/flac",
) -> str:
    t = escape_metadata(title)
    a = escape_metadata(artist)
    b = escape_metadata(album)
    i = escape_metadata(item_id)
    u = escape_metadata(url)
    d = format_hms(duration_s)
    art = (
        f"<upnp:albumArtURI>{escape_metadata(art_url)}</upnp:albumArtURI>"
        if art_url is not None
        else ""
    )
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        f'<item id="{i}" restricted="true" parentID="">'
        f"<dc:title>{t}</dc:title>"
        f"<dc:creator>{a}</dc:creator>"
        f"<upnp:album>{b}</upnp:album>"
        f"<upnp:artist>{a}</upnp:artist>"
        f"<dc:queueItemId>{i}</dc:queueItemId>"
        "<dc:description>wiim-bridge-poc</dc:description>"
        f"{art}"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res duration="{d}" protocolInfo="http-get:*:{mime}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;'
        f'DLNA.ORG_FLAGS=01500000000000000000000000000000">{u}</res>'
        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        "RINCON_AssociatedZPUDN</desc>"
        "</item></DIDL-Lite>"
    )


def parse_values(xml_text: str, names: Sequence[str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for name in names:
        pattern = re.escape(f"<{name}>") + "(.*?)" + re.escape(f"</{name}>")
        match = re.search(pattern, xml_text, re.DOTALL)
        values[name] = html.unescape(match.group(1)) if match else None
    return values


class UpnpError(Exception):
    def __init__(self, code: int | None, description: str) -> None:
        self.code = code
        self.description = description
        super().__init__(description if code is None else f"{code} {description}")


def parse_fault(xml_text: str) -> UpnpError:
    found = parse_values(xml_text, ("errorCode", "errorDescription"))
    raw_code = (found["errorCode"] or "").strip()
    code = int(raw_code) if re.fullmatch(r"[0-9]+", raw_code) else None
    return UpnpError(code, found["errorDescription"] or "")
