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

const SWIPE_MIN_PX = 60;
const SWIPE_MIN_FRACTION = 0.2;
const SWIPE_HORIZONTAL_RATIO = 1.5;
const SWIPE_MAX_MS = 800;
const SWIPE_LONG_FRACTION = 0.4;
const SWIPE_RUBBER = 0.3;

export function swipeAction(dx, dy, dtMs, width) {
  if (![dx, dy, dtMs].every(Number.isFinite)) return null;
  const threshold = Number.isFinite(width) && width > 0
    ? Math.min(SWIPE_MIN_PX, width * SWIPE_MIN_FRACTION)
    : SWIPE_MIN_PX;
  if (Math.abs(dx) < threshold) return null;
  if (!(Math.abs(dx) > SWIPE_HORIZONTAL_RATIO * Math.abs(dy))) return null;
  const long = Number.isFinite(width) && width > 0 && Math.abs(dx) >= width * SWIPE_LONG_FRACTION;
  if (dtMs > SWIPE_MAX_MS && !long) return null;
  return dx < 0 ? "next" : "previous";
}

export function swipeOffset(dx, hasNeighbour) {
  return hasNeighbour ? dx : dx * SWIPE_RUBBER;
}

export function swipeNeighbour(dx, index, length) {
  if (!(length > 0) || !Number.isFinite(dx) || dx === 0) return null;
  const target = dx < 0 ? index + 1 : index - 1;
  return target >= 0 && target < length ? target : null;
}

export function swipeTarget(action, index, length) {
  if (!(length > 0)) return null;
  if (action === "next") return nextTarget(index, length);
  if (action === "previous") return index <= 0 ? null : index - 1;
  return null;
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

export function addCalls(ids) {
  return chunk(ids).map((part) => ({ action: "add", ids: part }));
}

export function estimatePosition(position, playing, polledAtMs, nowMs, duration) {
  let value = position;
  if (playing) value += Math.max(0, (nowMs - polledAtMs) / 1000);
  if (duration > 0) value = Math.min(value, duration);
  return Math.max(value, 0);
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
