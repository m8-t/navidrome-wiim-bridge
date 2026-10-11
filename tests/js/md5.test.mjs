import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { md5 } from "../../bridge/remote/md5.js";

const VECTORS = [
  ["", "d41d8cd98f00b204e9800998ecf8427e"],
  ["a", "0cc175b9c0f1b6a831c399e269772661"],
  ["abc", "900150983cd24fb0d6963f7d28e17f72"],
  ["message digest", "f96b697d7cb7938d525a2f31aaf161d0"],
  ["abcdefghijklmnopqrstuvwxyz", "c3fcd3d76192e4007dfb496cca67e13b"],
  ["ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", "d174ab98d277d9f5a5611c2c9f419d9f"],
  ["12345678901234567890123456789012345678901234567890123456789012345678901234567890", "57edf4a22be3c955ac49da2e2107b67a"],
];

for (const [text, hex] of VECTORS) {
  test(`md5 RFC 1321 vector ${JSON.stringify(text.slice(0, 20))}`, () => {
    assert.equal(md5(text), hex);
  });
}

test("md5 matches node:crypto for non-ASCII input", () => {
  const text = "pässwört€";
  assert.equal(md5(text), createHash("md5").update(text, "utf8").digest("hex"));
});
