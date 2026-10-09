"""SP4 security + stream-liveness tests: anti-framing headers (C1), Origin /
Sec-Fetch-Site checks (C2), the SSE heartbeat (C3) and ``POST /run/cancel``
(B14). Every Claude call is a fake (no real `claude`)."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

import app as app_module
import claude_cli

RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


class Recorder:
    def __init__(self, text="# Notes\n"):
        self.text = text
        self.calls: list[dict] = []

    def __call__(self, prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        self.calls.append(kwargs)
        return iter([self.text])


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(out / "topic_index.json"))
    monkeypatch.delenv("LEETCOACH_MODEL", raising=False)
    return out


def _client(run_fn, auth_probe=_authed):
    application = app_module.create_app(run_fn=run_fn, auth_probe=auth_probe)
    application.config.update(TESTING=True)
    return application.test_client()


# --- C1: anti-framing headers ----------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/library", "/static/app.js"])
def test_responses_forbid_framing(env, path):
    resp = _client(Recorder()).get(path)
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"


# --- C2: Origin / Sec-Fetch-Site on unsafe methods ---------------------------------

@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://localhost:5001"},          # another local app, other port
    {"Origin": "https://localhost"},              # scheme mismatch
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"},
])
def test_unsafe_methods_reject_cross_origin_requests(env, headers):
    rec = Recorder()
    c = _client(rec)
    assert c.post("/run", json=RUN, headers=headers).status_code == 403
    assert rec.calls == []
    assert c.post("/ask", json={"question": "heapq?"}, headers=headers).status_code == 403
    assert c.post("/config/model", json={"model": "haiku"}, headers=headers).status_code == 403
    victim = env / "notes.md"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("keep", encoding="utf-8")
    assert c.delete("/library/file?path=notes.md", headers=headers).status_code == 403
    assert victim.exists()


@pytest.mark.parametrize("headers", [
    {},                                              # curl / scripts / tests
    {"Origin": "http://localhost"},
    {"Origin": "http://LOCALHOST:80"},               # default port spelled out
    {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost"},
    {"Sec-Fetch-Site": "none"},
])
def test_unsafe_methods_accept_same_origin_requests(env, headers):
    resp = _client(Recorder()).post("/run", json=RUN, headers=headers)
    assert resp.status_code == 200
    resp.get_data()


def test_origin_must_match_the_host_header_port(env):
    c = _client(Recorder())
    ok = c.post("/run", json=RUN, headers={"Host": "127.0.0.1:5000",
                                           "Origin": "http://127.0.0.1:5000"})
    assert ok.status_code == 200
    ok.get_data()
    bad = c.post("/run", json={**RUN, "problem": "other"},
                 headers={"Host": "127.0.0.1:5000", "Origin": "http://127.0.0.1:5001"})
    assert bad.status_code == 403


def test_index_rejects_cross_site_subresource_loads_without_probing(env):
    probes = []

    def probe():
        probes.append(1)
        return _authed()

    c = _client(Recorder(), auth_probe=probe)
    for headers in ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                     "Sec-Fetch-Dest": "image"},
                    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                     "Sec-Fetch-Dest": "iframe"},
                    {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors",
                     "Sec-Fetch-Dest": "empty"}):
        assert c.get("/", headers=headers).status_code == 403
    assert probes == []
    # a top-level navigation (a link the user clicked) still works
    nav = c.get("/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                              "Sec-Fetch-Dest": "document"})
    assert nav.status_code == 200
    assert c.get("/").status_code == 200


# --- C3: SSE heartbeat -------------------------------------------------------------

def test_sse_pings_while_claude_is_silent(env, monkeypatch):
    monkeypatch.setattr(app_module, "SSE_PING_INTERVAL", 0.05)

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            yield json.dumps(CLASSIFY_JSON)
            return
        yield "# Notes\n"
        time.sleep(0.4)  # long thinking: no output for a while
        yield "done thinking\n"

    body = _client(run_fn).post("/run", json=RUN).get_data(as_text=True)
    assert ": ping\n\n" in body
    text, events = parse_sse(body)  # comment frames are ignored by SSE parsers
    assert "".join(text) == "# Notes\ndone thinking\n"
    assert events[-1][0] == "done"


def test_sse_pings_while_waiting_on_the_classifier(env, monkeypatch):
    monkeypatch.setattr(app_module, "SSE_PING_INTERVAL", 0.05)
    monkeypatch.setattr(app_module, "CLASSIFIER_JOIN_TIMEOUT", 0.5)
    stop = threading.Event()

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            stop.wait(5)
            return iter(["{}"])
        return iter(["# Notes\n"])

    try:
        body = _client(run_fn).post("/run", json=RUN).get_data(as_text=True)
    finally:
        stop.set()
    after_text = body.split("# Notes", 1)[1]
    assert ": ping" in after_text
    assert parse_sse(body)[1][-1][0] == "done"


def test_sse_pings_while_verifying(env, monkeypatch):
    import sandbox

    monkeypatch.setattr(app_module, "SSE_PING_INTERVAL", 0.05)

    def slow_verify(code, problem, language, **kwargs):
        time.sleep(0.4)
        return sandbox.VerifyResult(status="not_verified", note="no samples")

    monkeypatch.setattr(sandbox, "verify_answer", slow_verify)
    rec = Recorder("Answer.\n\n```python solution\nprint(1)\n```\n")
    body = _client(rec).post(
        "/run", json={**RUN, "mode": "answer", "tier": "normal"}).get_data(as_text=True)
    before_verdict = body.split("not auto-verified", 1)[0]
    assert ": ping" in before_verdict.split("solution", 1)[1]
    assert parse_sse(body)[1][-1][0] == "done"


# --- B14: POST /run/cancel ----------------------------------------------------------

class CancellableRun:
    """Mimics claude_cli.ClaudeRun: yields one chunk, then blocks until
    cancel() (raising ClaudeCancelledError) or a safety timeout."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self._sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent:
            self._sent = True
            self.started.set()
            return "partial "
        if self.cancelled.wait(10):
            raise claude_cli.ClaudeCancelledError("cancelled")
        raise StopIteration

    def cancel(self):
        self.cancelled.set()


def _cancellable_client():
    runs: list[CancellableRun] = []

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        r = CancellableRun()
        runs.append(r)
        return r

    return _client(run_fn), runs


def test_run_echoes_its_run_id(env):
    c = _client(Recorder())
    resp = c.post("/run", json={**RUN, "run_id": "abc-123"})
    assert resp.headers["X-Run-Id"] == "abc-123"
    resp.get_data()
    generated = c.post("/run", json={**RUN, "problem": "x"})
    assert generated.headers["X-Run-Id"]
    generated.get_data()


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, 7, "../x"])
def test_run_rejects_a_malformed_run_id(env, bad):
    resp = _client(Recorder()).post("/run", json={**RUN, "run_id": bad})
    assert resp.status_code == 400


def test_cancel_kills_the_claude_call_and_frees_the_slot(env):
    c, runs = _cancellable_client()
    out = {}

    def consume():
        out["body"] = c.post("/run", json={**RUN, "run_id": "r1"}).get_data(as_text=True)

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (runs and runs[0].started.is_set()):
        time.sleep(0.02)
    assert runs and runs[0].started.is_set()

    # B14: an identical re-run is a 409 while the first is in flight ...
    assert c.post("/run", json=RUN).status_code == 409
    resp = c.post("/run/cancel", json={"run_id": "r1"})
    assert resp.status_code == 200
    assert resp.get_json() == {"cancelled": True}
    assert runs[0].cancelled.is_set()  # the claude process tree was killed
    # ... and accepted immediately after the cancel
    again = c.post("/run", json={**RUN, "run_id": "r2"}, buffered=False)
    assert again.status_code == 200
    c.post("/run/cancel", json={"run_id": "r2"})
    again.get_data()

    t.join(10)
    assert not t.is_alive()
    name, msg = parse_sse(out["body"])[1][-1]
    assert name == "cancelled" and "cancelled" in msg.lower()  # SP5 fix B2
    assert not (env / "learning").exists()  # nothing saved for a cancelled run


def test_cancel_with_a_runner_that_ignores_it_still_saves_nothing(env):
    release = threading.Event()
    started = threading.Event()

    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            yield json.dumps(CLASSIFY_JSON)
            return
        yield "partial "
        started.set()
        release.wait(5)  # a fake with no cancel(): it just keeps going
        yield "rest"

    c = _client(run_fn)
    out = {}
    t = threading.Thread(
        target=lambda: out.setdefault(
            "body", c.post("/run", json={**RUN, "run_id": "r9"}).get_data(as_text=True)),
        daemon=True)
    t.start()
    assert started.wait(5)
    assert c.post("/run/cancel", json={"run_id": "r9"}).status_code == 200
    release.set()
    t.join(10)
    assert parse_sse(out["body"])[1][-1][0] == "cancelled"  # SP5 fix B2
    assert not (env / "learning").exists()


def test_cancel_during_verification_saves_nothing(env, monkeypatch):
    import sandbox

    verifying = threading.Event()
    release = threading.Event()

    def slow_verify(code, problem, language, **kwargs):
        verifying.set()
        release.wait(5)
        return sandbox.VerifyResult(status="not_verified", note="no samples")

    monkeypatch.setattr(sandbox, "verify_answer", slow_verify)
    rec = Recorder("Answer.\n\n```python solution\nprint(1)\n```\n")
    c = _client(rec)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("body", c.post(
        "/run", json={**RUN, "mode": "answer", "tier": "normal", "run_id": "v1"},
    ).get_data(as_text=True)), daemon=True)
    t.start()
    try:
        assert verifying.wait(5)
        assert c.post("/run/cancel", json={"run_id": "v1"}).status_code == 200
    finally:
        release.set()
    t.join(10)
    name, msg = parse_sse(out["body"])[1][-1]
    assert name == "cancelled" and "cancelled" in msg.lower()  # SP5 fix B2
    assert not (env / "answers").exists()


def test_cancel_of_an_unknown_run_is_404(env):
    resp = _client(Recorder()).post("/run/cancel", json={"run_id": "nope"})
    assert resp.status_code == 404
    assert resp.get_json() == {"cancelled": False}


def test_cancel_requires_a_run_id(env):
    c = _client(Recorder())
    assert c.post("/run/cancel", json={}).status_code == 400
    assert c.post("/run/cancel", json=[1]).status_code == 400


def test_cancel_rejects_cross_origin(env):
    c = _client(Recorder())
    resp = c.post("/run/cancel", json={"run_id": "x"}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_a_response_closed_before_streaming_still_frees_its_slot(env):
    from werkzeug.test import EnvironBuilder

    application = app_module.create_app(run_fn=Recorder(), auth_probe=_authed)
    environ = EnvironBuilder(path="/run", method="POST", json=RUN).get_environ()
    body_iter = application(environ, lambda status, headers, exc_info=None: None)
    body_iter.close()  # the client vanished before a single byte was sent
    resp = application.test_client().post("/run", json=RUN)
    assert resp.status_code == 200, "the abandoned run must not hold the slot"
    resp.get_data()


# --- 3A W4: a link inside output/ never exposes files outside it -------------------

SECRET_DOC = "# Secret\n\n## Flashcards\n- Q: leaked question? - A: leaked answer\n"


def _link_out(link, target):
    """Make ``link`` (a directory link inside the library) point at ``target``:
    a symlink when the OS allows one, else (Windows without the privilege) an
    NTFS junction, which needs none; skips when neither can be made."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        pytest.skip("cannot create a directory symlink here")
    import _winapi  # Windows-only

    _winapi.CreateJunction(str(target), str(link))


def test_library_listing_skips_a_link_that_leaves_the_root(env, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.md").write_text(SECRET_DOC, encoding="utf-8")
    (env / "learning").mkdir(parents=True)
    (env / "learning" / "mine.md").write_text(
        "# Mine\n\n## Flashcards\n- Q: own question? - A: own answer\n", encoding="utf-8")
    _link_out(env / "linked", outside)
    assert (env / "linked" / "secret.md").is_file()  # the link really works

    c = _client(Recorder())
    paths = [f["path"] for f in c.get("/library").get_json()["files"]]
    assert paths == ["learning/mine.md"]
    cards = c.get("/flashcards").get_json()["cards"]
    assert [card["q"] for card in cards] == ["own question?"]
    tsv = c.get("/flashcards.tsv").get_data(as_text=True)
    assert "leaked" not in tsv
    assert "own question?" in tsv


def test_library_listing_skips_a_file_symlink_that_leaves_the_root(env, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text(SECRET_DOC, encoding="utf-8")
    env.mkdir(parents=True)
    try:
        os.symlink(outside, env / "link.md")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks not permitted here ({exc}); the directory "
                    "junction test and the predicate test cover the logic")
    paths = [f["path"] for f in _client(Recorder()).get("/library").get_json()["files"]]
    assert paths == []


def test_inside_root_predicate(tmp_path, monkeypatch):
    root = (tmp_path / "out").resolve()
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.md").write_text("x", encoding="utf-8")
    assert app_module._inside_root(root / "a" / "x.md", root)
    assert app_module._inside_root(root / "a" / ".." / "a" / "x.md", root)
    assert not app_module._inside_root(root / ".." / "other.md", root)
    assert not app_module._inside_root(tmp_path / "out2" / "x.md", root)

    # a path whose resolution lands outside (what a symlink / junction does)
    real_resolve = type(root).resolve

    def fake_resolve(self, strict=False):
        if self.name == "x.md":
            return tmp_path / "elsewhere" / "x.md"
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(type(root), "resolve", fake_resolve)
    assert not app_module._inside_root(root / "a" / "x.md", root)

    def broken(self, strict=False):
        raise OSError("loop")

    monkeypatch.setattr(type(root), "resolve", broken)
    assert not app_module._inside_root(root / "a" / "x.md", root)
