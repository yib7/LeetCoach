#!/usr/bin/env node
/* Zero-dependency runner for the front-end unit tests (SP5).
 *
 *   node tests/js/run.js
 *
 * Loads every tests/js/*.test.js. A test file calls the global
 * `test(name, fn)`; `fn` may throw (node's built-in `assert`) to fail.
 * Exit code 1 if any test failed. Wired into pytest by tests/test_js_unit.py
 * (skipped when `node` is not on PATH).
 */
"use strict";

var fs = require("fs");
var path = require("path");

var tests = [];
global.test = function (name, fn) { tests.push({ name: name, fn: fn }); };

var dir = __dirname;
var files = fs.readdirSync(dir).filter(function (f) { return /\.test\.js$/.test(f); }).sort();
var failed = 0;
var passed = 0;

files.forEach(function (file) {
  tests = [];
  require(path.join(dir, file));
  tests.forEach(function (t) {
    try {
      t.fn();
      passed++;
    } catch (e) {
      failed++;
      console.log("FAIL " + file + " :: " + t.name);
      console.log("     " + String((e && e.stack) || e).split("\n").slice(0, 4).join("\n     "));
    }
  });
});

console.log((failed ? "FAILED" : "OK") + " - " + passed + " passed, " + failed + " failed (" +
  files.length + " files)");
process.exit(failed ? 1 : 0);
