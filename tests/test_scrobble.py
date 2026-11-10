import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

import aiohttp
import pytest

from bridge.scrobble import (
    MAX_GAP_S,
    PlayTracker,
    Scrobble,
    Scrobbler,
    listen_threshold,
    watch,
)
from bridge.soap import UpnpError

T0 = 1_000_000.0
LONG = {"id": "a", "duration": 200}
SHORT = {"id": "s", "duration": 60}
OTHER = {"id": "b", "duration": 200}


def run(coro: Awaitable[Any]) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(guarded())


def ms(seconds: float) -> int:
    return round(seconds * 1000)


def play_through(
    tracker: PlayTracker,
    entry: Any,
    song: dict[str, Any],
    start: float,
    seconds: int,
    step: int = 5,
    first_position: float = 0.0,
) -> list[Scrobble]:
    events: list[Scrobble] = []
    for elapsed in range(0, seconds + 1, step):
        events += tracker.feed(entry, song, True, first_position + elapsed, start + elapsed)
    return events


@pytest.mark.parametrize(("duration", "expected"), [(200, 100), (600, 240), (479, 239.5), (0, 240)])
def test_listen_threshold(duration: int, expected: float) -> None:
    assert listen_threshold(duration) == expected


def test_normal_advance_sends_now_playing_then_the_play() -> None:
    tracker = PlayTracker()
    events = play_through(tracker, (1, 0), LONG, T0, 195)
    assert events == [Scrobble("a", ms(T0), False)]
    events = tracker.feed((1, 1), OTHER, True, 0.0, T0 + 200)
    assert events == [Scrobble("a", ms(T0), True), Scrobble("b", ms(T0 + 200), False)]


def test_skipping_after_ten_seconds_does_not_submit() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), LONG, T0, 10)
    events = tracker.feed((1, 1), OTHER, True, 0.0, T0 + 12)
    assert events == [Scrobble("b", ms(T0 + 12), False)]


def test_seeking_forward_does_not_count_as_listening() -> None:
    tracker = PlayTracker()
    tracker.feed((1, 0), LONG, True, 0.0, T0)
    tracker.feed((1, 0), LONG, True, 5.0, T0 + 5)
    assert tracker.feed((1, 0), LONG, True, 150.0, T0 + 10) == []
    assert tracker.feed((1, 0), LONG, True, 155.0, T0 + 15) == []
    events = tracker.feed((1, 1), OTHER, True, 0.0, T0 + 20)
    assert events == [Scrobble("b", ms(T0 + 20), False)]


def test_seeking_back_in_the_middle_is_neither_a_loop_nor_an_end() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), LONG, T0, 100, first_position=40.0)
    assert tracker.feed((1, 0), LONG, True, 8.0, T0 + 105) == []
    assert tracker.feed((1, 0), LONG, True, 13.0, T0 + 110) == []
    events = tracker.feed((1, 1), OTHER, True, 0.0, T0 + 115)
    assert [e.submission for e in events] == [True, False]


def test_paused_time_is_not_listening_time() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), SHORT, T0, 20)
    for step in range(1, 21):
        assert tracker.feed((1, 0), SHORT, False, 20.0, T0 + 20 + step * 5) == []
    events = tracker.feed((1, 1), OTHER, True, 0.0, T0 + 130)
    assert events == [Scrobble("b", ms(T0 + 130), False)]


def test_pause_and_resume_keep_one_play() -> None:
    tracker = PlayTracker()
    first = play_through(tracker, (1, 0), SHORT, T0, 20)
    assert first == [Scrobble("s", ms(T0), False)]
    assert tracker.feed((1, 0), SHORT, False, 20.0, T0 + 25) == []
    assert tracker.feed((1, 0), SHORT, False, 20.0, T0 + 300) == []
    assert tracker.feed((1, 0), SHORT, True, 20.0, T0 + 305) == []
    resumed = play_through(tracker, (1, 0), SHORT, T0 + 310, 20, first_position=20.0)
    assert resumed == []
    events = tracker.feed((1, 1), OTHER, True, 0.0, T0 + 335)
    assert events == [Scrobble("s", ms(T0), True), Scrobble("b", ms(T0 + 335), False)]


def test_a_long_gap_between_samples_is_capped() -> None:
    tracker = PlayTracker()
    tracker.feed((1, 0), SHORT, True, 0.0, T0)
    tracker.feed((1, 0), SHORT, True, 5.0, T0 + 1000)
    assert tracker.feed((1, 1), OTHER, True, 0.0, T0 + 1001) == [
        Scrobble("b", ms(T0 + 1001), False)
    ]
    assert MAX_GAP_S < 30


def test_repeat_one_counts_each_loop() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), SHORT, T0, 55)
    events = tracker.feed((1, 0), SHORT, True, 2.0, T0 + 62, repeat_one=True)
    assert events == [Scrobble("s", ms(T0), True), Scrobble("s", ms(T0 + 60), False)]
    play_through(tracker, (1, 0), SHORT, T0 + 62, 53, first_position=2.0)
    events = tracker.feed((1, 0), SHORT, True, 1.0, T0 + 121, repeat_one=True)
    assert events == [Scrobble("s", ms(T0 + 60), True), Scrobble("s", ms(T0 + 120), False)]


def test_without_repeat_one_a_wrap_to_the_start_is_not_a_new_play() -> None:
    # the WiiM reports position ~0 on the old index for a sample before it moves on
    tracker = PlayTracker()
    first = play_through(tracker, (1, 0), SHORT, T0, 55)
    assert first == [Scrobble("s", ms(T0), False)]
    assert tracker.feed((1, 0), SHORT, True, 0.0, T0 + 57) == []
    assert tracker.feed((1, 0), SHORT, True, 0.0, T0 + 58) == []
    events = tracker.feed((1, 1), OTHER, True, 1.0, T0 + 59)
    assert events == [Scrobble("s", ms(T0), True), Scrobble("b", ms(T0 + 58), False)]


def test_a_stopped_device_ends_the_play_even_without_repeat_one() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), SHORT, T0, 55)
    assert tracker.feed((1, 0), SHORT, False, 0.0, T0 + 58) == [Scrobble("s", ms(T0), True)]


def test_a_loop_that_was_barely_listened_to_is_not_submitted() -> None:
    tracker = PlayTracker()
    tracker.feed((1, 0), SHORT, True, 58.0, T0)
    events = tracker.feed((1, 0), SHORT, True, 2.0, T0 + 4, repeat_one=True)
    assert events == [Scrobble("s", ms(T0 + 2), False)]


def test_end_closes_the_play_and_credits_the_time_since_the_last_sample() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), LONG, T0, 95)
    assert tracker.end(T0 + 100) == [Scrobble("a", ms(T0), True)]
    assert tracker.end(T0 + 101) == []

    tracker = PlayTracker()
    play_through(tracker, (1, 0), LONG, T0, 90)
    assert tracker.end(T0 + 91) == []


def test_end_without_a_play_or_while_paused() -> None:
    tracker = PlayTracker()
    assert tracker.end(T0) == []
    tracker.feed((1, 0), SHORT, True, 0.0, T0)
    tracker.feed((1, 0), SHORT, False, 40.0, T0 + 40)
    assert tracker.end(T0 + 300) == []


def test_an_unloaded_queue_ends_the_play_without_extra_credit() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), SHORT, T0, 30)
    assert tracker.feed(None, None, False, 0.0, T0 + 200) == [Scrobble("s", ms(T0), True)]
    assert tracker.feed(None, None, False, 0.0, T0 + 205) == []


def test_replacing_the_queue_starts_a_new_play_for_the_same_key() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), SHORT, T0, 30)
    events = tracker.feed((2, 0), SHORT, True, 0.0, T0 + 32)
    assert events == [Scrobble("s", ms(T0), True), Scrobble("s", ms(T0 + 32), False)]


def test_the_same_song_in_two_entries_is_two_plays() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), SHORT, T0, 40)
    events = tracker.feed((1, 1), SHORT, True, 0.0, T0 + 45)
    assert events == [Scrobble("s", ms(T0), True), Scrobble("s", ms(T0 + 45), False)]
    play_through(tracker, (1, 1), SHORT, T0 + 50, 40)
    assert tracker.end(T0 + 95) == [Scrobble("s", ms(T0 + 45), True)]


def test_stopping_at_the_end_of_the_last_track_ends_the_play() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), LONG, T0, 195)
    assert tracker.feed((1, 0), LONG, False, 199.0, T0 + 200) == [Scrobble("a", ms(T0), True)]
    assert tracker.feed((1, 0), LONG, False, 199.0, T0 + 205) == []
    assert tracker.feed((1, 0), LONG, False, 0.0, T0 + 210) == []


def test_stopping_with_the_position_reset_ends_the_play() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), LONG, T0, 195)
    assert tracker.feed((1, 0), LONG, False, 0.0, T0 + 200) == [Scrobble("a", ms(T0), True)]


def test_pausing_in_the_middle_is_not_an_end() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), LONG, T0, 100, first_position=50.0)
    assert tracker.feed((1, 0), LONG, False, 150.0, T0 + 105) == []
    assert tracker.feed((1, 0), LONG, False, 150.0, T0 + 200) == []
    assert tracker.end(T0 + 201) == [Scrobble("a", ms(T0 - 50), True)]


def test_a_play_submits_at_most_once() -> None:
    tracker = PlayTracker()
    play_through(tracker, (1, 0), SHORT, T0, 40)
    first = tracker.feed(None, None, False, 0.0, T0 + 50)
    assert [e.submission for e in first] == [True]
    assert tracker.feed(None, None, False, 0.0, T0 + 55) == []
    assert tracker.end(T0 + 60) == []


def test_nothing_is_reported_for_a_track_that_is_not_playing() -> None:
    tracker = PlayTracker()
    assert tracker.feed((1, 0), LONG, False, 0.0, T0) == []
    assert tracker.feed((1, 0), LONG, False, 0.0, T0 + 5) == []
    assert tracker.feed((1, 0), LONG, True, 0.0, T0 + 10) == [Scrobble("a", ms(T0 + 10), False)]


def test_a_play_first_seen_midway_starts_when_it_began() -> None:
    tracker = PlayTracker()
    events = tracker.feed((1, 0), LONG, True, 30.0, T0)
    assert events == [Scrobble("a", ms(T0 - 30), False)]


def test_unknown_duration_needs_four_minutes() -> None:
    tracker = PlayTracker()
    unknown = {"id": "u"}
    play_through(tracker, (1, 0), unknown, T0, 235)
    assert tracker.end(T0 + 236) == []
    play_through(tracker, (1, 1), unknown, T0 + 300, 240)
    assert tracker.end(T0 + 541) == [Scrobble("u", ms(T0 + 300), True)]


def test_unknown_duration_loop_needs_a_big_drop_to_the_start() -> None:
    tracker = PlayTracker()
    unknown = {"id": "u"}
    play_through(tracker, (1, 0), unknown, T0, 40)
    assert tracker.feed((1, 0), unknown, True, 8.0, T0 + 45) == []
    play_through(tracker, (1, 0), unknown, T0 + 50, 70, first_position=8.0)
    events = tracker.feed((1, 0), unknown, True, 3.0, T0 + 125, repeat_one=True)
    assert events == [Scrobble("u", ms(T0 + 122), False)]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def drain(scrobbler: Scrobbler, sent: list[Any], count: int) -> None:
    task = asyncio.create_task(scrobbler.run())
    while len(sent) < count:
        await asyncio.sleep(0)
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_scrobbler_delivers_in_order_and_survives_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    sent: list[tuple[str, int, bool]] = []

    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        sent.append((song_id, time_ms, submission))
        if song_id == "bad":
            raise aiohttp.ClientError("GET http://h/rest/scrobble?u=dan&t=tok&s=salt failed")
        return song_id != "no"

    clock = Clock()

    async def scenario() -> None:
        scrobbler = Scrobbler(send, clock.sleep, clock)
        scrobbler.submit(
            [Scrobble("a", 1, False), Scrobble("bad", 2, False), Scrobble("no", 3, True)]
        )
        scrobbler.submit([Scrobble("c", 4, True)])
        await drain(scrobbler, sent, 4)

    run(scenario())
    assert sent == [("a", 1, False), ("bad", 2, False), ("no", 3, True), ("c", 4, True)]
    warnings = warnings_of(caplog)
    assert len(warnings) == 2
    assert "?<redacted>" in warnings[0]
    assert "tok" not in warnings[0]
    assert "was not accepted" in warnings[1]
    assert clock.slept == []


def test_a_play_is_retried_until_navidrome_answers(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    sent: list[str] = []

    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        sent.append(song_id)
        if len(sent) <= 2:
            raise aiohttp.ClientError("GET http://h/rest/scrobble?u=dan&t=tok&s=salt failed")
        return True

    clock = Clock()

    async def scenario() -> None:
        scrobbler = Scrobbler(send, clock.sleep, clock)
        scrobbler.submit([Scrobble("a", 1, True)])
        await drain(scrobbler, sent, 3)

    run(scenario())
    assert sent == ["a", "a", "a"]
    assert clock.slept == [5.0, 30.0]
    messages = [r.getMessage() for r in caplog.records if r.name == "bridge.scrobble"]
    assert len(messages) == 2
    assert messages[0].startswith("scrobble of play a failed, retrying: ")
    assert "tok" not in messages[0]
    assert messages[1] == "scrobbled play a"


def test_a_play_is_dropped_after_the_last_retry(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[str] = []

    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        sent.append(song_id)
        raise TimeoutError

    clock = Clock()

    async def scenario() -> None:
        scrobbler = Scrobbler(send, clock.sleep, clock)
        scrobbler.submit([Scrobble("a", 1, True)])
        await drain(scrobbler, sent, 5)

    run(scenario())
    assert len(sent) == 5
    assert clock.slept == [5.0, 30.0, 120.0, 600.0]
    warnings = warnings_of(caplog)
    assert len(warnings) == 2
    assert "retrying" in warnings[0]
    assert warnings[1] == "scrobble of play a failed: TimeoutError"


def test_a_refused_play_is_not_retried(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[str] = []

    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        sent.append(song_id)
        return False

    clock = Clock()

    async def scenario() -> None:
        scrobbler = Scrobbler(send, clock.sleep, clock)
        scrobbler.submit([Scrobble("a", 1, True)])
        await drain(scrobbler, sent, 1)

    run(scenario())
    assert sent == ["a"]
    assert clock.slept == []
    assert warnings_of(caplog) == ["scrobble of play a was not accepted"]


def test_events_queued_during_a_retry_wait_their_turn() -> None:
    sent: list[str] = []
    scrobbler_ref: list[Scrobbler] = []

    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        sent.append(song_id)
        if sent == ["a"]:
            raise aiohttp.ClientError("down")
        return True

    clock = Clock()

    async def sleep(seconds: float) -> None:
        scrobbler_ref[0].submit([Scrobble("b", 2, True)])
        await clock.sleep(seconds)

    async def scenario() -> None:
        scrobbler = Scrobbler(send, sleep, clock)
        scrobbler_ref.append(scrobbler)
        scrobbler.submit([Scrobble("a", 1, True)])
        await drain(scrobbler, sent, 3)

    run(scenario())
    assert sent == ["a", "a", "b"]


def test_stale_now_playing_is_dropped_and_a_fresh_one_is_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    sent: list[str] = []
    clock = Clock()

    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        sent.append(song_id)
        return True

    async def scenario() -> None:
        scrobbler = Scrobbler(send, clock.sleep, clock)
        scrobbler.submit([Scrobble("old", 1, False)])
        clock.now += 61.0
        scrobbler.submit([Scrobble("new", 2, False), Scrobble("p", 3, True)])
        clock.now += 60.0
        await drain(scrobbler, sent, 2)

    run(scenario())
    assert sent == ["new", "p"]
    assert warnings_of(caplog) == []
    assert any(r.levelno == logging.DEBUG and "old" in r.getMessage() for r in caplog.records)


def test_now_playing_that_raises_is_not_retried(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[str] = []

    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        sent.append(song_id)
        if song_id == "np":
            raise aiohttp.ClientError("down")
        return True

    clock = Clock()

    async def scenario() -> None:
        scrobbler = Scrobbler(send, clock.sleep, clock)
        scrobbler.submit([Scrobble("np", 1, False), Scrobble("p", 2, True)])
        await drain(scrobbler, sent, 2)

    run(scenario())
    assert sent == ["np", "p"]
    assert clock.slept == []
    assert warnings_of(caplog) == ["scrobble of now playing np failed: down"]


def test_scrobbler_drops_when_the_queue_is_full(caplog: pytest.LogCaptureFixture) -> None:
    async def send(song_id: str, time_ms: int, submission: bool) -> bool:
        return True

    async def scenario() -> None:
        scrobbler = Scrobbler(send)
        scrobbler.submit([Scrobble("a", n, True) for n in range(150)])

    run(scenario())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 50


def test_watch_polls_on_the_interval_and_survives_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    slept: list[float] = []
    calls: list[int] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) > 4:
            raise asyncio.CancelledError

    async def poll() -> bool:
        calls.append(1)
        if len(calls) == 2:
            raise UpnpError(701, "boom")
        if len(calls) == 3:
            raise TimeoutError
        return True

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await watch(poll, 5.0, sleep)

    run(scenario())
    assert slept == [5.0] * 5
    assert len(calls) == 4
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert warnings[1].endswith("TimeoutError")
