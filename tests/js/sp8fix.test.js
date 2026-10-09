"use strict";
/* SP8 final fix round (M7): "Give up / show solution" never picks a deleted
 * doc - candidates are filtered by the library listing and ordered so the
 * caller can fall back to the next one on a 404. */
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));

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
