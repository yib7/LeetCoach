"use strict";
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));

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

test("isNearBottom uses a threshold", function () {
  assert.strictEqual(core.isNearBottom({ scrollTop: 900, clientHeight: 100, scrollHeight: 1000 }), true);
  assert.strictEqual(core.isNearBottom({ scrollTop: 850, clientHeight: 100, scrollHeight: 1000 }), true);
  assert.strictEqual(core.isNearBottom({ scrollTop: 500, clientHeight: 100, scrollHeight: 1000 }), false);
  assert.strictEqual(core.isNearBottom({ scrollTop: 500, clientHeight: 100, scrollHeight: 1000 }, 400), true);
  assert.strictEqual(core.isNearBottom(null), true);
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
test("deriveRuns groups files, keeps the newest doc and its verdict", function () {
  var runs = core.deriveRuns([
    { path: "answers/hash_map/two_sum__normal.md", mtime: 100, verdict: "fail" },
    { path: "answers/hash_map/two_sum__normal.py", mtime: 101 },
    { path: "answers/hash_map/two_sum__optimal.md", mtime: 200, verdict: "pass" },
    { path: "learning/hash_map_learning/two_sum.md", mtime: 50 },
    { path: "loose.md", mtime: 10 },
  ]);
  assert.strictEqual(runs.length, 2);
  var ans = runs[0];
  assert.strictEqual(ans.mode, "Answer");
  assert.strictEqual(ans.topic, "Hash Map");
  assert.strictEqual(ans.problem, "Two Sum");
  assert.strictEqual(ans.mdPath, "answers/hash_map/two_sum__optimal.md");
  assert.strictEqual(ans.verdict, "pass");
  assert.strictEqual(ans.language, "Python");
  assert.strictEqual(ans.savedAt, 200);
  assert.strictEqual(runs[1].mode, "Learning");
  assert.strictEqual(runs[1].topic, "Hash Map");
  assert.strictEqual(runs[1].verdict, "");
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
  var opt = core.summaryActions({ mode: "guided", language: "python", tier: "basic" })[0];
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
  var m = require(path.join(__dirname, "..", "..", "static", "vendor", "marked.min.js"));
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
