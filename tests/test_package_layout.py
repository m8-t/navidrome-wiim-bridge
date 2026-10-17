import importlib
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_poc_proxy import PAYLOAD, PAYLOAD_SHA256, FakeUpstream, drive, make_config, url

from bridge.proxy import add_proxy_routes

ROOT = Path(__file__).resolve().parent.parent

EXPECTED = {
    "bridge.soap": [
        "escape_xml", "build_envelope", "soap_headers", "escape_metadata", "format_hms",
        "parse_hms", "build_didl", "parse_values", "UpnpError", "parse_fault",
    ],
    "bridge.wiim": [
        "device_urls", "CONTROL_PATH", "SERVICE",
        "PLAYQUEUE_CONTROL_PATH", "PLAYQUEUE_SERVICE", "QueueTrack", "build_queue_context",
        "parse_queue_index", "parse_queue_context", "parse_queue_tracks", "tracks_from_album",
        "DeviceState", "describe_change", "redact_uri", "Wiim", "open_sessions",
    ],
    "bridge.subsonic": [
        "ConfigError", "load_credentials", "subsonic_params", "DEFAULT_CREDENTIALS",
        "UPSTREAM_HEADERS", "parse_ping", "ping_navidrome", "describe_error", "AuthResult",
    ],
    "bridge.startup": ["check_navidrome", "write_info_file"],
    "bridge.proxy": [
        "PROFILES", "parse_range_start", "response_headers", "ProxyConfig", "RequestLog",
        "handle_stream", "create_app", "add_proxy_routes",
    ],
}  # fmt: skip


@pytest.mark.parametrize("module", sorted(EXPECTED))
def test_package_modules_expose_their_names(module: str) -> None:
    loaded = importlib.import_module(module)
    missing = [name for name in EXPECTED[module] if not hasattr(loaded, name)]
    assert missing == []


def test_add_proxy_routes_serves_full_get(tmp_path: Path) -> None:
    async def scenario() -> tuple[bytes, int, str]:
        upstream = FakeUpstream()
        async with TestServer(upstream.app) as fake:
            config = make_config(str(fake.make_url("")).rstrip("/"), tmp_path / "log.jsonl")
            app = web.Application()
            add_proxy_routes(app, config)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(url("range", "abc.flac"))
                return await resp.read(), resp.status, resp.headers["Content-Length"]

    body, status, length = drive(scenario())
    assert status == 200
    assert body == PAYLOAD
    assert length == "200000"
    ends = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert [e["ev"] for e in ends] == ["start", "end"]
    assert ends[1]["sha256"] == PAYLOAD_SHA256


def test_the_production_entrypoint_does_not_depend_on_the_poc_proxy() -> None:
    for relative in ("tools/bridge_server.py", "Dockerfile"):
        assert "poc_proxy" not in (ROOT / relative).read_text(encoding="utf-8"), relative
