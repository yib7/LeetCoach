"use strict";
/* SP7 fix round front-end helpers: the per-problem notes save queue (one
 * PUT at a time, newest text sent after an in-flight save even when no editor
 * is left, flushNow on pagehide) and the "today" used for due labels once a
 * tab has stayed open past midnight. */
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));

function fakeSend() {
  var calls = [];
  var send = function (text, keepalive, done) { calls.push({ text: text, keepalive: keepalive, done: done }); };
  return { calls: calls, send: send };
}

test("saveQueue sends at once when idle and reports the saved text", function () {
  var f = fakeSend();
  var q = core.makeSaveQueue(f.send);
  var got = [];
  q.push("a", false, function (err, text) { got.push([err, text]); });
  assert.strictEqual(f.calls.length, 1);
  assert.strictEqual(q.pending(), true);
  f.calls[0].done(null);
  assert.deepStrictEqual(got, [[null, "a"]]);
  assert.strictEqual(q.pending(), false);
  assert.strictEqual(q.latest(), "a");
});

test("saveQueue holds the newest text while a save is in flight, then sends it", function () {
  var f = fakeSend();
  var q = core.makeSaveQueue(f.send);
  var got = [];
  q.push("a", false);
  q.push("ab", false, function (err, text) { got.push(text); });
  q.push("abc", true, function (err, text) { got.push(text); });
  assert.strictEqual(f.calls.length, 1, "nothing new until the first save lands");
  assert.strictEqual(q.latest(), "abc");
  f.calls[0].done(null);
  assert.strictEqual(f.calls.length, 2);
  assert.strictEqual(f.calls[1].text, "abc", "coalesced to the newest text");
  assert.strictEqual(f.calls[1].keepalive, true, "a keepalive request stays keepalive");
  f.calls[1].done(null);
  assert.deepStrictEqual(got, ["abc", "abc"]);
  assert.strictEqual(q.pending(), false);
});

test("saveQueue still sends the follow-up when no callback (editor) is left", function () {
  var f = fakeSend();
  var q = core.makeSaveQueue(f.send);
  q.push("one", false);
  q.push("two", true); // the editor closed with this text pending
  f.calls[0].done(new Error("x")); // even a failed first save does not drop it
  assert.strictEqual(f.calls.length, 2);
  assert.strictEqual(f.calls[1].text, "two");
});

test("saveQueue reports an error and ignores a second done()", function () {
  var f = fakeSend();
  var q = core.makeSaveQueue(f.send);
  var got = [];
  q.push("a", false, function (err, text) { got.push(err ? err.message : "ok:" + text); });
  f.calls[0].done(new Error("HTTP 500"));
  f.calls[0].done(null);
  assert.deepStrictEqual(got, ["HTTP 500"]);
  assert.strictEqual(q.pending(), false);
});

test("saveQueue survives a throwing send", function () {
  var q = core.makeSaveQueue(function () { throw new Error("boom"); });
  var got = [];
  q.push("a", false, function (err) { got.push(err && err.message); });
  assert.deepStrictEqual(got, ["boom"]);
  assert.strictEqual(q.pending(), false);
});

test("saveQueue.flushNow sends the queued text with keepalive without waiting", function () {
  var f = fakeSend();
  var q = core.makeSaveQueue(f.send);
  q.push("a", false);
  q.push("ab", false);
  q.flushNow();
  assert.strictEqual(f.calls.length, 2);
  assert.strictEqual(f.calls[1].text, "ab");
  assert.strictEqual(f.calls[1].keepalive, true);
  q.flushNow(); // nothing queued: no-op
  assert.strictEqual(f.calls.length, 2);
  // the older save landing does not start anything new
  f.calls[0].done(null);
  assert.strictEqual(f.calls.length, 2);
  assert.strictEqual(q.pending(), true);
  // a push while the flushed save is still in flight waits for it
  q.push("abc", false);
  assert.strictEqual(f.calls.length, 2);
  f.calls[1].done(null);
  assert.strictEqual(f.calls.length, 3);
  assert.strictEqual(f.calls[2].text, "abc");
});

test("saveQueue.latest is null before any push", function () {
  var q = core.makeSaveQueue(function () {});
  assert.strictEqual(q.latest(), null);
  assert.strictEqual(q.pending(), false);
});

test("reviewToday trusts the server's day only on the day it was fetched", function () {
  var now = new Date(2026, 9, 9, 0, 5); // 00:05 on Oct 9 (local)
  assert.strictEqual(core.reviewToday({ today: "2026-10-09" }, "2026-10-09", now), "2026-10-09");
  // fetched yesterday, the tab stayed open past midnight
  assert.strictEqual(core.reviewToday({ today: "2026-10-08" }, "2026-10-08", now), "2026-10-09");
  assert.strictEqual(core.reviewToday(null, "", now), "2026-10-09");
  assert.strictEqual(core.reviewToday({}, "2026-10-09", now), "2026-10-09");
});
