import test from "node:test";
import assert from "node:assert/strict";
import { md5 } from "../../bridge/remote/md5.js";
import {
  makeAuth, apiParams, formatTime, previousTarget, nextTarget, chunk, playCalls,
  addCalls, estimatePosition, gainToPercent, percentToGain, unwrap,
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
