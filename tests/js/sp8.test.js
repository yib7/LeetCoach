"use strict";
/* SP8 front-end helpers: the D6 follow-up box - question validation, the
 * "resumed vs fallback" source label and the done message. */
var assert = require("assert");
var path = require("path");
var core = require(path.join(__dirname, "..", "..", "static", "lib", "core.js"));

test("followupCheck: empty, whitespace, too long, ok", function () {
  assert.deepStrictEqual(core.followupCheck("", 2000),
    { ok: false, message: "Type a question about this doc first." });
  assert.strictEqual(core.followupCheck("  \n\t ", 2000).ok, false);
  assert.strictEqual(core.followupCheck(null, 2000).ok, false);
  var long = core.followupCheck(new Array(2002).join("x"), 2000);
  assert.strictEqual(long.ok, false);
  assert.ok(/2000/.test(long.message), long.message);
  assert.deepStrictEqual(core.followupCheck("  Why a map?  ", 2000), { ok: true, question: "Why a map?" });
  // the cap counts the trimmed question, like the server
  assert.strictEqual(core.followupCheck("  " + new Array(2001).join("x") + "  ", 2000).ok, true);
});

test("followupSource: resumed vs each fallback reason", function () {
  var resumed = core.followupSource({ phase: "streaming", source: "resume" });
  assert.strictEqual(resumed.kind, "resume");
  assert.strictEqual(resumed.label, "Resumed study session");
  var none = core.followupSource({ phase: "streaming", source: "fallback", reason: "no_session" });
  assert.strictEqual(none.kind, "fallback");
  assert.strictEqual(none.label, "Answered from the doc");
  assert.ok(/no saved session/.test(none.note), none.note);
  assert.ok(/resumed/.test(core.followupSource({ source: "fallback", reason: "resume_failed" }).note));
  assert.ok(/--resume/.test(core.followupSource({ source: "fallback", reason: "unsupported" }).note));
  assert.strictEqual(core.followupSource({ source: "fallback", reason: "weird" }).note, "");
  assert.strictEqual(core.followupSource({ phase: "saving" }), null);
  assert.strictEqual(core.followupSource(null), null);
  assert.strictEqual(core.followupSource("x"), null);
});

test("followupDoneText names the heading and how it was answered", function () {
  assert.strictEqual(
    core.followupDoneText({ source: "resume", heading: "## Follow-up — Why?" }),
    "Added to the doc under “Follow-up — Why?” (resumed the study session).");
  assert.strictEqual(
    core.followupDoneText({ source: "fallback", heading: "## Follow-up — Q" }),
    "Added to the doc under “Follow-up — Q” (answered from the doc).");
  assert.strictEqual(core.followupDoneText({}), "Added to the doc.");
  assert.strictEqual(core.followupDoneText(null), "Added to the doc.");
});
