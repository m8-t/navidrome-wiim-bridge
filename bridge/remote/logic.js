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
