"use strict";
/* SP6 front-end helpers: click-to-reveal hints / Guided solution (D2), the
 * Diff column (D1) and per-run grouping for the recents + Stats fallback (A8). */
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));
var dom = require(path.join(__dirname, "fakedom.js"));

function guidedDoc() {
  return dom.build([
    ["h1", "1. Two Sum"],
    ["p", "Pattern: Arrays & Hashing · Difficulty: Easy"],
    "\n",
    ["h2", "Approach"],
    ["h3", "Hint 1"],
    ["p", "Think about complements."],
    ["h3", "Hint 2"],
    ["p", "A map from value to index."],
    ["h4", "Hint 2 detail"],
    ["p", "still part of hint 2"],
    ["h3", "Brute force"],
    ["p", "Try every pair."],
    ["h2", "Solution"],
    ["pre", "print(1)"],
    ["p", "after the code"],
    ["h2", "Complexity"],
    ["p", "O(n)"],
    ["hr", null],
    ["p", "Verification: PASS"],
  ]);
}

function tags(node) {
  return node.childNodes.filter(function (n) { return n.nodeType === 1; })
    .map(function (n) { return n.tagName.toLowerCase(); });
}

test("revealKind recognises hint headings and (opt-in) the Solution section", function () {
  assert.deepStrictEqual(core.revealKind("H3", "Hint 2", {}),
    { key: "hint-2", kind: "hint", level: 3 });
  assert.deepStrictEqual(core.revealKind("h3", "  hint 4: almost there ", {}).key, "hint-4");
  assert.strictEqual(core.revealKind("H2", "Hint 1", {}), null);        // hints are H3
  assert.strictEqual(core.revealKind("H3", "Hints and tips", {}), null);
  assert.strictEqual(core.revealKind("H2", "Solution", {}), null);      // not opted in
  assert.deepStrictEqual(core.revealKind("H2", "Solution (normal)", { revealSolution: true }),
    { key: "solution", kind: "solution", level: 2 });
  assert.strictEqual(core.revealKind("H2", "Solutions overview", { revealSolution: true }), null);
  assert.strictEqual(core.revealKind("P", "Hint 1", {}), null);
});

test("applyReveals wraps each hint and the Guided solution in a closed <details>", function () {
  var root = guidedDoc();
  var n = core.applyReveals(root, dom.document, { revealSolution: true });
  assert.strictEqual(n, 3);
  assert.deepStrictEqual(tags(root), ["h1", "p", "h2", "details", "details", "h3", "p",
    "details", "h2", "p", "hr", "p"]);
  var hints = root.children.filter(function (e) { return e.tagName === "DETAILS"; });
  var h1 = hints[0];
  assert.strictEqual(h1.className, "reveal reveal-hint");
  assert.strictEqual(h1.getAttribute("data-reveal"), "hint-1");
  assert.ok(!h1.open);
  var summary = h1.children[0];
  assert.strictEqual(summary.tagName, "SUMMARY");
  assert.strictEqual(summary.children[0].tagName, "H3");          // the heading moved in
  assert.strictEqual(summary.children[0].textContent, "Hint 1");
  assert.strictEqual(summary.children[1].textContent, "Show hint");
  var body = h1.children[1];
  assert.strictEqual(body.className, "reveal-body");
  assert.strictEqual(body.textContent, "Think about complements.");
  // Hint 2 keeps its h4 sub-heading; it ends at the next h3
  assert.deepStrictEqual(tags(hints[1].children[1]), ["p", "h4", "p"]);
  // the solution section ends at the next h2 and never swallows the verdict
  var sol = hints[2];
  assert.strictEqual(sol.className, "reveal reveal-solution");
  assert.strictEqual(sol.children[0].children[1].textContent, "Show solution");
  assert.deepStrictEqual(tags(sol.children[1]), ["pre", "p"]);
});

test("applyReveals stops a section at a horizontal rule", function () {
  var root = dom.build([["h2", "Solution"], ["pre", "x"], ["hr", null], ["p", "**Verification:**"]]);
  core.applyReveals(root, dom.document, { revealSolution: true });
  assert.deepStrictEqual(tags(root), ["details", "hr", "p"]);
});

test("applyReveals leaves Answer/Learning solutions visible unless opted in", function () {
  var root = guidedDoc();
  assert.strictEqual(core.applyReveals(root, dom.document, {}), 2);
  assert.ok(root.children.some(function (e) { return e.tagName === "H2" && e.textContent === "Solution"; }));
});

test("applyReveals keeps open state across re-renders and reports toggles", function () {
  var open = { "hint-2": true };
  var toggled = [];
  var root = guidedDoc();
  core.applyReveals(root, dom.document, {
    open: open, onToggle: function (key, isOpen) { toggled.push([key, isOpen]); },
  });
  var d = root.children.filter(function (e) { return e.tagName === "DETAILS"; });
  assert.ok(!d[0].open);
  assert.strictEqual(d[1].open, true);
  d[0].open = true;
  d[0].dispatch("toggle");
  assert.deepStrictEqual(toggled, [["hint-1", true]]);
});

test("applyReveals never uses innerHTML and tolerates empty / plain containers", function () {
  // fakedom throws on any innerHTML access, so the wraps above prove the
  // DOM-built path; here: nothing to wrap is a no-op.
  var root = dom.build([["p", "just text"], "\n"]);
  assert.strictEqual(core.applyReveals(root, dom.document, { revealSolution: true }), 0);
  assert.strictEqual(core.applyReveals(null, dom.document, {}), 0);
  // a hint heading at the very end has an empty body
  var tail = dom.build([["h3", "Hint 1"]]);
  core.applyReveals(tail, dom.document, {});
  assert.strictEqual(tail.children[0].children[1].childNodes.length, 0);
});

test("model text stays text: a heading with markup-looking text is moved, not re-parsed", function () {
  var root = dom.build([["h3", "Hint 1 <img src=x onerror=alert(1)>"], ["p", "<b>x</b>"]]);
  core.applyReveals(root, dom.document, {});
  var details = root.children[0];
  assert.strictEqual(details.children[0].children[0].textContent,
    "Hint 1 <img src=x onerror=alert(1)>");
  assert.strictEqual(details.children[1].textContent, "<b>x</b>");
});

test("isGuidedPath picks Guided docs in the library", function () {
  assert.strictEqual(core.isGuidedPath("guided/hash_map/two_sum.md"), true);
  assert.strictEqual(core.isGuidedPath("answers/hash_map/two_sum__normal.md"), false);
  assert.strictEqual(core.isGuidedPath(""), false);
});

// ---- D1: the Diff column ----------------------------------------------------------
test("diffInfo maps difficulties to the table's classes", function () {
  assert.deepStrictEqual(core.diffInfo("Easy"), { cls: "easy", label: "Easy" });
  assert.deepStrictEqual(core.diffInfo("medium"), { cls: "med", label: "Medium" });
  assert.deepStrictEqual(core.diffInfo("HARD"), { cls: "hard", label: "Hard" });
  assert.strictEqual(core.diffInfo(""), null);
  assert.strictEqual(core.diffInfo("Insane"), null);
  assert.strictEqual(core.diffInfo(null), null);
});

// ---- A8: one run per tier / slot ----------------------------------------------------
test("deriveRuns keeps tiers and __N slots apart (A8)", function () {
  var runs = core.deriveRuns([
    { path: "answers/hash_map/two_sum__normal.md", mtime: 100, verdict: "fail", difficulty: "Easy" },
    { path: "answers/hash_map/two_sum__normal.py", mtime: 101, difficulty: "Easy" },
    { path: "answers/hash_map/two_sum__optimal.md", mtime: 200, verdict: "pass" },
    { path: "answers/hash_map/two_sum__normal__2.md", mtime: 300 },
    { path: "learning/hash_map_learning/two_sum__2.md", mtime: 400 },
  ]);
  assert.strictEqual(runs.length, 4);
  var byPath = {};
  runs.forEach(function (r) { byPath[r.mdPath] = r; });
  var first = byPath["answers/hash_map/two_sum__normal.md"];
  assert.strictEqual(first.tier, "normal");
  assert.strictEqual(first.language, "Python");
  assert.strictEqual(first.verdict, "fail");
  assert.strictEqual(first.difficulty, "Easy");
  assert.strictEqual(first.savedAt, 101);
  var slot = byPath["answers/hash_map/two_sum__normal__2.md"];
  assert.strictEqual(slot.tier, "normal");          // "__2" is a slot, not a tier
  assert.strictEqual(slot.slot, 2);
  assert.strictEqual(slot.problem, "Two Sum");
  var learn = byPath["learning/hash_map_learning/two_sum__2.md"];
  assert.strictEqual(learn.tier, "");
  assert.strictEqual(learn.slot, 2);
});

test("computeStats over derived runs: the A8 4-day scenario gives a streak of 4", function () {
  var now = new Date(2026, 9, 8, 18, 0, 0);
  function at(daysAgo, hour) { return new Date(2026, 9, 8 - daysAgo, hour || 10, 0, 0).getTime() / 1000; }
  var files = [
    { path: "answers/hash_map/two_sum__normal.md", mtime: at(3) },
    { path: "answers/hash_map/two_sum__normal.py", mtime: at(3) },
    { path: "answers/hash_map/two_sum__optimal.md", mtime: at(2) },
    { path: "answers/hash_map/two_sum__normal__2.md", mtime: at(2, 16) },
    { path: "answers/hash_map/two_sum__normal__3.md", mtime: at(1) },
    { path: "learning/hash_map_learning/two_sum.md", mtime: at(0) },
  ];
  var s = core.computeStats(core.deriveRuns(files), now);
  assert.strictEqual(s.currentStreak, 4);
  assert.strictEqual(s.total, 5);
  assert.strictEqual(s.distinctProblems, 1);
});
