import test from "node:test";
import assert from "node:assert/strict";
import { md5 } from "../../bridge/remote/md5.js";
import {
  makeAuth, apiParams, formatTime, previousTarget, nextTarget, chunk, playCalls,
  addCalls, estimatePosition, gainToPercent, percentToGain, unwrap,
  formatDuration, queueSummary, progressFraction, trackArtistIfDifferent, silenceWav,
  mediaMetadataInit, positionState, swipeAction, swipeTarget, swipeOffset, swipeNeighbour, nextPage,
} from "../../bridge/remote/logic.js";

const ids = (n) => Array.from({ length: n }, (_, i) => `id${i}`);

test("makeAuth", () => {
  assert.deepEqual(makeAuth("dan", "pw", "abcd"), { u: "dan", t: md5("pwabcd"), s: "abcd" });
});

test("apiParams", () => {
  const auth = { u: "dan", t: "tok", s: "salt" };
  assert.deepEqual(apiParams(auth), {
    u: "dan", t: "tok", s: "salt", v: "1.16.1", c: "wiim-remote", f: "json",
  });
});

test("formatTime", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(65), "1:05");
  assert.equal(formatTime(3605), "60:05");
  assert.equal(formatTime(-5), "0:00");
  assert.equal(formatTime(NaN), "0:00");
  assert.equal(formatTime(null), "0:00");
  assert.equal(formatTime(undefined), "0:00");
  assert.equal(formatTime(65.9), "1:05");
});

test("previousTarget", () => {
  assert.deepEqual(previousTarget(3, 10), { index: 3, offset: 0 });
  assert.deepEqual(previousTarget(3, 3), { index: 2, offset: 0 });
  assert.deepEqual(previousTarget(3, 3.5), { index: 3, offset: 0 });
  assert.deepEqual(previousTarget(0, 0), { index: 0, offset: 0 });
  assert.deepEqual(previousTarget(-1, 0), { index: -1, offset: 0 });
});

test("nextTarget", () => {
  assert.equal(nextTarget(0, 3), 1);
  assert.equal(nextTarget(1, 3), 2);
  assert.equal(nextTarget(2, 3), null);
  assert.equal(nextTarget(0, 0), null);
});

test("chunk", () => {
  assert.deepEqual(chunk([]), []);
  assert.deepEqual(chunk(ids(3), 2), [["id0", "id1"], ["id2"]]);
  assert.equal(chunk(ids(50)).length, 1);
  assert.equal(chunk(ids(51)).length, 2);
  assert.equal(chunk(ids(120))[2].length, 20);
  assert.throws(() => chunk(ids(3), 0), RangeError);
  assert.throws(() => chunk(ids(3), -1), RangeError);
});

test("playCalls", () => {
  assert.deepEqual(playCalls([], 0), []);
  assert.deepEqual(playCalls(["a", "b"], 1), [
    { action: "set", ids: ["a", "b"] },
    { action: "skip", index: 1, offset: 0 },
    { action: "start" },
  ]);
  const calls = playCalls(ids(120), 7);
  assert.deepEqual(calls.map((c) => c.action), ["set", "add", "add", "skip", "start"]);
  assert.equal(calls[0].ids.length, 50);
  assert.equal(calls[2].ids.length, 20);
  assert.deepEqual(calls[3], { action: "skip", index: 7, offset: 0 });
});

test("addCalls", () => {
  assert.deepEqual(addCalls([]), []);
  assert.deepEqual(addCalls(["a"]), [{ action: "add", ids: ["a"] }]);
  assert.deepEqual(addCalls(ids(51)).map((c) => c.ids.length), [50, 1]);
});

test("estimatePosition", () => {
  assert.equal(estimatePosition(10, true, 1000, 3500, 100), 12.5);
  assert.equal(estimatePosition(10, false, 1000, 3500, 100), 10);
  assert.equal(estimatePosition(99, true, 1000, 6000, 100), 100);
  assert.equal(estimatePosition(99, true, 1000, 6000, 0), 104);
  assert.equal(estimatePosition(-3, false, 0, 0, 0), 0);
  assert.equal(estimatePosition(5, true, 5000, 1000, 100), 5);
  assert.equal(estimatePosition(-3, false, 0, 0, 100), 0);
});

test("gainToPercent and percentToGain", () => {
  assert.equal(gainToPercent(0.5), 50);
  assert.equal(gainToPercent(0.456), 46);
  assert.equal(gainToPercent(-1), 0);
  assert.equal(gainToPercent(2), 100);
  assert.equal(percentToGain(50), 0.5);
  assert.equal(percentToGain(-10), 0);
  assert.equal(percentToGain(150), 1);
});

test("unwrap", () => {
  const body = { status: "ok", jukeboxStatus: {} };
  assert.equal(unwrap({ "subsonic-response": body }), body);
  assert.throws(
    () => unwrap({ "subsonic-response": { status: "failed", error: { code: 40, message: "bad" } } }),
    (err) => err instanceof Error && err.message === "bad" && err.code === 40,
  );
  assert.throws(
    () => unwrap({ "subsonic-response": { status: "failed" } }),
    (err) => err.message === "unexpected reply" && err.code === null,
  );
  assert.throws(() => unwrap({}), (err) => err.message === "unexpected reply" && err.code === null);
  assert.throws(() => unwrap(null), (err) => err.code === null);
});

test("formatDuration", () => {
  assert.equal(formatDuration(0), "0 min");
  assert.equal(formatDuration(59), "0 min");
  assert.equal(formatDuration(60), "1 min");
  assert.equal(formatDuration(14 * 60 + 20), "14 min");
  assert.equal(formatDuration(3600), "1 h");
  assert.equal(formatDuration(3600 + 14 * 60), "1 h 14 min");
  assert.equal(formatDuration(7200), "2 h");
  assert.equal(formatDuration(-1), "0 min");
  assert.equal(formatDuration(NaN), "0 min");
  assert.equal(formatDuration(undefined), "0 min");
});

test("queueSummary", () => {
  assert.equal(queueSummary([]), "");
  assert.equal(queueSummary([{ duration: 240 }]), "1 track, 4 min");
  assert.equal(queueSummary([{ duration: 240 }, {}]), "2 tracks, 4 min");
  const many = Array.from({ length: 22 }, () => ({ duration: (74 * 60) / 22 }));
  assert.equal(queueSummary(many), "22 tracks, 1 h 14 min");
});

test("progressFraction", () => {
  assert.equal(progressFraction(50, 200), 0.25);
  assert.equal(progressFraction(300, 200), 1);
  assert.equal(progressFraction(-5, 200), 0);
  assert.equal(progressFraction(10, 0), 0);
  assert.equal(progressFraction(10, -3), 0);
  assert.equal(progressFraction(10, NaN), 0);
  assert.equal(progressFraction(NaN, 10), 0);
});

test("trackArtistIfDifferent", () => {
  assert.equal(trackArtistIfDifferent({ artist: "Muse" }, "Muse"), "");
  assert.equal(trackArtistIfDifferent({ artist: " muse " }, "MUSE"), "");
  assert.equal(trackArtistIfDifferent({ artist: "Muse feat. X" }, "Muse"), "Muse feat. X");
  assert.equal(trackArtistIfDifferent({}, "Muse"), "");
  assert.equal(trackArtistIfDifferent({ artist: "A" }, undefined), "A");
});

test("silenceWav", () => {
  const wav = silenceWav();
  assert.ok(wav instanceof Uint8Array);
  assert.equal(wav.length, 44 + 10 * 8000);
  const text = (a, b) => String.fromCharCode(...wav.slice(a, b));
  assert.equal(text(0, 4), "RIFF");
  assert.equal(text(8, 12), "WAVE");
  assert.equal(text(12, 16), "fmt ");
  assert.equal(text(36, 40), "data");
  const v = new DataView(wav.buffer);
  assert.equal(v.getUint32(4, true), wav.length - 8);
  assert.equal(v.getUint32(16, true), 16);
  assert.equal(v.getUint16(20, true), 1);
  assert.equal(v.getUint16(22, true), 1);
  assert.equal(v.getUint32(24, true), 8000);
  assert.equal(v.getUint32(28, true), 8000);
  assert.equal(v.getUint16(32, true), 1);
  assert.equal(v.getUint16(34, true), 8);
  assert.equal(v.getUint32(40, true), 80000);
  assert.ok(wav.slice(44).every((b) => b === 128));
  const small = silenceWav(1, 100);
  assert.equal(small.length, 144);
  assert.equal(new DataView(small.buffer).getUint32(24, true), 100);
});

test("mediaMetadataInit", () => {
  const cover = (id, size) => `/c/${id}/${size}`;
  assert.deepEqual(
    mediaMetadataInit({ title: "T", artist: "A", album: "B", coverArt: "c1" }, cover),
    { title: "T", artist: "A", album: "B", artwork: [{ src: "/c/c1/512", sizes: "512x512" }] },
  );
  assert.deepEqual(mediaMetadataInit({ title: "T" }, cover), { title: "T", artist: "", album: "", artwork: [] });
  assert.deepEqual(mediaMetadataInit(null, cover), { title: "", artist: "", album: "", artwork: [] });
});

test("positionState", () => {
  assert.deepEqual(positionState(10, 100), { duration: 100, position: 10, playbackRate: 1 });
  assert.deepEqual(positionState(150, 100), { duration: 100, position: 100, playbackRate: 1 });
  assert.deepEqual(positionState(-4, 100), { duration: 100, position: 0, playbackRate: 1 });
  assert.equal(positionState(5, 0), null);
  assert.equal(positionState(5, -1), null);
  assert.equal(positionState(5, NaN), null);
  assert.equal(positionState(5, Infinity), null);
  assert.equal(positionState(5, undefined), null);
});

test("swipeAction", () => {
  assert.equal(swipeAction(-100, 0, 200, 400), "next");
  assert.equal(swipeAction(100, 0, 200, 400), "previous");
  assert.equal(swipeAction(-59, 0, 200, 400), null);
  assert.equal(swipeAction(-60, 0, 200, 400), "next");
  assert.equal(swipeAction(60, 0, 200, 400), "previous");
  assert.equal(swipeAction(10, 100, 200, 400), null);
  assert.equal(swipeAction(-90, 61, 200, 400), null);
  assert.equal(swipeAction(-90, 60, 200, 400), null);
  assert.equal(swipeAction(-90, 59, 200, 400), "next");
  assert.equal(swipeAction(-100, 0, 801, 400), null);
  assert.equal(swipeAction(-100, 0, 800, 400), "next");
  assert.equal(swipeAction(-40, 0, 200, 200), "next");
  assert.equal(swipeAction(-39, 0, 200, 200), null);
  assert.equal(swipeAction(-59, 0, 200, 0), null);
  assert.equal(swipeAction(-60, 0, 200, 0), "next");
  assert.equal(swipeAction(-59, 0, 200, NaN), null);
  assert.equal(swipeAction(-60, 0, 200, NaN), "next");
  assert.equal(swipeAction(NaN, 0, 200, 400), null);
  assert.equal(swipeAction(-100, NaN, 200, 400), null);
  assert.equal(swipeAction(-100, 0, NaN, 400), null);
});

test("swipeTarget", () => {
  assert.equal(swipeTarget("next", 2, 5), 3);
  assert.equal(swipeTarget("next", 4, 5), null);
  assert.equal(swipeTarget("previous", 2, 5), 1);
  assert.equal(swipeTarget("previous", 0, 5), null);
  assert.equal(swipeTarget("next", 0, 0), null);
  assert.equal(swipeTarget("previous", 3, 0), null);
  assert.equal(swipeTarget("sideways", 2, 5), null);
  assert.equal(swipeTarget(null, 2, 5), null);
});

test("swipeAction long drags", () => {
  assert.equal(swipeAction(-160, 0, 2000, 400), "next");
  assert.equal(swipeAction(160, 0, 2000, 400), "previous");
  assert.equal(swipeAction(-159, 0, 801, 400), null);
  assert.equal(swipeAction(-160, 300, 2000, 400), null);
  assert.equal(swipeAction(-1000, 900, 100, 400), null);
  assert.equal(swipeAction(-1000, 900, 2000, 400), null);
  assert.equal(swipeAction(-100, 0, 2000, 0), null);
});

test("swipeOffset", () => {
  assert.equal(swipeOffset(-100, true), -100);
  assert.equal(swipeOffset(100, true), 100);
  assert.ok(Math.abs(swipeOffset(100, false) - 30) < 1e-9);
  assert.ok(Math.abs(swipeOffset(-100, false) + 30) < 1e-9);
  assert.equal(swipeOffset(0, false), 0);
});

test("swipeNeighbour", () => {
  assert.equal(swipeNeighbour(-50, 2, 5), 3);
  assert.equal(swipeNeighbour(50, 2, 5), 1);
  assert.equal(swipeNeighbour(50, 0, 5), null);
  assert.equal(swipeNeighbour(-50, 4, 5), null);
  assert.equal(swipeNeighbour(-50, 0, 5), 1);
  assert.equal(swipeNeighbour(50, 4, 5), 3);
  assert.equal(swipeNeighbour(-50, 0, 0), null);
  assert.equal(swipeNeighbour(0, 2, 5), null);
  assert.equal(swipeNeighbour(NaN, 2, 5), null);
});

test("nextPage", () => {
  assert.deepEqual(nextPage({ offset: 0 }, 40, 40), { offset: 40, done: false });
  assert.deepEqual(nextPage({ offset: 40 }, 40, 40), { offset: 80, done: false });
  assert.deepEqual(nextPage({ offset: 120 }, 10, 40), { offset: 130, done: true });
  assert.deepEqual(nextPage({ offset: 0 }, 0, 40), { offset: 0, done: true });
  assert.deepEqual(nextPage({ offset: 40 }, NaN, 40), { offset: 40, done: true });
});
