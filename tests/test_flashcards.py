"""SP7 / D10: parsing ``## Flashcards`` (leniently), collecting cards across
the library, ``GET /flashcards`` and the ``GET /flashcards.tsv`` Anki
export (escaping, UTF-8, attachment)."""
from __future__ import annotations

import csv
import io
import os

import pytest

import app as app_module
import claude_cli
import flashcards as fc
import problem_store


def _doc(section: str, tail: str = "") -> str:
    return ("# 1. Two Sum\nPattern: Arrays & Hashing · Difficulty: Easy\n\n"
            "## Key insight\n\n- Q: not a card (outside the section) - A: nope\n\n"
            "## Flashcards\n\n" + section + tail)


# --- the parser ---------------------------------------------------------------------------

def test_the_contract_format():
    doc = _doc("- Q: What does the map store? - A: value -> index of every element seen.\n"
               "- Q: Why check before inserting? - A: so an element never pairs with itself.\n")
    assert fc.parse_flashcards(doc) == [
        ("What does the map store?", "value -> index of every element seen."),
        ("Why check before inserting?", "so an element never pairs with itself."),
    ]


@pytest.mark.parametrize("line,card", [
    ("- Q: A? — A: B", ("A?", "B")),
    ("- Q: A? – A: B", ("A?", "B")),
    ("- Q: A? | A: B", ("A?", "B")),
    ("- Q: A? A: B", ("A?", "B")),
    ("* **Q:** What is O(n)? **A:** linear time", ("What is O(n)?", "linear time")),
    ("1. Question: Which DS? - Answer: a heap", ("Which DS?", "a heap")),
    ("2) Q1: Big-O of lookup? - A: O(1) average", ("Big-O of lookup?", "O(1) average")),
    ("- q: lower case label - A: ok", ("lower case label", "ok")),
    ("- **Q: bold whole line - A: answer**", ("bold whole line", "answer")),
    ("- Q: ends with dash - A: x -", ("ends with dash", "x")),
])
def test_lenient_one_line_variants(line, card):
    assert fc.parse_flashcards(_doc(line + "\n")) == [card]


def test_answer_on_the_next_line_or_next_bullet():
    doc = _doc("- Q: Two-line card?\n  A: the answer\n  continues here\n\n"
               "- Q: Next-bullet card?\n- A: separate bullet\n\n"
               "**Q:** Paragraph card?\n**A:** paragraph answer\n")
    assert fc.parse_flashcards(doc) == [
        ("Two-line card?", "the answer\ncontinues here"),
        ("Next-bullet card?", "separate bullet"),
        ("Paragraph card?", "paragraph answer"),
    ]


def test_non_cards_are_ignored():
    doc = _doc("Some intro text.\n\n- A plain bullet with no labels\n"
               "- Q: a question with no answer\n- Q: ok? - A: yes\n"
               "```\n- Q: in code - A: skipped\n```\n- A: orphan answer\n")
    assert fc.parse_flashcards(doc) == [("ok?", "yes")]


def test_lowercase_a_colon_inside_text_does_not_split():
    doc = _doc("- Q: What is e.g. a: b mapping? - A: a dict\n")
    assert fc.parse_flashcards(doc) == [("What is e.g. a: b mapping?", "a dict")]


def test_section_ends_at_next_heading_or_rule():
    tail = ("\n---\n\n**Verification:** ✓ Sample tests PASS\n\n- Q: after rule - A: no\n"
            "\n## Your attempt\n\n- Q: after h2 - A: no\n")
    doc = _doc("- Q: inside - A: yes\n", tail)
    assert fc.parse_flashcards(doc) == [("inside", "yes")]
    doc2 = _doc("- Q: inside - A: yes\n## Follow-up — x\n- Q: later - A: no\n")
    assert fc.parse_flashcards(doc2) == [("inside", "yes")]


def test_heading_variants_and_crlf():
    doc = "# T\r\n\r\n## 11. Flash cards\r\n\r\n- Q: crlf? - A: fine\r\n"
    assert fc.parse_flashcards(doc) == [("crlf?", "fine")]
    assert fc.parse_flashcards("## Flashcards\n- Q: a - A: b") == [("a", "b")]


def test_no_section_and_caps():
    assert fc.parse_flashcards("# T\n\n## Solution\n\n- Q: x - A: y\n") == []
    assert fc.parse_flashcards("") == []
    many = "".join(f"- Q: q{i} - A: a{i}\n" for i in range(80))
    assert len(fc.parse_flashcards(_doc(many))) == fc.MAX_CARDS_PER_DOC
    long = fc.parse_flashcards(_doc("- Q: x - A: " + "y" * 5000 + "\n"))
    assert len(long[0][1]) == fc.FIELD_CAP + 1 and long[0][1].endswith("…")


# --- TSV ----------------------------------------------------------------------------------

def test_tsv_field_escaping():
    assert fc.tsv_field("plain text") == "plain text"
    assert fc.tsv_field("a\tb") == '"a\tb"'
    assert fc.tsv_field("line1\nline2") == '"line1\nline2"'
    assert fc.tsv_field("cr\r\nlf\rx") == '"cr\nlf\nx"'
    assert fc.tsv_field('say "hi"') == '"say ""hi"""'
    assert fc.tsv_field("#not a comment") == '"#not a comment"'
    assert fc.tsv_field("") == ""


def test_tsv_round_trips_through_a_tsv_reader():
    cards = [
        {"q": "Tab\there?", "a": 'multi\nline "quoted"', "pattern": "hash_map",
         "problem_id": "1-two_sum"},
        {"q": "Unicode → ✓ 两数之和", "a": "ok", "pattern": "", "problem_id": None},
    ]
    text = fc.to_tsv(cards)
    head, _, body = text.partition("#tags column:3\n")
    assert head == "#separator:tab\n#html:false\n#columns:Front\tBack\tTags\n"
    rows = list(csv.reader(io.StringIO(body), delimiter="\t"))
    assert rows == [
        ["Tab\there?", 'multi\nline "quoted"', "leetcoach hash_map 1-two_sum"],
        ["Unicode → ✓ 两数之和", "ok", "leetcoach"],
    ]
    # every physical line outside quoted fields has exactly 3 columns
    assert text.endswith("\n")


# --- the endpoints ---------------------------------------------------------------------------

@pytest.fixture
def out(tmp_path, monkeypatch):
    root = tmp_path / "out"
    monkeypatch.setenv("LEETCOACH_OUTPUT_DIR", str(root))
    return root


@pytest.fixture
def client(out):
    def no_claude(*a, **k):  # pragma: no cover
        raise AssertionError("no Claude call expected")

    application = app_module.create_app(
        run_fn=no_claude,
        auth_probe=lambda: claude_cli.AuthStatus(installed=True, logged_in=True))
    application.config.update(TESTING=True)
    return application.test_client()


def _write(root, rel, text, mtime):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _library(out):
    _write(out, "guided/hash_map/1_two_sum.md",
           _doc("- Q: newest? - A: guided\n- Q: shared? - A: same\n"), 2_000_000_000)
    _write(out, "learning/hash_map_learning/1_two_sum.md",
           _doc("- Q: shared? - A: same\n- Q: tab\there? - A: \"quoted\"\n"), 1_900_000_000)
    _write(out, "answers/stack/20_valid__optimal.md",
           _doc("- Q: stack? - A: LIFO\n"), 1_800_000_000)
    _write(out, "answers/stack/20_valid__optimal.py", "- Q: not md - A: x\n", 1_800_000_000)
    _write(out, ".leetcoach/hidden.md", _doc("- Q: hidden - A: x\n"), 1_800_000_000)
    problem_store.record_run(
        "1. Two Sum\nEasy\n\nGiven nums", mode="guided", language="python", tier=None,
        model="m", verdict="pass", paths=[out / "guided/hash_map/1_two_sum.md"],
        session_id=None, duration_s=1.0, pattern="hash_map", root=out)


def test_flashcards_endpoint_collects_dedupes_and_orders(client, out):
    _library(out)
    data = client.get("/flashcards").get_json()
    assert [c["q"] for c in data["cards"]] == ["newest?", "shared?", "tab\there?", "stack?"]
    assert data["count"] == 4
    first = data["cards"][0]
    assert first["path"] == "guided/hash_map/1_two_sum.md"
    assert first["problem_id"] == "1-two_sum" and first["title"] == "Two Sum"
    assert first["pattern"] == "hash_map" and len(first["id"]) == 12
    assert data["cards"][2]["pattern"] == "hash_map"  # "_learning" dropped


def test_flashcards_filters(client, out):
    _library(out)
    by_pid = client.get("/flashcards?problem_id=1-two_sum").get_json()["cards"]
    assert [c["q"] for c in by_pid] == ["newest?", "shared?"]
    by_path = client.get("/flashcards?path=answers/stack/20_valid__optimal.md").get_json()
    assert [c["q"] for c in by_path["cards"]] == ["stack?"]
    assert client.get("/flashcards?problem_id=../x").status_code == 400
    assert client.get("/flashcards?path=../secret.md").status_code == 404
    assert client.get("/flashcards?path=.leetcoach/hidden.md").status_code == 404
    assert client.get("/flashcards?path=answers/stack/20_valid__optimal.py").status_code == 404


def test_flashcards_empty_library(client):
    assert client.get("/flashcards").get_json() == {"cards": [], "count": 0}
    resp = client.get("/flashcards.tsv")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True).count("\n") == 4  # header only


def test_tsv_download(client, out):
    _library(out)
    resp = client.get("/flashcards.tsv")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "text/tab-separated-values; charset=utf-8"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="leetcoach-flashcards.tsv"'
    raw = resp.get_data()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM (Anki reads the header line)
    text = raw.decode("utf-8")
    body = text.split("#tags column:3\n", 1)[1]
    rows = list(csv.reader(io.StringIO(body), delimiter="\t"))
    assert rows[0] == ["newest?", "guided", "leetcoach hash_map 1-two_sum"]
    assert rows[2] == ["tab\there?", '"quoted"', "leetcoach hash_map"]
    assert len(rows) == 4
    one = client.get("/flashcards.tsv?problem_id=1-two_sum").get_data(as_text=True)
    assert one.count("leetcoach hash_map 1-two_sum") == 2


def test_flashcards_cache_sees_an_edited_doc(client, out):
    _library(out)
    assert client.get("/flashcards?path=answers/stack/20_valid__optimal.md").get_json()["count"] == 1
    _write(out, "answers/stack/20_valid__optimal.md",
           _doc("- Q: stack? - A: LIFO\n- Q: queue? - A: FIFO\n"), 1_800_000_100)
    assert client.get("/flashcards?path=answers/stack/20_valid__optimal.md").get_json()["count"] == 2
