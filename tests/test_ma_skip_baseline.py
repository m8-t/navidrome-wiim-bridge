import asyncio
import dataclasses
from collections.abc import Awaitable, Iterable
from typing import Any

import pytest

import ma_skip_baseline as m

SAMPLE_TEXT = (
    '{"type":"0","ch":"0","mode":"2","loop":"3","eq":"0","vendor":"","status":"play",'
    '"curpos":"29564","offset_pts":"29564","totlen":"0",'
    '"Title":"21446F6E6E657277657474657221","Artist":"5072696E7A205069",'
    '"Album":"21446F6E6E657277657474657221","alarmflag":"0","plicount":"0","plicurr":"0",'
    '"vol":"27","mute":"0"}'
)
TITLE_HEX = "21446F6E6E657277657474657221"
ARTIST_HEX = "5072696E7A205069"

OLD_SIG = ("AA", "BB", "CC")
NEW_SIG = ("DD", "EE", "FF")


def sample(
    status: str = "play",
    curpos_ms: int = 0,
    sig: m.Sig = NEW_SIG,
    t: float = 0.0,
) -> m.Sample:
    return m.Sample(status=status, curpos_ms=curpos_ms, sig=sig, t=t)


def drive(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(guarded())


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)
        self.now += seconds


class ScriptedDevice:
    def __init__(self, clock: FakeClock, script: Iterable[tuple[str, int, m.Sig]]) -> None:
        self._clock = clock
        self._script = iter(script)
        self.taken: list[m.Sample] = []

    async def poll(self) -> m.Sample:
        step = next(self._script, None)
        if step is None:
            raise AssertionError("scripted device ran out of samples")
        status, curpos_ms, sig = step
        taken = sample(status, curpos_ms, sig, t=self._clock())
        self.taken.append(taken)
        return taken


def test_parse_status_real_sample() -> None:
    result = m.parse_status(SAMPLE_TEXT, 12.5)
    assert result.status == "play"
    assert result.curpos_ms == 29564
    assert result.sig == (TITLE_HEX, ARTIST_HEX, TITLE_HEX)
    assert result.t == 12.5


def test_parse_status_missing_and_bad_fields() -> None:
    empty = m.parse_status("{}", 1.0)
    assert empty.status == ""
    assert empty.curpos_ms == 0
    assert empty.sig == ("", "", "")
    assert m.parse_status('{"curpos": "abc"}', 1.0).curpos_ms == 0


def test_decode_hex() -> None:
    assert m.decode_hex(TITLE_HEX) == "!Donnerwetter!"
    assert m.decode_hex(ARTIST_HEX) == "Prinz Pi"
    assert m.decode_hex("zz") == "zz"
    assert m.decode_hex("") == ""


def test_is_new_track_playing_one_condition_at_a_time() -> None:
    prev = sample(curpos_ms=1000, sig=NEW_SIG)
    cur = sample(curpos_ms=1500, sig=NEW_SIG)
    check = m.is_new_track_playing

    assert check(OLD_SIG, prev, cur) is True
    old_prev = dataclasses.replace(prev, sig=OLD_SIG)
    old_cur = dataclasses.replace(cur, sig=OLD_SIG)
    assert check(OLD_SIG, old_prev, old_cur) is False
    assert check(OLD_SIG, prev, dataclasses.replace(cur, status="load")) is False
    assert check(OLD_SIG, None, cur) is False
    assert check(OLD_SIG, old_prev, cur) is False
    assert check(OLD_SIG, prev, dataclasses.replace(cur, curpos_ms=1000)) is False
    far_prev = dataclasses.replace(prev, curpos_ms=24000)
    assert check(OLD_SIG, far_prev, dataclasses.replace(cur, curpos_ms=25000)) is False
    edge_prev = dataclasses.replace(prev, curpos_ms=19000)
    assert check(OLD_SIG, edge_prev, dataclasses.replace(cur, curpos_ms=20000)) is True


def test_is_seek_landed_one_condition_at_a_time() -> None:
    expected_ms = 20500.0
    prev = sample(curpos_ms=20000, sig=OLD_SIG)
    cur = sample(curpos_ms=80000, sig=OLD_SIG)
    check = m.is_seek_landed

    assert check(expected_ms, prev, cur) is True
    assert check(expected_ms, prev, dataclasses.replace(cur, curpos_ms=21000)) is False
    assert check(expected_ms, prev, dataclasses.replace(cur, curpos_ms=23500)) is False
    assert check(expected_ms, dataclasses.replace(prev, curpos_ms=80000), cur) is False
    assert check(expected_ms, None, cur) is False
    assert check(expected_ms, prev, dataclasses.replace(cur, status="pause")) is False
    reset_prev = dataclasses.replace(prev, curpos_ms=100)
    assert check(expected_ms, reset_prev, dataclasses.replace(cur, curpos_ms=600)) is True


def test_summarize() -> None:
    result = m.summarize([0.5, 0.2, 0.9])
    assert result["count"] == 3
    assert result["median"] == 0.5
    assert result["worst"] == 0.9
    assert result["best"] == 0.2
    with pytest.raises(ValueError):
        m.summarize([])


def test_measure_skip_ignores_stale_position_then_detects() -> None:
    clock = FakeClock(10.0)
    device = ScriptedDevice(
        clock,
        [
            ("play", 30000, OLD_SIG),
            ("load", 30000, NEW_SIG),
            ("play", 100, NEW_SIG),
            ("play", 150, NEW_SIG),
        ],
    )

    async def send_next() -> None:
        clock.now += 0.2

    result = drive(
        m.measure_skip(send_next, device.poll, clock, clock.sleep, OLD_SIG, 20.0, 0.05)
    )

    assert len(device.taken) == 4
    assert result.latency_s == device.taken[-1].t - 10.0
    assert result.latency_s == pytest.approx(0.35)
    assert result.new_sig == NEW_SIG
    assert result.ack_s == pytest.approx(0.2)


def test_measure_skip_times_out_when_track_never_changes() -> None:
    clock = FakeClock(10.0)
    device = ScriptedDevice(clock, (("play", 30000 + 50 * i, OLD_SIG) for i in range(100)))

    async def send_next() -> None:
        clock.now += 0.2

    result = drive(
        m.measure_skip(send_next, device.poll, clock, clock.sleep, OLD_SIG, 1.0, 0.1)
    )

    assert result.latency_s is None
    assert result.new_sig is None
    assert result.ack_s == pytest.approx(0.2)
    assert 10 <= len(device.taken) <= 12


def test_measure_seek_detects_reset_after_normal_progress() -> None:
    clock = FakeClock(100.0)
    before = sample(curpos_ms=10000, sig=OLD_SIG, t=95.0)
    device = ScriptedDevice(
        clock,
        [
            ("play", 15100, OLD_SIG),
            ("play", 15150, OLD_SIG),
            ("play", 200, OLD_SIG),
            ("play", 250, OLD_SIG),
        ],
    )

    async def send_seek() -> None:
        clock.now += 0.1

    result = drive(
        m.measure_seek(send_seek, device.poll, clock, clock.sleep, before, 20.0, 0.05)
    )

    assert len(device.taken) == 4
    assert result.landed_curpos_ms == 250
    assert result.latency_s == device.taken[-1].t - 100.0
    assert result.latency_s == pytest.approx(0.25)
    assert result.ack_s == pytest.approx(0.1)
