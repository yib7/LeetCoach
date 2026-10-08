"use strict";
/* SP6 fix round, front end: hint extents (I1), Key insight reveal (M5),
 * summary a11y structure (M6), open-state tracking on summary click (M7),
 * tier only for Answer (O2) and display titles (O3). */
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));
var dom = require(path.join(__dirname, "fakedom.js"));

function tags(node) {
  return node.childNodes.filter(function (n) { return n.nodeType === 1; })
    .map(function (n) { return n.tagName.toLowerCase(); });
}
function reveals(root) {
  return root.children.filter(function (e) { return e.tagName === "DETAILS"; });
}
function byKey(root, key) {
  return reveals(root).filter(function (d) { return d.getAttribute("data-reveal") === key; })[0];
}
function bodyOf(details) { return details.children[1]; }

// ---- I1: a hint never swallows the rest of ## Approach ----------------------------
test("the last hint keeps only its first block when no heading follows it", function () {
  var root = dom.build([
    ["h2", "Approach"],
    ["h3", "Hint 4"],
    ["p", "Walk once and look up the complement."],
    ["p", "Brute force: try every pair."],
    ["p", "Optimal: one pass with a map."],
    ["h2", "Solution"],
    ["pre", "x"],
  ]);
  core.applyReveals(root, dom.document, {});
  var h4 = byKey(root, "hint-4");
  assert.deepStrictEqual(tags(bodyOf(h4)), ["p"]);
  assert.strictEqual(bodyOf(h4).textContent, "Walk once and look up the complement.");
  // the walkthrough paragraphs stay visible, outside the reveal
  assert.deepStrictEqual(tags(root), ["h2", "h3", "details", "p", "p", "h2", "pre"]);
});

test("a hint ends at the next heading of any level", function () {
  var root = dom.build([
    ["h2", "Approach"],
    ["h3", "Hint 3"], ["p", "Remember values."], ["p", "With their index."],
    ["h3", "Hint 4"], ["p", "Walk once."],
    ["h3", "Walkthrough"], ["p", "Brute force..."], ["p", "Optimal..."],
    ["h2", "Solution"],
  ]);
  core.applyReveals(root, dom.document, {});
  assert.deepStrictEqual(tags(bodyOf(byKey(root, "hint-3"))), ["p", "p"]);
  assert.strictEqual(bodyOf(byKey(root, "hint-4")).textContent, "Walk once.");
  var sub = dom.build([["h3", "Hint 1"], ["p", "a"], ["h4", "Detail"], ["p", "b"]]);
  core.applyReveals(sub, dom.document, {});
  assert.deepStrictEqual(tags(bodyOf(byKey(sub, "hint-1"))), ["p"]);
});

test("the last-hint rule also stops at a rule or the end of the doc", function () {
  var root = dom.build([["h3", "Hint 4"], "\n", ["ul", "a list"], ["p", "more"], ["hr", null], ["p", "v"]]);
  core.applyReveals(root, dom.document, {});
  assert.deepStrictEqual(tags(bodyOf(byKey(root, "hint-4"))), ["ul"]);
  var tail = dom.build([["h3", "Hint 2"], ["p", "one"], ["p", "two"]]);
  core.applyReveals(tail, dom.document, {});
  assert.deepStrictEqual(tags(bodyOf(byKey(tail, "hint-2"))), ["p"]);
});

// ---- M5: Key insight hidden in Guided / Learning ------------------------------------
test("Key insight is revealed on demand when opted in; recognition stays visible", function () {
  function doc() {
    return dom.build([
      ["h2", "How to recognize this pattern"], ["p", "pair with a fixed sum"],
      ["h2", "Key insight"], ["p", "partner = target - x"], ["h3", "Why"], ["p", "lookup"],
      ["h2", "Approach"], ["h3", "Hint 1"], ["p", "nudge"],
    ]);
  }
  var root = doc();
  core.applyReveals(root, dom.document, { revealInsight: true });
  var ki = byKey(root, "insight");
  assert.ok(ki && !ki.open);
  assert.strictEqual(ki.className, "reveal reveal-insight");
  assert.deepStrictEqual(tags(bodyOf(ki)), ["p", "h3", "p"]);
  assert.strictEqual(ki.children[0].children[1].textContent, "Show key insight");
  assert.strictEqual(root.children[0].tagName, "H2");          // recognition untouched
  assert.strictEqual(root.children[1].textContent, "pair with a fixed sum");
  var answer = doc();
  core.applyReveals(answer, dom.document, {});
  assert.strictEqual(byKey(answer, "insight"), undefined);   // Answer: visible
  assert.deepStrictEqual(core.revealKind("H2", "Key insight", { revealInsight: true }),
    { key: "insight", kind: "insight", level: 2 });
  assert.strictEqual(core.revealKind("H2", "Key insight", {}), null);
  assert.strictEqual(core.revealsFor("guided").revealInsight, true);
  assert.strictEqual(core.revealsFor("guided").revealSolution, true);
  assert.strictEqual(core.revealsFor("learning").revealInsight, true);
  assert.strictEqual(core.revealsFor("learning").revealSolution, false);
  assert.strictEqual(core.revealsFor("answer").revealInsight, false);
  assert.strictEqual(core.revealsFor("answer").revealSolution, false);
});

// ---- M6: no heading inside <summary> -------------------------------------------------
test("summary holds a text span, the heading stays a (visually hidden) heading", function () {
  var root = dom.build([["h2", "Approach"], ["h3", "Hint 1"], ["p", "nudge"], ["h3", "Hint 2"], ["p", "more"]]);
  core.applyReveals(root, dom.document, {});
  var d = byKey(root, "hint-1");
  var summary = d.children[0];
  assert.strictEqual(summary.tagName, "SUMMARY");
  summary.children.forEach(function (c) { assert.ok(!/^H[1-6]$/.test(c.tagName), c.tagName); });
  assert.strictEqual(summary.children[0].className, "reveal-title");
  assert.strictEqual(summary.children[0].textContent, "Hint 1");
  assert.strictEqual(summary.children[1].className, "reveal-cue");
  // the original heading is still in the document, right before its reveal,
  // so heading navigation finds every hint
  var idx = root.childNodes.indexOf(d);
  var heading = root.childNodes[idx - 1];
  assert.strictEqual(heading.tagName, "H3");
  assert.strictEqual(heading.textContent, "Hint 1");
  assert.ok(/\bsr-only\b/.test(heading.className) && /\breveal-heading\b/.test(heading.className));
  // a second pass does not wrap the hidden headings again
  assert.strictEqual(core.applyReveals(root, dom.document, {}), 0);
  assert.strictEqual(reveals(root).length, 2);
});

// ---- M7: open state is recorded on the summary click --------------------------------
test("open state is reported synchronously on summary click, not on toggle", function () {
  var seen = [];
  var root = dom.build([["h3", "Hint 1"], ["p", "a"]]);
  core.applyReveals(root, dom.document, { onToggle: function (k, o) { seen.push([k, o]); } });
  var d = byKey(root, "hint-1");
  d.children[0].dispatch("click");                 // closed -> about to open
  assert.deepStrictEqual(seen, [["hint-1", true]]);
  d.open = true;
  d.dispatch("toggle");                            // the async toggle is ignored
  assert.deepStrictEqual(seen, [["hint-1", true]]);
  d.children[0].dispatch("click");                 // open -> about to close
  assert.deepStrictEqual(seen, [["hint-1", true], ["hint-1", false]]);
});

// ---- O2: tier only for Answer --------------------------------------------------------
test("tierApplies is Answer-only and the Optimal re-run follows it", function () {
  assert.strictEqual(core.tierApplies("answer"), true);
  assert.strictEqual(core.tierApplies("Answer"), true);
  assert.strictEqual(core.tierApplies("guided"), false);
  assert.strictEqual(core.tierApplies("Learning"), false);
  assert.strictEqual(core.tierApplies(""), false);
  var ids = core.summaryActions({ mode: "guided", language: "python", tier: "basic" })
    .map(function (a) { return a.id; });
  assert.strictEqual(ids.indexOf("optimal"), -1);
});

// ---- O3: display titles -----------------------------------------------------------------
test("problemTitle prefers the record and splits the number off", function () {
  assert.deepStrictEqual(core.problemTitle({ title: "Two Sum", number: 1, stem: "1_two_sum" }),
    { title: "Two Sum", number: 1, label: "#1 · Two Sum" });
  assert.deepStrictEqual(core.problemTitle({ stem: "1_two_sum" }),
    { title: "Two Sum", number: 1, label: "#1 · Two Sum" });
  assert.deepStrictEqual(core.problemTitle({ stem: "two_sum" }),
    { title: "Two Sum", number: null, label: "Two Sum" });
  assert.strictEqual(core.problemTitle({ stem: "3sum" }).title, "3sum");      // no space: kept
  assert.strictEqual(core.problemTitle({ title: "1. Two Sum" }).title, "Two Sum");
  assert.strictEqual(core.problemTitle({ title: "1) Two Sum" }).number, 1);
  assert.strictEqual(core.problemTitle({ stem: "" }).label, "");
});

test("deriveRuns shows the record title and number, legacy names stripped", function () {
  var runs = core.deriveRuns([
    { path: "guided/hash_map/1_two_sum.md", mtime: 3, title: "Two Sum", number: 1 },
    { path: "answers/heap/215_kth_largest_element_in_an_array__optimal.md", mtime: 2 },
    { path: "answers/stack/valid_parentheses__optimal.md", mtime: 1 },
  ]);
  var byPath = {};
  runs.forEach(function (r) { byPath[r.mdPath] = r; });
  var a = byPath["guided/hash_map/1_two_sum.md"];
  assert.strictEqual(a.problem, "Two Sum");
  assert.strictEqual(a.number, 1);
  assert.strictEqual(a.problemLabel, "#1 · Two Sum");
  var b = byPath["answers/heap/215_kth_largest_element_in_an_array__optimal.md"];
  assert.strictEqual(b.problem, "Kth Largest Element In An Array");
  assert.strictEqual(b.number, 215);
  var c = byPath["answers/stack/valid_parentheses__optimal.md"];
  assert.strictEqual(c.problemLabel, "Valid Parentheses");
  assert.strictEqual(c.number, null);
});
