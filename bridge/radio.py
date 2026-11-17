"""Optional radio: an endless shuffled mix that Liquidsoap pulls track by track."""

import asyncio
import hmac
import json
import logging
import math
import os
import random
import re
import time
import unicodedata
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import web

from bridge import proxy
from bridge.log import clean
from bridge.subsonic import AuthResult

logger = logging.getLogger("bridge.radio")

Song = dict[str, Any]
Rng = Callable[[], float]

JUNK_GENRE_RE = re.compile(r"(?:\d+|divers|other|unknown|misc|none)?", re.ASCII)

# first match wins, so the specific hip hop buckets come before R&B, Rock and Pop
GENRE_BUCKETS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.ASCII))
    for name, pattern in (
        ("Deutschrap", r"deutsch|german (?:hip hop|rap)"),
        ("Gangsta", r"gangsta|gangster|thug|g funk|gfunk|mafioso|street rap"),
        (
            "Hip Hop",
            r"hip hop|hiphop|\brap\b|\btrap\b|boom bap|crunk|dirty south|east coast|west coast"
            r"|\bdrill\b|grime|conscious|turntablism|\bscrewed?\b|horrorcore",
        ),
        ("Funk", r"^(?!.*\b(?:rock|metal|punk)\b).*\bfunk\b"),
        ("R&B / Soul", r"\br b\b|\brnb\b|rhythm and blues|\bsoul\b|motown"),
        ("Rock", r"\brock\b|\bmetal\b|punk|grunge|hardcore|\bfunk\b"),
        ("Pop", r"\bk?pop\b"),
        (
            "Electronic",
            r"electr|\bhouse\b|techno|\bdance\b|\bedm\b|bass music|dubstep|drum (?:and|n) bass"
            r"|\bdnb\b|trance|ambient",
        ),
        ("Jazz", r"jazz|blues|swing|bossa"),
        (
            "Soundtrack",
            r"soundtrack|\bscore\b|\bfilms?\b|\bgames?\b|musiques? de films?|jeux vid"
            r"|stage (?:and )?screen",
        ),
        ("Instrumental", r"\binstrumental\b"),
        ("World / Reggae", r"reggae|dancehall|latin|world|afro|folk|country|caribbean"),
        ("Other", r"."),
    )
)
BUCKET_NAMES = tuple(name for name, _ in GENRE_BUCKETS)

# [^\W_] is a letter or number, the counterpart of \p{L}\p{N}
_NOT_ALNUM_RE = re.compile(r"[\W_]+")
_LB = r"(?<![^\W_])"
_LA = r"(?![^\W_])"
ACAPELLA_RE = re.compile(rf"{_LB}a[ -]?cap+ellas?{_LA}", re.IGNORECASE)
KEYWORD_RE = re.compile(
    rf"{_LB}(?:intro|outro|skit|interlude|prelude|postlude|intermission|commercial|interview"
    rf"|zwischenspiel)s?{_LA}",
    re.IGNORECASE,
)
INSTRUMENTAL_RE = re.compile(rf"{_LB}instrumentals?{_LA}", re.IGNORECASE)

MIX_MIN_SECONDS = 60
MIX_KEYWORD_MAX_SECONDS = 90
MIX_EXTRA_ROUNDS = 3
NEXT_WAIT_S = 8.0


def normalize_genre(value: Any) -> str:
    text = "" if value is None else str(value)
    return _NOT_ALNUM_RE.sub(" ", text.lower()).strip()


def bucket_of(raw: Any) -> str | None:
    text = normalize_genre(raw)
    if JUNK_GENRE_RE.fullmatch(text):
        return None
    return next(name for name, pattern in GENRE_BUCKETS if pattern.search(text))


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def bucket_genres(raw_genres: Sequence[Any] | None) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for entry in raw_genres or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("value"), str):
            continue
        name = bucket_of(entry["value"])
        if name is None:
            continue
        groups.setdefault(name, []).append(entry)
    out = []
    for name in BUCKET_NAMES:
        if name not in groups:
            continue
        entries = sorted(groups[name], key=lambda e: -_number(e.get("songCount")))
        out.append(
            {
                "name": name,
                "genres": [e["value"] for e in entries],
                "songCount": sum(_number(e.get("songCount")) for e in entries),
            }
        )
    out.sort(key=lambda b: (b["name"] == "Other", -b["songCount"]))
    for bucket in out:
        bucket["songCount"] = _plain(bucket["songCount"])
    return out


def _plain(value: float) -> int | float:
    return int(value) if value == int(value) else value


def bucket_fetch_genres(bucket: Mapping[str, Any], max_genres: int = 8) -> list[str]:
    return list(bucket.get("genres") or [])[: max(0, max_genres)]


def is_interlude(song: Any) -> bool:
    title = song.get("title") if isinstance(song, Mapping) else None
    if not isinstance(title, str):
        return False
    if ACAPELLA_RE.search(title):
        return True
    raw = song.get("duration")
    duration = (
        float(raw)
        if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw)
        else None
    )
    if duration is not None and duration < MIX_MIN_SECONDS:
        return True
    if not KEYWORD_RE.search(title):
        return False
    return duration is None or duration < MIX_KEYWORD_MAX_SECONDS


def is_instrumental(song: Any) -> bool:
    title = song.get("title") if isinstance(song, Mapping) else None
    return isinstance(title, str) and INSTRUMENTAL_RE.search(title) is not None


def merge_random(lists: Sequence[Sequence[Song]], count: int, rng: Rng = random.random) -> list[Song]:
    def shuffle(items: list[Song]) -> list[Song]:
        for i in range(len(items) - 1, 0, -1):
            j = math.floor(rng() * (i + 1))
            items[i], items[j] = items[j], items[i]
        return items

    seen: set[Any] = set()
    pools = []
    for items in lists:
        fresh = []
        for song in items:
            key = song.get("id")
            if key in seen:
                continue
            seen.add(key)
            fresh.append(song)
        pools.append(shuffle(fresh))
    limit = max(0, count)
    picked: list[Song] = []
    # round robin keeps one large list from crowding out the others
    round_no = 0
    while len(picked) < limit:
        any_left = False
        for pool in pools:
            if round_no >= len(pool):
                continue
            any_left = True
            if len(picked) < limit:
                picked.append(pool[round_no])
        if not any_left:
            break
        round_no += 1
    return shuffle(picked)


async def collect_mix(
    fetch_round: Callable[[], Awaitable[Sequence[Sequence[Song]]]],
    count: int,
    skip_interludes: bool = False,
    skip_instrumentals: bool = False,
    rng: Rng = random.random,
) -> tuple[list[Song], bool]:
    first = await fetch_round()
    if not skip_interludes and not skip_instrumentals:
        return merge_random(first, count, rng), False
    seen: set[Any] = set()
    kept: list[list[Song]] = []
    total = 0

    def take(lists: Sequence[Sequence[Song]]) -> int:
        nonlocal total
        fresh = 0
        for i, items in enumerate(lists):
            while len(kept) <= i:
                kept.append([])
            for song in items:
                key = song.get("id")
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                if not (skip_interludes and is_interlude(song)) and not (
                    skip_instrumentals and is_instrumental(song)
                ):
                    kept[i].append(song)
                    total += 1
        return fresh

    take(first)
    for _ in range(MIX_EXTRA_ROUNDS):
        if total >= count:
            break
        if not take(await fetch_round()):
            break
    return merge_random(kept, count, rng), total < count


@dataclass(frozen=True)
class RadioSettings:
    buckets: tuple[str, ...] = ()
    skip_interludes: bool = True
    skip_instrumentals: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "buckets": list(self.buckets),
            "skipInterludes": self.skip_interludes,
            "skipInstrumentals": self.skip_instrumentals,
        }


class SettingsError(ValueError):
    """Raised for settings that fail validation."""


def parse_settings(data: Any, base: RadioSettings, known: Sequence[str] | None) -> RadioSettings:
    """Merge a JSON object into base; known restricts bucket names, None accepts any."""
    if not isinstance(data, dict):
        raise SettingsError("settings must be a JSON object")
    buckets = base.buckets
    if "buckets" in data:
        raw = data["buckets"]
        if not isinstance(raw, list) or not all(isinstance(b, str) for b in raw):
            raise SettingsError("buckets must be a list of names")
        if known is not None:
            for name in raw:
                if name not in known:
                    raise SettingsError(f"unknown bucket: {name}")
        buckets = tuple(dict.fromkeys(raw))
    flags = {}
    for key, current in (
        ("skipInterludes", base.skip_interludes),
        ("skipInstrumentals", base.skip_instrumentals),
    ):
        value = data.get(key, current)
        if not isinstance(value, bool):
            raise SettingsError(f"{key} must be true or false")
        flags[key] = value
    return RadioSettings(buckets, flags["skipInterludes"], flags["skipInstrumentals"])


def load_settings(path: Path | None) -> RadioSettings:
    defaults = RadioSettings()
    if path is None:
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_settings(data, defaults, None)
    except FileNotFoundError:
        logger.warning("radio settings file %s not found, using defaults", path)
    except (OSError, ValueError) as exc:
        logger.warning("radio settings file %s is unusable (%s), using defaults", path, exc)
    return defaults


def save_settings(path: Path, settings: RadioSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(settings.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class RadioError(Exception):
    """Raised when no song can be fetched."""


class Radio:
    def __init__(
        self,
        fetch_random: Callable[[int, str | None], Awaitable[list[Song]]],
        fetch_genres: Callable[[], Awaitable[list[Any]]],
        state_path: Path | None = None,
        *,
        rng: Rng = random.random,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        batch: int = 50,
        low_water: int = 10,
        history: int = 300,
        grant_ttl: float = 6 * 3600,
        genres_ttl: float = 600,
        wait: float | None = None,
    ) -> None:
        self._fetch_random = fetch_random
        self._fetch_genres = fetch_genres
        self._state_path = state_path
        self._rng = rng
        self._clock = clock
        self._sleep = sleep
        self._batch = batch
        self._low_water = low_water
        self._history_size = history
        self._grant_ttl = grant_ttl
        self._genres_ttl = genres_ttl
        self._wait = NEXT_WAIT_S if wait is None else wait
        self.settings = load_settings(state_path)
        self.now_playing: dict[str, Any] | None = None
        self._buffer: deque[Song] = deque()
        self._history: deque[str] = deque()
        self._recent: Counter[str] = Counter()
        self._granted: dict[str, tuple[float, str]] = {}
        self._generation = 0
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._genres: tuple[float, dict[str, list[str]], list[dict[str, Any]]] | None = None

    def start(self) -> None:
        self._kick()

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _kick(self) -> None:
        if len(self._buffer) >= self._low_water:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._fill_loop())

    async def _fill_loop(self) -> None:
        failures = 0
        while len(self._buffer) < self._low_water:
            if await self._refill_once():
                failures = 0
                continue
            failures += 1
            await self._sleep(min(2**failures, 60))

    async def _buckets(self) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
        cached = self._genres
        if cached is not None and self._clock() - cached[0] < self._genres_ttl:
            return cached[1], cached[2]
        try:
            buckets = bucket_genres(await self._fetch_genres())
        except Exception:
            if cached is None:
                raise
            logger.warning("could not refresh the genre list, using the previous one")
            return cached[1], cached[2]
        table = {b["name"]: b["genres"] for b in buckets}
        self._genres = (self._clock(), table, buckets)
        return table, buckets

    async def available_buckets(self) -> list[dict[str, Any]]:
        try:
            _, buckets = await self._buckets()
        except Exception as exc:
            logger.warning("could not load the genre list: %s", type(exc).__name__)
            return []
        return [{"name": b["name"], "songCount": b["songCount"]} for b in buckets]

    async def _genre_targets(self, settings: RadioSettings) -> list[str | None]:
        if not settings.buckets:
            return [None]
        table, _ = await self._buckets()
        targets = [
            genre
            for name in settings.buckets
            if name in table
            for genre in bucket_fetch_genres({"genres": table[name]})
        ]
        if not targets:
            raise RadioError("none of the selected buckets exist in the library")
        return targets

    async def _refill_once(self) -> bool:
        generation = self._generation
        settings = self.settings
        try:
            targets = await self._genre_targets(settings)

            async def fetch_round() -> list[list[Song]]:
                return list(
                    await asyncio.gather(*(self._fetch_random(self._batch, g) for g in targets))
                )

            songs, _ = await collect_mix(
                fetch_round,
                self._batch,
                settings.skip_interludes,
                settings.skip_instrumentals,
                self._rng,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("radio refill failed: %s", str(exc) or type(exc).__name__)
            return False
        if generation != self._generation:
            return True
        queued = {s.get("id") for s in self._buffer}
        fresh = [s for s in songs if s.get("id") not in self._recent and s.get("id") not in queued]
        if not fresh:
            # library too small to avoid repeats: allow them rather than stall
            fresh = [s for s in songs if s.get("id") not in queued]
        if not fresh:
            logger.warning("radio refill returned no new songs")
            return False
        self._buffer.extend(fresh)
        self._ready.set()
        return True

    async def next_song(self) -> Song | None:
        if not self._buffer:
            self._ready.clear()
            self._kick()
            try:
                await asyncio.wait_for(self._ready.wait(), self._wait)
            except TimeoutError:
                return None
        if not self._buffer:
            return None
        song = self._buffer.popleft()
        self._remember(song)
        self._kick()
        return song

    def _remember(self, song: Song) -> None:
        song_id = str(song.get("id"))
        if len(self._history) >= self._history_size:
            old = self._history.popleft()
            self._recent[old] -= 1
            if self._recent[old] <= 0:
                del self._recent[old]
        self._history.append(song_id)
        self._recent[song_id] += 1
        now = self._clock()
        for key in [k for k, (at, _) in self._granted.items() if now - at > self._grant_ttl]:
            del self._granted[key]
        self._granted.pop(song_id, None)
        self._granted[song_id] = (now, str(song.get("title") or song_id))
        while len(self._granted) > 5000:
            del self._granted[next(iter(self._granted))]
        self.now_playing = {
            "id": song_id,
            "title": str(song.get("title") or ""),
            "artist": str(song.get("artist") or ""),
            "album": str(song.get("album") or ""),
        }

    def granted_label(self, song_id: str) -> str | None:
        entry = self._granted.get(song_id)
        if entry is None or self._clock() - entry[0] > self._grant_ttl:
            return None
        return entry[1]

    async def update(self, settings: RadioSettings) -> None:
        self.settings = settings
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._buffer.clear()
        self._ready.clear()
        if self._state_path is not None:
            try:
                save_settings(self._state_path, settings)
            except OSError as exc:
                logger.warning("could not save radio settings to %s: %s", self._state_path, exc)
        self._kick()


def annotate_text(value: str) -> str:
    text = "".join(
        " " if unicodedata.category(c) == "Cc" or c in "\u2028\u2029" else c for c in value
    )
    text = text.replace("#{", "# {")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def annotate_line(song: Song, url: str) -> str:
    fields = ",".join(
        f'{key}="{annotate_text(str(song.get(key) or ""))}"' for key in ("title", "artist", "album")
    )
    return f"annotate:{fields}:{url}"


Authenticator = Callable[[Mapping[str, str]], Awaitable[AuthResult]]


@dataclass(frozen=True)
class RadioConfig:
    token: str
    audio_format: str = "raw"
    stream_url: str | None = None


def token_ok(request: web.Request, token: str) -> bool:
    given = []
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        given.append(value.strip())
    if "token" in request.query:
        given.append(request.query["token"])
    expected = token.encode()
    return any(hmac.compare_digest(g.encode(), expected) for g in given)


_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def add_radio_routes(
    app: web.Application, config: RadioConfig, radio: Radio, authenticate: Authenticator
) -> None:
    def unauthorized() -> web.Response:
        return web.Response(status=401, text="unauthorized", headers={"WWW-Authenticate": "Bearer"})

    async def next_track(request: web.Request) -> web.Response:
        if not token_ok(request, config.token):
            return unauthorized()
        song = await radio.next_song()
        if song is None:
            return web.Response(status=503, text="no songs available")
        url = (
            f"{request.scheme}://{request.host}/radio/track/"
            f"{quote(str(song['id']), safe='')}?token={quote(config.token, safe='')}"
        )
        logger.info('radio next: "%s"', clean(str(song.get("title") or song["id"])))
        return web.Response(text=annotate_line(song, url), content_type="text/plain")

    async def track(request: web.Request) -> web.StreamResponse:
        if not token_ok(request, config.token):
            return unauthorized()
        song_id = request.match_info["id"]
        label = radio.granted_label(song_id) if _ID_RE.fullmatch(song_id) else None
        if label is None:
            return web.Response(status=404, text="not found")
        info = proxy.RequestInfo(request.method, song_id, request.headers.get("Range"))
        outcome = proxy.Outcome()
        ref = f'radio track "{clean(label)}"'
        try:
            return await proxy.serve(request, info, outcome, config.audio_format)
        finally:
            proxy.log_end(ref, outcome)

    async def login(request: web.Request) -> web.Response | None:
        verdict = await authenticate(dict(request.query))
        if verdict is AuthResult.REJECTED:
            return web.json_response({"error": "wrong username or password"}, status=401)
        if verdict is AuthResult.UNREACHABLE:
            return web.json_response({"error": "Navidrome is unreachable"}, status=503)
        return None

    async def state() -> web.Response:
        return web.json_response(
            {
                "settings": radio.settings.to_json(),
                "buckets": await radio.available_buckets(),
                "nowPlaying": radio.now_playing,
                "streamUrl": config.stream_url,
            }
        )

    async def get_settings(request: web.Request) -> web.Response:
        return await login(request) or await state()

    async def put_settings(request: web.Request) -> web.Response:
        denied = await login(request)
        if denied is not None:
            return denied
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "body must be JSON"}, status=400)
        try:
            settings = parse_settings(data, radio.settings, BUCKET_NAMES)
        except SettingsError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if settings != radio.settings:
            await radio.update(settings)
        return await state()

    app.router.add_get("/radio/next", next_track)
    app.router.add_get("/radio/track/{id}", track)
    app.router.add_get("/radio/settings", get_settings)
    app.router.add_put("/radio/settings", put_settings)
