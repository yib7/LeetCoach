"use strict";
/* SP7 front-end helpers: the practice loop - Leitner preview (D4), due labels,
 * the give-up doc pick and case statuses (D3), the code textarea's Tab indent,
 * flashcard deck wrap (D10), notes status (D9) and the Code Review mode name (D5). */
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));

test("modeName / modeLabel / untiered know Code Review", function () {
  assert.strictEqual(core.modeName("review"), "Code Review");
  assert.strictEqual(core.modeName("guided"), "Guided");
  assert.strictEqual(core.modeName("answer"), "Answer");
  assert.strictEqual(core.modeLabel("reviews"), "Code Review");
  assert.strictEqual(core.untiered("review"), true);
  assert.strictEqual(core.untiered("learning"), true);
  assert.strictEqual(core.untiered("answer"), false);
});

test("summaryActions for a review: open + learn only", function () {
  var acts = core.summaryActions({ mode: "review", language: "python", mdPath: "reviews/x.md" });
  assert.deepStrictEqual(acts.map(function (a) { return a.id; }), ["open", "learn"]);
  var answer = core.summaryActions({ mode: "answer", language: "python", tier: "brute" });
  assert.ok(answer.some(function (a) { return a.id === "lang-java"; }));
});

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
