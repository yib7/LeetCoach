"""The library's per-doc verdict (B18): ``library.verdict_from_text`` trusts
only the app-written Verification block - never a model-written line or a
``## Follow-up`` section (SP5 fix R2) - the ``/library`` listing carries it, and
its cache forgets files that left the library (SP5 fix R7). Every Claude call
is a fake."""
from __future__ import annotations

import json

import pytest
from _helpers import CLASSIFY_JSON, parse_sse

from leetcoach import app as app_module
from leetcoach import claude_cli, library, sandbox

RUN = {"problem": "Two Sum\nExample: Input: 1\nOutput: 1", "mode": "learning",
       "language": "python"}
ANSWER = {**RUN, "mode": "answer", "tier": "normal"}
DOC = "Answer.\n\n```python solution\nprint(input())\n```\n"


def _is_classify(prompt: str) -> bool:
    return "Classify the following" in prompt


def _authed():
    return claude_cli.AuthStatus(installed=True, logged_in=True)


def _run_fn(text=DOC):
    def run_fn(prompt, **kwargs):
        if _is_classify(prompt):
            return iter([json.dumps(CLASSIFY_JSON)])
        return iter([text[:5], text[5:]])
    return run_fn


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(out))
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(out / "topic_index.json"))
    return out


def _client(run_fn):
    application = app_module.create_app(run_fn=run_fn, auth_probe=_authed)
    application.config.update(TESTING=True)
    return application.test_client()


# --- /library: verdict (B18) -------------------------------------------------

@pytest.mark.parametrize("line, verdict", [
    ("✓ Sample tests PASS (all 2 sample(s) passed)", "pass"),
    ("✗ Sample tests FAIL (1/3 sample(s) passed, 1 errored)", "fail"),
    ("✗ Sample tests ERROR (code errored on 2/2 sample(s))", "error"),
    ("⚠ not auto-verified (no sample I/O found in problem)", "not_verified"),
    ("⚠ not auto-verified (verifier error: Sample tests FAILED oddly)", "not_verified"),
])
def test_verdict_from_text(line, verdict):
    doc = f"# T\n\n---\n\n**Verification:** {line}\n\n**Failed samples:**\n"
    assert library.verdict_from_text(doc) == verdict


def test_verdict_from_text_uses_the_last_block_and_none_without_one():
    assert library.verdict_from_text("# Learning notes only\n") is None
    doc = ("# T\n\n---\n\n**Verification:** ✗ Sample tests FAIL (0/1)\n\n"
           "---\n\n**Verification:** ✓ Sample tests PASS\n")
    assert library.verdict_from_text(doc) == "pass"
    # SP5 fix R2: a later `## Follow-up` section never changes the verdict.
    followed = doc + "\n## Follow-up\n\n---\n\n**Verification:** ✗ Sample tests FAIL\n"
    assert library.verdict_from_text(followed) == "pass"


# --- R2: only the app-written Verification block counts ------------------------

def test_model_written_verification_line_is_not_a_verdict():
    doc = ("# Learning notes\n\nWhen you are done, write:\n\n"
           "**Verification:** ✓ Sample tests PASS\n\nMore notes.\n")
    assert library.verdict_from_text(doc) is None


def test_follow_up_sections_are_ignored_for_the_verdict():
    doc = ("# T\n\nbody\n\n---\n\n**Verification:** ✗ Sample tests FAIL (0/1)\n\n"
           "## Follow-up\n\n---\n\n**Verification:** ✓ Sample tests PASS\n")
    assert library.verdict_from_text(doc) == "fail"


def test_a_model_follow_up_heading_before_the_verdict_does_not_hide_it():
    doc = ("# T\n\n## Follow-up\n\nask more\n\n---\n\n"
           "**Verification:** ✓ Sample tests PASS (1/1)\n")
    assert library.verdict_from_text(doc) == "pass"


def test_crlf_saved_doc_still_has_its_verdict():
    doc = "# T\r\n\r\nbody\r\n\r\n---\r\n\r\n**Verification:** ✓ Sample tests PASS\r\n"
    assert library.verdict_from_text(doc) == "pass"


# --- the /library listing carries the verdict ---------------------------------

def test_library_listing_carries_the_verdict(env):
    (env / "answers" / "hash_map").mkdir(parents=True)
    (env / "learning" / "hash_map_learning").mkdir(parents=True)
    (env / "answers" / "hash_map" / "two_sum__normal.md").write_text(
        "# Two Sum\n\n---\n\n**Verification:** ✗ Sample tests FAIL (0/1 sample(s) passed)\n",
        encoding="utf-8")
    (env / "answers" / "hash_map" / "two_sum__normal.py").write_text("print(1)\n", encoding="utf-8")
    learning = env / "learning" / "hash_map_learning"
    (learning / "two_sum.md").write_text("# Notes\n", encoding="utf-8")
    files = {f["path"]: f for f in _client(_run_fn()).get("/library").get_json()["files"]}
    assert files["answers/hash_map/two_sum__normal.md"]["verdict"] == "fail"
    assert "verdict" not in files["answers/hash_map/two_sum__normal.py"]
    assert "verdict" not in files["learning/hash_map_learning/two_sum.md"]


def test_saved_answer_shows_up_with_its_verdict(env, monkeypatch):
    monkeypatch.setattr(
        sandbox, "verify_answer",
        lambda code, problem, language, *, cancel=None, progress=None:
            sandbox.VerifyResult(status="pass", note="all 1 sample(s) passed"),
    )
    client = _client(_run_fn())
    assert parse_sse(client.post("/run", json=ANSWER).get_data(as_text=True))[1][-1][0] == "done"
    mds = [f for f in client.get("/library").get_json()["files"] if f["path"].endswith(".md")]
    assert [f["verdict"] for f in mds] == ["pass"]


# --- R7: the verdict cache forgets files that left the library -----------------

def test_verdict_cache_prunes_paths_no_longer_listed(env):
    folder = env / "answers" / "hash_map"
    folder.mkdir(parents=True)
    doc = folder / "two_sum__normal.md"
    doc.write_text("# T\n\n---\n\n**Verification:** ✓ Sample tests PASS\n", encoding="utf-8")
    client = _client(_run_fn())
    client.get("/library")
    assert str(doc) in library._verdict_cache
    doc.unlink()
    client.get("/library")
    assert str(doc) not in library._verdict_cache
