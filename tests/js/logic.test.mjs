import test from "node:test";
import assert from "node:assert/strict";
import { md5 } from "../../bridge/remote/md5.js";
import {
  makeAuth, apiParams, formatTime, previousTarget, nextTarget, chunk, playCalls,
  addCalls, estimatePosition, gainToPercent, percentToGain, unwrap,
  formatDuration, queueSummary, progressFraction, trackArtistIfDifferent, silenceWav,
  mediaMetadataInit, positionState, flowTileStyle, flowPosition, flowVelocity, flowTarget, nextPage, mixCalls, randomSongParams, mergeRandom, isInterlude, collectMix, needsRefresh, reconcilePosition,
  needsEntries, isAddOnly, isBarrier, createLanes,
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

test("flowTileStyle centre and sides", () => {
  assert.deepEqual(flowTileStyle(0), { x: 0, rotateY: 0, scale: 1, z: 100, opacity: 1 });
  const right = flowTileStyle(1);
  assert.equal(right.x, 1);
  assert.ok(right.rotateY < 0);
  assert.ok(right.scale < 1 && right.scale > 0.5);
  assert.ok(right.z < 100);
  const left = flowTileStyle(-1);
  assert.ok(left.x < 0 && left.rotateY > 0);
});

test("flowTileStyle mirrors and stays continuous", () => {
  for (const o of [0.1, 0.5, 1, 1.4, 2, 2.75, 3, 3.5, 4]) {
    const r = flowTileStyle(o);
    const l = flowTileStyle(-o);
    assert.equal(l.x, -r.x);
    assert.equal(l.rotateY, -r.rotateY);
    assert.equal(l.scale, r.scale);
    assert.equal(l.z, r.z);
    assert.equal(l.opacity, r.opacity);
    assert.ok(Number.isInteger(r.z));
  }
  const eps = 1e-6;
  for (const o of [0, 1, 3]) {
    const a = flowTileStyle(o - eps);
    const b = flowTileStyle(o + eps);
    for (const key of ["x", "rotateY", "scale", "opacity"]) {
      assert.ok(Math.abs(a[key] - b[key]) < 1e-3, `${key} jumps at ${o}`);
    }
  }
  assert.equal(flowTileStyle(3.5).opacity, 0);
  assert.equal(flowTileStyle(9).opacity, 0);
  assert.equal(flowTileStyle(NaN).x, 0);
});

test("flowPosition follows the finger and rubber bands", () => {
  assert.equal(flowPosition(4, 0, 200, 10), 4);
  assert.equal(flowPosition(4, -100, 200, 10), 4.5);
  assert.equal(flowPosition(4, 100, 200, 10), 3.5);
  assert.equal(flowPosition(4, -400, 200, 10), 6);
  assert.ok(Math.abs(flowPosition(0, 200, 200, 10) + 0.3) < 1e-9);
  assert.ok(Math.abs(flowPosition(9, -200, 200, 10) - 9.3) < 1e-9);
  assert.ok(Math.abs(flowPosition(0, 200, 200, 1) + 0.3) < 1e-9);
  assert.ok(Math.abs(flowPosition(0, -200, 200, 1) - 0.3) < 1e-9);
  assert.equal(flowPosition(3, 50, 0, 10), 3);
  assert.equal(flowPosition(3, NaN, 200, 10), 3);
  assert.equal(flowPosition(3, 50, 200, 0), 0);
});

test("flowVelocity", () => {
  const samples = [{ t: 0, p: 4 }, { t: 50, p: 4.5 }, { t: 100, p: 5 }];
  assert.equal(flowVelocity(samples, 100), 10);
  assert.equal(flowVelocity(samples, 150), 10);
  assert.equal(flowVelocity(samples, 201), 0);
  assert.equal(flowVelocity([], 100), 0);
  assert.equal(flowVelocity([{ t: 10, p: 1 }], 10), 0);
  assert.equal(flowVelocity([{ t: 10, p: 1 }, { t: 10, p: 2 }], 10), 0);
  assert.equal(flowVelocity([{ t: 0, p: 1 }, { t: 400, p: 9 }], 400), 0);
  assert.equal(flowVelocity([{ t: 0, p: 1 }, { t: 90, p: 0 }], 90), -1000 / 90);
});

test("flowTarget", () => {
  assert.equal(flowTarget(4.2, 0, 10), 4);
  assert.equal(flowTarget(4.6, 0, 10), 5);
  assert.equal(flowTarget(3.4, 0, 10), 3);
  assert.equal(flowTarget(4.3, 3, 10), 5);
  assert.equal(flowTarget(4.1, 15, 10), 7);
  assert.equal(flowTarget(4.1, -15, 10), 1);
  assert.equal(flowTarget(4.3, 0.5, 10), 4);
  assert.equal(flowTarget(8, 50, 10), 9);
  assert.equal(flowTarget(1, -50, 10), 0);
  assert.equal(flowTarget(-0.3, 0, 10), 0);
  assert.equal(flowTarget(9.3, 0, 10), 9);
  assert.equal(flowTarget(0.4, 0, 1), 0);
  assert.equal(flowTarget(NaN, NaN, 10), 0);
  assert.equal(flowTarget(4, Infinity, 10), 4);
  assert.equal(flowTarget(4, 5, 0), 0);
});

test("nextPage", () => {
  assert.deepEqual(nextPage({ offset: 0 }, 40, 40), { offset: 40, done: false });
  assert.deepEqual(nextPage({ offset: 40 }, 40, 40), { offset: 80, done: false });
  assert.deepEqual(nextPage({ offset: 120 }, 10, 40), { offset: 130, done: true });
  assert.deepEqual(nextPage({ offset: 0 }, 0, 40), { offset: 0, done: true });
  assert.deepEqual(nextPage({ offset: 40 }, NaN, 40), { offset: 40, done: true });
});

test("mixCalls", () => {
  assert.deepEqual(mixCalls([]), []);
  assert.deepEqual(mixCalls(ids(1)), [
    { action: "set", ids: ["id0"] },
    { action: "skip", index: 0, offset: 0 },
    { action: "start" },
  ]);
  const fifty = mixCalls(ids(50));
  assert.equal(fifty.length, 3);
  assert.equal(fifty[0].ids.length, 50);
  const calls = mixCalls(ids(51));
  assert.deepEqual(calls.map((c) => c.action), ["set", "skip", "start", "add"]);
  assert.deepEqual(calls[3].ids, ["id50"]);
  const big = mixCalls(ids(500));
  assert.deepEqual(big.map((c) => c.action), ["set", "skip", "start", ...Array(9).fill("add")]);
  assert.ok(big.filter((c) => c.ids).every((c) => c.ids.length === 50));
  assert.deepEqual(big.flatMap((c) => c.ids || []), ids(500));
});

test("randomSongParams", () => {
  assert.deepEqual(randomSongParams({ count: 100 }), { size: 100 });
  assert.deepEqual(randomSongParams({ count: 900 }), { size: 500 });
  assert.deepEqual(randomSongParams({}), { size: 500 });
  assert.deepEqual(randomSongParams({ count: "250", fromYear: "1990", toYear: 1999, genre: "Rock" }), {
    size: 250, fromYear: 1990, toYear: 1999, genre: "Rock",
  });
  assert.deepEqual(randomSongParams({ count: 10, fromYear: 2005, toYear: 1995 }), { size: 10, fromYear: 1995, toYear: 2005 });
  assert.deepEqual(randomSongParams({ count: 10, fromYear: "", toYear: "" }), { size: 10 });
  assert.deepEqual(randomSongParams({ count: 10, fromYear: "1990" }), { size: 10, fromYear: 1990 });
  assert.deepEqual(randomSongParams({ count: 10, toYear: "2001" }), { size: 10, toYear: 2001 });
  assert.deepEqual(randomSongParams({ count: 10, fromYear: "99", toYear: "20000" }), { size: 10 });
  assert.deepEqual(randomSongParams({ count: 10, fromYear: "1849", toYear: "2101" }), { size: 10 });
  assert.deepEqual(randomSongParams({ count: 10, fromYear: "abcd", toYear: "19 9" }), { size: 10 });
  assert.deepEqual(randomSongParams({ count: 10, fromYear: 1850, toYear: 2100 }), { size: 10, fromYear: 1850, toYear: 2100 });
  assert.deepEqual(randomSongParams({ count: 10, genre: "  " }), { size: 10 });
  assert.deepEqual(randomSongParams({ count: NaN }), { size: 500 });
});

function seeded(seed) {
  let x = seed;
  return () => {
    x = (x * 1664525 + 1013904223) % 4294967296;
    return x / 4294967296;
  };
}

test("mergeRandom dedupes, shuffles deterministically and cuts", () => {
  const a = [{ id: "1", n: "a1" }, { id: "2" }, { id: "3" }];
  const b = [{ id: "2", n: "b2" }, { id: "4" }, { id: "1", n: "b1" }];
  const merged = mergeRandom([a, b], 10, seeded(7));
  assert.deepEqual(merged.map((s) => s.id).sort(), ["1", "2", "3", "4"]);
  assert.equal(merged.find((s) => s.id === "1").n, "a1");
  assert.deepEqual(merged, mergeRandom([a, b], 10, seeded(7)));
  assert.notDeepEqual(merged.map((s) => s.id), mergeRandom([a, b], 10, seeded(8)).map((s) => s.id));
  assert.equal(mergeRandom([a, b], 2, seeded(7)).length, 2);
  assert.deepEqual(mergeRandom([a, b], 2, seeded(7)), merged.slice(0, 2));
  assert.deepEqual(mergeRandom([], 5), []);
  assert.deepEqual(mergeRandom([a], 0), []);
});

test("mergeRandom does not touch its input and uses every item", () => {
  const list = Array.from({ length: 200 }, (_, i) => ({ id: `s${i}` }));
  const copy = list.map((s) => s.id);
  const merged = mergeRandom([list], 200, seeded(3));
  assert.deepEqual(list.map((s) => s.id), copy);
  assert.equal(new Set(merged.map((s) => s.id)).size, 200);
  assert.notDeepEqual(merged.map((s) => s.id), copy);
});

test("needsRefresh is true only for queue-changing calls", () => {
  assert.equal(needsRefresh([{ action: "set", id: ["a"] }, { action: "start" }]), true);
  assert.equal(needsRefresh([{ action: "add", id: ["a"] }]), true);
  assert.equal(needsRefresh([{ action: "skip", index: 0, offset: 0 }, { action: "start" }]), false);
  assert.equal(needsRefresh([]), false);
});

const rec = (over) => reconcilePosition({
  shown: 10.4, reported: 10.1, playing: true, wasPlaying: true, sameTrack: true, forced: false,
  threshold: 1.5, ...over,
});

test("reconcilePosition keeps the local estimate when the report is within the threshold", () => {
  assert.equal(rec({}), 10.4);
  assert.equal(rec({ reported: 11.5 }), 10.4);
  assert.equal(rec({ reported: 8.95 }), 10.4);
});

test("reconcilePosition snaps beyond the threshold", () => {
  assert.equal(rec({ reported: 12.0 }), 12.0);
  assert.equal(rec({ reported: 5 }), 5);
});

test("reconcilePosition snaps on track change, play flip, forced and paused", () => {
  assert.equal(rec({ sameTrack: false }), 10.1);
  assert.equal(rec({ wasPlaying: false }), 10.1);
  assert.equal(rec({ playing: false }), 10.1);
  assert.equal(rec({ forced: true }), 10.1);
});

test("reconcilePosition never goes backwards over repeated noisy polls", () => {
  let shown = 3;
  for (const noise of [0.9, 0.1, 0.7, 0.0, 1.0, 0.4]) {
    const next = rec({ shown: shown + 1.5, reported: shown + 1.5 - noise });
    assert.ok(next >= shown);
    shown = next;
  }
});

test("reconcilePosition falls back to the report for a non-finite estimate", () => {
  assert.equal(rec({ shown: NaN }), 10.1);
});

test("needsEntries compares queueVersion and treats a missing one as changed", () => {
  assert.equal(needsEntries({ queueVersion: "a:1" }, "a:1"), false);
  assert.equal(needsEntries({ queueVersion: "a:2" }, "a:1"), true);
  assert.equal(needsEntries({ queueVersion: "a:1" }, null), true);
  assert.equal(needsEntries({}, undefined), true);
  assert.equal(needsEntries(undefined, null), true);
});

test("isAddOnly and isBarrier classify batches", () => {
  assert.equal(isAddOnly([]), false);
  assert.equal(isAddOnly([{ action: "add" }, { action: "add" }]), true);
  assert.equal(isAddOnly([{ action: "add" }, { action: "skip" }]), false);
  assert.equal(isBarrier([{ action: "skip" }, { action: "start" }]), false);
  for (const action of ["set", "remove", "clear"]) assert.equal(isBarrier([{ action }]), true);
});

function laneHarness() {
  const lanes = createLanes();
  const log = [];
  const gates = {};
  const run = (name, calls) => {
    const result = lanes.schedule(calls, () => {
      log.push(`start ${name}`);
      return new Promise((resolve) => {
        gates[name] = () => {
          log.push(`end ${name}`);
          resolve(true);
        };
      });
    });
    return result;
  };
  const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
  return { lanes, log, gates, run, tick };
}

const A = [{ action: "add" }];
const SKIP = [{ action: "skip" }];
const SET = [{ action: "set" }, { action: "skip" }, { action: "start" }];

test("lanes: playback batch overtakes a running add batch", async () => {
  const h = laneHarness();
  h.run("add1", A);
  h.run("skip", SKIP);
  await h.tick();
  assert.deepEqual([...h.log].sort(), ["start add1", "start skip"]);
  assert.equal(h.lanes.pendingMain(), 1);
  h.gates.skip();
  await h.tick();
  assert.equal(h.lanes.pendingMain(), 0);
});

test("lanes: main batches and add batches each keep their own order", async () => {
  const h = laneHarness();
  h.run("add1", A);
  h.run("add2", A);
  h.run("skip1", SKIP);
  h.run("skip2", SKIP);
  await h.tick();
  assert.deepEqual([...h.log].sort(), ["start add1", "start skip1"]);
  h.gates.skip1();
  h.gates.add1();
  await h.tick();
  assert.deepEqual(h.log.slice(2).sort(), ["end add1", "end skip1", "start add2", "start skip2"]);
});

test("lanes: set waits for the add lane and holds back later adds", async () => {
  const h = laneHarness();
  h.run("add1", A);
  h.run("set", SET);
  h.run("add2", A);
  h.run("skip", SKIP);
  await h.tick();
  assert.deepEqual(h.log, ["start add1"]);
  h.gates.add1();
  await h.tick();
  assert.deepEqual(h.log.slice(1), ["end add1", "start set"]);
  h.gates.set();
  await h.tick();
  assert.deepEqual(h.log.slice(3).sort(), ["end set", "start add2", "start skip"]);
});

test("lanes: a failing batch does not wedge its lane", async () => {
  const lanes = createLanes();
  const failed = lanes.schedule(SKIP, async () => {
    throw new Error("boom");
  });
  await assert.rejects(failed);
  assert.equal(await lanes.schedule(SKIP, async () => true), true);
  assert.equal(lanes.pendingMain(), 0);
});

test("isInterlude matches whole words only", () => {
  for (const t of ["Intro", "intro", "Outro (feat. X)", "Skit", "Interlude: Love", "The Interlude", "Intro - Live", "Skit #2", "Interludes of Sorrow", "SKITS", "Outros", "Éxito Intro"]) {
    assert.equal(isInterlude(t), true, t);
  }
  for (const t of ["Introduction", "Outrospective", "Skittles", "Retro", "Intron", "Éintro", "intro2", "", null, undefined, 5]) {
    assert.equal(isInterlude(t), false, String(t));
  }
});

const pool = (prefix, n, bad = 0) =>
  Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}`, title: i < bad ? "Skit" : `Song ${prefix}${i}` }));

test("collectMix without the option fetches once and merges as before", async () => {
  let calls = 0;
  const lists = [pool("a", 6, 3), pool("b", 4)];
  const { songs, short } = await collectMix(async () => (calls++, lists), 5, false, seeded(3));
  assert.equal(calls, 1);
  assert.equal(short, false);
  assert.deepEqual(songs, mergeRandom(lists, 5, seeded(3)));
});

test("collectMix filters and tops up until count is reached", async () => {
  let round = 0;
  const fetchRound = async () => [pool(`r${round++}_`, 10, 5)];
  const { songs, short } = await collectMix(fetchRound, 8, true, seeded(1));
  assert.equal(round, 2);
  assert.equal(songs.length, 8);
  assert.equal(short, false);
  assert.ok(songs.every((s) => !isInterlude(s.title)));
  assert.equal(new Set(songs.map((s) => s.id)).size, 8);
});

test("collectMix gives up after three extra rounds and reports short", async () => {
  let round = 0;
  const fetchRound = async () => [pool(`r${round++}_`, 4, 3)];
  const { songs, short } = await collectMix(fetchRound, 50, true, seeded(1));
  assert.equal(round, 4);
  assert.equal(songs.length, 4);
  assert.equal(short, true);
});

test("collectMix dedupes across rounds and stops when nothing new arrives", async () => {
  let round = 0;
  const same = [pool("x", 5, 1), pool("y", 2)];
  const { songs, short } = await collectMix(async () => (round++, same), 20, true);
  assert.equal(round, 2);
  assert.equal(songs.length, 6);
  assert.equal(short, true);
});
