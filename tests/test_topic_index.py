"""Tests for `topic_index.py` and its wiring into Learning mode.

The pure index tests use a tmp JSON file (via ``LEETCOACH_TOPIC_INDEX``). The
route test mocks Claude (injected ``run_fn``) and asserts the already-learned
topics actually reach ``prompts.build_learning`` — and that a Learning run
records the classifier's topics back into the index.
"""
from __future__ import annotations

import json

import pytest

import topic_index


@pytest.fixture
def idx_path(tmp_path, monkeypatch):
    p = tmp_path / "topic_index.json"
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(p))
    return p


# --- load / save roundtrip -----------------------------------------------

def test_save_then_load_roundtrip(idx_path):
    data = {"by_type": {"hashing": ["hash_map"]}, "all": ["hash_map"]}
    topic_index.save(data)
    loaded = topic_index.load()
    assert loaded["by_type"] == {"hashing": ["hash_map"]}
    assert loaded["all"] == ["hash_map"]


def test_save_uses_config_default_path(tmp_path, monkeypatch):
    # No explicit path + no override env -> falls back to <output_dir>/topic_index.json
    monkeypatch.delenv("LEETCOACH_TOPIC_INDEX", raising=False)
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path))
    topic_index.save({"by_type": {}, "all": ["sliding_window"]})
    expected = tmp_path / "topic_index.json"
    assert expected.exists()
    assert "sliding_window" in expected.read_text(encoding="utf-8")


# --- record then known_topics --------------------------------------------

def test_record_then_known_topics_reflects_it(idx_path):
    topic_index.record("two_pointers", ["arrays", "hashing"])
    known = topic_index.known_topics()
    assert "arrays" in known
    assert "hashing" in known


def test_record_merges_without_duplicates(idx_path):
    topic_index.record("two_pointers", ["arrays", "hashing"])
    topic_index.record("sliding_window", ["arrays", "two_pointers_topic"])
    known = topic_index.known_topics()
    # "arrays" recorded twice but appears once; order preserved (first-seen)
    assert known.count("arrays") == 1
    assert known == ["arrays", "hashing", "two_pointers_topic"]
    # per-type buckets are kept separate
    data = topic_index.load()
    assert data["by_type"]["two_pointers"] == ["arrays", "hashing"]
    assert data["by_type"]["sliding_window"] == ["arrays", "two_pointers_topic"]


def test_record_persists_to_disk(idx_path):
    topic_index.record("graphs", ["bfs", "dfs"])
    # re-load fresh from disk (not in-memory) to prove persistence
    on_disk = json.loads(idx_path.read_text(encoding="utf-8"))
    assert "bfs" in on_disk["all"]
    assert on_disk["by_type"]["graphs"] == ["bfs", "dfs"]


# --- robustness: missing / corrupt file ----------------------------------

def test_missing_file_loads_empty(idx_path):
    assert not idx_path.exists()
    data = topic_index.load()
    assert data == {"by_type": {}, "all": []}
    assert topic_index.known_topics() == []


def test_corrupt_file_loads_empty_without_crashing(idx_path):
    idx_path.write_text("{not valid json at all ::::", encoding="utf-8")
    data = topic_index.load()  # must not raise
    assert data == {"by_type": {}, "all": []}


def test_wrong_shape_file_loads_empty(idx_path):
    # A JSON array (not an object) is the wrong shape -> empty, no crash.
    idx_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert topic_index.load() == {"by_type": {}, "all": []}


def test_record_on_corrupt_file_recovers(idx_path):
    idx_path.write_text("garbage", encoding="utf-8")
    topic_index.record("dp", ["memoization"])  # starts from empty, then records
    assert "memoization" in topic_index.known_topics()


# --- Learning route passes known topics into the prompt builder ----------

CLASSIFY_JSON = {"problem_type": "two_pointers", "topics": ["binary_search"]}
LEARNING_MARKDOWN = "# Learning\n\nA hash map remembers seen values.\n"


def test_learning_route_passes_known_topics_to_prompt(tmp_path, monkeypatch):
    """The Learning route must (a) feed already-learned topics into
    build_learning and (b) record the run's topics afterward."""
    import app as app_module

    # Index points at a tmp file pre-seeded with a known topic.
    idx = tmp_path / "topic_index.json"
    monkeypatch.setenv("LEETCOACH_TOPIC_INDEX", str(idx))
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(tmp_path / "out"))
    topic_index.save({"by_type": {"x": ["sliding_window"]}, "all": ["sliding_window"]})

    # Spy on build_learning to capture the already_learned_topics it receives.
    captured = {}
    real_build = app_module.prompts.build_learning

    def spy_build_learning(problem, *, language, already_learned_topics=None):
        captured["topics"] = already_learned_topics
        return real_build(
            problem, language=language, already_learned_topics=already_learned_topics
        )

    monkeypatch.setattr(app_module.prompts, "build_learning", spy_build_learning)

    def fake_run(prompt, **kwargs):
        if "Classify the following" in prompt and "Respond with ONLY a tiny" in prompt:
            text = json.dumps(CLASSIFY_JSON)
        else:
            text = LEARNING_MARKDOWN
        for i in range(0, len(text), 20):
            yield text[i : i + 20]

    application = app_module.create_app(run_fn=fake_run)
    application.config.update(TESTING=True)
    client = application.test_client()

    resp = client.post(
        "/run",
        json={"problem": "Two Sum", "mode": "learning", "language": "python"},
    )
    assert resp.status_code == 200
    # drain the stream so the generator runs to completion (record happens at end)
    _ = resp.get_data(as_text=True)

    # (a) the already-learned topic was passed into build_learning
    assert captured.get("topics") is not None
    assert "sliding_window" in captured["topics"]

    # (b) the classifier's topic was recorded back into the index
    known = topic_index.known_topics()
    assert "binary_search" in known
    # the pre-existing topic is still there too
    assert "sliding_window" in known
