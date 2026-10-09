"use strict";
/* Phase 3A web fixes: W6 (only the newest library / file request may update
 * the page) and W8 (Quick Ask sends only the problem context the server uses). */
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));

// ---- W6: makeRequestSeq -------------------------------------------------------

test("makeRequestSeq: only the newest request is current", function () {
  var seq = core.makeRequestSeq();
  var a = seq.start("a.md");
  var b = seq.start("b.md");
  // A's response lands last: it is stale and must be dropped
  assert.strictEqual(seq.done(b), true);
  assert.strictEqual(seq.done(a), false);
  assert.strictEqual(seq.isCurrent(a), false);
  assert.strictEqual(seq.isCurrent(b), true);
});

test("makeRequestSeq: a stale response that lands first is still dropped", function () {
  var seq = core.makeRequestSeq();
  var a = seq.start("a.md");
  var b = seq.start("b.md");
  assert.strictEqual(seq.done(a), false);
  assert.strictEqual(seq.pending(), "b.md"); // B is still on its way
  assert.strictEqual(seq.done(b), true);
  assert.strictEqual(seq.pending(), null);
});

test("makeRequestSeq: pending() names the in-flight newest request", function () {
  var seq = core.makeRequestSeq();
  assert.strictEqual(seq.pending(), null);
  var t = seq.start("a.md");
  assert.strictEqual(seq.pending(), "a.md");
  seq.done(t);
  assert.strictEqual(seq.pending(), null);
  seq.start(); // no tag
  assert.strictEqual(seq.pending(), "");
});

test("makeRequestSeq: invalidate() drops whatever is in flight", function () {
  var seq = core.makeRequestSeq();
  var t = seq.start("a.md");
  seq.invalidate(); // e.g. the viewer was closed
  assert.strictEqual(seq.pending(), null);
  assert.strictEqual(seq.done(t), false);
  var u = seq.start("b.md");
  assert.strictEqual(seq.done(u), true);
});

test("makeRequestSeq: separate sequences are independent", function () {
  var files = core.makeRequestSeq();
  var lib = core.makeRequestSeq();
  var f = files.start("a.md");
  var l = lib.start();
  lib.start();
  assert.strictEqual(files.done(f), true);
  assert.strictEqual(lib.done(l), false);
});

// app.js is one DOM-bound IIFE the zero-dependency harness cannot boot, so the
// wiring of the guard into it is checked at the source level.
var fs = require("fs");
var APP = fs.readFileSync(path.join(__dirname, "..", "..", "static", "app.js"), "utf8");

function fnBody(name) {
  var start = APP.indexOf("  function " + name + "(");
  assert.ok(start !== -1, name + " not found in app.js");
  var end = APP.indexOf("\n  }\n", start);
  return APP.slice(start, end);
}

test("app.js: openFile drops a response that is no longer the newest open", function () {
  var body = fnBody("openFile");
  assert.ok(/fileSeq\.start\(relPath\)/.test(body), body);
  // both the success and the error branch check the token before touching the page
  assert.strictEqual(body.match(/if \(!fileSeq\.isCurrent\(token\)\) return;/g).length, 2, body);
  assert.ok(body.indexOf("fileSeq.isCurrent(token)") < body.indexOf("showFile("), body);
});

test("app.js: loadLibrary drops a stale listing; closeViewer and fuFinish respect opens", function () {
  var body = fnBody("loadLibrary");
  assert.ok(/libSeq\.start\(\)/.test(body), body);
  assert.strictEqual(body.match(/if \(!libSeq\.isCurrent\(token\)\) return libNewest;/g).length, 2,
    body);
  assert.ok(body.indexOf("libSeq.isCurrent(token)") < body.indexOf("libFiles ="), body);
  assert.ok(/fileSeq\.invalidate\(\)/.test(fnBody("closeViewer")));
  var fin = fnBody("fuFinish");
  assert.ok(fin.indexOf("fileSeq.pending()") !== -1 &&
    fin.indexOf("fileSeq.pending()") < fin.indexOf("openFile(fu.path)"), fin);
});

// ---- W8: clipChars -------------------------------------------------------------

test("clipChars keeps short text and clips long text to n characters", function () {
  assert.strictEqual(core.clipChars("abc", 5), "abc");
  assert.strictEqual(core.clipChars("abcdef", 3), "abc");
  assert.strictEqual(core.clipChars("", 3), "");
  assert.strictEqual(core.clipChars(null, 3), "");
  var big = new Array(2 * 1024 * 1024 + 1).join("x");
  assert.strictEqual(core.clipChars(big, 6000).length, 6000);
});

test("clipChars counts code points like the server's Python slice", function () {
  // "😀" is one Python character but two UTF-16 units; never split the pair
  var s = "a😀b😀c";
  assert.strictEqual(core.clipChars(s, 2), "a😀");
  assert.strictEqual(core.clipChars(s, 3), "a😀b");
  assert.strictEqual(core.clipChars(s, 5), s);
  var emoji = new Array(7001).join("😀");
  var clipped = core.clipChars(emoji, 6000);
  assert.strictEqual(Array.from(clipped).length, 6000);
});
