"use strict";
/* The practice loop's front-end helpers: the Leitner preview and due labels
 * (D4) and the review queue's "today" once a tab stays open past midnight; the
 * re-attempt view - the give-up doc pick that never offers a deleted doc (D3,
 * SP8 fix M7), case statuses, starter code and the code textarea's Tab indent;
 * flashcard deck wrap (D10); and problem notes - the status text and the save
 * queue (D9: one PUT at a time, newest text last, flushNow on pagehide). */
var assert = require("assert");
var path = require("path");
var core = require(path.join(require("./paths.js").STATIC, "lib", "core.js"));

test("leitnerPreview mirrors the server rule", function () {
  assert.deepStrictEqual(core.leitnerPreview(1, "solo"), { box: 2, days: 3 });
  assert.deepStrictEqual(core.leitnerPreview(4, "solo"), { box: 5, days: 30 });
  assert.deepStrictEqual(core.leitnerPreview(5, "solo"), { box: 5, days: 30 });
  assert.deepStrictEqual(core.leitnerPreview(3, "hints"), { box: 3, days: 7 });
  assert.deepStrictEqual(core.leitnerPreview(5, "peeked"), { box: 1, days: 1 });
  assert.deepStrictEqual(core.leitnerPreview(9, "hints"), { box: 5, days: 30 });
  assert.deepStrictEqual(core.leitnerPreview("x", "solo"), { box: 2, days: 3 });
  assert.strictEqual(core.leitnerPreview(2, "bogus"), null);
});

test("dueLabel across month ends and bad input", function () {
  assert.strictEqual(core.dueLabel("2026-10-08", "2026-10-08"), "due today");
  assert.strictEqual(core.dueLabel("2026-11-01", "2026-10-31"), "due tomorrow");
  assert.strictEqual(core.dueLabel("2026-03-03", "2026-02-28"), "due in 3 days");
  assert.strictEqual(core.dueLabel("2026-09-30", "2026-10-01"), "overdue by 1 day");
  assert.strictEqual(core.dueLabel("2026-09-01", "2026-10-01"), "overdue by 30 days");
  assert.strictEqual(core.dueLabel(null, "2026-10-01"), "not scheduled yet");
  assert.strictEqual(core.dueLabel("garbage", "2026-10-01"), "due garbage");
  assert.strictEqual(core.daysBetween("2026-10-01", "nope"), null);
});

test("docCandidatesFor puts the newest Answer/Guided doc first", function () {
  var rec = { log: [
    { ts: "2026-10-01T10:00:00", mode: "answer", files: ["answers/h/1.py", "answers/h/1.md"] },
    { ts: "2026-10-03T10:00:00", mode: "learning", files: ["learning/h_learning/1.md"] },
    { ts: "2026-10-02T10:00:00", mode: "guided", files: ["guided/h/1.md"] },
  ] };
  assert.strictEqual(core.docCandidatesFor(rec)[0], "guided/h/1.md");
  var learnOnly = { log: [{ ts: "1", mode: "learning", files: ["learning/a.md"] },
    { ts: "2", mode: "review", files: ["reviews/a.md"] }] };
  assert.strictEqual(core.docCandidatesFor(learnOnly)[0], "reviews/a.md");
  assert.strictEqual(core.docCandidatesFor({ runs: ["a.py", "a.md", "b.md"] })[0], "b.md");
  assert.deepStrictEqual(core.docCandidatesFor({}), []);
  assert.deepStrictEqual(core.docCandidatesFor(null), []);
});

test("caseInfo maps statuses, unknown -> not run", function () {
  assert.strictEqual(core.caseInfo("pass").cls, "pass");
  assert.strictEqual(core.caseInfo("fail").label, "Wrong answer");
  assert.strictEqual(core.caseInfo("error").cls, "fail");
  assert.strictEqual(core.caseInfo("ran").cls, "none");
  assert.strictEqual(core.caseInfo("weird").label, "Not run");
});

test("starterCode is Python-only and follows the stdin contract", function () {
  var py = core.starterCode("python");
  assert.ok(/sys\.stdin\.read\(\)/.test(py));
  assert.ok(/def solve\(args\)/.test(py));
  assert.ok(py.indexOf("r\"(\\w+)") !== -1);  // a real regex escape, not a newline
  assert.strictEqual(core.starterCode("cpp"), "");
});

test("indentText: caret insert, block indent and outdent", function () {
  var r = core.indentText("ab", 1, 1, false);
  assert.deepStrictEqual(r, { value: "a    b", start: 5, end: 5 });
  r = core.indentText("x\ny\nz", 0, 3, false);  // lines x and y
  assert.strictEqual(r.value, "    x\n    y\nz");
  assert.deepStrictEqual([r.start, r.end], [4, 11]);
  r = core.indentText("    x\n  y\nz", 2, 8, true);
  assert.strictEqual(r.value, "x\ny\nz");
  assert.deepStrictEqual([r.start, r.end], [0, 2]);
  r = core.indentText("\tx", 2, 2, true);  // outdent a tab with a caret
  assert.deepStrictEqual(r, { value: "x", start: 1, end: 1 });
  r = core.indentText("x\ny\n", 0, 2, false);  // selection ending at a line start
  assert.strictEqual(r.value, "    x\ny\n");
  r = core.indentText("abc", 1, 1, true);  // nothing to outdent
  assert.deepStrictEqual(r, { value: "abc", start: 1, end: 1 });
});

test("wrapIndex wraps both ways", function () {
  assert.strictEqual(core.wrapIndex(0, 3, -1), 2);
  assert.strictEqual(core.wrapIndex(2, 3, 1), 0);
  assert.strictEqual(core.wrapIndex(1, 3, 1), 2);
  assert.strictEqual(core.wrapIndex(0, 0, 1), 0);
});

test("notesStatus wording", function () {
  assert.strictEqual(core.notesStatus("saved"), "Saved");
  assert.strictEqual(core.notesStatus("saving"), "Saving…");
  assert.strictEqual(core.notesStatus("dirty"), "Unsaved changes");
  assert.ok(/^Not saved \(offline\)/.test(core.notesStatus("error", { reason: "offline" })));
  assert.strictEqual(core.notesStatus("idle"), "");
  assert.strictEqual(core.plural(1, "card"), "1 card");
  assert.strictEqual(core.plural(2, "card"), "2 cards");
});

// ---- the notes save queue -------------------------------------------------------
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

// ---- the review queue's today -----------------------------------------------------
test("reviewToday trusts the server's day only on the day it was fetched", function () {
  var now = new Date(2026, 9, 9, 0, 5); // 00:05 on Oct 9 (local)
  assert.strictEqual(core.reviewToday({ today: "2026-10-09" }, "2026-10-09", now), "2026-10-09");
  // fetched yesterday, the tab stayed open past midnight
  assert.strictEqual(core.reviewToday({ today: "2026-10-08" }, "2026-10-08", now), "2026-10-09");
  assert.strictEqual(core.reviewToday(null, "", now), "2026-10-09");
  assert.strictEqual(core.reviewToday({}, "2026-10-09", now), "2026-10-09");
});

// ---- give up / show solution never picks a deleted doc (SP8 fix M7) ---------------
var REC = {
  log: [
    { ts: "2026-10-01T10:00:00", mode: "answer", files: ["answers/h/1.py", "answers/h/1.md"] },
    { ts: "2026-10-03T10:00:00", mode: "learning", files: ["learning/h_learning/1.md"] },
    { ts: "2026-10-02T10:00:00", mode: "guided", files: ["guided/h/1.md"] },
  ],
  runs: ["answers/h/1.py", "answers/h/1.md", "guided/h/1.md", "learning/h_learning/1.md",
    "reviews/h/1.md"],
};

test("docCandidatesFor: solutions newest first, then other modes, then record runs", function () {
  assert.deepStrictEqual(core.docCandidatesFor(REC), [
    "guided/h/1.md", "answers/h/1.md", "learning/h_learning/1.md", "reviews/h/1.md",
  ]);
});

test("docCandidatesFor skips docs missing from the library listing (map or array)", function () {
  var listing = { "answers/h/1.md": { path: "answers/h/1.md" }, "answers/h/1.py": {} };
  assert.deepStrictEqual(core.docCandidatesFor(REC, listing), ["answers/h/1.md"]);
  var arr = [{ path: "learning/h_learning/1.md" }, "reviews/h/1.md"];
  assert.deepStrictEqual(core.docCandidatesFor(REC, arr),
    ["learning/h_learning/1.md", "reviews/h/1.md"]);
});

test("every doc deleted: no candidate (no 404 fetch)", function () {
  assert.deepStrictEqual(core.docCandidatesFor(REC, {}), []);
  assert.deepStrictEqual(core.docCandidatesFor(REC, []), []);
  // a key inherited from Object.prototype is not a library file
  assert.deepStrictEqual(core.docCandidatesFor({ runs: ["constructor.md"] }, {}), []);
});

test("docCandidatesFor: no duplicates, tolerant of junk", function () {
  var rec = { log: [{ ts: "1", mode: "answer", files: ["a.md"] },
    { ts: "2", mode: "answer", files: ["a.md"] }], runs: ["a.md", "a.py"] };
  assert.deepStrictEqual(core.docCandidatesFor(rec), ["a.md"]);
  assert.deepStrictEqual(core.docCandidatesFor(null), []);
  assert.deepStrictEqual(core.docCandidatesFor({}), []);
});
