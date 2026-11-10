import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict

import bridge.proxy as bp
import bridge.subsonic as bs

PAYLOAD = bytes(i % 251 for i in range(200000))
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
LAST_MODIFIED = "Wed, 01 Jan 2025 00:00:00 GMT"
MISSING_BODY = '{"subsonic-response":{"status":"failed","error":{"code":70,"message":"not found"}}}'
SECRET = "s3cr3t"
CUT_AFTER = 50000


def drive(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guarded())


@dataclass
class Seen:
    method: str
    query: dict[str, str]
    headers: CIMultiDict[str]


class FakeUpstream:
    def __init__(self) -> None:
        self.requests: list[Seen] = []
        self.app = web.Application()
        self.app.router.add_get("/rest/stream", self.stream)

    async def stream(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            Seen(request.method, dict(request.query), CIMultiDict(request.headers))
        )
        song_id = request.query.get("id")
        if song_id == "missing":
            return web.Response(text=MISSING_BODY, content_type="application/json")
        if song_id == "gone":
            return web.Response(status=404, text="nope")
        if song_id == "cut":
            return await self.truncated(request)
        headers = {"Content-Type": "audio/flac", "Accept-Ranges": "bytes"}
        headers["Last-Modified"] = LAST_MODIFIED
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", request.headers.get("Range", ""))
        if match is None:
            return web.Response(body=PAYLOAD, headers=headers)
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else len(PAYLOAD) - 1
        if start >= len(PAYLOAD):
            return web.Response(status=416, headers={"Content-Range": f"bytes */{len(PAYLOAD)}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{len(PAYLOAD)}"
        return web.Response(status=206, body=PAYLOAD[start : end + 1], headers=headers)

    async def truncated(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={"Content-Type": "audio/flac", "Content-Length": str(len(PAYLOAD))}
        )
        await resp.prepare(request)
        await resp.write(PAYLOAD[:CUT_AFTER])
        assert request.transport is not None
        request.transport.close()
        return resp


@dataclass
class Rig:
    client: TestClient[Any, Any]
    upstream: FakeUpstream


def make_config(navidrome_url: str) -> bp.ProxyConfig:
    return bp.ProxyConfig(
        navidrome_url=navidrome_url,
        user="dan",
        password="pw",
        salt="abcd",
        secret=SECRET,
    )


@asynccontextmanager
async def rig(describe: bp.Describe | None = None) -> AsyncIterator[Rig]:
    upstream = FakeUpstream()
    async with TestServer(upstream.app) as fake:
        config = make_config(str(fake.make_url("")).rstrip("/"))
        app = web.Application()
        bp.add_proxy_routes(app, config, describe=describe)
        async with TestClient(TestServer(app)) as client:
            yield Rig(client, upstream)


def url(profile: str, name: str, secret: str = SECRET) -> str:
    return f"/s/{secret}/{profile}/{name}"


def test_subsonic_params() -> None:
    params = bs.subsonic_params("dan", "pw", "abcd")
    assert params.pop("t") == hashlib.md5(b"pwabcd").hexdigest()
    assert params == {"u": "dan", "s": "abcd", "v": "1.16.1", "c": "wiim-bridge", "f": "json"}


def test_response_headers() -> None:
    upstream = {
        "content-type": "audio/flac",
        "CONTENT-LENGTH": "200000",
        "Content-range": "bytes 0-9/200000",
        "accept-ranges": "bytes",
        "LAST-MODIFIED": LAST_MODIFIED,
        "Etag": '"abc"',
        "Server": "Go",
    }
    assert bp.response_headers(upstream) == {
        "Content-Type": "audio/flac",
        "Content-Length": "200000",
        "Content-Range": "bytes 0-9/200000",
        "Accept-Ranges": "bytes",
        "Last-Modified": LAST_MODIFIED,
        "ETag": '"abc"',
        "transferMode.dlna.org": "Streaming",
        "contentFeatures.dlna.org": (
            "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000"
        ),
    }


def write_credentials(path: Path, content: str, mode: int) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def test_load_credentials(tmp_path: Path) -> None:
    body = '{"user": "dan", "password": "topsecret"}'
    good = write_credentials(tmp_path / "ok.json", body, 0o600)
    assert bs.load_credentials(good) == ("dan", "topsecret")

    loose = write_credentials(tmp_path / "loose.json", body, 0o644)
    with pytest.raises(bs.ConfigError, match="chmod 600") as loose_err:
        bs.load_credentials(loose)
    assert "topsecret" not in str(loose_err.value)

    no_password = write_credentials(tmp_path / "nopw.json", '{"user": "dan"}', 0o600)
    with pytest.raises(bs.ConfigError):
        bs.load_credentials(no_password)

    with pytest.raises(bs.ConfigError):
        bs.load_credentials(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "content",
    [
        '["dan", "pw"]',
        "not json",
        '{"user": "", "password": "pw"}',
        '{"user": "dan", "password": ""}',
        '{"user": 5, "password": "pw"}',
        '{"user": "dan", "password": null}',
    ],
)
def test_load_credentials_rejects_bad_content(tmp_path: Path, content: str) -> None:
    path = write_credentials(tmp_path / "bad.json", content, 0o600)
    with pytest.raises(bs.ConfigError):
        bs.load_credentials(path)

def test_full_get() -> None:
    async def scenario() -> Rig:
        async with rig() as r:
            resp = await r.client.get(url("range", "abc.flac"))
            assert resp.status == 200
            assert await resp.read() == PAYLOAD
            assert resp.headers["Content-Length"] == "200000"
            assert "contentFeatures.dlna.org" in resp.headers
            return r

    r = drive(scenario())
    (seen,) = r.upstream.requests
    assert seen.method == "GET"
    assert seen.query["id"] == "abc"
    assert seen.query["format"] == "raw"
    assert seen.query["u"] == "dan"
    assert seen.query["s"] == "abcd"
    assert seen.query["t"] == hashlib.md5(b"pwabcd").hexdigest()
    assert "Range" not in seen.headers
    assert seen.headers["Accept-Encoding"] == "identity"


def test_range_get() -> None:
    async def scenario() -> Rig:
        async with rig() as r:
            resp = await r.client.get(
                url("range", "abc.flac"), headers={"Range": "bytes=1000-1999"}
            )
            assert resp.status == 206
            assert await resp.read() == PAYLOAD[1000:2000]
            assert resp.headers["Content-Range"] == "bytes 1000-1999/200000"
            return r

    (seen,) = drive(scenario()).upstream.requests
    assert seen.headers["Range"] == "bytes=1000-1999"


def test_if_range_is_forwarded() -> None:
    async def scenario() -> Rig:
        async with rig() as r:
            headers = {"Range": "bytes=10-", "If-Range": LAST_MODIFIED}
            await (await r.client.get(url("range", "abc.flac"), headers=headers)).read()
            return r

    (seen,) = drive(scenario()).upstream.requests
    assert seen.headers["If-Range"] == LAST_MODIFIED


def test_upstream_error_body_becomes_502() -> None:
    async def scenario() -> None:
        async with rig() as r:
            resp = await r.client.get(url("range", "missing.flac"))
            assert resp.status == 502
            assert await resp.text() == "upstream error"

    drive(scenario())


def test_upstream_unreachable_becomes_502() -> None:
    async def scenario() -> None:
        async with TestServer(FakeUpstream().app) as fake:
            dead_url = str(fake.make_url("")).rstrip("/")
        async with TestClient(TestServer(bp.create_app(make_config(dead_url)))) as client:
            resp = await client.get(url("range", "abc.flac"))
            assert resp.status == 502

    drive(scenario())


def test_out_of_range_request_passes_416_through() -> None:
    async def scenario() -> None:
        async with rig() as r:
            resp = await r.client.get(url("range", "abc.flac"), headers={"Range": "bytes=300000-"})
            assert resp.status == 416
            assert resp.headers["Content-Range"] == "bytes */200000"
            assert await resp.read() == b""

    drive(scenario())


def test_rejections_are_logged_without_the_secret(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> Rig:
        async with rig() as r:
            for path in (
                url("range", "abc.flac", secret="wrong"),
                url("foo", "abc.flac"),
                url("norange", "abc.flac"),
                url("range", "abc.mp3"),
                url("range", "a%20b.flac"),
                url("range", ".flac"),
                url("range", "abc.flac%0A"),
            ):
                resp = await r.client.get(path)
                assert resp.status == 404
                assert await resp.read() == b""
            return r

    r = drive(scenario())
    assert r.upstream.requests == []
    rejects = [
        r.getMessage() for r in caplog.records if r.name == "bridge.proxy" and r.levelname == "WARNING"
    ]
    reasons = [re.fullmatch(r"reject reason=(\w+) remote=\S+", m) for m in rejects]
    assert [m.group(1) for m in reasons if m] == [
        "secret", "profile", "profile", "name", "name", "name", "name",
    ]  # fmt: skip
    assert SECRET not in caplog.text


def test_non_ascii_secret_is_rejected_not_an_error(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        async with rig() as r:
            resp = await r.client.get(url("range", "abc.flac", secret="s%C3%A4"))
            assert resp.status == 404

    drive(scenario())
    assert "reject reason=secret" in caplog.text


def test_http10_client_gets_close_delimited_body() -> None:
    async def scenario() -> bytes:
        async with rig() as r:
            reader, writer = await asyncio.open_connection("127.0.0.1", r.client.server.port)
            writer.write(f"GET {url('range', 'abc.flac')} HTTP/1.0\r\n\r\n".encode())
            data = await reader.read()
            writer.close()
            return data

    head, _, body = drive(scenario()).partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200")
    assert b"transfer-encoding" not in head.lower()
    assert body == PAYLOAD


def test_head() -> None:
    async def scenario() -> Rig:
        async with rig() as r:
            resp = await r.client.head(url("range", "abc.flac"))
            assert resp.status == 200
            assert resp.headers["Content-Length"] == "200000"
            assert await resp.read() == b""
            return r

    (seen,) = drive(scenario()).upstream.requests
    assert seen.method == "HEAD"
    assert seen.headers["Accept-Encoding"] == "identity"


def test_connection_lost_during_write_is_handled_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original = web.StreamResponse.write
    writes = 0

    async def failing_write(self: web.StreamResponse, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise ConnectionError("Connection lost")
        await original(self, data)

    monkeypatch.setattr(web.StreamResponse, "write", failing_write)

    async def scenario() -> None:
        async with rig() as r:
            resp = await r.client.get(url("range", "abc.flac"))
            with pytest.raises(aiohttp.ClientError):
                await resp.read()
            await asyncio.sleep(0.2)

    drive(scenario())
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def ping_against(handler: Any) -> tuple[bool, str | None]:
    app = web.Application()
    app.router.add_get("/rest/ping", handler)
    async with TestServer(app) as server:
        return await bs.ping_navidrome(str(server.make_url("")).rstrip("/"), {"u": "dan"})


def test_ping_navidrome_accepts_ok_status() -> None:
    encodings: list[str | None] = []

    async def ok(request: web.Request) -> web.Response:
        encodings.append(request.headers.get("Accept-Encoding"))
        return web.json_response({"subsonic-response": {"status": "ok", "version": "1.16.1"}})

    assert drive(ping_against(ok)) == (True, None)
    assert encodings == ["identity"]


def test_ping_navidrome_reports_error_message() -> None:
    async def failed(request: web.Request) -> web.Response:
        error = {"code": 40, "message": "Wrong username or password"}
        return web.json_response({"subsonic-response": {"status": "failed", "error": error}})

    assert drive(ping_against(failed)) == (False, "Wrong username or password")


def test_ping_navidrome_survives_non_json_reply() -> None:
    async def html(request: web.Request) -> web.Response:
        return web.Response(text="<html>login</html>", content_type="text/html")

    assert drive(ping_against(html)) == (False, None)




def test_config_repr_hides_secrets() -> None:
    text = repr(make_config("http://x"))
    assert "pw" not in text.replace("pwd", "")
    assert "abcd" not in text
    assert SECRET not in text


def describe_abc(song_id: str) -> tuple[str, str] | None:
    return ("Art - Title", "Alb") if song_id == "abc" else None


def proxy_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "bridge.proxy"]


def fetch_and_log(
    caplog: pytest.LogCaptureFixture,
    name: str,
    *,
    describe: bp.Describe | None = describe_abc,
    headers: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with rig(describe) as r:
            resp = await r.client.get(url("range", name), headers=headers)
            try:
                await resp.read()
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.2)

    drive(scenario())
    return proxy_lines(caplog)


def test_proxy_names_the_track_in_both_lines(caplog: pytest.LogCaptureFixture
) -> None:
    lines = fetch_and_log(caplog, "abc.flac")
    assert lines == [
        ("INFO", 'device fetches "Art - Title" from "Alb"'),
        ("INFO", 'sent "Art - Title": 200 kB, complete'),
    ]
    assert not any("abc" in message for _, message in lines)


def test_proxy_omits_empty_album(caplog: pytest.LogCaptureFixture) -> None:
    lines = fetch_and_log(caplog, "abc.flac", describe=lambda _id: ("Solo", ""))
    assert lines[0] == ("INFO", 'device fetches "Solo"')


def test_proxy_without_description_uses_song_id(caplog: pytest.LogCaptureFixture
) -> None:
    lines = fetch_and_log(caplog, "abc.flac", describe=None)
    assert lines == [
        ("INFO", "device fetches song abc"),
        ("INFO", "sent song abc: 200 kB, complete"),
    ]
    unknown = fetch_and_log(caplog, "other.flac")
    assert unknown[-2] == ("INFO", "device fetches song other")


@pytest.mark.parametrize(
    ("header", "suffix"),
    [
        ("bytes=1000-", " (range 1000-)"),
        ("bytes=1000-1999", " (range 1000-1999)"),
        ("bytes=0-", ""),
        (None, ""),
    ],
)
def test_proxy_shows_range_unless_from_start(caplog: pytest.LogCaptureFixture, header: str | None, suffix: str
) -> None:
    headers = None if header is None else {"Range": header}
    lines = fetch_and_log(caplog, "abc.flac", headers=headers)
    assert lines[0] == ("INFO", 'device fetches "Art - Title" from "Alb"' + suffix)


def test_proxy_aborted_transfer_says_aborted(caplog: pytest.LogCaptureFixture
) -> None:
    lines = fetch_and_log(caplog, "cut.flac", describe=lambda _id: ("Cut", "Alb"))
    assert lines[-1] == ("INFO", 'sent "Cut": 50 kB, aborted')


def test_proxy_upstream_404_warns(caplog: pytest.LogCaptureFixture) -> None:
    lines = fetch_and_log(caplog, "gone.flac", describe=lambda _id: ("Gone", "Alb"))
    assert lines[-1] == ("WARNING", 'could not send "Gone": upstream answered 404')
