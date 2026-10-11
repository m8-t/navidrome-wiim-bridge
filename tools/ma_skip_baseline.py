"""Measure how long Music Assistant takes to make a WiiM amplifier play the next track."""

import argparse
import asyncio
import functools
import json
import os
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_MA_URL = "https://ma.m8t.io/api"
DEFAULT_QUEUE_ID = "wiim_uuid:FF98F9ED-8DDE-B826-8681-21C8FF98F9ED"
DEFAULT_TOKEN_FILE = "~/.claude/keys/ma_token"

Sig = tuple[str, str, str]


@dataclass(frozen=True)
class Sample:
    status: str
    curpos_ms: int
    sig: Sig
    t: float


SendFn = Callable[[], Awaitable[None]]
PollFn = Callable[[], Awaitable[Sample]]
ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class SkipResult:
    ack_s: float
    latency_s: float | None
    new_sig: Sig | None


@dataclass(frozen=True)
class SeekResult:
    ack_s: float
    latency_s: float | None
    landed_curpos_ms: int | None


def parse_status(text: str, t: float) -> Sample:
    data = json.loads(text)
    try:
        curpos_ms = int(data.get("curpos", 0))
    except (TypeError, ValueError):
        curpos_ms = 0
    sig = (data.get("Title", ""), data.get("Artist", ""), data.get("Album", ""))
    return Sample(status=data.get("status", ""), curpos_ms=curpos_ms, sig=sig, t=t)


def decode_hex(value: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8", "replace")
    except ValueError:
        return value


def is_new_track_playing(
    before_sig: Sig, prev: Sample | None, cur: Sample, max_start_ms: int = 20000
) -> bool:
    return (
        cur.status == "play"
        and cur.sig != before_sig
        and prev is not None
        and prev.sig == cur.sig
        and cur.curpos_ms > prev.curpos_ms
        and cur.curpos_ms <= max_start_ms
    )


def is_seek_landed(
    expected_ms: float, prev: Sample | None, cur: Sample, tolerance_ms: int = 3000
) -> bool:
    return (
        cur.status == "play"
        and prev is not None
        and cur.curpos_ms > prev.curpos_ms
        and abs(cur.curpos_ms - expected_ms) > tolerance_ms
    )


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("cannot summarize an empty list")
    return {
        "count": float(len(values)),
        "median": statistics.median(values),
        "worst": max(values),
        "best": min(values),
    }


async def measure_skip(
    send_next: SendFn,
    poll_status: PollFn,
    clock: ClockFn,
    sleep: SleepFn,
    before_sig: Sig,
    timeout_s: float,
    poll_interval_s: float,
) -> SkipResult:
    t0 = clock()
    await send_next()
    ack_s = clock() - t0
    prev: Sample | None = None
    while True:
        cur = await poll_status()
        if is_new_track_playing(before_sig, prev, cur):
            return SkipResult(ack_s=ack_s, latency_s=cur.t - t0, new_sig=cur.sig)
        if clock() - t0 > timeout_s:
            return SkipResult(ack_s=ack_s, latency_s=None, new_sig=None)
        prev = cur
        await sleep(poll_interval_s)


async def measure_seek(
    send_seek: SendFn,
    poll_status: PollFn,
    clock: ClockFn,
    sleep: SleepFn,
    before: Sample,
    timeout_s: float,
    poll_interval_s: float,
) -> SeekResult:
    t0 = clock()
    await send_seek()
    ack_s = clock() - t0
    prev: Sample | None = None
    while True:
        cur = await poll_status()
        expected_ms = before.curpos_ms + (cur.t - before.t) * 1000
        if is_seek_landed(expected_ms, prev, cur):
            return SeekResult(ack_s=ack_s, latency_s=cur.t - t0, landed_curpos_ms=cur.curpos_ms)
        if clock() - t0 > timeout_s:
            return SeekResult(ack_s=ack_s, latency_s=None, landed_curpos_ms=None)
        prev = cur
        await sleep(poll_interval_s)


class MaApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"MA API returned HTTP {status}: {body}")
        self.status = status
        self.body = body


class MaClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def command(self, command: str, **args: Any) -> Any:
        payload = {"command": command, "args": args}
        async with self._session.post(self._url, headers=self._headers, json=payload) as resp:
            if resp.status != 200:
                raise MaApiError(resp.status, (await resp.text())[:300])
            return await resp.json(content_type=None)


async def poll_wiim(session: aiohttp.ClientSession, url: str) -> Sample:
    async with session.get(url, ssl=False) as resp:
        text = await resp.text()
        t = time.monotonic()
    return parse_status(text, t)


class Abort(Exception):
    """A precondition failed; the run ends with exit code 2."""


def _fmt_ms(seconds: float | None) -> str:
    return "TIMEOUT" if seconds is None else f"{seconds * 1000:.0f} ms"


def _skip_line(n: int, before: Sample, result: SkipResult) -> str:
    after = decode_hex(result.new_sig[0]) if result.new_sig else "?"
    return (
        f"skip {n}: {decode_hex(before.sig[0])} -> {after}"
        f"  ack {_fmt_ms(result.ack_s)}  latency {_fmt_ms(result.latency_s)}"
    )


def _seek_line(result: SeekResult) -> str:
    landed = "n/a" if result.landed_curpos_ms is None else f"{result.landed_curpos_ms} ms"
    return (
        f"seek: ack {_fmt_ms(result.ack_s)}  latency {_fmt_ms(result.latency_s)}"
        f"  landed at {landed}"
    )


async def _check_queue(client: MaClient, args: argparse.Namespace) -> None:
    queue = await client.command("player_queues/get", queue_id=args.queue_id)
    if queue["state"] != "playing":
        raise Abort(f"queue state is {queue['state']!r}, expected 'playing'")
    left = queue["items"] - queue["current_index"] - 1
    if left < args.skips:
        raise Abort(f"only {left} tracks left in the queue, need {args.skips}")


async def _skip_phase(
    client: MaClient, poll: PollFn, args: argparse.Namespace
) -> list[SkipResult]:
    async def send_next() -> None:
        await client.command("player_queues/next", queue_id=args.queue_id)

    results: list[SkipResult] = []
    for n in range(1, args.skips + 1):
        await asyncio.sleep(args.settle)
        before = await poll()
        if before.status != "play":
            raise Abort(f"WiiM status is {before.status!r} before skip {n}, expected 'play'")
        result = await measure_skip(
            send_next,
            poll,
            time.monotonic,
            asyncio.sleep,
            before.sig,
            args.timeout,
            args.poll_interval,
        )
        print(_skip_line(n, before, result))
        results.append(result)
    return results


async def _wait_for_position(
    poll: PollFn, min_ms: int = 8000, give_up_s: float = 30.0, interval_s: float = 0.5
) -> Sample | None:
    deadline = time.monotonic() + give_up_s
    while True:
        sample = await poll()
        if sample.curpos_ms >= min_ms:
            return sample
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(interval_s)


async def _seek_phase(
    client: MaClient, poll: PollFn, args: argparse.Namespace
) -> SeekResult | None:
    await asyncio.sleep(args.settle)
    before = await _wait_for_position(poll)
    if before is None:
        print("seek skipped: position did not reach 8000 ms within 30 s")
        return None
    queue = await client.command("player_queues/get", queue_id=args.queue_id)
    p = round(before.curpos_ms / 1000)
    duration = queue["current_item"]["duration"]
    if duration < p + 80:
        print(f"seek skipped: track too short (duration {duration} s, position {p} s)")
        return None

    async def send_seek() -> None:
        await client.command("player_queues/seek", queue_id=args.queue_id, position=p + 60)

    result = await measure_seek(
        send_seek, poll, time.monotonic, asyncio.sleep, before, args.timeout, args.poll_interval
    )
    print(_seek_line(result))
    return result


def _summarize_ms(values_s: list[float]) -> dict[str, float] | None:
    return summarize([v * 1000 for v in values_s]) if values_s else None


def _stats_line(label: str, stats: dict[str, float] | None) -> str:
    if stats is None:
        return f"{label}: no data"
    return (
        f"{label}: n={stats['count']:.0f}  median {stats['median']:.0f} ms"
        f"  worst {stats['worst']:.0f} ms  best {stats['best']:.0f} ms"
    )


def _summary(skips: list[SkipResult]) -> dict[str, Any]:
    done = [r for r in skips if r.latency_s is not None]
    return {
        "skip_latency_ms": _summarize_ms([r.latency_s for r in done if r.latency_s is not None]),
        "ack_ms": _summarize_ms([r.ack_s for r in done]),
        "skip_timeouts": len(skips) - len(done),
    }


def _report(
    args: argparse.Namespace, skips: list[SkipResult], seek: SeekResult | None
) -> int:
    summary = _summary(skips)
    print()
    print(_stats_line("skip latency", summary["skip_latency_ms"]))
    print(_stats_line("skip ack", summary["ack_ms"]))
    print(f"skip timeouts: {summary['skip_timeouts']}")
    if seek is not None:
        print(_seek_line(seek))
    if args.json_out:
        payload = {
            "skips": [asdict(r) for r in skips],
            "seek": asdict(seek) if seek is not None else None,
            "summary": summary,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
    seek_timed_out = seek is not None and seek.latency_s is None
    return 1 if summary["skip_timeouts"] or seek_timed_out else 0


async def run(args: argparse.Namespace) -> int:
    token = Path(args.token_file).expanduser().read_text().strip()
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = MaClient(session, args.ma_url, token)
        poll = functools.partial(poll_wiim, session, args.wiim_url)
        try:
            await _check_queue(client, args)
            skips = await _skip_phase(client, poll, args)
            seek = None if args.no_seek else await _seek_phase(client, poll, args)
        except Abort as exc:
            print(f"abort: {exc}", file=sys.stderr)
            return 2
    return _report(args, skips, seek)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skips", type=int, default=10)
    parser.add_argument("--settle", type=float, default=6.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--poll-interval", type=float, default=0.05)
    parser.add_argument("--no-seek", action="store_true")
    parser.add_argument("--json-out", metavar="PATH", default=None)
    parser.add_argument("--ma-url", default=DEFAULT_MA_URL)
    parser.add_argument("--queue-id", default=DEFAULT_QUEUE_ID)
    parser.add_argument(
        "--wiim-url",
        default=os.environ.get("BRIDGE_STATUS_URL") or None,
        help="status API URL (env BRIDGE_STATUS_URL, required)",
    )
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.wiim_url:
        parser.error("set --wiim-url or BRIDGE_STATUS_URL")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
