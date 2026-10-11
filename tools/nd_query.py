"""Look up albums in Navidrome and save an album's track list as JSON.

Runs on the desktop host. The JSON lands in the project's results folder, which a
sandbox that cannot reach Navidrome can read.
"""

import argparse
import asyncio
import json
import secrets
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import aiohttp

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bridge.subsonic import (  # noqa: E402
    DEFAULT_CREDENTIALS,
    UPSTREAM_HEADERS,
    ConfigError,
    describe_error,
    load_credentials,
    subsonic_params,
)

UNEXPECTED_REPLY = "unexpected reply from Navidrome"
TRACK_FIELDS = (
    ("id", "id"),
    ("disc", "discNumber"),
    ("track", "track"),
    ("title", "title"),
    ("artist", "artist"),
    ("album", "album"),
    ("duration", "duration"),
    ("suffix", "suffix"),
    ("sampling_rate", "samplingRate"),
    ("bit_depth", "bitDepth"),
    ("bit_rate", "bitRate"),
    ("size", "size"),
)


class QueryError(Exception):
    """Raised when Navidrome answers with an error or an unusable reply."""


def unwrap(payload: Any) -> dict[str, Any]:
    body = payload.get("subsonic-response") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise QueryError(UNEXPECTED_REPLY)
    if body.get("status") == "ok":
        return body
    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    raise QueryError(message if isinstance(message, str) and message else UNEXPECTED_REPLY)


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def albums_from_search(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = response.get("searchResult3")
    albums = _dicts(result.get("album")) if isinstance(result, dict) else []
    return [
        {
            "id": album.get("id"),
            "name": album.get("name"),
            "artist": album.get("artist"),
            "year": album.get("year"),
            "song_count": album.get("songCount"),
        }
        for album in albums
    ]


def _track(song: Mapping[str, Any]) -> dict[str, Any]:
    return {key: song.get(source) for key, source in TRACK_FIELDS}


def album_document(response: Mapping[str, Any]) -> dict[str, Any]:
    album = response.get("album")
    if not isinstance(album, dict):
        raise QueryError("album not found in reply")
    songs = _dicts(album.get("song"))
    if not songs:
        raise QueryError("album has no tracks")
    tracks = sorted(map(_track, songs), key=lambda t: (t["disc"] or 1, t["track"] or 0))
    return {
        "album_id": album.get("id"),
        "name": album.get("name"),
        "artist": album.get("artist"),
        "tracks": tracks,
    }


def _cell(value: Any) -> str:
    return "?" if value is None else str(value)


def _table(rows: Iterable[Sequence[str]]) -> str:
    rows = list(rows)
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))] if rows else []
    lines = ("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) for row in rows)
    return "\n".join(line.rstrip() for line in lines)


def format_albums(albums: Iterable[Mapping[str, Any]]) -> str:
    return _table(
        (
            _cell(a["id"]),
            _cell(a["artist"]),
            _cell(a["name"]),
            _cell(a["year"]),
            "?" if a["song_count"] is None else f"{a['song_count']} tracks",
        )
        for a in albums
    )


def _position(track: Mapping[str, Any]) -> str:
    number = "?" if track["track"] is None else f"{track['track']:02d}"
    return f"{_cell(track['disc'])}.{number}"


def _duration(seconds: Any) -> str:
    return "?" if seconds is None else f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _quality(track: Mapping[str, Any]) -> str:
    if track["sampling_rate"] is None or track["bit_depth"] is None:
        return "?"
    return f"{track['sampling_rate']}/{track['bit_depth']}"


def format_tracks(document: Mapping[str, Any]) -> str:
    return _table(
        (
            _position(t),
            _cell(t["title"]),
            _duration(t["duration"]),
            _quality(t),
            _cell(t["id"]),
        )
        for t in document["tracks"]
    )


async def fetch(
    session: aiohttp.ClientSession,
    navidrome_url: str,
    method: str,
    auth: Mapping[str, str],
    **params: str,
) -> dict[str, Any]:
    async with session.get(
        f"{navidrome_url}/rest/{method}", params={**auth, **params}, headers=UPSTREAM_HEADERS
    ) as resp:
        text = await resp.text(errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        raise QueryError(UNEXPECTED_REPLY) from None
    return unwrap(payload)


def write_document(path: Path, document: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(document, indent=2, ensure_ascii=False)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        raise QueryError(f"cannot write {path}: {exc.strerror}") from None


async def execute(args: argparse.Namespace, navidrome_url: str, auth: Mapping[str, str]) -> str:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if args.command == "search":
            response = await fetch(
                session,
                navidrome_url,
                "search3",
                auth,
                query=args.query,
                albumCount=str(args.count),
                songCount="0",
                artistCount="0",
            )
            albums = albums_from_search(response)
            return format_albums(albums) if albums else "no albums found"
        response = await fetch(session, navidrome_url, "getAlbum", auth, id=args.album_id)
    document = album_document(response)
    path = args.out or PROJECT_DIR / "results" / f"nd_album_{args.album_id}.json"
    write_document(path, document)
    return f"{format_tracks(document)}\nwritten: {path}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query Navidrome through the Subsonic API.")
    parser.add_argument(
        "--navidrome-url",
        default="http://127.0.0.1:4533",
        help="Navidrome base URL (default http://127.0.0.1:4533)",
    )
    parser.add_argument(
        "--credentials",
        default=DEFAULT_CREDENTIALS,
        help=f"JSON file with user and password, mode 600 (default {DEFAULT_CREDENTIALS})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="find albums by text")
    search.add_argument("query")
    search.add_argument("--count", type=int, default=10, help="maximum albums (default 10)")
    album = commands.add_parser("album", help="list an album's tracks and save them as JSON")
    album.add_argument("album_id")
    album.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output file (default <project>/results/nd_album_<album_id>.json)",
    )
    return parser.parse_args(argv)


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        user, password = load_credentials(Path(args.credentials).expanduser())
    except ConfigError as exc:
        return fail(str(exc))
    navidrome_url = args.navidrome_url.rstrip("/")
    salt = secrets.token_hex(8)
    auth = subsonic_params(user, password, salt)
    try:
        output = asyncio.run(execute(args, navidrome_url, auth))
    except QueryError as exc:
        return fail(str(exc))
    except (aiohttp.ClientError, TimeoutError) as exc:
        reason = describe_error(exc, [password, salt, auth["t"]])
        return fail(f"cannot reach Navidrome at {navidrome_url}: {reason}")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
