"use strict";
/* Click-to-reveal sections in a rendered doc (core.applyReveals / revealKind /
 * revealsFor): hints (D2) and where a hint ends (I1), the Guided solution and
 * the Key insight (M5), the summary's a11y structure (M6) and open-state
 * tracking on the summary click (M7). */
var assert = require("assert");
var path = require("path");
var core = require(path.join(require("./paths.js").STATIC, "lib", "core.js"));
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
function reveals(root) {
  return root.children.filter(function (e) { return e.tagName === "DETAILS"; });
}
function byKey(root, key) {
  return reveals(root).filter(function (d) { return d.getAttribute("data-reveal") === key; })[0];
}
function bodyOf(details) { return details.children[1]; }

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
  // each heading stays in place (visually hidden, SP6 fix M6) before its reveal
  assert.deepStrictEqual(tags(root), ["h1", "p", "h2", "h3", "details", "h3", "details", "h4",
    "p", "h3", "p", "h2", "details", "h2", "p", "hr", "p"]);
  var hints = root.children.filter(function (e) { return e.tagName === "DETAILS"; });
  var h1 = hints[0];
  assert.strictEqual(h1.className, "reveal reveal-hint");
  assert.strictEqual(h1.getAttribute("data-reveal"), "hint-1");
  assert.ok(!h1.open);
  var summary = h1.children[0];
  assert.strictEqual(summary.tagName, "SUMMARY");
  assert.strictEqual(summary.children[0].tagName, "SPAN");        // text, not the heading
  assert.strictEqual(summary.children[0].textContent, "Hint 1");
  assert.strictEqual(summary.children[1].textContent, "Show hint");
  var body = h1.children[1];
  assert.strictEqual(body.className, "reveal-body");
  assert.strictEqual(body.textContent, "Think about complements.");
  // SP6 fix I1: a hint ends at the next heading of any level (here an h4)
  assert.deepStrictEqual(tags(hints[1].children[1]), ["p"]);
  // the solution section ends at the next h2 and never swallows the verdict
  var sol = hints[2];
  assert.strictEqual(sol.className, "reveal reveal-solution");
  assert.strictEqual(sol.children[0].children[1].textContent, "Show solution");
  assert.deepStrictEqual(tags(sol.children[1]), ["pre", "p"]);
});

test("applyReveals stops a section at a horizontal rule", function () {
  var root = dom.build([["h2", "Solution"], ["pre", "x"], ["hr", null], ["p", "**Verification:**"]]);
  core.applyReveals(root, dom.document, { revealSolution: true });
  assert.deepStrictEqual(tags(root), ["h2", "details", "hr", "p"]);
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
  d[0].children[0].dispatch("click"); // SP6 fix M7: reported on the summary click
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
  assert.strictEqual(tail.children[1].children[1].childNodes.length, 0);
});

test("model text stays text: a heading with markup-looking text is moved, not re-parsed", function () {
  var root = dom.build([["h3", "Hint 1 <img src=x onerror=alert(1)>"], ["p", "<b>x</b>"]]);
  core.applyReveals(root, dom.document, {});
  var details = root.children[1];
  assert.strictEqual(details.children[0].children[0].textContent,
    "Hint 1 <img src=x onerror=alert(1)>");
  assert.strictEqual(details.children[1].textContent, "<b>x</b>");
});

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
