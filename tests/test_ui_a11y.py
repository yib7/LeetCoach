"""Phase 7.4: accessibility checks on the served page and the front-end sources.

Parses the rendered ``GET /`` with the stdlib HTML parser (no browser) and checks
what a screen reader needs from the static markup: every form control has a
label, every button has a name, decorative SVGs and emoji are hidden from the
accessibility tree, and an ``aria-label`` is never put on a role-less
``div``/``span`` (where ARIA 1.2 says it is ignored). ``app.js`` builds more DOM
at runtime, so its SVG snippets are checked as source text.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from leetcoach import app as app_module
from leetcoach import claude_cli

STATIC = Path(app_module.__file__).resolve().parent / "static"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children: list[_Node | str] = []

    def text(self) -> str:
        out = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.attrs.get("aria-hidden") != "true":
                out.append(c.text())
        return "".join(out)

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def walk(self):
        for c in self.children:
            if isinstance(c, _Node):
                yield c
                yield from c.walk()


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    out = tmp_path_factory.mktemp("a11y-out")
    mp = pytest.MonkeyPatch()
    mp.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    try:
        application = app_module.create_app(
            run_fn=lambda *a, **k: iter(()),
            auth_probe=lambda: claude_cli.AuthStatus(installed=True, logged_in=True),
        )
        application.config.update(TESTING=True)
        html = application.test_client().get("/").get_data(as_text=True)
    finally:
        mp.undo()
    tree = _Tree()
    tree.feed(html)
    return tree.root


def _nodes(root, *tags):
    return [n for n in root.walk() if n.tag in tags]


def _labelled_ids(root):
    return {n.attrs.get("for") for n in _nodes(root, "label") if n.attrs.get("for")}


def test_every_form_control_has_a_label(page):
    labelled = _labelled_ids(page)
    missing = []
    for n in _nodes(page, "input", "select", "textarea"):
        if n.attrs.get("type") == "hidden":
            continue
        ok = (n.attrs.get("id") in labelled or n.attrs.get("aria-label")
              or n.attrs.get("aria-labelledby")
              or any(a.tag == "label" for a in n.ancestors()))
        if not ok:
            missing.append(n.attrs.get("id") or n.tag)
    assert not missing, f"form controls without a label: {missing}"


def test_every_button_has_an_accessible_name(page):
    nameless = []
    for n in _nodes(page, "button") + [x for x in page.walk() if x.attrs.get("role") == "button"]:
        if n.attrs.get("aria-label") or n.attrs.get("aria-labelledby") or n.text().strip():
            continue
        nameless.append(n.attrs.get("id") or n.attrs.get("class"))
    assert not nameless, f"buttons with no accessible name: {nameless}"


def test_images_have_alt_text(page):
    assert all("alt" in n.attrs for n in _nodes(page, "img"))


def test_decorative_svgs_are_hidden_from_assistive_tech(page):
    shown = [n.parent.attrs.get("id") or n.parent.attrs.get("class") or n.parent.tag
             for n in _nodes(page, "svg")
             if n.attrs.get("aria-hidden") != "true" or n.attrs.get("focusable") != "false"]
    assert not shown, f"<svg> without aria-hidden=true focusable=false inside: {shown}"


def test_decorative_emoji_are_hidden_from_assistive_tech(page):
    emoji = re.compile("[\U0001F300-\U0001FAFF✅]")
    exposed = []
    for n in page.walk():
        own = "".join(c for c in n.children if isinstance(c, str))
        if emoji.search(own) and not (n.attrs.get("aria-hidden") == "true"
                                      or any(a.attrs.get("aria-hidden") == "true"
                                             for a in n.ancestors())):
            exposed.append(n.attrs.get("class") or n.tag)
    assert not exposed, f"emoji read aloud by screen readers: {exposed}"


def test_aria_label_is_never_on_a_roleless_div_or_span(page):
    bad = [n.attrs.get("id") or n.attrs.get("class") for n in _nodes(page, "div", "span")
           if n.attrs.get("aria-label") and not n.attrs.get("role")]
    assert not bad, f"aria-label on a generic element is ignored (ARIA 1.2): {bad}"


def test_cli_status_pill_keeps_a_text_state_next_to_the_dot(page):
    pill = next(n for n in page.walk() if n.attrs.get("id") == "claude-status")
    classes = [x.attrs.get("class", "") for x in pill.walk()]
    assert "dot" in classes and "tb-status-state" in classes, classes
    assert any(x.attrs.get("class") == "dot" and x.attrs.get("aria-hidden") == "true"
               for x in pill.walk())


def test_app_js_svg_snippets_are_decorative():
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    svgs = re.findall(r"<svg\b[^>]*>", src)
    assert svgs, "expected inline SVG snippets in app.js"
    bad = [s for s in svgs if 'aria-hidden="true"' not in s or 'focusable="false"' not in s]
    assert not bad, bad


def test_app_js_never_puts_aria_label_on_a_roleless_div():
    """Runtime-built groups (custom cases, review boxes) carry a role with their label."""
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    labelled = []
    for m in re.finditer(r"var (\w+) = el\(\"(?:div|span)\"[^;]*\);", src):
        var = re.escape(m.group(1))
        # the element's own setup block: up to the next var of the same name
        nxt = re.search(rf"var {var} = ", src[m.end():])
        block = src[m.end():m.end() + (nxt.start() if nxt else 600)][:1500]
        if re.search(rf"\b{var}\.setAttribute\(\"aria-label\"", block):
            labelled.append(m.group(0))
            assert re.search(rf"\b{var}\.setAttribute\(\"role\", ", block), m.group(0)
    # the custom-case rows and the review-box rows are the labelled ones today
    assert any("pr-case" in s for s in labelled) and any("bd-row" in s for s in labelled)
