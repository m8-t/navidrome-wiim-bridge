import { md5 } from "./md5.js";

export function makeAuth(user, password, salt) {
  return { u: user, t: md5(password + salt), s: salt };
}

export function apiParams(auth) {
  return { u: auth.u, t: auth.t, s: auth.s, v: "1.16.1", c: "wiim-remote", f: "json" };
}

export function formatTime(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return "0:00";
  const total = Math.floor(n);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function previousTarget(index, positionSeconds) {
  if (positionSeconds > 3 || index <= 0) return { index, offset: 0 };
  return { index: index - 1, offset: 0 };
}

export function nextTarget(index, length, repeat) {
  if (!(length > 0)) return null;
  if (index + 1 >= length) return repeat === "all" ? 0 : null;
  return index + 1;
}

const REPEAT_MODES = ["off", "all", "one"];

export function normalizeRepeat(value) {
  return REPEAT_MODES.includes(value) ? value : "off";
}

export function nextRepeat(mode) {
  return REPEAT_MODES[(REPEAT_MODES.indexOf(normalizeRepeat(mode)) + 1) % REPEAT_MODES.length];
}

const FLOW_RUBBER = 0.3;
const FLOW_FLING_SEC = 0.2;
const FLOW_ANGLE = 55;

const clampIndex = (value, length) => Math.min(Math.max(value, 0), length - 1) || 0;
const finiteOr = (value, fallback) => (Number.isFinite(value) ? value : fallback);

export function flowTileStyle(offset) {
  const o = finiteOr(offset, 0);
  const a = Math.abs(o);
  const side = Math.sign(o);
  let opacity = 1;
  if (a > 3) opacity = Math.max(0, 0.5 - (a - 3));
  else if (a > 1) opacity = 1 - 0.25 * (a - 1);
  return {
    x: side * (Math.min(a, 1) + 0.5 * Math.max(a - 1, 0)) || 0,
    rotateY: -side * FLOW_ANGLE * Math.min(a, 1) || 0,
    scale: 1 - 0.25 * Math.min(a, 1) - 0.05 * Math.max(a - 1, 0),
    z: Math.round(100 - a * 10),
    opacity,
  };
}

export function flowPosition(index, dx, spacing, length) {
  if (!(length > 0)) return 0;
  const last = length - 1;
  const base = clampIndex(finiteOr(index, 0), length);
  if (!(spacing > 0) || !Number.isFinite(dx)) return base;
  const raw = base - dx / spacing;
  if (raw < 0) return raw * FLOW_RUBBER;
  if (raw > last) return last + (raw - last) * FLOW_RUBBER;
  return raw;
}

export function flowVelocity(samples, now, windowMs = 100) {
  const last = samples[samples.length - 1];
  if (!last || !(now - last.t <= windowMs)) return 0;
  const first = samples.find((s) => last.t - s.t <= windowMs);
  const dt = last.t - first.t;
  if (!(dt > 0)) return 0;
  return finiteOr(((last.p - first.p) / dt) * 1000, 0);
}

export function flowTarget(position, velocity, length) {
  if (!(length > 0)) return 0;
  const projected = finiteOr(position, 0) + finiteOr(velocity, 0) * FLOW_FLING_SEC;
  return clampIndex(Math.round(projected), length);
}

export function nextPage(state, receivedCount, pageSize) {
  const received = Number.isFinite(receivedCount) && receivedCount > 0 ? receivedCount : 0;
  return { offset: state.offset + received, done: received < pageSize };
}

export function chunk(ids, size = 50) {
  if (!(size >= 1)) throw new RangeError("size must be at least 1");
  const out = [];
  for (let i = 0; i < ids.length; i += size) out.push(ids.slice(i, i + size));
  return out;
}

export function playCalls(ids, startIndex) {
  if (ids.length === 0) return [];
  const [first, ...rest] = chunk(ids);
  return [
    { action: "set", ids: first },
    ...rest.map((part) => ({ action: "add", ids: part })),
    { action: "skip", index: startIndex, offset: 0 },
    { action: "start" },
  ];
}

export function mixCalls(ids) {
  if (ids.length === 0) return [];
  const [first, ...rest] = chunk(ids);
  return [
    { action: "set", ids: first },
    { action: "skip", index: 0, offset: 0 },
    { action: "start" },
    ...rest.map((part) => ({ action: "add", ids: part })),
  ];
}

export function addCalls(ids) {
  return chunk(ids).map((part) => ({ action: "add", ids: part }));
}

// Every insert lands right after the current track, so the chunks go in reverse to end up in input order.
export function insertCalls(ids) {
  return chunk(ids).reverse().map((part) => ({ action: "insert", ids: part }));
}

// On an empty queue the server treats insert as add, and later chunks would land inside the first one.
export function playNextCalls(ids, queueLength) {
  return queueLength > 0 ? insertCalls(ids) : addCalls(ids);
}

export function estimatePosition(position, playing, polledAtMs, nowMs, duration) {
  let value = position;
  if (playing) value += Math.max(0, (nowMs - polledAtMs) / 1000);
  if (duration > 0) value = Math.min(value, duration);
  return Math.max(value, 0);
}

// The server sample is only trusted to about a second (network and queueing delay),
// so smaller disagreements are noise; the local clock is the better 1x predictor.
export const SNAP_THRESHOLD_S = 1.5;

export function reconcilePosition({
  shown, reported, playing, wasPlaying, sameTrack, forced, threshold = SNAP_THRESHOLD_S,
}) {
  if (forced || !playing || !wasPlaying || !sameTrack) return reported;
  if (!Number.isFinite(shown) || Math.abs(reported - shown) > threshold) return reported;
  return shown;
}

export function gainToPercent(gain) {
  return Math.min(100, Math.max(0, Math.round(gain * 100)));
}

export function percentToGain(percent) {
  return Math.min(1, Math.max(0, percent / 100));
}

export function unwrap(json) {
  const body = json && json["subsonic-response"];
  if (body && body.status === "ok") return body;
  const err = new Error((body && body.error && body.error.message) || "unexpected reply");
  err.code = body && body.error && body.error.code !== undefined ? body.error.code : null;
  throw err;
}

export function formatDuration(totalSeconds) {
  const n = Number(totalSeconds);
  if (!Number.isFinite(n) || n < 0) return "0 min";
  const minutes = Math.floor(n / 60);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

export function queueSummary(entries) {
  if (!entries || entries.length === 0) return "";
  const total = entries.reduce((sum, e) => sum + (Number(e && e.duration) || 0), 0);
  const noun = entries.length === 1 ? "track" : "tracks";
  return `${entries.length} ${noun}, ${formatDuration(total)}`;
}

export function progressFraction(position, duration) {
  const d = Number(duration);
  if (!(d > 0) || !Number.isFinite(d)) return 0;
  const p = Number(position);
  if (!Number.isFinite(p)) return 0;
  return Math.min(1, Math.max(0, p / d));
}

export function trackArtistIfDifferent(song, albumArtist) {
  const artist = (song && song.artist) || "";
  const norm = (s) => String(s || "").trim().toLowerCase();
  return norm(artist) === norm(albumArtist) ? "" : artist;
}

export function silenceWav(seconds = 10, sampleRate = 8000) {
  const dataSize = seconds * sampleRate;
  const out = new Uint8Array(44 + dataSize);
  const view = new DataView(out.buffer);
  const tag = (offset, text) => {
    for (let i = 0; i < text.length; i++) out[offset + i] = text.charCodeAt(i);
  };
  tag(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  tag(8, "WAVE");
  tag(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate, true);
  view.setUint16(32, 1, true);
  view.setUint16(34, 8, true);
  tag(36, "data");
  view.setUint32(40, dataSize, true);
  out.fill(128, 44);
  return out;
}

export function mediaMetadataInit(song, coverUrl) {
  const s = song || {};
  return {
    title: s.title || "",
    artist: s.artist || "",
    album: s.album || "",
    artwork: s.coverArt ? [{ src: coverUrl(s.coverArt, 512), sizes: "512x512" }] : [],
  };
}

export function positionState(position, duration) {
  const d = Number(duration);
  if (!(d > 0) || !Number.isFinite(d)) return null;
  const p = Number(position);
  const safe = Number.isFinite(p) ? Math.max(0, p) : 0;
  return { duration: d, position: Math.min(safe, d), playbackRate: 1 };
}

const RANDOM_MAX = 500;
const YEAR_MIN = 1850;
const YEAR_MAX = 2100;

function parseYear(value) {
  const text = String(value ?? "").trim();
  if (!/^\d{4}$/.test(text)) return null;
  const year = Number(text);
  return year >= YEAR_MIN && year <= YEAR_MAX ? year : null;
}

export function randomSongParams({ count, fromYear, toYear, genre } = {}) {
  const n = Math.floor(Number(count));
  const params = { size: Number.isFinite(n) && n >= 1 ? Math.min(n, RANDOM_MAX) : RANDOM_MAX };
  let from = parseYear(fromYear);
  let to = parseYear(toYear);
  if (from !== null && to !== null && from > to) [from, to] = [to, from];
  if (from !== null) params.fromYear = from;
  if (to !== null) params.toYear = to;
  if (typeof genre === "string" && genre.trim() !== "") params.genre = genre.trim();
  return params;
}

export function mergeRandom(lists, count, rng = Math.random) {
  const shuffle = (items) => {
    for (let i = items.length - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      [items[i], items[j]] = [items[j], items[i]];
    }
    return items;
  };
  const seen = new Set();
  const pools = lists.map((list) => {
    const fresh = [];
    for (const song of list) {
      if (seen.has(song.id)) continue;
      seen.add(song.id);
      fresh.push(song);
    }
    return shuffle(fresh);
  });
  const limit = Math.max(0, count);
  const picked = [];
  // round robin keeps one large list from crowding out the others
  for (let round = 0; picked.length < limit; round++) {
    let any = false;
    for (const pool of pools) {
      if (round >= pool.length) continue;
      any = true;
      if (picked.length < limit) picked.push(pool[round]);
    }
    if (!any) break;
  }
  return shuffle(picked);
}

const normalizeGenre = (value) =>
  String(value ?? "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

const JUNK_GENRE_RE = /^(?:\d+|divers|other|unknown|misc|none)?$/;

// first match wins, so the specific hip hop buckets come before R&B, Rock and Pop
export const GENRE_BUCKETS = [
  { name: "Deutschrap", test: /deutsch|german (?:hip hop|rap)/ },
  { name: "Gangsta", test: /gangsta|gangster|thug|g funk|gfunk|mafioso|street rap/ },
  {
    name: "Hip Hop",
    test: /hip hop|hiphop|\brap\b|\btrap\b|boom bap|crunk|dirty south|east coast|west coast|\bdrill\b|grime|conscious|turntablism|\bscrewed?\b|horrorcore/,
  },
  { name: "Funk", test: /^(?!.*\b(?:rock|metal|punk)\b).*\bfunk\b/ },
  { name: "R&B / Soul", test: /\br b\b|\brnb\b|rhythm and blues|\bsoul\b|motown/ },
  { name: "Rock", test: /\brock\b|\bmetal\b|punk|grunge|hardcore|\bfunk\b/ },
  { name: "Pop", test: /\bk?pop\b/ },
  {
    name: "Electronic",
    test: /electr|\bhouse\b|techno|\bdance\b|\bedm\b|bass music|dubstep|drum (?:and|n) bass|\bdnb\b|trance|ambient/,
  },
  { name: "Jazz", test: /jazz|blues|swing|bossa/ },
  {
    name: "Soundtrack",
    test: /soundtrack|\bscore\b|\bfilms?\b|\bgames?\b|musiques? de films?|jeux vid|stage (?:and )?screen/,
  },
  { name: "Instrumental", test: /\binstrumental\b/ },
  { name: "World / Reggae", test: /reggae|dancehall|latin|world|afro|folk|country|caribbean/ },
  { name: "Other", test: /./ },
];

function bucketOf(raw) {
  const text = normalizeGenre(raw);
  if (JUNK_GENRE_RE.test(text)) return null;
  return GENRE_BUCKETS.find((bucket) => bucket.test.test(text)).name;
}

const bySongCount = (a, b) => (b.songCount || 0) - (a.songCount || 0);

export function bucketGenres(rawGenres) {
  const groups = new Map();
  for (const entry of rawGenres || []) {
    if (!entry || typeof entry.value !== "string") continue;
    const name = bucketOf(entry.value);
    if (!name) continue;
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(entry);
  }
  return GENRE_BUCKETS.filter((bucket) => groups.has(bucket.name))
    .map((bucket) => {
      const entries = groups.get(bucket.name).sort(bySongCount);
      return {
        name: bucket.name,
        genres: entries.map((entry) => entry.value),
        songCount: entries.reduce((sum, entry) => sum + (Number(entry.songCount) || 0), 0),
        entries,
      };
    })
    .sort((a, b) => (a.name === "Other") - (b.name === "Other") || b.songCount - a.songCount)
    .map(({ entries, ...bucket }) => bucket);
}

export function bucketFetchGenres(bucket, max = 8) {
  return (bucket.genres || []).slice(0, Math.max(0, max));
}

const INTERLUDE_RE = /(?<![\p{L}\p{N}])(?:intro|outro|skit|interlude|a[ -]?cap+ella)s?(?![\p{L}\p{N}])/iu;

export function isInterlude(title) {
  return typeof title === "string" && INTERLUDE_RE.test(title);
}

export const MIX_EXTRA_ROUNDS = 3;

export async function collectMix(fetchRound, count, skipInterludes, rng = Math.random) {
  const first = await fetchRound();
  if (!skipInterludes) return { songs: mergeRandom(first, count, rng), short: false };
  const seen = new Set();
  const kept = [];
  let total = 0;
  const take = (lists) => {
    let fresh = 0;
    lists.forEach((list, i) => {
      kept[i] ??= [];
      for (const song of list) {
        if (seen.has(song.id)) continue;
        seen.add(song.id);
        fresh++;
        if (!isInterlude(song.title)) {
          kept[i].push(song);
          total++;
        }
      }
    });
    return fresh;
  };
  take(first);
  for (let round = 0; round < MIX_EXTRA_ROUNDS && total < count; round++) {
    if (!take(await fetchRound())) break;
  }
  return { songs: mergeRandom(kept, count, rng), short: total < count };
}

const REFRESHING_ACTIONS = new Set(["set", "add", "insert", "move", "setShuffle", "remove", "clear"]);

export function needsRefresh(calls) {
  return calls.some((call) => REFRESHING_ACTIONS.has(call.action));
}

const BARRIER_ACTIONS = new Set(["set", "remove", "clear", "setShuffle"]);

export function isAddOnly(calls) {
  return calls.length > 0 && calls.every((call) => call.action === "add");
}

export function isBarrier(calls) {
  return calls.some((call) => BARRIER_ACTIONS.has(call.action));
}

export function needsEntries(statusBody, knownVersion) {
  const version = statusBody && statusBody.queueVersion;
  return typeof version !== "string" || version !== knownVersion;
}

// Add-only batches run on a background lane so playback controls are not stuck behind
// them; set/remove/clear/setShuffle batches are barriers that wait for the add lane to drain
// and hold back adds queued after them.
export function createLanes() {
  const settled = (promise) => promise.then(() => {}, () => {});
  let main = Promise.resolve();
  let adds = Promise.resolve();
  let barrier = Promise.resolve();
  let pendingMain = 0;
  return {
    pendingMain: () => pendingMain,
    schedule(calls, task) {
      if (isAddOnly(calls)) {
        const result = Promise.all([adds, barrier]).then(task);
        adds = settled(result);
        return result;
      }
      pendingMain += 1;
      const wait = isBarrier(calls) ? Promise.all([main, adds]) : main;
      const result = wait.then(task);
      const done = settled(result);
      main = done;
      if (isBarrier(calls)) barrier = done;
      return result.finally(() => {
        pendingMain -= 1;
      });
    },
  };
}

export function moveEntry(list, from, to) {
  const out = list.slice();
  const inside = (i) => Number.isInteger(i) && i >= 0 && i < out.length;
  if (inside(from) && inside(to)) out.splice(to, 0, ...out.splice(from, 1));
  return out;
}

export function indexAfterMove(current, from, to) {
  if (from === current) return to;
  if (from < current && to >= current) return current - 1;
  if (from > current && to <= current) return current + 1;
  return current;
}

export function dragTargetIndex(from, offsetY, rowHeight, length) {
  if (!(rowHeight > 0) || !(length > 0) || !Number.isFinite(offsetY)) return from;
  return Math.min(Math.max(from + Math.round(offsetY / rowHeight), 0), length - 1);
}

// Rows between the grabbed one and the drop slot make room by one row height.
export function dragShift(index, from, to) {
  if (from < to && index > from && index <= to) return -1;
  if (from > to && index >= to && index < from) return 1;
  return 0;
}

export const DRAG_EDGE_PX = 60;
export const DRAG_SCROLL_MAX = 18;

export function autoScrollStep(pointerY, top, bottom, edge = DRAG_EDGE_PX, maxStep = DRAG_SCROLL_MAX) {
  if (!Number.isFinite(pointerY) || !(bottom > top)) return 0;
  const zone = Math.min(edge, (bottom - top) / 2);
  if (pointerY < top + zone) return -Math.round(maxStep * Math.min(1, (top + zone - pointerY) / zone)) || 0;
  if (pointerY > bottom - zone) return Math.round(maxStep * Math.min(1, (pointerY - (bottom - zone)) / zone)) || 0;
  return 0;
}

export function sortAlbumsByYear(albums) {
  const yearOf = (album) => {
    const year = Number(album && album.year);
    return Number.isFinite(year) && year > 0 ? year : null;
  };
  return [...albums].sort((a, b) => {
    const ya = yearOf(a);
    const yb = yearOf(b);
    if (ya === yb) return 0;
    if (ya === null) return 1;
    if (yb === null) return -1;
    return yb - ya;
  });
}

export function countLabel(count, noun) {
  const n = Number(count) || 0;
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

export function playlistInfo(owner, entries) {
  return [owner, queueSummary(entries)].filter(Boolean).join(" \u00b7 ");
}

export function ownPlaylists(playlists, user) {
  return (playlists || []).filter((playlist) => playlist && playlist.owner === user);
}

// Frames above the root list each remember their own scroll position; the root's is kept apart.
export function createViewStack() {
  const frames = [];
  let rootScroll = 0;
  const top = () => frames[frames.length - 1] || null;
  return {
    depth: () => frames.length,
    top,
    all: () => frames.slice(),
    rootScroll: () => rootScroll,
    push(view, scroll) {
      const current = top();
      if (current) current.scroll = scroll;
      else rootScroll = scroll;
      frames.push({ ...view, scroll: 0 });
    },
    pop() {
      frames.pop();
      return top();
    },
    clear() {
      frames.length = 0;
    },
    reset() {
      frames.length = 0;
      rootScroll = 0;
    },
  };
}

export const THEMES = ["dark", "macchiato", "frappe", "contrast"];
export const THEME_KEY = "remote-theme";
export const THEME_COLORS = { dark: "#0d0e11", macchiato: "#24273a", frappe: "#3f4459", contrast: "#000000" };

export function isTheme(value) {
  return THEMES.includes(value);
}

export function loadTheme(storage) {
  try {
    const value = storage.getItem(THEME_KEY);
    return isTheme(value) ? value : "dark";
  } catch {
    return "dark";
  }
}

export function saveTheme(storage, theme) {
  if (!isTheme(theme)) return false;
  try {
    storage.setItem(THEME_KEY, theme);
    return true;
  } catch {
    return false;
  }
}

export function parseThemeMessage(data) {
  if (!data || typeof data !== "object") return null;
  if (data.type !== "kiosk-theme" || !isTheme(data.theme)) return null;
  return data.theme;
}
