"use strict";
// C9: text tokens in leetcoach/static/style.css meet WCAG AA (4.5:1) on the surfaces
// they are drawn on. Parses the :root custom properties straight from the CSS.
var assert = require("assert");
var fs = require("fs");
var path = require("path");

var css = fs.readFileSync(path.join(require("./paths.js").STATIC, "style.css"), "utf8");
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

// ---- Phase 7 (ship v1.5.0): contrast of the remaining text / UI-component pairs ----------
function rgba(c) {
  var m = /rgba\(([^)]*)\)/.exec(c);
  if (!m) {
    var h = c.replace("#", "");
    return [0, 2, 4].map(function (i) { return parseInt(h.substr(i, 2), 16); }).concat(1);
  }
  return m[1].split(",").map(Number);
}
function over(top, bot) {
  var a = top.length > 3 ? top[3] : 1;
  return [0, 1, 2].map(function (i) { return Math.round(top[i] * a + bot[i] * (1 - a)); });
}
function hex(rgb) {
  return "#" + rgb.map(function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
}
var rgbaVars = {};
rootBlock.replace(/--([\w-]+)\s*:\s*(rgba\([^)]*\))/g, function (_, name, c) { rgbaVars[name] = c; });

test("--tx4 stays >= 4.5:1 on the hover surface --s3 too", function () {
  var r = ratio(vars.tx4, vars.s3);
  assert.ok(r >= 4.5, "--tx4 on --s3 is " + r.toFixed(2) + ":1");
});

test("the selected search row keeps its hint text at >= 4.5:1", function () {
  var m = /\.pal-row\.on \.pal-hint\s*\{[^}]*color:\s*var\(--([\w-]+)\)/.exec(css);
  assert.ok(m, ".pal-row.on .pal-hint must set its own colour");
  var bg = hex(over(rgba(rgbaVars["grn-weak"]), rgba(vars.s1)));
  var r = ratio(vars[m[1]], bg);
  assert.ok(r >= 4.5, "hint on the selected row is " + r.toFixed(2) + ":1");
});

test("the focus ring is >= 3:1 against every surface (WCAG 1.4.11)", function () {
  var m = /--ring:\s*0 0 0 (\d+)px (rgba\([^)]*\))/.exec(rootBlock);
  assert.ok(m, "--ring must be a solid spread ring");
  assert.ok(+m[1] >= 2, "ring is at least 2px");
  ["bg", "bg-elev", "s1", "s2", "s3", "inset"].forEach(function (bg) {
    var ring = hex(over(rgba(m[2]), rgba(vars[bg])));
    var r = ratio(ring, vars[bg]);
    assert.ok(r >= 3, "ring on --" + bg + " is " + r.toFixed(2) + ":1");
  });
});

test("form-field borders are >= 3:1 against the field and the panel", function () {
  assert.ok(vars["field-line"], "missing --field-line");
  ["inset", "s1", "s2"].forEach(function (bg) {
    var r = ratio(vars["field-line"], vars[bg]);
    assert.ok(r >= 3, "--field-line on --" + bg + " is " + r.toFixed(2) + ":1");
  });
  ["#qa-input", ".pr-select", ".pr-case-in", ".notes-input", ".fu-input"].forEach(function (sel) {
    assert.ok(/border:\s*1px solid var\(--field-line\)/.test(rule(sel)), sel + " uses --field-line");
  });
});

test("native controls render in the dark scheme", function () {
  assert.ok(/color-scheme:\s*dark/.test(rootBlock), ":root declares color-scheme: dark");
});

// Phase 7: `.tb.md` (a Markdown file badge) also matched the `.md` prose container and
// rendered at 15px. The badge must set its own font so the class collision cannot win.
test("the MD file badge keeps the badge font, not the prose font", function () {
  assert.ok(/font:\s*700 9\.5px var\(--mono\)/.test(rule(".tb.md")), rule(".tb.md"));
});

// Phase 7: absolutely positioned .sr-only text inside a scroller escaped it (no positioned
// ancestor) and made the whole document scroll, pushing the top bar off-screen.
test("scroll containers are positioned so .sr-only text cannot escape them", function () {
  [".content", ".table", ".recents"].forEach(function (sel) {
    assert.ok(/position:\s*relative/.test(rule(sel)), sel + " must be position:relative");
  });
});

test("panel heads, toolbars and section heads wrap on narrow screens", function () {
  [".p-head", ".p-tools", ".section-h", ".filterpills", ".vw-head"].forEach(function (sel) {
    assert.ok(/flex-wrap:\s*wrap/.test(rule(sel)), sel + " must wrap");
  });
  assert.ok(/white-space:\s*nowrap/.test(rule(".btn")), ".btn labels never break mid-label");
});
