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

export function nextTarget(index, length) {
  return index + 1 >= length ? null : index + 1;
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
  const seen = new Set();
  const songs = [];
  for (const song of lists.flat()) {
    if (seen.has(song.id)) continue;
    seen.add(song.id);
    songs.push(song);
  }
  for (let i = songs.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [songs[i], songs[j]] = [songs[j], songs[i]];
  }
  return songs.slice(0, Math.max(0, count));
}

const REFRESHING_ACTIONS = new Set(["set", "add", "remove", "clear"]);

export function needsRefresh(calls) {
  return calls.some((call) => REFRESHING_ACTIONS.has(call.action));
}
