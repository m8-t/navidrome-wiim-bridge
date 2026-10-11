"""Control a WiiM amplifier over UPnP (AVTransport and PlayQueue) and record its state."""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.soap import UpnpError, build_didl  # noqa: E402
from bridge.wiim import (  # noqa: E402
    REQUEST_ERRORS,
    DeviceState,
    Wiim,
    build_queue_context,
    describe_change,
    device_urls,
    open_sessions,
    parse_queue_tracks,
    redact_uri,
    tracks_from_album,
)

DEFAULT_PROXY_INFO = Path(__file__).resolve().parent.parent / "results" / "proxy_info.json"


def error_message(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def default_item_id(url: str) -> str:
    segment = urlsplit(url).path.rsplit("/", 1)[-1]
    stem, _, _ = segment.rpartition(".")
    return stem or segment


def _metadata_for(args: argparse.Namespace) -> str:
    if args.no_metadata:
        return ""
    return build_didl(
        args.url,
        args.title,
        args.artist,
        args.album,
        args.duration,
        args.item_id if args.item_id is not None else default_item_id(args.url),
        args.art_url,
    )


Handler = Callable[["Wiim", argparse.Namespace], Awaitable[dict[str, Any]]]


async def cmd_state(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    state = await wiim.state()
    position = await wiim.position()
    return {
        "transport": state.transport,
        "current_uri": redact_uri(state.current_uri),
        "next_uri": redact_uri(state.next_uri),
        "api_status": state.api_status,
        "curpos_ms": state.curpos_ms,
        "queue_index": state.queue_index,
        "volume": state.volume,
        "muted": state.muted,
        **position,
    }


async def cmd_play(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    metadata = _metadata_for(args)
    t0 = time.time()
    await wiim.set_uri(args.url, metadata)
    t1 = time.time()
    await wiim.play()
    t2 = time.time()
    return {
        "action": "play",
        "t_start": t0,
        "set_uri_ms": (t1 - t0) * 1000,
        "play_ms": (t2 - t1) * 1000,
    }


async def cmd_set_next(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    metadata = _metadata_for(args)
    t0 = time.time()
    await wiim.set_next(args.url, metadata)
    t1 = time.time()
    return {"action": "set-next", "t_start": t0, "ms": (t1 - t0) * 1000}


def _simple_command(name: str, method: str) -> Handler:
    async def run(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
        t0 = time.time()
        await getattr(wiim, method)()
        t1 = time.time()
        return {"action": name, "t_start": t0, "ms": (t1 - t0) * 1000}

    return run


async def cmd_volume(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.time()
    await wiim.set_volume(args.set)
    t1 = time.time()
    return {"action": "volume", "volume": args.set, "t_start": t0, "ms": (t1 - t0) * 1000}


async def cmd_mute(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.time()
    await wiim.set_mute(args.on)
    t1 = time.time()
    return {"action": "mute", "muted": args.on, "t_start": t0, "ms": (t1 - t0) * 1000}


async def cmd_seek(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.time()
    await wiim.seek(args.seconds)
    t1 = time.time()
    return {"action": "seek", "seconds": args.seconds, "t_start": t0, "ms": (t1 - t0) * 1000}


class InputError(Exception):
    """Bad local input (album or proxy info file); reported with exit code 2."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"{path}: {exc}") from exc


def _resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url is not None:
        return str(args.base_url)
    info = _read_json(Path(args.proxy_info))
    base_url = info.get("base_url") if isinstance(info, dict) else None
    if not isinstance(base_url, str) or not base_url:
        raise InputError(f"{args.proxy_info}: no base_url")
    return base_url


def queue_context_from_args(args: argparse.Namespace) -> tuple[str, int]:
    album = _read_json(Path(args.album_file))
    base_url = _resolve_base_url(args)
    try:
        tracks = tracks_from_album(album, base_url, args.profile)
        return build_queue_context(args.name, tracks), len(tracks)
    except ValueError as exc:
        raise InputError(str(exc)) from exc


async def cmd_queue_create(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    context, count = queue_context_from_args(args)
    t0 = time.time()
    await wiim.queue_create(context)
    t1 = time.time()
    return {
        "action": "queue-create",
        "name": args.name,
        "tracks": count,
        "t_start": t0,
        "ms": (t1 - t0) * 1000,
    }


async def cmd_queue_play(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.time()
    await wiim.queue_play(args.name, args.index)
    t1 = time.time()
    return {
        "action": "queue-play",
        "name": args.name,
        "index": args.index,
        "t_start": t0,
        "ms": (t1 - t0) * 1000,
    }


async def cmd_queue_index(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    return dict(await wiim.queue_index(args.name))


async def cmd_queue_browse(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    context = await wiim.queue_browse(args.name)
    tracks = parse_queue_tracks(context) if context is not None else []
    return {
        "name": args.name,
        "tracks": [{**track, "url": redact_uri(track["url"])} for track in tracks],
    }


async def cmd_queue_delete(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    t0 = time.time()
    await wiim.queue_delete(args.name)
    t1 = time.time()
    return {"action": "queue-delete", "name": args.name, "t_start": t0, "ms": (t1 - t0) * 1000}


async def cmd_queue_loop_mode(wiim: Wiim, args: argparse.Namespace) -> dict[str, Any]:
    if args.set_mode is not None:
        await wiim.queue_set_loop_mode(args.set_mode)
    return {"loop_mode": await wiim.queue_loop_mode()}


def _iso(t: float) -> str:
    stamp = datetime.fromtimestamp(t, tz=timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_line(handle: Any, record: dict[str, Any]) -> None:
    handle.write(json.dumps(record) + "\n")
    handle.flush()


async def cmd_watch(wiim: Wiim, args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.seconds
    prev: DeviceState | None = None
    samples = errors = 0
    with out.open("a", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            samples += 1
            try:
                cur = await wiim.state()
            except (UpnpError, *REQUEST_ERRORS) as exc:
                errors += 1
                now = time.time()
                _write_line(handle, {"t": now, "iso": _iso(now), "error": error_message(exc)})
            else:
                _record_sample(handle, prev, cur)
                prev = cur
            await asyncio.sleep(args.interval)
    print(f"samples={samples} errors={errors}")
    return 0


def _record_sample(handle: Any, prev: DeviceState | None, cur: DeviceState) -> None:
    iso = _iso(cur.t)
    _write_line(
        handle,
        {
            "t": cur.t,
            "iso": iso,
            "transport": cur.transport,
            "current_uri": redact_uri(cur.current_uri),
            "next_uri": redact_uri(cur.next_uri),
            "api_status": cur.api_status,
            "curpos_ms": cur.curpos_ms,
            "queue_index": cur.queue_index,
            "volume": cur.volume,
        },
    )
    change = describe_change(prev, cur)
    if change is not None:
        print(
            f"{iso} {change} | transport={cur.transport} "
            f"api={cur.api_status} curpos={cur.curpos_ms} queue={cur.queue_index}",
            flush=True,
        )


COMMANDS: dict[str, Handler] = {
    "state": cmd_state,
    "play": cmd_play,
    "set-next": cmd_set_next,
    "stop": _simple_command("stop", "stop"),
    "pause": _simple_command("pause", "pause"),
    "resume": _simple_command("resume", "play"),
    "next": _simple_command("next", "next"),
    "previous": _simple_command("previous", "previous"),
    "seek": cmd_seek,
    "volume": cmd_volume,
    "mute": cmd_mute,
    "queue-create": cmd_queue_create,
    "queue-play": cmd_queue_play,
    "queue-index": cmd_queue_index,
    "queue-browse": cmd_queue_browse,
    "queue-delete": cmd_queue_delete,
    "queue-loop-mode": cmd_queue_loop_mode,
}


def _add_track_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--artist", default="")
    parser.add_argument("--album", default="")
    parser.add_argument("--duration", type=int, default=0)
    parser.add_argument("--item-id")
    parser.add_argument("--art-url")
    parser.add_argument("--no-metadata", action="store_true")


def _add_queue_commands(sub: Any) -> None:
    create = sub.add_parser("queue-create", help="create a queue from an album file and play it")
    create.add_argument("--name", required=True)
    create.add_argument("--album-file", required=True)
    create.add_argument("--profile", default="range")
    create.add_argument("--proxy-info", default=str(DEFAULT_PROXY_INFO))
    create.add_argument("--base-url")
    play = sub.add_parser("queue-play", help="play a queue from a 1-based index")
    play.add_argument("--name", required=True)
    play.add_argument("--index", type=int, required=True)
    index = sub.add_parser("queue-index", help="print the current queue position")
    index.add_argument("--name", default="CurrentQueue")
    browse = sub.add_parser("queue-browse", help="list the tracks of a queue")
    browse.add_argument("--name", required=True)
    delete = sub.add_parser("queue-delete", help="delete a queue")
    delete.add_argument("--name", required=True)
    loop = sub.add_parser("queue-loop-mode", help="read or set the queue loop mode")
    loop.add_argument("--set", type=int, dest="set_mode")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control a WiiM amplifier over UPnP.")
    parser.add_argument("--device-host", help="amplifier host or IP (env BRIDGE_DEVICE_HOST)")
    parser.add_argument(
        "--device-base", help="UPnP base URL (env BRIDGE_DEVICE_BASE, default derived from host)"
    )
    parser.add_argument(
        "--status-url", help="status API URL (env BRIDGE_STATUS_URL, default derived from host)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("state", help="print transport, URIs and position")
    _add_track_options(sub.add_parser("play", help="set the URI and start playback"))
    _add_track_options(sub.add_parser("set-next", help="queue the next URI"))
    for name in ("stop", "pause", "resume", "next", "previous"):
        sub.add_parser(name)
    seek = sub.add_parser("seek", help="seek to an absolute position")
    seek.add_argument("--seconds", type=int, required=True)
    volume = sub.add_parser("volume", help="set the device volume")
    volume.add_argument("--set", type=int, required=True)
    mute = sub.add_parser("mute", help="mute or unmute the device")
    group = mute.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", dest="on", action="store_true")
    group.add_argument("--off", dest="on", action="store_false")
    watch = sub.add_parser("watch", help="record device state to a JSON lines file")
    watch.add_argument("--seconds", type=int, required=True)
    watch.add_argument("--out", required=True)
    watch.add_argument("--interval", type=float, default=0.1)
    _add_queue_commands(sub)
    return parser


async def run(args: argparse.Namespace) -> int:
    async with open_sessions(5) as (soap_session, status_session):
        wiim = Wiim(soap_session, status_session, args.device_base, args.status_url)
        if args.command == "watch":
            return await cmd_watch(wiim, args)
        result = await COMMANDS[args.command](wiim, args)
    print(json.dumps(result))
    return 0


def resolve_device(args: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """Fill args.device_base and args.status_url from options, environment or the host."""
    host = args.device_host or environ.get("BRIDGE_DEVICE_HOST") or None
    base = args.device_base or environ.get("BRIDGE_DEVICE_BASE") or None
    status = args.status_url or environ.get("BRIDGE_STATUS_URL") or None
    if host is not None:
        try:
            derived_base, derived_status = device_urls(host)
        except ValueError as exc:
            raise InputError(str(exc)) from None
        base, status = base or derived_base, status or derived_status
    elif base is None or status is None:
        raise InputError("set BRIDGE_DEVICE_HOST (or --device-host) to the amplifier's address")
    args.device_base, args.status_url = base, status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        resolve_device(args, os.environ)
        return asyncio.run(run(args))
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (UpnpError, *REQUEST_ERRORS) as exc:
        print(f"error: {error_message(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
