import asyncio
import hashlib
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import nd_query as nd
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict

PASSWORD = "hunter2-pw"
SALT = "feedfacecafe0123"


def drive(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guarded())


def ok(**fields: Any) -> dict[str, Any]:
    return {"subsonic-response": {"status": "ok", "version": "1.16.1", **fields}}


def failed(message: str) -> dict[str, Any]:
    error = {"code": 70, "message": message}
    return {"subsonic-response": {"status": "failed", "error": error}}


SONGS = [
    {"id": "t3", "title": "Café", "track": 1, "discNumber": 2, "duration": 61},
    {
        "id": "t2",
        "title": "Two",
        "artist": "Slum Village",
        "album": "Fantastic, Vol. 2",
        "track": 2,
        "discNumber": 1,
        "duration": 257,
        "suffix": "flac",
        "bitRate": 890,
        "samplingRate": 44100,
        "bitDepth": 16,
        "size": 28769102,
    },
    {
        "id": "t1",
        "title": "One",
        "track": 1,
        "discNumber": 1,
        "samplingRate": 96000,
        "bitDepth": 24,
    },
]
ALBUM_REPLY = ok(
    album={"id": "al-1", "name": "Fantastic, Vol. 2", "artist": "Slum Village", "song": SONGS}
)
SEARCH_REPLY = ok(
    searchResult3={
        "album": [
            {"id": "al-1", "name": "Fantastic, Vol. 2", "artist": "Slum Village", "year": 2000},
            {"id": "al-2", "name": "Dilated", "artist": "Dilated Peoples", "songCount": 12},
        ]
    }
)


@dataclass
class Seen:
    path: str
    query: dict[str, str]
    headers: CIMultiDict[str]


class FakeNavidrome:
    def __init__(self) -> None:
        self.requests: list[Seen] = []
        self.app = web.Application()
        self.app.router.add_get("/rest/search3", self.search3)
        self.app.router.add_get("/rest/getAlbum", self.get_album)

    def record(self, request: web.Request) -> None:
        seen = Seen(request.path, dict(request.query), CIMultiDict(request.headers))
        self.requests.append(seen)

    async def search3(self, request: web.Request) -> web.Response:
        self.record(request)
        if request.query["query"] == "nothing":
            return web.json_response(ok(searchResult3={}))
        return web.json_response(SEARCH_REPLY)

    async def get_album(self, request: web.Request) -> web.Response:
        self.record(request)
        match request.query["id"]:
            case "gone":
                return web.json_response(failed("Data not found"))
            case "html":
                return web.Response(text="<html>login</html>", content_type="text/html")
            case _:
                return web.json_response(ALBUM_REPLY)


def credentials_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"user": "dan", "password": PASSWORD}), encoding="utf-8")
    path.chmod(mode)
    return path


def auth_params() -> dict[str, str]:
    return nd.subsonic_params("dan", PASSWORD, SALT)


def tracks_for_format() -> dict[str, Any]:
    return nd.album_document(ALBUM_REPLY["subsonic-response"])


def test_unwrap() -> None:
    inner = {"status": "ok", "version": "1.16.1"}
    assert nd.unwrap({"subsonic-response": inner}) is inner
    with pytest.raises(nd.QueryError, match="^Data not found$"):
        nd.unwrap(failed("Data not found"))
    for payload in (
        {},
        "x",
        {"subsonic-response": "x"},
        {"subsonic-response": {"status": "failed"}},
    ):
        with pytest.raises(nd.QueryError, match="^unexpected reply from Navidrome$"):
            nd.unwrap(payload)


def test_albums_from_search() -> None:
    albums = nd.albums_from_search(SEARCH_REPLY["subsonic-response"])
    assert albums == [
        {"id": "al-1", "name": "Fantastic, Vol. 2", "artist": "Slum Village", "year": 2000, "song_count": None},
        {"id": "al-2", "name": "Dilated", "artist": "Dilated Peoples", "year": None, "song_count": 12},
    ]  # fmt: skip
    assert nd.albums_from_search({"status": "ok"}) == []
    assert nd.albums_from_search({"searchResult3": {}}) == []


def test_album_document_sorts_tracks_and_fills_gaps() -> None:
    document = tracks_for_format()
    assert document["album_id"] == "al-1"
    assert document["name"] == "Fantastic, Vol. 2"
    assert document["artist"] == "Slum Village"
    assert [t["id"] for t in document["tracks"]] == ["t1", "t2", "t3"]
    assert document["tracks"][1] == {
        "id": "t2",
        "disc": 1,
        "track": 2,
        "title": "Two",
        "artist": "Slum Village",
        "album": "Fantastic, Vol. 2",
        "duration": 257,
        "suffix": "flac",
        "sampling_rate": 44100,
        "bit_depth": 16,
        "bit_rate": 890,
        "size": 28769102,
    }
    sparse = document["tracks"][2]
    assert sparse["id"] == "t3"
    assert sparse["artist"] is None
    assert sparse["suffix"] is None
    assert sparse["size"] is None


def test_album_document_treats_missing_numbers_as_disc_one_track_zero() -> None:
    songs = [{"id": "b", "title": "b", "track": 1}, {"id": "a", "title": "a"}]
    document = nd.album_document({"album": {"id": "x", "song": songs}})
    assert [t["id"] for t in document["tracks"]] == ["a", "b"]


def test_album_document_errors() -> None:
    with pytest.raises(nd.QueryError, match="^album not found in reply$"):
        nd.album_document({})
    for album in ({"id": "x"}, {"id": "x", "song": []}):
        with pytest.raises(nd.QueryError, match="^album has no tracks$"):
            nd.album_document({"album": album})


def test_format_tracks() -> None:
    text = nd.format_tracks(tracks_for_format())
    rows = text.splitlines()
    assert len(rows) == 3
    assert "4:17" in rows[1]
    assert "44100/16" in rows[1]
    assert "1.02" in rows[1]
    assert "96000/24" in rows[0]
    assert "?" in rows[2]
    assert "0:00" not in text


def test_format_albums() -> None:
    text = nd.format_albums(nd.albums_from_search(SEARCH_REPLY["subsonic-response"]))
    first, second = text.splitlines()
    assert "al-1" in first and "2000" in first and "?" in first
    assert "12 tracks" in second and "Dilated Peoples" in second
    assert nd.format_albums([]) == ""


def test_fetch_sends_auth_parameters_and_returns_unwrapped_reply() -> None:
    fake = FakeNavidrome()

    async def scenario() -> dict[str, Any]:
        async with TestServer(fake.app) as server, aiohttp.ClientSession() as session:
            url = str(server.make_url("")).rstrip("/")
            return await nd.fetch(
                session,
                url,
                "search3",
                auth_params(),
                query="dilated",
                albumCount="5",
                songCount="0",
                artistCount="0",
            )

    reply = drive(scenario())
    assert reply["searchResult3"]["album"][0]["id"] == "al-1"
    (seen,) = fake.requests
    assert seen.path == "/rest/search3"
    assert seen.query == {
        "u": "dan",
        "t": hashlib.md5((PASSWORD + SALT).encode()).hexdigest(),
        "s": SALT,
        "v": "1.16.1",
        "c": "wiim-bridge-poc",
        "f": "json",
        "query": "dilated",
        "albumCount": "5",
        "songCount": "0",
        "artistCount": "0",
    }
    assert seen.headers["Accept-Encoding"] == "identity"


def test_fetch_raises_query_error_for_failed_and_html_replies() -> None:
    async def scenario() -> dict[str, Any]:
        async with TestServer(FakeNavidrome().app) as server, aiohttp.ClientSession() as session:
            url = str(server.make_url("")).rstrip("/")

            async def call(album_id: str) -> dict[str, Any]:
                return await nd.fetch(session, url, "getAlbum", auth_params(), id=album_id)

            with pytest.raises(nd.QueryError, match="^Data not found$"):
                await call("gone")
            with pytest.raises(nd.QueryError, match="^unexpected reply from Navidrome$"):
                await call("html")
            return await call("al-1")

    assert drive(scenario())["album"]["id"] == "al-1"


async def run_main(fake: FakeNavidrome, argv: list[str]) -> int:
    async with TestServer(fake.app) as server:
        url = str(server.make_url("")).rstrip("/")
        return await asyncio.to_thread(nd.main, ["--navidrome-url", url + "/", *argv])


def test_main_album_writes_sorted_json_and_prints_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_file = tmp_path / "nested" / "album.json"
    argv = [
        "--credentials",
        str(credentials_file(tmp_path)),
        "album",
        "al-1",
        "--out",
        str(out_file),
    ]
    assert drive(run_main(FakeNavidrome(), argv)) == 0

    raw = out_file.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert document["album_id"] == "al-1"
    assert [t["id"] for t in document["tracks"]] == ["t1", "t2", "t3"]
    assert "Café" in raw
    assert raw.startswith('{\n  "album_id"')
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == f"written: {out_file}"
    assert "4:17" in captured.out
    assert captured.err == ""


def test_main_album_defaults_to_results_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nd, "PROJECT_DIR", tmp_path)
    argv = ["--credentials", str(credentials_file(tmp_path)), "album", "al-1"]
    assert drive(run_main(FakeNavidrome(), argv)) == 0
    expected = tmp_path / "results" / "nd_album_al-1.json"
    assert json.loads(expected.read_text(encoding="utf-8"))["album_id"] == "al-1"
    assert capsys.readouterr().out.splitlines()[-1] == f"written: {expected}"


def test_main_search_prints_albums_and_forwards_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeNavidrome()
    argv = ["--credentials", str(credentials_file(tmp_path)), "search", "dilated", "--count", "3"]
    assert drive(run_main(fake, argv)) == 0
    assert "Dilated Peoples" in capsys.readouterr().out
    (seen,) = fake.requests
    assert seen.query["query"] == "dilated"
    assert seen.query["albumCount"] == "3"
    assert seen.query["songCount"] == seen.query["artistCount"] == "0"


def test_main_search_without_matches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--credentials", str(credentials_file(tmp_path)), "search", "nothing"]
    assert drive(run_main(FakeNavidrome(), argv)) == 0
    assert capsys.readouterr().out.strip() == "no albums found"


def test_main_reports_navidrome_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--credentials", str(credentials_file(tmp_path)), "album", "gone"]
    assert drive(run_main(FakeNavidrome(), argv)) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: Data not found\n"
    assert captured.out == ""


def test_main_rejects_loose_credentials_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--navidrome-url",
        "http://navidrome.invalid:4533",
        "--credentials",
        str(credentials_file(tmp_path, mode=0o644)),
        "search",
        "x",
    ]
    assert nd.main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "chmod 600" in err
    assert PASSWORD not in err


def test_main_closed_port_hides_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def closed_url() -> str:
        async with TestServer(FakeNavidrome().app) as server:
            return str(server.make_url("")).rstrip("/")

    dead_url = drive(closed_url())
    monkeypatch.setattr(nd.secrets, "token_hex", lambda nbytes: SALT)
    argv = [
        "--navidrome-url",
        dead_url,
        "--credentials",
        str(credentials_file(tmp_path)),
        "search",
        "x",
    ]
    assert nd.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith(f"error: cannot reach Navidrome at {dead_url}: ")
    token = hashlib.md5((PASSWORD + SALT).encode()).hexdigest()
    for hidden in (PASSWORD, SALT, token):
        assert hidden not in captured.out + captured.err


def test_main_connect_timeout_hides_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timing_out(self: Any, req: Any, traces: Any, timeout: Any) -> Any:
        raise asyncio.TimeoutError

    monkeypatch.setattr(aiohttp.BaseConnector, "connect", timing_out)
    monkeypatch.setattr(nd.secrets, "token_hex", lambda nbytes: SALT)
    argv = [
        "--navidrome-url",
        "http://127.0.0.1:1",
        "--credentials",
        str(credentials_file(tmp_path)),
        "search",
        "x",
    ]
    assert nd.main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: cannot reach Navidrome at http://127.0.0.1:1: ")
    token = hashlib.md5((PASSWORD + SALT).encode()).hexdigest()
    for hidden in (PASSWORD, SALT, token):
        assert hidden not in err


def test_main_reports_unwritable_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    out_file = blocker / "album.json"
    argv = [
        "--credentials",
        str(credentials_file(tmp_path)),
        "album",
        "al-1",
        "--out",
        str(out_file),
    ]
    assert drive(run_main(FakeNavidrome(), argv)) == 2
    assert capsys.readouterr().err.startswith(f"error: cannot write {out_file}: ")
