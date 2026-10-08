/* A tiny DOM stand-in for the node unit tests (no jsdom dependency).
 *
 * Just enough of Node/Element for the post-render helpers in core.js:
 * childNodes / parentNode / nextSibling, appendChild / insertBefore /
 * removeChild (moving a node out of its old parent like the real DOM),
 * textContent, className, attributes, addEventListener + dispatch.
 * Assigning innerHTML THROWS: the helpers must build model text into the
 * DOM with createElement / textContent only.
 */
"use strict";

function FakeNode(nodeType, tagName, text) {
  this.nodeType = nodeType;
  this.tagName = tagName ? String(tagName).toUpperCase() : undefined;
  this.childNodes = [];
  this.parentNode = null;
  this._text = text || "";
  this._attrs = {};
  this._listeners = {};
  this.className = "";
}

Object.defineProperty(FakeNode.prototype, "nextSibling", {
  get: function () {
    if (!this.parentNode) return null;
    var sib = this.parentNode.childNodes;
    var i = sib.indexOf(this);
    return i >= 0 && i + 1 < sib.length ? sib[i + 1] : null;
  },
});
Object.defineProperty(FakeNode.prototype, "children", {
  get: function () { return this.childNodes.filter(function (n) { return n.nodeType === 1; }); },
});
Object.defineProperty(FakeNode.prototype, "textContent", {
  get: function () {
    if (this.nodeType === 3) return this._text;
    return this.childNodes.map(function (n) { return n.textContent; }).join("");
  },
  set: function (v) {
    if (this.nodeType === 3) { this._text = String(v); return; }
    this.childNodes.forEach(function (n) { n.parentNode = null; });
    this.childNodes = [];
    if (v !== "" && v != null) this.appendChild(new FakeNode(3, null, String(v)));
  },
});
Object.defineProperty(FakeNode.prototype, "innerHTML", {
  get: function () { throw new Error("innerHTML read in a test DOM"); },
  set: function () { throw new Error("innerHTML assigned - model text must be DOM-built"); },
});

FakeNode.prototype._detach = function () {
  if (this.parentNode) this.parentNode.removeChild(this);
};
FakeNode.prototype.appendChild = function (n) {
  n._detach();
  n.parentNode = this;
  this.childNodes.push(n);
  return n;
};
FakeNode.prototype.insertBefore = function (n, ref) {
  if (ref == null) return this.appendChild(n);
  n._detach();
  var i = this.childNodes.indexOf(ref);
  if (i < 0) throw new Error("insertBefore: ref is not a child");
  n.parentNode = this;
  this.childNodes.splice(i, 0, n);
  return n;
};
FakeNode.prototype.removeChild = function (n) {
  var i = this.childNodes.indexOf(n);
  if (i < 0) throw new Error("removeChild: not a child");
  this.childNodes.splice(i, 1);
  n.parentNode = null;
  return n;
};
FakeNode.prototype.setAttribute = function (k, v) { this._attrs[k] = String(v); };
FakeNode.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
};
FakeNode.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
FakeNode.prototype.dispatch = function (type) {
  var self = this;
  (this._listeners[type] || []).forEach(function (fn) { fn.call(self, { type: type, target: self }); });
};

var document = {
  createElement: function (tag) { return new FakeNode(1, tag); },
  createTextNode: function (text) { return new FakeNode(3, null, text); },
};

// Build a tree from a compact spec: ["h2", "Title"] / ["p", "text"] / "\n" (text).
function build(specs) {
  var root = document.createElement("div");
  specs.forEach(function (s) {
    if (typeof s === "string") { root.appendChild(document.createTextNode(s)); return; }
    var e = document.createElement(s[0]);
    if (s[1] != null) e.textContent = s[1];
    root.appendChild(e);
  });
  return root;
}

module.exports = { document: document, build: build, FakeNode: FakeNode };
