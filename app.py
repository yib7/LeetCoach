"""Flask web layer for LeetCoach: a single page + an SSE `/run` endpoint.

Design mirrors the Xeno RAG pattern, in Flask flavour:

* ``create_app(*, run_fn=claude_cli.run)`` is a factory with an **injectable**
  Claude runner. The SAME ``run_fn`` is threaded into BOTH the classifier and
  the answer stream, so a single injected fake (tests) covers every Claude call
  while the real orchestration + save still runs end-to-end.
* ``/run`` returns ``Response(stream_with_context(event_stream()),
  mimetype="text/event-stream")``. The generator yields ``data:`` text events
  for each delta and a terminal ``event: done`` (or ``event: error``) so the
  stream always closes cleanly — a last-resort ``except`` guarantees it.

Only **Answer** mode is wired here (SP3). Learning / Guided are recognised as
valid modes but return a "coming soon" error event for now (SP4 wires them).

SSE event protocol (SP4 reuses it for Learning / Guided):
    data: "<text delta>"\n\n                 # incremental answer text (json string)
    event: done\ndata: {json}\n\n             # terminal success:
        { "problem_type": str, "topics": [str], "paths": [str], "mode": str }
    event: error\ndata: "<message>"\n\n        # terminal failure (json string)
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, Response, jsonify, request, stream_with_context

import claude_cli
import classifier
import parsing
import prompts
import storage

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

# Allowlists — never pass an arbitrary string downstream to prompts/storage.
MODES = ("answer", "learning", "guided")
LANGUAGES = prompts.LANGUAGES          # ("python", "cpp", "java")
TIERS = prompts.TIERS                  # ("simple", "normal", "complex")


def _sse_text(delta: str) -> str:
    """A streamed text delta. JSON-encoded so newlines/markdown survive transport
    (a raw newline is an SSE event boundary)."""
    return f"data: {json.dumps(delta)}\n\n"


def _sse_event(name: str, payload) -> str:
    """A named terminal event carrying a JSON payload."""
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"


def create_app(*, run_fn=claude_cli.run) -> Flask:
    """Build the Flask app. ``run_fn`` is the injectable Claude runner used by
    BOTH the classifier and the answer stream (tests pass a fake)."""
    app = Flask(__name__, template_folder=str(TEMPLATES), static_folder=str(STATIC))

    @app.after_request
    def _no_cache(resp):
        # Force revalidation of the frontend code so an edited app.js/style.css
        # is never silently served stale during local iteration.
        if request.path == "/" or request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        # is_available is checked at request time (not import) so the app
        # constructs without a live claude (tests / CI). The page surfaces the
        # state; it never crashes here.
        try:
            available = claude_cli.is_available()
        except Exception:  # noqa: BLE001 - availability probe must never 500 the page
            available = False
        html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        # Inject a tiny banner flag the page reads (kept out of a template engine
        # to keep the page a plain static file editable by hand).
        flag = "true" if available else "false"
        html = html.replace("__CLAUDE_AVAILABLE__", flag)
        return Response(html, mimetype="text/html")

    @app.post("/run")
    def run():
        data = request.get_json(silent=True) or {}
        problem = (data.get("problem") or "").strip()
        mode = (data.get("mode") or "").strip().lower()
        language = (data.get("language") or "").strip().lower()
        tier = (data.get("tier") or "").strip().lower()

        # --- validation (reject unknown values up front, before any Claude call)
        if not problem:
            return jsonify({"error": "Problem text is required."}), 400
        if mode not in MODES:
            return jsonify({"error": f"Unknown mode {mode!r}."}), 400
        if language not in LANGUAGES:
            return jsonify({"error": f"Unknown language {language!r}."}), 400
        # Learning has no tier; Answer/Guided require a valid one.
        if mode != "learning" and tier not in TIERS:
            return jsonify({"error": f"Unknown tier {tier!r}."}), 400

        def event_stream():
            try:
                if mode != "answer":
                    # SP4 wires Learning / Guided; recognised but not yet live.
                    yield _sse_event(
                        "error",
                        f"{mode.title()} mode is coming soon (Answer mode is wired).",
                    )
                    return

                # 1) classify (same injected run_fn -> tests cover this call too)
                cls = classifier.classify(problem, run_fn=run_fn)

                # 2) build the Answer prompt for this tier x language
                prompt = prompts.build_answer(problem, tier=tier, language=language)

                # 3) stream the answer, accumulating the full text as we go
                full = []
                for delta in run_fn(prompt):
                    if delta:
                        full.append(delta)
                        yield _sse_text(delta)
                answer_md = "".join(full)

                # 4) split into code + reasoning and persist
                code = parsing.extract_code(answer_md, language)
                code_path, reasoning_path = storage.save_answer(
                    problem,
                    cls.problem_type,
                    tier=tier,
                    language=language,
                    code=code,
                    reasoning=answer_md,
                )

                # 5) terminal success event
                yield _sse_event(
                    "done",
                    {
                        "mode": mode,
                        "problem_type": cls.problem_type,
                        "topics": cls.topics,
                        "paths": [code_path, reasoning_path],
                    },
                )
            except Exception as exc:  # noqa: BLE001 - last-resort: always close cleanly
                yield _sse_event("error", f"Run failed: {exc}")

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable proxy buffering if present
            },
        )

    return app


# Module-level app for `flask run` / WSGI servers (real claude runner).
app = create_app()


if __name__ == "__main__":
    host = "127.0.0.1"
    port = 5000
    if not claude_cli.is_available():
        print(
            "WARNING: the `claude` CLI was not found on PATH. The page will load "
            "but runs will fail until Claude Code is installed/authenticated "
            "(or set LEETCOACH_CLAUDE_BIN)."
        )
    print(f"LeetCoach running at  http://{host}:{port}  (Ctrl-C to stop)")
    app.run(host=host, port=port, debug=False, threaded=True)
