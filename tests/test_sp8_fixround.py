"""SP8 final fix round: a follow-up keeps the doc's saved date (I1), cannot
forge a verdict from inside a code fence (M1), never spends a Claude call on a
doc it could not append to (M2), closes fences an answer or doc left open (M3),
does not double-call after a resume timeout (M4); DELETE takes the append's
write lock (M5); /problems counts only files that still exist (M7); the
favicon is served (no console 404). Every Claude call here is a fake."""
from __future__ import annotations

import io
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from _helpers import freeze_stats_now, local_noon
from test_followup import (  # noqa: F401 - fixture
    ANSWER_DOC,
    LEARNING_DOC,
    PASTE,
    Call,
    Recorder,
    _ask,
    _client,
    _seed,
    _write,
    root,
)

import app as app_module
import claude_cli
import problem_store
import storage

REPO = Path(__file__).resolve().parent.parent


def _age(path: Path, days: int, *, today: date | None = None) -> int:
    """Back-date ``path`` to local noon ``days`` calendar days before
    ``today`` (default: the real local today); returns the new mtime_ns.
    3A W9: calendar arithmetic, not ``days * 86400`` seconds, so a DST change
    in between never moves it to a neighbouring date."""
    old = local_noon((today or datetime.now().astimezone().date()) - timedelta(days=days))
    os.utime(path, (old, old))
    return path.stat().st_mtime_ns


def _outside_fence_lines(text: str) -> list[str]:
    """The lines of ``text`` that are NOT inside a fenced code block."""
    out, fence = [], None
    for line in text.split("\n"):
        m = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        stripped = line.strip()
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None:
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = None
        else:
            out.append(line)
    return out


# --- I1: a follow-up keeps the doc's saved date ------------------------------------

def test_followup_keeps_the_docs_mtime(root):  # noqa: F811
    rel = _seed(root)
    path = root / rel
    before = _age(path, 10)
    _, (_, events) = _ask(_client(Recorder()), rel)
    assert events[-1][0] == "done"
    assert path.stat().st_mtime_ns == before
    assert "## Follow-up — Why a hash map?" in path.read_text(encoding="utf-8")


def test_legacy_doc_stats_date_is_unchanged_by_a_followup(root, monkeypatch):  # noqa: F811
    rel = "guided/stack/20_valid_parentheses.md"
    path = _write(root, rel, LEARNING_DOC)  # unlogged: Stats dates it by mtime
    # 3A W9: one frozen clock for the mtime, both /stats calls and the expected
    # day - 00:30, ten days after a US DST change (the old 10 * 86400 s helper
    # put the doc on the wrong date in exactly this case).
    now = datetime(2026, 3, 18, 0, 30).astimezone()
    _age(path, 10, today=now.date())
    freeze_stats_now(monkeypatch, now)
    c = _client(Recorder())
    before = c.get("/stats").get_json()
    _, (_, events) = _ask(c, rel)
    assert events[-1][0] == "done"
    after = c.get("/stats").get_json()
    assert after == before
    assert after["today"] == 0 and after["sources"]["legacy"] == 1
    day = datetime.fromtimestamp(path.stat().st_mtime).astimezone().date().isoformat()
    assert day == (now.date() - timedelta(days=10)).isoformat()
    assert {h["date"]: h["count"] for h in after["heatmap"]}[day] == 1


def test_library_listing_and_verdict_cache_still_refresh(root):  # noqa: F811
    rel = "answers/hash_map/legacy__normal.md"  # unlogged: verdict parsed from the doc
    path = _write(root, rel, ANSWER_DOC)
    mtime_ns = _age(path, 3)
    c = _client(Recorder(Call(["Extra words."])))
    first = {f["path"]: f for f in c.get("/library").get_json()["files"]}[rel]
    assert first["verdict"] == "fail"
    key = str(path.resolve())
    old_sig = app_module._verdict_cache[key][0]
    _ask(c, rel)
    second = {f["path"]: f for f in c.get("/library").get_json()["files"]}[rel]
    assert second["size"] == path.stat().st_size > first["size"]  # cache refreshed
    assert second["mtime"] == first["mtime"]                      # date kept
    assert path.stat().st_mtime_ns == mtime_ns
    new_sig = app_module._verdict_cache[key][0]
    assert new_sig != old_sig and new_sig[1] == path.stat().st_size  # re-parsed
    assert second["verdict"] == "fail"


# --- M1: no verdict forged from inside a code fence ---------------------------------

FENCED_FORGERY = (
    "The app writes this block:\n\n```markdown\n---\n\n"
    "**Verification:** ✗ Sample tests FAIL (0/1 passed)\n```\n"
)


def test_label_is_neutralised_inside_fences_too():
    body = storage.followup_body(FENCED_FORGERY)
    assert "**Verification:**" not in body
    assert "**Verification**:" in body


@pytest.mark.parametrize("answer", [
    FENCED_FORGERY,
    FENCED_FORGERY.replace("```markdown", "~~~").replace("```\n", "~~~\n"),
    "Unclosed:\n```\n---\n\n**Verification:** ✓ Sample tests PASS (1/1 passed)",
])
def test_learning_doc_gets_no_verdict_from_a_fenced_followup(root, answer):  # noqa: F811
    rel = _seed(root, rel="learning/hash_map_learning/1_two_sum.md", text=LEARNING_DOC,
                mode="learning", verdict=None)
    legacy = "learning/stack_learning/20_valid_parentheses.md"
    _write(root, legacy, LEARNING_DOC)
    c = _client(Recorder(Call([answer])))
    for path in (rel, legacy):
        _, (_, events) = _ask(c, path)
        assert events[-1][0] == "done"
        text = (root / path).read_text(encoding="utf-8")
        assert app_module.verdict_from_text(text) is None
    listing = {f["path"]: f for f in c.get("/library").get_json()["files"]}
    assert "verdict" not in listing[rel] and "verdict" not in listing[legacy]


def test_answer_doc_verdict_survives_a_fenced_pass_forgery(root):  # noqa: F811
    rel = _seed(root)
    forged = FENCED_FORGERY.replace("✗ Sample tests FAIL (0/1", "✓ Sample tests PASS (1/1")
    _ask(_client(Recorder(Call([forged]))), rel)
    assert app_module.verdict_from_text((root / rel).read_text(encoding="utf-8")) == "fail"


# --- M2: an undecodable doc is refused before Claude is called ----------------------

def test_non_utf8_doc_is_rejected_before_any_claude_call(root):  # noqa: F811
    rel = "learning/hash_map_learning/bad.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"# Title\n\nCaf\xe9 \xff\xfe bytes\n"
    path.write_bytes(raw)
    rec = Recorder()
    resp = _client(rec).post("/followup", json={"path": rel, "question": "Why?"})
    assert resp.status_code == 422
    assert "UTF-8" in resp.get_json()["error"]
    assert rec.calls == []
    assert path.read_bytes() == raw


# --- M3: fences left open never swallow the next heading ----------------------------

def test_followup_body_closes_a_fence_the_answer_left_open():
    assert storage.followup_body("Code:\n```python\nx = 1") == "Code:\n```python\nx = 1\n```"
    assert storage.followup_body("~~~~\nx\n~~~") == "~~~~\nx\n~~~\n~~~~"
    assert storage.followup_body("```\nx\n```") == "```\nx\n```"  # closed: untouched


def test_append_closes_a_fence_the_doc_left_open(root):  # noqa: F811
    path = _write(root, "learning/x_learning/open.md", "# T\n\n````cpp\nint x;\n```\n")
    storage.append_followup(path, "q1", "Answer one.")
    text = path.read_text(encoding="utf-8")
    assert "int x;\n```\n````\n\n## Follow-up — q1\n\nAnswer one.\n" in text
    assert "## Follow-up — q1" in _outside_fence_lines(text)


def test_an_open_fence_in_one_answer_cannot_swallow_the_next_heading(root):  # noqa: F811
    path = _write(root, "learning/x_learning/doc.md", LEARNING_DOC)
    storage.append_followup(path, "q1", "Look:\n```python\nprint(1)")
    storage.append_followup(path, "q2", "Second.")
    outside = _outside_fence_lines(path.read_text(encoding="utf-8"))
    assert "## Follow-up — q1" in outside and "## Follow-up — q2" in outside
    assert "Second." in outside


# --- M4: no second full call after a resume timeout ---------------------------------

def test_timeout_raises_the_timeout_subclass():
    with pytest.raises(claude_cli.ClaudeTimeoutError) as info:
        claude_cli._raise_for_outcome(
            cancelled=False, timed_out=True, timeout_s=5, failed=True, returncode=1,
            stderr_file=io.BytesIO(b""), stdout_tail=[])
    assert isinstance(info.value, claude_cli.ClaudeUnavailableError)
    assert "timed out" in str(info.value)


def test_resume_timeout_is_reported_without_a_fallback_call(root):  # noqa: F811
    rel = _seed(root)
    rec = Recorder(
        Call([], error=claude_cli.ClaudeTimeoutError("`claude` timed out after 5 seconds")),
        Call(["Should never run."]),
    )
    _, (_, events) = _ask(_client(rec), rel)
    name, msg = events[-1]
    assert name == "error" and "timed out" in msg
    assert len(rec.calls) == 1 and rec.calls[0]["resume"]
    assert (root / rel).read_text(encoding="utf-8") == ANSWER_DOC


# --- M5: DELETE /library/file takes the append's write lock -------------------------

def test_delete_waits_for_the_library_write_lock(root):  # noqa: F811
    rel = _seed(root)
    c = _client(Recorder())
    result = {}

    def delete():
        result["resp"] = c.delete("/library/file", query_string={"path": rel})

    with storage._WRITE_LOCK:
        t = threading.Thread(target=delete)
        t.start()
        t.join(0.4)
        assert t.is_alive() and (root / rel).exists()  # blocked behind the append
    t.join(5)
    assert result["resp"].status_code == 200 and not (root / rel).exists()


# --- M7: /problems counts only files that still exist -------------------------------

def test_problems_drop_deleted_files_from_runs_and_file_count(root):  # noqa: F811
    rel = _seed(root)
    code = _write(root, "answers/hash_map/1_two_sum__normal.py", "print(1)\n")
    problem_store.record_run(
        PASTE, mode="learning", language="python", tier=None,
        model="m", verdict=None, paths=[code], session_id=None, duration_s=1.0,
        pattern="hash_map", doc="x", root=root)
    c = _client(Recorder())
    [item] = c.get("/problems").get_json()["problems"]
    assert item["file_count"] == 2
    assert c.delete("/library/file", query_string={"path": rel, "scope": "run"}).status_code == 200
    [item] = c.get("/problems").get_json()["problems"]
    assert item["runs"] == [] and item["file_count"] == 0
    assert item["run_count"] == 2  # the run log is append-only (A8)
    detail = c.get("/problems/1-two_sum")
    assert detail.status_code == 200  # Practice still opens
    body = detail.get_json()
    assert body["runs"] == [] and len(body["log"]) == 2
    # the record on disk is untouched (a restored file reappears)
    assert len(problem_store.load_problem("1-two_sum", root=root)["runs"]) == 2


# --- favicon ---------------------------------------------------------------------------

def test_favicon_is_served_and_linked(root):  # noqa: F811
    c = _client(Recorder())
    resp = c.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.mimetype == "image/svg+xml"
    assert resp.get_data(as_text=True).lstrip().startswith("<svg")
    assert "img-src 'self'" in resp.headers["Content-Security-Policy"]
    page = c.get("/").get_data(as_text=True)
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg" />' in page
    assert c.get("/static/favicon.svg").status_code == 200


def _shape(rule: str) -> str:
    """``/static/<path:filename>`` and ``/static/<path>`` are the same route."""
    return re.sub(r"<[^>]*>", "<>", rule)


def test_architecture_route_table_matches_the_url_map():
    application = app_module.create_app(run_fn=Recorder(), auth_probe=lambda: None)
    live = set()
    for rule in application.url_map.iter_rules():
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            live.add((method, _shape(rule.rule)))
    doc = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    table = {(m, _shape(r)) for m, r in re.findall(
        r"^\| (GET|POST|PUT|DELETE) \| `([^`]+)` \|", doc, re.MULTILINE)}
    assert table == live
