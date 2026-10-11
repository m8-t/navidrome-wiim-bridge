import asyncio
import hashlib
import json
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
import poc_proxy as p

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
    log_path: Path

    def events(self) -> list[dict[str, Any]]:
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def event(self, kind: str) -> dict[str, Any]:
        (found,) = [e for e in self.events() if e["ev"] == kind]
        return found


def make_config(navidrome_url: str, log_path: Path) -> bp.ProxyConfig:
    return bp.ProxyConfig(
        navidrome_url=navidrome_url,
        user="dan",
        password="pw",
        salt="abcd",
        secret=SECRET,
        log_path=log_path,
    )


@asynccontextmanager
async def rig(tmp_path: Path, describe: bp.Describe | None = None) -> AsyncIterator[Rig]:
    upstream = FakeUpstream()
    log_path = tmp_path / "log.jsonl"
    async with TestServer(upstream.app) as fake:
        config = make_config(str(fake.make_url("")).rstrip("/"), log_path)
        app = web.Application()
        bp.add_proxy_routes(app, config, describe=describe)
        async with TestClient(TestServer(app)) as client:
            yield Rig(client, upstream, log_path)


def url(profile: str, name: str, secret: str = SECRET) -> str:
    return f"/s/{secret}/{profile}/{name}"


def test_parse_range_start() -> None:
    assert bp.parse_range_start(None) is None
    assert bp.parse_range_start("bytes=0-") == 0
    assert bp.parse_range_start("bytes=1000-1999") == 1000
    assert bp.parse_range_start(" Bytes=5-") == 5
    assert bp.parse_range_start("bytes=-500") is None
    assert bp.parse_range_start("bytes=0-1,5-6") is None
    assert bp.parse_range_start("items=0-") is None
    assert bp.parse_range_start("garbage") is None


def test_subsonic_params() -> None:
    params = bs.subsonic_params("dan", "pw", "abcd")
    assert params.pop("t") == hashlib.md5(b"pwabcd").hexdigest()
    assert params == {"u": "dan", "s": "abcd", "v": "1.16.1", "c": "wiim-bridge-poc", "f": "json"}


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
    assert bp.response_headers("range", upstream) == {
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
    assert bp.response_headers("norange", upstream) == {
        "Content-Type": "audio/flac",
        "transferMode.dlna.org": "Streaming",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Accept-Ranges": "none",
        "Connection": "close",
        "contentFeatures.dlna.org": (
            "DLNA.ORG_OP=00;DLNA.ORG_FLAGS=01700000000000000000000000000000"
        ),
    }
    assert bp.response_headers("norange", {})["Content-Type"] == "audio/flac"
    with pytest.raises(ValueError):
        bp.response_headers("other", upstream)


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


def test_full_get_range_profile(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
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
    start, end = r.event("start"), r.event("end")
    assert start["req"] == end["req"] == 1
    assert start["song_id"] == "abc"
    assert start["range"] is None
    assert start["range_start"] is None
    assert start["method"] == "GET"
    assert start["profile"] == "range"
    assert "User-Agent" in start["headers"]
    assert end["sha256"] == PAYLOAD_SHA256
    assert end["complete"] is True
    assert end["bytes"] == 200000
    assert end["status"] == 200
    assert end["upstream_status"] == 200
    assert end["upstream_content_type"] == "audio/flac"
    assert end["upstream_body"] is None
    assert end["resp_headers"]["Content-Length"] == "200000"


def test_range_get_range_profile(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.get(
                url("range", "abc.flac"), headers={"Range": "bytes=1000-1999"}
            )
            assert resp.status == 206
            assert await resp.read() == PAYLOAD[1000:2000]
            assert resp.headers["Content-Range"] == "bytes 1000-1999/200000"
            return r

    r = drive(scenario())
    (seen,) = r.upstream.requests
    assert seen.headers["Range"] == "bytes=1000-1999"
    start, end = r.event("start"), r.event("end")
    assert start["range"] == "bytes=1000-1999"
    assert start["range_start"] == 1000
    assert end["status"] == 206
    assert end["bytes"] == 1000
    assert end["complete"] is True
    assert end["sha256"] is None


def test_if_range_is_forwarded_on_range_profile_only(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            headers = {"Range": "bytes=10-", "If-Range": LAST_MODIFIED}
            await (await r.client.get(url("range", "abc.flac"), headers=headers)).read()
            await (await r.client.get(url("norange", "abc.flac"), headers=headers)).read()
            return r

    ranged, plain = drive(scenario()).upstream.requests
    assert ranged.headers["If-Range"] == LAST_MODIFIED
    assert "If-Range" not in plain.headers
    assert ranged.headers["Accept-Encoding"] == plain.headers["Accept-Encoding"] == "identity"


def test_range_request_on_norange_profile(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.get(url("norange", "abc.flac"), headers={"Range": "bytes=1000-"})
            assert resp.status == 200
            assert await resp.read() == PAYLOAD
            assert resp.headers["Accept-Ranges"] == "none"
            assert "Content-Length" not in resp.headers
            assert resp.headers["Transfer-Encoding"] == "chunked"
            assert resp.headers["Cache-Control"] == "no-cache"
            return r

    r = drive(scenario())
    (seen,) = r.upstream.requests
    assert "Range" not in seen.headers
    assert seen.headers["Accept-Encoding"] == "identity"
    start, end = r.event("start"), r.event("end")
    assert start["range"] == "bytes=1000-"
    assert start["range_start"] == 1000
    assert end["status"] == 200
    assert end["sha256"] == PAYLOAD_SHA256
    assert "Content-Length" not in end["resp_headers"]


def test_upstream_error_body_becomes_502(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.get(url("range", "missing.flac"))
            assert resp.status == 502
            assert await resp.text() == "upstream error"
            return r

    end = drive(scenario()).event("end")
    assert end["status"] == 502
    assert end["upstream_status"] == 200
    assert end["upstream_content_type"].startswith("application/json")
    assert "not found" in end["upstream_body"]
    assert end["resp_headers"] is None
    assert end["sha256"] is None


def test_upstream_unreachable_becomes_502(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        upstream = FakeUpstream()
        log_path = tmp_path / "log.jsonl"
        async with TestServer(upstream.app) as fake:
            dead_url = str(fake.make_url("")).rstrip("/")
        async with TestClient(TestServer(bp.create_app(make_config(dead_url, log_path)))) as client:
            resp = await client.get(url("range", "abc.flac"))
            assert resp.status == 502
            return Rig(client, upstream, log_path)

    end = drive(scenario()).event("end")
    assert end["status"] == 502
    assert end["upstream_status"] is None
    assert end["complete"] is False


def test_out_of_range_request_passes_416_through(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.get(url("range", "abc.flac"), headers={"Range": "bytes=300000-"})
            assert resp.status == 416
            assert resp.headers["Content-Range"] == "bytes */200000"
            assert await resp.read() == b""
            return r

    end = drive(scenario()).event("end")
    assert end["status"] == 416
    assert end["upstream_status"] == 416
    assert end["upstream_body"] is None


def test_rejections_are_logged_without_the_secret(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            for path in (
                url("range", "abc.flac", secret="wrong"),
                url("foo", "abc.flac"),
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
    events = r.events()
    assert [e["ev"] for e in events] == ["reject"] * 6
    assert [e["reason"] for e in events] == ["secret", "profile", "name", "name", "name", "name"]
    assert all(e["remote"] for e in events)
    assert SECRET not in r.log_path.read_text(encoding="utf-8")


def test_non_ascii_secret_is_rejected_not_an_error(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.get(url("range", "abc.flac", secret="s%C3%A4"))
            assert resp.status == 404
            return r

    assert drive(scenario()).event("reject")["reason"] == "secret"


def test_http10_client_gets_close_delimited_body(tmp_path: Path) -> None:
    async def scenario() -> bytes:
        async with rig(tmp_path) as r:
            reader, writer = await asyncio.open_connection("127.0.0.1", r.client.server.port)
            writer.write(f"GET {url('norange', 'abc.flac')} HTTP/1.0\r\n\r\n".encode())
            data = await reader.read()
            writer.close()
            return data

    head, _, body = drive(scenario()).partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200")
    assert b"transfer-encoding" not in head.lower()
    assert body == PAYLOAD


def test_head_range_profile(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.head(url("range", "abc.flac"))
            assert resp.status == 200
            assert resp.headers["Content-Length"] == "200000"
            assert await resp.read() == b""
            return r

    r = drive(scenario())
    (seen,) = r.upstream.requests
    assert seen.method == "HEAD"
    assert seen.headers["Accept-Encoding"] == "identity"
    end = r.event("end")
    assert end["bytes"] == 0
    assert end["sha256"] is None


def test_head_norange_profile_skips_upstream(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.head(url("norange", "abc.flac"))
            assert resp.status == 200
            assert resp.headers["Accept-Ranges"] == "none"
            assert "Content-Length" not in resp.headers
            assert await resp.read() == b""
            return r

    r = drive(scenario())
    assert r.upstream.requests == []
    end = r.event("end")
    assert end["upstream_status"] is None
    assert end["resp_headers"]["Accept-Ranges"] == "none"


def test_request_numbers_count_up(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            await (await r.client.get(url("range", "abc.flac"))).read()
            await (await r.client.head(url("norange", "abc.flac"))).read()
            return r

    ends = [e for e in drive(scenario()).events() if e["ev"] == "end"]
    assert [e["req"] for e in ends] == [1, 2]


def test_truncated_upstream_is_logged_incomplete(tmp_path: Path) -> None:
    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.get(url("range", "cut.flac"))
            with pytest.raises(aiohttp.ClientPayloadError):
                await resp.read()
            await asyncio.sleep(0.2)
            return r

    end = drive(scenario()).event("end")
    assert end["complete"] is False
    assert 0 < end["bytes"] < len(PAYLOAD)
    assert end["sha256"] is None


def test_connection_lost_during_write_is_handled_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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

    async def scenario() -> Rig:
        async with rig(tmp_path) as r:
            resp = await r.client.get(url("range", "abc.flac"))
            with pytest.raises(aiohttp.ClientError):
                await resp.read()
            await asyncio.sleep(0.2)
            return r

    end = drive(scenario()).event("end")
    assert end["complete"] is False
    assert end["sha256"] is None
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


def test_unreachable_message_hides_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    async def timing_out(self: Any, req: Any, traces: Any, timeout: Any) -> Any:
        raise asyncio.TimeoutError

    monkeypatch.setattr(aiohttp.BaseConnector, "connect", timing_out)
    params = bs.subsonic_params("dan", "pw-secret", "abcd1234")
    with pytest.raises(aiohttp.ClientError) as raw:
        drive(bs.ping_navidrome("http://127.0.0.1:1", params))
    assert "abcd1234" in str(raw.value)

    message = p.check_navidrome("http://127.0.0.1:1", "dan", "pw-secret", "abcd1234")
    assert message is not None
    assert message.startswith("cannot reach Navidrome at http://127.0.0.1:1: ")
    for hidden in ("pw-secret", "abcd1234", params["t"]):
        assert hidden not in message


def credentials_file(tmp_path: Path, password: str = "hunter2-pw") -> Path:
    content = json.dumps({"user": "dan", "password": password})
    return write_credentials(tmp_path / "credentials.json", content, 0o600)


def cli_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--credentials",
        str(credentials_file(tmp_path)),
        "--info-file",
        str(tmp_path / "info.json"),
        "--log",
        str(tmp_path / "out" / "requests.jsonl"),
        "--navidrome-url",
        "http://navidrome.invalid:4533/",
        *(() if "--advertise-host" in extra else ("--advertise-host", "192.0.2.30")),
        *extra,
    ]


def test_main_reports_bad_credentials(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--credentials", str(tmp_path / "missing.json"), "--advertise-host", "192.0.2.30"]
    assert p.main(args) == 2
    assert capsys.readouterr().err.startswith("error: credentials file not found")


def test_main_reports_rejected_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def rejected(url: str, params: Any) -> tuple[bool, str | None]:
        return False, "Wrong username or password"

    monkeypatch.setattr(p, "ping_navidrome", rejected)
    assert p.main(cli_args(tmp_path)) == 2
    err = capsys.readouterr().err
    assert err == "error: Navidrome rejected the credentials: Wrong username or password\n"
    assert not (tmp_path / "info.json").exists()


def test_main_reports_unreachable_navidrome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(url: str, params: Any) -> tuple[bool, str | None]:
        raise aiohttp.ClientConnectionError("Cannot connect to host navidrome.invalid:4533")

    monkeypatch.setattr(p, "ping_navidrome", unreachable)
    assert p.main(cli_args(tmp_path)) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: cannot reach Navidrome at http://navidrome.invalid:4533: ")


def test_main_writes_info_file_and_starts_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pinged: dict[str, Any] = {}
    started: dict[str, Any] = {}

    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        pinged.update(url=url, params=dict(params))
        return True, None

    def fake_run_app(app: web.Application, **kwargs: Any) -> None:
        started.update(app=app, **kwargs)

    monkeypatch.setattr(p, "ping_navidrome", accepted)
    monkeypatch.setattr(p.web, "run_app", fake_run_app)
    args = cli_args(tmp_path, "--secret", SECRET, "--port", "9001", "--advertise-host", "10.0.0.5")
    assert p.main(args) == 0

    base_url = f"http://10.0.0.5:9001/s/{SECRET}"
    info = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    assert info["base_url"] == base_url
    assert info["url_template"] == f"{base_url}/<profile>/<song_id>.flac"
    assert info["profiles"] == ["range", "norange"]
    assert info["navidrome_url"] == "http://navidrome.invalid:4533"
    assert info["log"] == str(tmp_path / "out" / "requests.jsonl")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", info["started_iso"])
    assert pinged["url"] == "http://navidrome.invalid:4533"
    assert started["host"] == "0.0.0.0"
    assert started["port"] == 9001
    assert started["print"] is None

    out = capsys.readouterr()
    assert base_url in out.out
    assert str(tmp_path / "out" / "requests.jsonl") in out.out
    for hidden in ("hunter2-pw", pinged["params"]["s"], pinged["params"]["t"]):
        assert hidden not in out.out + out.err
        assert hidden not in json.dumps(info)


def test_default_secret_is_generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def accepted(url: str, params: Any) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(p, "ping_navidrome", accepted)
    monkeypatch.setattr(p.web, "run_app", lambda app, **kwargs: None)
    assert p.main(cli_args(tmp_path)) == 0
    info = json.loads((tmp_path / "info.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"http://192\.0\.2\.30:8098/s/[A-Za-z0-9_-]{12}", info["base_url"])


def test_config_repr_hides_secrets(tmp_path: Path) -> None:
    text = repr(make_config("http://x", tmp_path / "log.jsonl"))
    assert "pw" not in text.replace("pwd", "")
    assert "abcd" not in text
    assert SECRET not in text


def describe_abc(song_id: str) -> tuple[str, str] | None:
    return ("Art - Title", "Alb") if song_id == "abc" else None


def proxy_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "bridge.proxy"]


def fetch_and_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    name: str,
    *,
    describe: bp.Describe | None = describe_abc,
    headers: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with rig(tmp_path, describe) as r:
            resp = await r.client.get(url("range", name), headers=headers)
            try:
                await resp.read()
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.2)

    drive(scenario())
    return proxy_lines(caplog)


def test_proxy_names_the_track_in_both_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lines = fetch_and_log(tmp_path, caplog, "abc.flac")
    assert lines == [
        ("INFO", 'device fetches "Art - Title" from "Alb"'),
        ("INFO", 'sent "Art - Title": 200 kB, complete'),
    ]
    assert not any("abc" in message for _, message in lines)


def test_proxy_omits_empty_album(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lines = fetch_and_log(tmp_path, caplog, "abc.flac", describe=lambda _id: ("Solo", ""))
    assert lines[0] == ("INFO", 'device fetches "Solo"')


def test_proxy_without_description_uses_song_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lines = fetch_and_log(tmp_path, caplog, "abc.flac", describe=None)
    assert lines == [
        ("INFO", "device fetches song abc"),
        ("INFO", "sent song abc: 200 kB, complete"),
    ]
    unknown = fetch_and_log(tmp_path, caplog, "other.flac")
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
def test_proxy_shows_range_unless_from_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, header: str | None, suffix: str
) -> None:
    headers = None if header is None else {"Range": header}
    lines = fetch_and_log(tmp_path, caplog, "abc.flac", headers=headers)
    assert lines[0] == ("INFO", 'device fetches "Art - Title" from "Alb"' + suffix)


def test_proxy_aborted_transfer_says_aborted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lines = fetch_and_log(tmp_path, caplog, "cut.flac", describe=lambda _id: ("Cut", "Alb"))
    assert lines[-1] == ("INFO", 'sent "Cut": 50 kB, aborted')


def test_proxy_upstream_404_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lines = fetch_and_log(tmp_path, caplog, "gone.flac", describe=lambda _id: ("Gone", "Alb"))
    assert lines[-1] == ("WARNING", 'could not send "Gone": upstream answered 404')
