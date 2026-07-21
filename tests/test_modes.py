"""Tests for Learning + Guided Learning modes on the Flask `/run` endpoint (SP4).

Like ``test_web.py``, no real ``claude`` is spawned: ``create_app(run_fn=<fake>)``
injects a fake that mirrors ``claude_cli.run`` — it accepts ``(prompt, **kwargs)``
and yields text deltas. The SAME fake covers BOTH Claude calls per request:

  1. ``classifier.classify`` (asks for a tiny JSON object), and
  2. the mode stream (Learning teach doc / Guided piped doc).

The fake branches on the prompt so it returns the right canned payload for each.
The real orchestration (classify -> build prompt -> stream -> accumulate -> save)
runs end-to-end, and we assert a markdown file lands under the correct subtree
(``output/learning/...`` / ``output/guided/...``) via a tmp ``LEETCOACH_OUTPUT_DIR``.

SSE protocol under test (shared with Answer mode):
  * ``data: <json-string>\n\n``              — a streamed text delta
  * ``event: done\ndata: <json-obj>\n\n``    — terminal success; payload carries
        ``mode``, ``problem_type``, ``topics`` and saved ``paths``
  * ``event: error\ndata: <json-string>\n\n`` — terminal failure
"""
from __future__ import annotations

import json

from _helpers import parse_sse as _parse_sse

import app as app_module

# --- a fake Claude that answers both the classify and the mode prompt ----

# Kept local (not imported from _helpers): this module deliberately exercises a
# TWO-topic classifier reply; the shared default is single-topic (see _helpers).
CLASSIFY_JSON = {"problem_type": "two_pointers", "topics": ["arrays", "hashing"]}

LEARNING_MARKDOWN = (
    "# Learning: Two Sum\n\n"
    "Let's build intuition. A hash map lets you remember numbers you've seen "
    "so you can check for the complement in O(1).\n\n"
    "Key technique: the **complement trick**.\n"
)

GUIDED_MARKDOWN = (
    "# Guided session: Two Sum\n\n"
    "1) Restate: find two indices whose values sum to the target.\n"
    "2) Teach: a hash map remembers seen values.\n"
    "3) Reason: scan once, check the complement each step.\n"
    "4) Answer:\n\n"
    "```python\n"
    "def two_sum(nums, target):\n"
    "    seen = {}\n"
    "    for i, n in enumerate(nums):\n"
    "        if target - n in seen:\n"
    "            return [seen[target - n], i]\n"
    "        seen[n] = i\n"
    "```\n\n"
    "Complexity: time O(n), space O(n).\n"
)


def make_fake_run(mode_markdown):
    """Build a fake runner that serves the classify call and a given mode doc."""

    def fake_run(prompt, **kwargs):
        if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
            text = json.dumps(CLASSIFY_JSON)
        else:
            text = mode_markdown
        # chunk it so the stream is genuinely incremental
        for i in range(0, len(text), 20):
            yield text[i : i + 20]

    return fake_run


def _make_client(tmp_path, monkeypatch, mode_markdown):
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    application = app_module.create_app(run_fn=make_fake_run(mode_markdown))
    application.config.update(TESTING=True)
    return application.test_client()


# --- Learning mode (no tier) ---------------------------------------------

def test_run_learning_streams_and_saves(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, LEARNING_MARKDOWN)
    resp = c.post(
        "/run",
        json={
            "problem": "Two Sum: return indices of two numbers adding to target.",
            "mode": "learning",
            "language": "python",
            # no tier — Learning must not require one
        },
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"

    body = resp.get_data(as_text=True)
    text_chunks, events = _parse_sse(body)

    # streamed text reassembles into the learning markdown
    streamed = "".join(text_chunks)
    assert "complement trick" in streamed
    assert "hash map" in streamed

    # terminal `done` event carries classification + saved paths + mode
    done = [p for (name, p) in events if name == "done"]
    assert done, f"no done event in: {events}"
    payload = done[0]
    assert payload["mode"] == "learning"
    assert payload["problem_type"] == "two_pointers"
    assert payload.get("paths"), "done payload should list saved file paths"
    assert not [n for (n, _) in events if n == "error"]

    # a markdown file landed under output/learning/<type>_learning/...
    learning_dir = tmp_path / "learning"
    files = [p for p in learning_dir.rglob("*") if p.is_file()]
    assert files, f"expected a saved learning file under {learning_dir}"
    md = files[0]
    assert md.suffix == ".md"
    # folder name is "<problem_type>_learning"
    assert md.parent.name == "two_pointers_learning"
    assert "complement trick" in md.read_text(encoding="utf-8")
    # the saved path is reported back in the done payload
    assert str(md) in payload["paths"]


def test_run_learning_does_not_require_tier(tmp_path, monkeypatch):
    """Learning must NOT 400 when tier is missing (it has no tier)."""
    c = _make_client(tmp_path, monkeypatch, LEARNING_MARKDOWN)
    resp = c.post(
        "/run",
        json={"problem": "Two Sum", "mode": "learning", "language": "python"},
    )
    assert resp.status_code == 200


# --- Guided Learning mode (tier required) --------------------------------

def test_run_guided_streams_and_saves(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, GUIDED_MARKDOWN)
    resp = c.post(
        "/run",
        json={
            "problem": "Two Sum: return indices of two numbers adding to target.",
            "mode": "guided",
            "language": "python",
            "tier": "normal",
        },
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"

    body = resp.get_data(as_text=True)
    text_chunks, events = _parse_sse(body)

    streamed = "".join(text_chunks)
    assert "Guided session" in streamed
    assert "def two_sum" in streamed

    done = [p for (name, p) in events if name == "done"]
    assert done, f"no done event in: {events}"
    payload = done[0]
    assert payload["mode"] == "guided"
    assert payload["problem_type"] == "two_pointers"
    assert payload.get("paths"), "done payload should list saved file paths"
    assert not [n for (n, _) in events if n == "error"]

    # a markdown file landed under output/guided/<type>/...
    guided_dir = tmp_path / "guided"
    files = [p for p in guided_dir.rglob("*") if p.is_file()]
    assert files, f"expected a saved guided file under {guided_dir}"
    md = files[0]
    assert md.suffix == ".md"
    assert md.parent.name == "two_pointers"
    assert "Guided session" in md.read_text(encoding="utf-8")
    assert str(md) in payload["paths"]


def test_run_guided_rejects_missing_tier(tmp_path, monkeypatch):
    """Guided requires a valid tier; a missing/bad tier must 400."""
    c = _make_client(tmp_path, monkeypatch, GUIDED_MARKDOWN)
    resp = c.post(
        "/run",
        json={"problem": "Two Sum", "mode": "guided", "language": "python"},
    )
    assert resp.status_code == 400
