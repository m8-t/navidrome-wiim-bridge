import asyncio
import json
import re
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from test_poc_proxy import drive

import bridge.subsonic as bs
from bridge import startup
from bridge.subsonic import AuthResult


def test_unreachable_message_hides_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    async def timing_out(self: Any, req: Any, traces: Any, timeout: Any) -> Any:
        raise asyncio.TimeoutError

    monkeypatch.setattr(aiohttp.BaseConnector, "connect", timing_out)
    params = bs.subsonic_params("dan", "pw-secret", "abcd1234")
    with pytest.raises(aiohttp.ClientError) as raw:
        drive(bs.ping_navidrome("http://127.0.0.1:1", params))
    assert "abcd1234" in str(raw.value)

    verdict, message = startup.check_navidrome("http://127.0.0.1:1", "dan", "pw-secret", "abcd1234")
    assert verdict is AuthResult.UNREACHABLE
    assert message is not None
    assert message.startswith("cannot reach Navidrome at http://127.0.0.1:1: ")
    for hidden in ("pw-secret", "abcd1234", params["t"]):
        assert hidden not in message


def patch_ping(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    async def ping(url: str, params: Any) -> tuple[bool, str | None]:
        sent.append({"url": url, **params})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[no-any-return]

    monkeypatch.setattr(startup, "ping_navidrome", ping)
    return sent


def test_check_navidrome_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = patch_ping(monkeypatch, (True, None))
    assert startup.check_navidrome("http://n:1", "dan", "pw", "abcd") == (AuthResult.OK, None)
    assert sent[0]["url"] == "http://n:1"
    assert sent[0]["u"] == "dan"
    assert sent[0]["s"] == "abcd"


@pytest.mark.parametrize(
    ("message", "text"),
    [
        ("Nope", "Navidrome rejected the credentials: Nope"),
        ("", "Navidrome rejected the credentials"),
    ],
)
def test_check_navidrome_rejected_keeps_the_server_message(
    monkeypatch: pytest.MonkeyPatch, message: str, text: str
) -> None:
    patch_ping(monkeypatch, (False, message))
    assert startup.check_navidrome("http://n:1", "dan", "pw", "abcd") == (
        AuthResult.REJECTED,
        text,
    )


def test_check_navidrome_reply_without_a_reason_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_ping(monkeypatch, (False, None))
    verdict, message = startup.check_navidrome("http://n:1", "dan", "pw", "abcd")
    assert verdict is AuthResult.UNREACHABLE
    assert message == "Navidrome at http://n:1 sent an unusable reply"


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("Cannot connect to host n:1"),
        TimeoutError(),
        ConnectionRefusedError("refused"),
    ],
)
def test_check_navidrome_connection_errors_are_unreachable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    patch_ping(monkeypatch, error)
    verdict, message = startup.check_navidrome("http://n:1", "dan", "pw", "abcd")
    assert verdict is AuthResult.UNREACHABLE
    assert message is not None
    assert message.startswith("cannot reach Navidrome at http://n:1: ")


def test_write_info_file(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "info.json"
    startup.write_info_file(path, "http://h:1/s/abc", "http://n:2", tmp_path / "log.jsonl")
    info = json.loads(path.read_text(encoding="utf-8"))
    assert info["base_url"] == "http://h:1/s/abc"
    assert info["url_template"] == "http://h:1/s/abc/<profile>/<song_id>.flac"
    assert info["profiles"] == ["range", "norange"]
    assert info["navidrome_url"] == "http://n:2"
    assert info["log"] == str(tmp_path / "log.jsonl")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", info["started_iso"])
