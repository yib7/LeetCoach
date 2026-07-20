"""Shared test helpers (audit P2-7 de-duplication).

`tests/` is not a package; pytest's default (prepend) import mode puts this
directory on ``sys.path``, so test modules can ``from _helpers import ...``.

Holds the pieces that were copy-pasted across several test files:

* :func:`parse_sse` — split a raw SSE response body into ``(text_chunks,
  events)``; four byte-for-byte-equivalent copies previously lived in
  ``test_web``, ``test_modes``, ``test_classifier_offpath`` and
  ``test_verify_detail``.
* :data:`CLASSIFY_JSON` — the default canned classifier reply
  (``two_pointers`` / ``["arrays"]``) those same modules fed to the fake Claude.
  Two test modules intentionally keep their OWN classifier fixture and are NOT
  unified here: ``test_topic_index`` asserts on ``binary_search`` and
  ``test_modes`` exercises a two-topic reply — importing this constant would
  break the first and hide the intent of the second.
"""
from __future__ import annotations

import json

# The default canned classifier JSON (matches what the real classifier prompt
# asks for: a tiny object naming the technique + topics).
CLASSIFY_JSON = {"problem_type": "two_pointers", "topics": ["arrays"]}


def parse_sse(body: str):
    """Split a raw SSE body into ``(text_chunks, events)`` where ``events`` is a
    list of ``(event_name, payload)``."""
    text_chunks = []
    events = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event_name = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        data = "\n".join(data_lines)
        if event_name is None:
            # plain data: event => a text delta (json-encoded string)
            text_chunks.append(json.loads(data))
        else:
            events.append((event_name, json.loads(data) if data else None))
    return text_chunks, events
