"use strict";
var assert = require("assert");
var path = require("path");
var core = require(path.join(require("./paths.js").STATIC, "lib", "core.js"));

// ---- model chip --------------------------------------------------------------
test("modelLabel maps known ids and falls back to the raw id", function () {
  assert.strictEqual(core.modelLabel("claude-opus-5-5"), "Opus 5.5");
  assert.strictEqual(core.modelLabel("claude-fable-5-1"), "Fable 5.1");
  assert.strictEqual(core.modelLabel("claude-sonnet-4-5-20250929"), "Sonnet 4.5");
  assert.strictEqual(core.modelLabel("claude-opus-4-20250514"), "Opus 4");
  assert.strictEqual(core.modelLabel("claude-3-5-haiku-20241022"), "Haiku 3.5");
  assert.strictEqual(core.modelLabel("claude-sonnet-5-5[1m]"), "Sonnet 5.5");
  assert.strictEqual(core.modelLabel("haiku"), "Haiku");
  assert.strictEqual(core.modelLabel("gpt-something"), "gpt-something");
  assert.strictEqual(core.modelLabel("claude-mystery-9"), "claude-mystery-9");
  assert.strictEqual(core.modelLabel(""), "");
  assert.strictEqual(core.modelLabel(null), "");
});

// ---- run lifecycle -------------------------------------------------------------
test("retryDelay doubles and then gives up", function () {
  assert.deepStrictEqual([0, 1, 2, 3].map(core.retryDelay), [400, 800, 1600, 3200]);
  assert.strictEqual(core.retryDelay(4), -1);
  assert.strictEqual(core.retryDelay(-1), -1);
});

test("phaseText covers every phase", function () {
  assert.strictEqual(core.phaseText({ phase: "streaming" }).label, "Streaming");
  assert.strictEqual(core.phaseText({ phase: "verifying" }).label, "Verifying");
  var v = core.phaseText({ phase: "verifying", i: 2, n: 3 });
  assert.strictEqual(v.label, "Verifying 2/3");
  assert.strictEqual(v.note, "verifying sample 2 of 3…");
  assert.strictEqual(core.phaseText({ phase: "saving" }).label, "Saving");
  assert.strictEqual(core.phaseText(null).label, "Streaming");
});

test("hasOpenFence tracks fence open/close", function () {
  assert.strictEqual(core.hasOpenFence(""), false);
  assert.strictEqual(core.hasOpenFence("text\n```python\nx = 1\n"), true);
  assert.strictEqual(core.hasOpenFence("```python\nx = 1\n```\n"), false);
  assert.strictEqual(core.hasOpenFence("````\n```\nstill open\n"), true);
  assert.strictEqual(core.hasOpenFence("````\n```\n````\n"), false);
  assert.strictEqual(core.hasOpenFence("~~~\ncode\n```\n"), true);
  assert.strictEqual(core.hasOpenFence("inline ```code``` here\n"), false);
  assert.strictEqual(core.hasOpenFence("```python solution\nx\n``` trailing\n"), true);
});

test("hasOpenFence handles fences indented inside list items (R3)", function () {
  assert.strictEqual(core.hasOpenFence("1. step\n\n    ```python\n    x=1"), true);
  assert.strictEqual(core.hasOpenFence("1. step\n\n    ```python\n    x=1\n    ```\n"), false);
  assert.strictEqual(core.hasOpenFence("- a\n\n   ```\n   y\n   ```\n\nafter"), false);
  assert.strictEqual(core.hasOpenFence("1. step\n\n\t```js\n\tx\n\t```\n"), false);
  // a dedented closer ends the list item, and the block with it
  assert.strictEqual(core.hasOpenFence("1. s\n\n    ```\n    x\n```\n"), false);
  // a ``` nested deeper than the opener is code inside the block
  assert.strictEqual(core.hasOpenFence("```python\ndef f():\n    \"\"\"\n    ```\n"), true);
  assert.strictEqual(core.hasOpenFence("```python\r\nx\r\n```\r\n"), false);
});

test("runEventKind maps terminal SSE events to finish kinds (R8)", function () {
  assert.strictEqual(core.runEventKind("done"), "done");
  assert.strictEqual(core.runEventKind("error"), "error");
  assert.strictEqual(core.runEventKind("cancelled"), "stopped");
  assert.strictEqual(core.runEventKind("phase"), null);
  assert.strictEqual(core.runEventKind("meta"), null);
  assert.strictEqual(core.runEventKind(null), null);
});

test("cancelOutcome reads the /run/cancel answer (R8)", function () {
  assert.strictEqual(core.cancelOutcome(200, { cancelled: false }), "committed");
  assert.strictEqual(core.cancelOutcome(200, { cancelled: true }), "cancelled");
  assert.strictEqual(core.cancelOutcome(404, { cancelled: false }), "cancelled");
  assert.strictEqual(core.cancelOutcome(200, null), "cancelled");
  assert.strictEqual(core.cancelOutcome(0, null), "cancelled");
});

test("runRetryDelay retries a 409 only for the current run (R8)", function () {
  assert.strictEqual(core.runRetryDelay(200, 0, true), -1);
  assert.strictEqual(core.runRetryDelay(500, 0, true), -1);
  assert.strictEqual(core.runRetryDelay(409, 0, true), 400);
  assert.strictEqual(core.runRetryDelay(409, 3, true), 3200);
  assert.strictEqual(core.runRetryDelay(409, 4, true), -1);
  assert.strictEqual(core.runRetryDelay(409, 0, false), -1);
});

test("needsReplaceConfirm only when a different draft would be lost (R4)", function () {
  assert.strictEqual(core.needsReplaceConfirm("", "Two Sum"), false);
  assert.strictEqual(core.needsReplaceConfirm("   \n", "Two Sum"), false);
  assert.strictEqual(core.needsReplaceConfirm("Two Sum", "Two Sum"), false);
  assert.strictEqual(core.needsReplaceConfirm("Two Sum  \n", "Two Sum"), false);
  assert.strictEqual(core.needsReplaceConfirm("3Sum draft", "Two Sum"), true);
  assert.strictEqual(core.needsReplaceConfirm("3Sum draft", ""), false);
});

test("throttleDelay waits out the interval", function () {
  assert.strictEqual(core.throttleDelay(0, 1000, 150), 0);
  assert.strictEqual(core.throttleDelay(1000, 1050, 150), 100);
  assert.strictEqual(core.throttleDelay(1000, 1200, 150), 0);
});

test("nearEdge compares the live edge with the view bottom", function () {
  assert.strictEqual(core.nearEdge(900, 880), true);
  assert.strictEqual(core.nearEdge(1200, 880), false);
  assert.strictEqual(core.nearEdge(1200, 880, 400), true);
});

test("libRelPath maps an absolute saved path onto the listing", function () {
  var files = [{ path: "answers/hm/two_sum__normal.md" }, { path: "two_sum__normal.md" }];
  assert.strictEqual(core.libRelPath("C:\\out\\answers\\hm\\two_sum__normal.md", files),
    "answers/hm/two_sum__normal.md");
  assert.strictEqual(core.libRelPath("/home/u/out/answers/hm/two_sum__normal.md", files),
    "answers/hm/two_sum__normal.md");
  assert.strictEqual(core.libRelPath("/x/other.md", files), "");
  assert.strictEqual(core.libRelPath("/x/xtwo_sum__normal.md", files), "");
});

// ---- verdicts -------------------------------------------------------------------
test("verdictFromLine mirrors the server", function () {
  assert.strictEqual(core.verdictFromLine("✓ Sample tests PASS (all 2 sample(s) passed)"), "pass");
  assert.strictEqual(core.verdictFromLine("✗ Sample tests FAIL (1/3 sample(s) passed, 1 errored)"), "fail");
  assert.strictEqual(core.verdictFromLine("✗ Sample tests ERROR (code errored on 1/1 sample(s))"), "error");
  assert.strictEqual(core.verdictFromLine("⚠ not auto-verified (no sample I/O found)"), "not_verified");
  assert.strictEqual(core.verdictFromLine(""), "");
});

test("verdictInfo gives a class, glyph and label for each verdict", function () {
  assert.strictEqual(core.verdictInfo("pass").cls, "pass");
  assert.strictEqual(core.verdictInfo("fail").cls, "fail");
  assert.strictEqual(core.verdictInfo("error").cls, "fail");
  assert.strictEqual(core.verdictInfo("not_verified").cls, "warn");
  assert.strictEqual(core.verdictInfo("").cls, "none");
  ["pass", "fail", "error", "not_verified", ""].forEach(function (v) {
    assert.ok(core.verdictInfo(v).label.length > 3);
  });
});

// ---- library runs -----------------------------------------------------------------
test("deriveRuns groups a run's files and keeps its doc and verdict", function () {
  // SP6/A8: tiers are separate runs (they used to collapse into one).
  var runs = core.deriveRuns([
    { path: "answers/hash_map/two_sum__normal.md", mtime: 100, verdict: "fail" },
    { path: "answers/hash_map/two_sum__normal.py", mtime: 101 },
    { path: "answers/hash_map/two_sum__optimal.md", mtime: 200, verdict: "pass" },
    { path: "learning/hash_map_learning/two_sum.md", mtime: 50 },
    { path: "loose.md", mtime: 10 },
  ]);
  assert.strictEqual(runs.length, 3);
  var ans = runs[0];
  assert.strictEqual(ans.mode, "Answer");
  assert.strictEqual(ans.topic, "Hash Map");
  assert.strictEqual(ans.problem, "Two Sum");
  assert.strictEqual(ans.mdPath, "answers/hash_map/two_sum__normal.md");
  assert.strictEqual(ans.verdict, "fail");
  assert.strictEqual(ans.language, "Python");
  assert.strictEqual(ans.savedAt, 101);
  assert.strictEqual(runs[1].mdPath, "answers/hash_map/two_sum__optimal.md");
  assert.strictEqual(runs[1].verdict, "pass");
  assert.strictEqual(runs[1].tier, "optimal");
  assert.strictEqual(runs[2].mode, "Learning");
  assert.strictEqual(runs[2].topic, "Hash Map");
  assert.strictEqual(runs[2].verdict, "");
});

test("runSiblings matches the server's scope=run", function () {
  var files = [
    { path: "answers/hm/two_sum__normal.md" },
    { path: "answers/hm/two_sum__normal.py" },
    { path: "answers/hm/two_sum__normal__2.md" },
    { path: "answers/hm/two_sum__optimal.py" },
    { path: "answers/other/two_sum__normal.py" },
    { path: "answers/hm/two_sum__normal.json" },
  ];
  assert.deepStrictEqual(core.runSiblings(files, "answers/hm/two_sum__normal.md"),
    ["answers/hm/two_sum__normal.md", "answers/hm/two_sum__normal.py"]);
  assert.deepStrictEqual(core.runSiblings([], "a/b/c.md"), ["a/b/c.md"]);
});

// ---- stats --------------------------------------------------------------------------
test("computeStats counts streaks across days", function () {
  var now = new Date(2026, 9, 8, 12, 0, 0);
  function at(daysAgo) { return new Date(2026, 9, 8 - daysAgo, 10, 0, 0).getTime() / 1000; }
  var runs = [
    { problem: "A", mode: "Answer", language: "Python", topic: "X", savedAt: at(0) },
    { problem: "B", mode: "Learning", language: "—", topic: "", savedAt: at(1) },
    { problem: "A", mode: "Answer", language: "Python", topic: "X", savedAt: at(2) },
    { problem: "C", mode: "Guided", language: "Java", topic: "Y", savedAt: at(5) },
  ];
  var s = core.computeStats(runs, now);
  assert.strictEqual(s.total, 4);
  assert.strictEqual(s.distinctProblems, 3);
  assert.strictEqual(s.today, 1);
  assert.strictEqual(s.thisWeek, 4);
  assert.strictEqual(s.currentStreak, 3);
  assert.strictEqual(s.longestStreak, 3);
  assert.strictEqual(s.byLanguage.Unknown, 1);
  assert.strictEqual(s.byTopic.Uncategorized, 1);
  assert.strictEqual(s.heatmap.length, 119);
  assert.strictEqual(s.heatmap[118].count, 1);
});

test("computeStats streak grace: yesterday still counts", function () {
  var now = new Date(2026, 9, 8, 8, 0, 0);
  var y = new Date(2026, 9, 7, 22, 0, 0).getTime() / 1000;
  assert.strictEqual(core.computeStats([{ savedAt: y }], now).currentStreak, 1);
  var old = new Date(2026, 9, 5, 22, 0, 0).getTime() / 1000;
  assert.strictEqual(core.computeStats([{ savedAt: old }], now).currentStreak, 0);
});

test("heatBucket buckets", function () {
  assert.deepStrictEqual([0, 1, 2, 3, 4, 5, 9].map(core.heatBucket), [0, 1, 2, 3, 3, 4, 4]);
});

// ---- summary actions --------------------------------------------------------------------
test("summaryActions offers the right re-runs", function () {
  var ids = function (m) { return core.summaryActions(m).map(function (a) { return a.id; }); };
  assert.deepStrictEqual(ids({ mode: "answer", language: "python", tier: "normal", mdPath: "a/b/c.md" }),
    ["open", "optimal", "lang-cpp", "lang-java", "learn"]);
  assert.deepStrictEqual(ids({ mode: "answer", language: "cpp", tier: "optimal" }),
    ["lang-python", "lang-java", "learn"]);
  assert.deepStrictEqual(ids({ mode: "learning", language: "java", tier: "" }),
    ["lang-python", "lang-cpp"]);
  var opt = core.summaryActions({ mode: "answer", language: "python", tier: "basic" })[0];
  assert.deepStrictEqual(opt.patch, { tier: "optimal" });
  assert.strictEqual(core.summaryActions({ mode: "answer", language: "python" })[1].label, "Re-run in C++");
});

// ---- platform / a11y / storage --------------------------------------------------------------
test("modKey picks the platform glyph", function () {
  assert.strictEqual(core.modKey("MacIntel"), "⌘");
  assert.strictEqual(core.modKey("iPhone"), "⌘");
  assert.strictEqual(core.modKey("Win32"), "Ctrl");
  assert.strictEqual(core.modKey("Linux x86_64"), "Ctrl");
  assert.strictEqual(core.modKey(""), "Ctrl");
});

test("nextFocusIndex wraps both ways", function () {
  assert.strictEqual(core.nextFocusIndex(2, 3, false), 0);
  assert.strictEqual(core.nextFocusIndex(0, 3, true), 2);
  assert.strictEqual(core.nextFocusIndex(1, 3, false), 2);
  assert.strictEqual(core.nextFocusIndex(-1, 3, false), 0);
  assert.strictEqual(core.nextFocusIndex(-1, 3, true), 2);
  assert.strictEqual(core.nextFocusIndex(0, 0, false), -1);
});

test("isComposingEnter guards IME confirmation", function () {
  assert.strictEqual(core.isComposingEnter({ key: "Enter", isComposing: true }), true);
  assert.strictEqual(core.isComposingEnter({ key: "Enter", keyCode: 229 }), true);
  assert.strictEqual(core.isComposingEnter({ key: "Enter", keyCode: 13 }), false);
  assert.strictEqual(core.isComposingEnter(null), false);
});

test("makePrefs round-trips and survives a throwing store", function () {
  var mem = {};
  var store = {
    getItem: function (k) { return k in mem ? mem[k] : null; },
    setItem: function (k, v) { mem[k] = v; },
    removeItem: function (k) { delete mem[k]; },
  };
  var p = core.makePrefs(function () { return store; });
  assert.strictEqual(p.get("mode", "answer"), "answer");
  assert.strictEqual(p.set("mode", "guided"), true);
  assert.strictEqual(mem["leetcoach.mode"], "guided");
  assert.strictEqual(p.get("mode", "answer"), "guided");
  p.set("mode", "");
  assert.strictEqual("leetcoach.mode" in mem, false);

  var broken = core.makePrefs(function () { throw new Error("SecurityError"); });
  assert.strictEqual(broken.get("x", "fallback"), "fallback");
  assert.strictEqual(broken.set("x", "1"), false);
  var quota = core.makePrefs(function () {
    return { getItem: function () { throw new Error("no"); },
             setItem: function () { throw new Error("QuotaExceeded"); },
             removeItem: function () {} };
  });
  assert.strictEqual(quota.get("x", "d"), "d");
  assert.strictEqual(quota.set("x", "1"), false);
});

// ---- strings / time ------------------------------------------------------------------------
test("string helpers", function () {
  assert.strictEqual(core.humanize("two_sum-ii"), "Two Sum Ii");
  assert.strictEqual(core.extOf("a/b/c.PY"), "py");
  assert.strictEqual(core.extOf("a\\b\\c"), "");
  assert.strictEqual(core.firstLine("\n\n  Two Sum  \nrest"), "Two Sum");
  assert.deepStrictEqual(core.midTrunc("abcdefghijkl", 4), { a: "abcdefgh", b: "ijkl" });
  assert.strictEqual(core.langLabel("python"), "Python");
  assert.strictEqual(core.langLabel("rb"), "—");
  assert.deepStrictEqual(core.otherLanguages("cpp"), ["python", "java"]);
  assert.strictEqual(core.unescapeEntities("a&amp;b&lt;c&gt;&quot;&#39;"), "a&b<c>\"'");
  assert.strictEqual(core.escapeHtml("<a&b>"), "&lt;a&amp;b&gt;");
});

test("time helpers", function () {
  assert.strictEqual(core.fmtClock(65000), "1:05");
  assert.strictEqual(core.fmtDuration(4200), "4.2s");
  assert.strictEqual(core.fmtDuration(119700), "2:00");
  assert.strictEqual(core.relTime(1000, 1000 * 1000 + 10000), "now");
  assert.strictEqual(core.relTime(1000, (1000 + 600) * 1000), "10m");
  assert.strictEqual(core.dayKey(new Date(2026, 0, 5)), "2026-01-05");
  assert.strictEqual(core.parseDayKey("2026-01-05").getDate(), 5);
});

// ---- hardened marked renderer (with the vendored marked) -----------------------------------
test("hardened renderer: raw HTML escaped, unsafe links/images neutralised, & not double-escaped", function () {
  global.window = global.window || global;
  var m = require(path.join(require("./paths.js").STATIC, "vendor", "marked.min.js"));
  var Marked = m.Marked;
  var md = new Marked();
  md.use({ renderer: core.hardenedRenderer() });
  var html = md.parse(
    "<script>alert(1)</script>\n\n" +
    "[x](javascript:alert(1)) <https://z.com/?a&b> https://y.com/?q=1&r=2 " +
    "![i](https://evil/p.png) ![d](data:image/png;base64,AAAA)");
  assert.ok(html.indexOf("<script>") === -1, html);
  assert.ok(html.indexOf("&lt;script&gt;") !== -1, html);
  assert.ok(html.indexOf("javascript:") === -1 || html.indexOf('href="javascript') === -1, html);
  assert.ok(html.indexOf('href="javascript') === -1, html);
  assert.ok(html.indexOf('href="https://z.com/?a&amp;b"') !== -1, html);
  assert.ok(html.indexOf("&amp;amp;") === -1, html);
  assert.ok(html.indexOf('href="https://y.com/?q=1&amp;r=2"') !== -1, html);
  assert.ok(html.indexOf("https://evil/p.png") === -1, html);
  assert.ok(html.indexOf('<img src="data:image/png;base64,AAAA"') !== -1, html);
});

function hardenedMarked() {
  global.window = global.window || global;
  var m = require(path.join(require("./paths.js").STATIC, "vendor", "marked.min.js"));
  var md = new m.Marked();
  md.use({ renderer: core.hardenedRenderer() });
  return md;
}

test("hardened renderer: link text keeps inline markdown, titles are dropped", function () {
  var html = hardenedMarked().parse('[**bold** `c`](https://a.com/x "T")');
  assert.ok(html.indexOf('<a href="https://a.com/x" rel="noopener" target="_blank">' +
    "<strong>bold</strong> <code>c</code></a>") !== -1, html);
  assert.ok(html.indexOf("title=") === -1, html);
});

test("hardened renderer: an unsafe link keeps its rendered text, never an href", function () {
  var html = hardenedMarked().parse("[*see* <b>me</b>](javascript:alert(1)) [x](data:text/html,hi)");
  assert.ok(html.indexOf("href") === -1, html);
  assert.ok(html.indexOf("<em>see</em>") !== -1, html);
  assert.ok(html.indexOf("<b>") === -1 && html.indexOf("&lt;b&gt;me&lt;/b&gt;") !== -1, html);
  assert.ok(html.indexOf("javascript:") === -1, html);
});

test("hardened renderer: autolink and bare-URL text is escaped, never raw", function () {
  var html = hardenedMarked().parse("<https://z.com/?a<b>&c> and https://y.com/p?q=<i>");
  assert.ok(html.indexOf("<b>") === -1 && html.indexOf("<i>") === -1, html);
  assert.ok(html.indexOf("&amp;amp;") === -1, html);
});

test("hardened renderer: image alt text is escaped and a refused image keeps only text", function () {
  var md = hardenedMarked();
  var ok = md.parse('![a "q" <b>x</b>](data:image/png;base64,AAAA)');
  assert.ok(ok.indexOf('<img src="data:image/png;base64,AAAA" alt="') !== -1, ok);
  assert.ok(ok.indexOf("<b>") === -1 && ok.indexOf('alt="a "q"') === -1, ok);
  var refused = md.parse("![<b>alt</b> *e*](https://evil/p.png)");
  assert.ok(refused.indexOf("<img") === -1 && refused.indexOf("evil") === -1, refused);
  assert.ok(refused.indexOf("<b>") === -1 && refused.indexOf("alt") !== -1, refused);
});

test("hardened renderer: block and inline raw HTML is shown as text", function () {
  var html = hardenedMarked().parse('<div onclick="x()">hi</div>\n\ntext <img src=x onerror=alert(1)> end');
  assert.ok(html.indexOf("<div") === -1 && html.indexOf("&lt;div") !== -1, html);
  assert.ok(html.indexOf("<img") === -1 && html.indexOf("&lt;img") !== -1, html);
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

// ---- Code Review mode (D5) ------------------------------------------------------
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

// ---- Phase 7: the top-bar CLI pill says its state in words, not just with a dot colour -----
test("cliStatus names every state in text and only colours the not-ready ones", function () {
  assert.deepStrictEqual(core.cliStatus(true, true), { ready: true, state: "ready", dot: "" });
  assert.deepStrictEqual(core.cliStatus(true, false), { ready: false, state: "signed out", dot: "red" });
  assert.deepStrictEqual(core.cliStatus(false, false), { ready: false, state: "not found", dot: "red" });
  assert.deepStrictEqual(core.cliStatus(false, true), { ready: false, state: "not found", dot: "red" });
});

test("verdict glyphs differ by shape, so the verdict never rests on colour alone", function () {
  var glyphs = {};
  ["pass", "fail", "not_verified", ""].forEach(function (v) {
    var info = core.verdictInfo(v);
    assert.ok(info.glyph && info.label, "glyph + label for " + v);
    glyphs[info.cls] = info.glyph;
  });
  var shapes = Object.keys(glyphs).map(function (k) { return glyphs[k]; });
  assert.strictEqual(new Set(shapes).size, shapes.length, "one shape per verdict class: " + shapes);
});

test("tierNote explains a disabled Code Quality group", function () {
  assert.strictEqual(core.tierNote("answer"), "");
  assert.strictEqual(core.tierNote("guided"), "");
  assert.ok(/Learning/.test(core.tierNote("learning")) && /Answer and Guided/.test(core.tierNote("learning")));
  assert.ok(/Code Review/.test(core.tierNote("review")));
});
