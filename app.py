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

All three modes (Answer / Learning / Guided) are wired here. They share one
shape — classify -> build a mode-specific prompt -> stream + accumulate the
deltas -> save the result -> emit a terminal ``done`` — so the streaming and the
``done``/``error`` plumbing live in one place (``event_stream`` +
``_stream_and_accumulate``); only the per-mode prompt-builder and save call
differ.

SSE event protocol (shared by every mode):
    data: "<text delta>"\n\n                 # incremental answer text (json string)
    event: done\ndata: {json}\n\n             # terminal success:
        { "problem_type": str, "topics": [str], "paths": [str], "mode": str,
          "verification": str (Answer/Guided only — the sandbox verdict line) }
    event: error\ndata: "<message>"\n\n        # terminal failure (json string)
"""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context

import classifier
import claude_cli
import parsing
import prompts
import sandbox
import storage
import topic_index

# Load .env from the project root (next to this file) if present, so LEETCOACH_*
# settings written to a .env take effect for `python app.py` and WSGI imports.
# Never overrides vars already set in the real environment; a missing .env is a
# silent no-op.
load_dotenv(Path(__file__).resolve().parent / ".env")

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


def _verification_line(result) -> str:
    """A short one-line human verdict for the stream + saved markdown, derived
    from a ``sandbox.VerifyResult``."""
    status = getattr(result, "status", "not_verified")
    note = getattr(result, "note", "") or ""
    if status == "pass":
        return f"✓ Sample tests PASS ({note})" if note else "✓ Sample tests PASS"
    if status == "fail":
        return f"✗ Sample tests FAIL ({note})" if note else "✗ Sample tests FAIL"
    if status == "error":
        return f"✗ Sample tests ERROR ({note})" if note else "✗ Sample tests ERROR"
    # not_verified
    return f"⚠ not auto-verified ({note})" if note else "⚠ not auto-verified"


def _verify_code(body: str, problem: str, language: str):
    """Best-effort sandbox verification of the extracted code. Returns
    ``(result, verdict_line)``; never raises (a verifier hiccup must not break a
    run). ``result`` may be ``None`` if verification couldn't even start."""
    try:
        code = parsing.extract_code(body, language)
        result = sandbox.verify_answer(code, problem, language)
        return result, _verification_line(result)
    except Exception as exc:  # noqa: BLE001 - verification is strictly best-effort
        return None, f"⚠ not auto-verified (verifier error: {exc})"


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

        def _stream_and_accumulate(prompt):
            """Stream ``run_fn(prompt)`` deltas to the client (yielding SSE text
            events) while accumulating the full text. Returns the joined text via
            a one-element list trick — generators can't ``return`` a value the
            caller easily reads while also yielding, so we stash it on ``out[0]``.

            If iterating ``run_fn`` fails mid-stream (e.g. the `claude` subprocess
            dies part-way through a response), the exception is caught HERE and
            re-raised only after the accumulator is left in a defined state — so
            the outer ``event_stream`` handler converts it into a terminal SSE
            ``error`` event instead of the stream cutting off silently. Whatever
            text arrived before the failure has already been yielded to the
            client; we do NOT proceed to save a partial/incomplete answer.
            """
            out[0] = ""  # reset accumulator for this call
            full = []
            try:
                for delta in run_fn(prompt):
                    if delta:
                        full.append(delta)
                        yield _sse_text(delta)
            finally:
                # Publish whatever we accumulated even if the loop raised, so any
                # cleanup path sees a consistent value (the raise still aborts the
                # mode's save/done steps below).
                out[0] = "".join(full)

        out = [""]  # accumulator shared with the helper above

        def event_stream():
            try:
                # 1) classify (same injected run_fn -> tests cover this call too)
                cls = classifier.classify(problem, run_fn=run_fn)

                # 2) build the mode-specific prompt; 3) stream + accumulate;
                #    4) save with the mode's own storage call. Only these two
                #    bits differ between modes — the stream/accumulate/done
                #    plumbing is shared. ``verification`` (Answer/Guided) is the
                #    sandbox verdict reported in the stream, saved .md and done
                #    payload; it stays None when a mode doesn't verify.
                verification = None
                if mode == "answer":
                    prompt = prompts.build_answer(problem, tier=tier, language=language)
                    yield from _stream_and_accumulate(prompt)
                    body = out[0]
                    code = parsing.extract_code(body, language)

                    # SP5: best-effort sample-I/O verification. Stream a short
                    # verdict line and fold it into the saved reasoning .md.
                    result, verdict = _verify_code(body, problem, language)
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    reasoning = body + "\n\n---\n\n**Verification:** " + verdict + "\n"

                    code_path, reasoning_path = storage.save_answer(
                        problem,
                        cls.problem_type,
                        tier=tier,
                        language=language,
                        code=code,
                        reasoning=reasoning,
                    )
                    paths = [code_path, reasoning_path]
                elif mode == "learning":
                    # SP5: feed already-learned topics so Claude skips/cross-links
                    # covered tech, then record this run's topics afterward.
                    try:
                        learned = topic_index.known_topics()
                    except Exception:  # noqa: BLE001 - index is best-effort
                        learned = []
                    prompt = prompts.build_learning(
                        problem,
                        language=language,
                        already_learned_topics=learned or None,
                    )
                    yield from _stream_and_accumulate(prompt)
                    paths = [storage.save_learning(problem, cls.problem_type, out[0])]
                    try:
                        topic_index.record(cls.problem_type, cls.topics)
                    except Exception:  # noqa: BLE001 - recording is best-effort
                        pass
                else:  # mode == "guided" (validation guarantees a valid tier)
                    prompt = prompts.build_guided(problem, tier=tier, language=language)
                    yield from _stream_and_accumulate(prompt)
                    body = out[0]

                    # SP5: verify Guided's answer step the same way as Answer.
                    result, verdict = _verify_code(body, problem, language)
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    saved = body + "\n\n---\n\n**Verification:** " + verdict + "\n"
                    paths = [storage.save_guided(problem, cls.problem_type, saved)]

                # 5) terminal success event
                done_payload = {
                    "mode": mode,
                    "problem_type": cls.problem_type,
                    "topics": cls.topics,
                    "paths": paths,
                }
                if verification is not None:
                    done_payload["verification"] = verification
                yield _sse_event("done", done_payload)
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
