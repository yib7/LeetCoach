"use strict";
// C9: text tokens in static/style.css meet WCAG AA (4.5:1) on the surfaces
// they are drawn on. Parses the :root custom properties straight from the CSS.
var assert = require("assert");
var fs = require("fs");
var path = require("path");

var css = fs.readFileSync(path.join(__dirname, "..", "..", "static", "style.css"), "utf8");
var rootBlock = /:root\s*\{([\s\S]*?)\}/.exec(css)[1];
var vars = {};
rootBlock.replace(/--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})/g, function (_, name, hex) {
  vars[name] = hex;
});

function lum(hex) {
  var h = hex.replace("#", "");
  var c = [0, 2, 4].map(function (i) { return parseInt(h.substr(i, 2), 16) / 255; })
    .map(function (v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function ratio(a, b) {
  var x = lum(a);
  var y = lum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

test("--tx4 has >= 4.5:1 contrast on every base surface", function () {
  ["bg", "bg-elev", "s1", "s2", "inset"].forEach(function (bg) {
    assert.ok(vars[bg] && vars.tx4, "missing token " + bg);
    var r = ratio(vars.tx4, vars[bg]);
    assert.ok(r >= 4.5, "--tx4 on --" + bg + " is " + r.toFixed(2) + ":1");
  });
});

test("--tx3 and --tx2 have >= 4.5:1 contrast even on hover surfaces", function () {
  ["tx3", "tx2"].forEach(function (fg) {
    ["bg", "bg-elev", "s1", "s2", "s3", "inset"].forEach(function (bg) {
      var r = ratio(vars[fg], vars[bg]);
      assert.ok(r >= 4.5, "--" + fg + " on --" + bg + " is " + r.toFixed(2) + ":1");
    });
  });
});

test("text tokens keep their hierarchy (tx > tx2 > tx3 > tx4)", function () {
  assert.ok(lum(vars.tx) > lum(vars.tx2));
  assert.ok(lum(vars.tx2) > lum(vars.tx3));
  assert.ok(lum(vars.tx3) > lum(vars.tx4));
});

// SP5 fix B5: a long slug in the recent-runs Problem column must not spill
// into the Topic column.
function rule(selector) {
  var esc = selector.replace(/[.*+?^${}()|[\]\>]/g, function (c) { return "\\" + c; });
  var m = new RegExp("(?:^|\})\s*" + esc + "\s*\{([^}]*)\}", "m").exec(css);
  return m ? m[1] : "";
}

test("recent-runs table cells truncate instead of overflowing (B5)", function () {
  var pm = rule(".pcell .pm");
  assert.ok(/white-space:\s*nowrap/.test(pm) && /overflow:\s*hidden/.test(pm) &&
    /text-overflow:\s*ellipsis/.test(pm), ".pcell .pm must ellipsize: " + pm);
  assert.ok(/minmax\(0,\s*1fr\)/.test(rule(".trow")), ".trow problem column must be minmax(0,1fr)");
  assert.ok(/minmax\(0,\s*1fr\)/.test(rule(".thead")), ".thead must match .trow columns");
  assert.ok(/min-width:\s*0/.test(rule(".trow>*")), "grid items must be allowed to shrink");
});
