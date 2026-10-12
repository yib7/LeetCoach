/* Where the front-end files live, resolved once for every node test: the
 * package's static/ folder (leetcoach/static). Point this, not each test, at a
 * new location if the front end ever moves again. */
var path = require("path");

var REPO_ROOT = path.resolve(__dirname, "..", "..");
var STATIC = path.join(REPO_ROOT, "leetcoach", "static");

module.exports = { REPO_ROOT: REPO_ROOT, STATIC: STATIC };
