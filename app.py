"""Flask web layer for LeetCoach: a single page, an SSE `/run` endpoint, the
read-only `/library` pair, and the plain-JSON Quick Ask `/ask` endpoint.

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
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, stream_with_context

import classifier
import claude_cli
import config
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

# Bind address for `python app.py` (single source of truth — the Host-header
# allowlist below keys off loopback hostnames, so no port is duplicated here).
HOST = "127.0.0.1"
PORT = 5000

# Host-header allowlist (DNS-rebinding defense). Hostnames only, ANY port: a
# rebinding attacker controls what IP their hostname resolves to, never the
# hostname the victim's browser sends — so matching the hostname IS the whole
# defense, and pinning a port would only break `flask run` on a non-default
# port. Bracketed "[::1]" covers the IPv6 loopback literal.
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})

# Cap on how many already-learned topics get interpolated into the Learning
# prompt (audit6 P2-12): the index grows forever, the prompt must not. The
# stored list is insertion-ordered, so "the most recent N" is its tail.
LEARNED_TOPICS_CAP = 50

# Extensions the library browser (SP10) will list and serve. Everything the
# app itself writes (storage._LANG_EXT + .md, .txt fallback, topic_index.json)
# is covered; anything else in the output dir is invisible to the read path.
LIBRARY_EXTENSIONS = frozenset({".md", ".py", ".cpp", ".java", ".txt", ".json"})

# Quick Ask bounds: the question stays small (it's a syntax lookup, not an
# essay), and the optional problem CONTEXT is capped server-side so a pasted
# novel can't balloon the Haiku prompt.
QUICK_ASK_MAX_QUESTION = 500
QUICK_ASK_PROBLEM_CONTEXT_CAP = 6000

# Allowlists — never pass an arbitrary string downstream to prompts/storage.
MODES = ("answer", "learning", "guided")
LANGUAGES = prompts.LANGUAGES          # ("python", "cpp", "java")
TIERS = prompts.TIERS                  # ("simple", "normal", "complex")


def _non_string_field_error(data: dict, fields) -> str | None:
    """Return a 400-worthy message if any named field is PRESENT but not a
    string, else ``None``. ``fields`` is an iterable of ``(key, Label)`` pairs.

    These endpoints are unauthenticated local routes any script/curl can hit,
    so a JSON number/list/object in a text field must yield a clean 400 — not an
    ``AttributeError``/``TypeError`` 500 from a downstream ``.strip()`` or slice
    (checklist 3.12). A missing field (``None``) and a real string both pass, so
    the existing missing/string handling downstream is untouched."""
    for key, label in fields:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return f"{label} must be text."
    return None


def _hostname(host: str) -> str:
    """The hostname part of a Host header value, port stripped, lowercased.
    Handles the bracketed IPv6 form ("[::1]:5000" -> "[::1]")."""
    host = host.strip().lower()
    if host.startswith("["):
        return host.partition("]")[0] + "]"
    return host.rsplit(":", 1)[0]


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


def _verify_code(code: str, problem: str, language: str):
    """Best-effort sandbox verification of pre-extracted ``code`` (the caller
    extracts exactly once — audit6 P2-13). Returns ``(result, verdict_line)``;
    never raises (a verifier hiccup must not break a run). ``result`` may be
    ``None`` if verification couldn't even start."""
    try:
        result = sandbox.verify_answer(code, problem, language)
        return result, _verification_line(result)
    except Exception as exc:  # noqa: BLE001 - verification is strictly best-effort
        return None, f"⚠ not auto-verified (verifier error: {exc})"


def _library_files(root: Path) -> list[dict]:
    """The library listing: every allowlisted file under ``root``, as
    ``{"path": <relative, forward slashes>, "size": <bytes>, "mtime": <epoch
    seconds>}`` dicts, sorted by path. ``mtime`` lets the frontend show real
    saved dates and derive the recent-runs list; it is an additive field, so
    older callers that read only ``path``/``size`` are unaffected. A
    missing/empty root is an empty list, never an error."""
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LIBRARY_EXTENSIONS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # vanished mid-walk — skip, never 500 a listing
        files.append({
            "path": path.relative_to(root).as_posix(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return files


def _resolve_library_file(rel: str) -> Path | None:
    """Resolve a request's ``path`` param against the output root, or ``None``.

    This is the containment gate for the library's read path — the same rigor
    as ``storage.slug`` on the write path. ``None`` (-> a uniform 404) unless
    ALL of the following hold, so a rejection never leaks what exists:

    * non-empty, no NUL, and not absolute / drive-anchored (``C:\\...``,
      ``\\\\server\\share``, ``/etc``, ``\\foo``);
    * ``(root / rel).resolve()`` — which collapses ``..`` (in slash OR
      backslash form; Windows Path treats both as separators) and follows
      symlinks — stays inside ``root.resolve()`` per ``is_relative_to``;
    * the suffix is on ``LIBRARY_EXTENSIONS`` (checked on the RESOLVED path);
    * it is an existing regular file (directories and reserved names fail).
    """
    if not rel or "\x00" in rel:
        return None
    root = config.output_dir().resolve()
    try:
        candidate = Path(rel)
        if candidate.is_absolute() or candidate.drive:
            return None
        resolved = (root / candidate).resolve()
    except (OSError, ValueError):
        return None
    if not resolved.is_relative_to(root):
        return None
    if resolved.suffix.lower() not in LIBRARY_EXTENSIONS:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _verification_detail(result) -> str:
    """A compact markdown block describing each NON-passing sample (audit6
    P2-9), appended to the SAVED reasoning ``.md`` only — the stream keeps the
    one-line verdict (which already carries the pass/fail counts).

    Empty string unless ``result`` is a fail/error with per-sample detail.
    Input/expected/got/stderr are rendered in fenced blocks so multi-line
    sample bodies (P1-1) stay readable; the sandbox has already truncated and
    capped every captured field.
    """
    if result is None or getattr(result, "status", None) not in ("fail", "error"):
        return ""
    blocks = []
    for entry in getattr(result, "detail", None) or []:
        status = entry.get("status", result.status)
        if status == "pass":
            continue  # the verdict line's counts already cover passing samples
        header = f"**Sample {entry.get('sample', '?')} — {status}**"
        rc = entry.get("returncode")
        if rc is not None:
            header = header[:-2] + f" (exit code {rc})**"
        lines = [header, ""]
        for label, key in (("Input", "stdin"), ("Expected", "expected"), ("Got", "stdout")):
            value = str(entry.get(key, "")).rstrip("\n")
            lines += [f"{label}:", "```", value, "```"]
        stderr = str(entry.get("stderr") or "").rstrip("\n")
        if stderr:
            lines += ["Stderr:", "```", stderr, "```"]
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n**Failed samples:**\n\n" + "\n\n".join(blocks) + "\n"


def create_app(*, run_fn=claude_cli.run) -> Flask:
    """Build the Flask app. ``run_fn`` is the injectable Claude runner used by
    BOTH the classifier and the answer stream (tests pass a fake)."""
    app = Flask(__name__, template_folder=str(TEMPLATES), static_folder=str(STATIC))

    @app.before_request
    def _reject_foreign_hosts():
        # DNS-rebinding defense (audit P1-3): a malicious page can point its own
        # hostname at 127.0.0.1 and drive /run (spending subscription budget and
        # executing generated code in the sandbox). The browser still sends the
        # attacker's hostname in Host, so rejecting non-loopback hostnames
        # blocks the attack for every route.
        if _hostname(request.host) not in ALLOWED_HOSTNAMES:
            return jsonify({"error": "Forbidden host."}), 403

    @app.after_request
    def _response_headers(resp):
        # Force revalidation of the frontend code so an edited app.js/style.css
        # is never silently served stale during local iteration.
        if request.path == "/" or request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        # Defense-in-depth for the untrusted-markdown surface (Claude's output,
        # incl. the cheaper Quick Ask model, is rendered into the page). Every
        # script/style/font is a self-hosted file and the only images the
        # renderer emits are inline data: URIs, so a strict policy holds without
        # 'unsafe-inline'. connect-src 'self' keeps /run, /ask, /library fetches
        # same-origin. nosniff protects the text/plain /library/file route.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "base-uri 'none'; form-action 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
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

    @app.get("/library")
    def library():
        # Read-only listing of the study library (SP10). Missing/empty output
        # dir is an empty listing — the library just hasn't accumulated yet.
        return jsonify({"files": _library_files(config.output_dir().resolve())})

    @app.get("/library/file")
    def library_file():
        # One library file's raw text. Served as text/plain (never HTML) so
        # nothing in the library can execute in the browser; rendering happens
        # client-side through the same hardened pipeline as run output. Every
        # rejection is the same 404 — don't leak which check failed or what
        # exists outside the root.
        resolved = _resolve_library_file(request.args.get("path", ""))
        if resolved is None:
            return jsonify({"error": "Not found."}), 404
        try:
            body = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return jsonify({"error": "Not found."}), 404
        return Response(body, mimetype="text/plain")

    @app.post("/run")
    def run():
        data = request.get_json(silent=True) or {}

        # Type-check before any .strip()/.lower(): a non-string field (a script
        # posting a JSON number/list) must be a clean 400, not a 500 (3.12).
        type_err = _non_string_field_error(
            data,
            (("problem", "Problem"), ("mode", "Mode"),
             ("language", "Language"), ("tier", "Tier")),
        )
        if type_err:
            return jsonify({"error": type_err}), 400

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
                # A stream that ends without producing any text is a failure,
                # not an empty success (audit P2-1): raising here — one place
                # covering all three modes — aborts before any save, and the
                # last-resort handler turns it into the terminal SSE error.
                # A client disconnect instead raises GeneratorExit at the yield
                # above, so it can never reach (or be misreported by) this line.
                if not full:
                    raise RuntimeError("Claude returned an empty answer")
            finally:
                # Publish whatever we accumulated even if the loop raised, so any
                # cleanup path sees a consistent value (the raise still aborts the
                # mode's save/done steps below).
                out[0] = "".join(full)

        out = [""]  # accumulator shared with the helper above

        def event_stream():
            try:
                # 1) classify on a background thread (audit6 P2-4). The short
                #    classification round-trip used to complete BEFORE the first
                #    answer delta streamed, delaying every run by a full Claude
                #    call; its result is only needed at save time, so it now
                #    runs concurrently with the answer stream (same injected
                #    run_fn -> tests still cover it) on the cheap classifier
                #    model. The pre-seeded fallback in ``cls_holder`` keeps the
                #    run alive even if the thread dies: classify never raises by
                #    contract, but a crash here must degrade to "uncategorized",
                #    never abort the run.
                cls_holder = [classifier.Classification(classifier.FALLBACK_TYPE, [])]

                def _classify_in_background():
                    try:
                        cls_holder[0] = classifier.classify(
                            problem, run_fn=run_fn, model=config.classifier_model()
                        )
                    except Exception:  # noqa: BLE001 - fallback already seeded above
                        app.logger.exception("background classification failed")

                cls_thread = threading.Thread(
                    target=_classify_in_background,
                    name="leetcoach-classify",
                    daemon=True,
                )
                cls_thread.start()

                def _classification():
                    """Join the classifier thread and return its result. The
                    join is deliberately unbounded: classify rides the same
                    claude_cli machinery as every run, whose wall-clock watchdog
                    (LEETCOACH_RUN_TIMEOUT) already bounds a hung CLI — a second
                    timeout here would only mask that one."""
                    cls_thread.join()
                    return cls_holder[0]

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
                    # verdict line; the saved reasoning .md gets the verdict
                    # PLUS the per-sample failure detail (audit6 P2-9).
                    result, verdict = _verify_code(code, problem, language)
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    reasoning = (
                        body + "\n\n---\n\n**Verification:** " + verdict + "\n"
                        + _verification_detail(result)
                    )

                    cls = _classification()  # join before the save needs its result
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
                    # Capped to the most recent LEARNED_TOPICS_CAP so the
                    # prompt stays bounded as the index grows (audit6 P2-12).
                    try:
                        learned = topic_index.known_topics(limit=LEARNED_TOPICS_CAP)
                    except Exception:  # noqa: BLE001 - index is best-effort
                        learned = []
                    prompt = prompts.build_learning(
                        problem,
                        language=language,
                        already_learned_topics=learned or None,
                    )
                    yield from _stream_and_accumulate(prompt)
                    cls = _classification()  # join before the save needs its result
                    paths = [storage.save_learning(problem, cls.problem_type, out[0])]
                    try:
                        topic_index.record(cls.problem_type, cls.topics)
                    except Exception:  # noqa: BLE001 - recording is best-effort
                        pass
                else:  # mode == "guided" (validation guarantees a valid tier)
                    prompt = prompts.build_guided(problem, tier=tier, language=language)
                    yield from _stream_and_accumulate(prompt)
                    body = out[0]

                    # SP5: verify Guided's answer step the same way as Answer —
                    # extract the code from the full piped doc exactly once
                    # (P2-13), and save verdict + failure detail (P2-9).
                    code = parsing.extract_code(body, language)
                    result, verdict = _verify_code(code, problem, language)
                    verification = verdict
                    yield _sse_text("\n\n" + verdict + "\n")
                    saved = (
                        body + "\n\n---\n\n**Verification:** " + verdict + "\n"
                        + _verification_detail(result)
                    )
                    cls = _classification()  # join before the save needs its result
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
                # Keep the full traceback in the server log (audit P2-8); the
                # client still gets only the short message below.
                app.logger.exception("run failed (mode=%s)", mode)
                yield _sse_event("error", f"Run failed: {exc}")

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable proxy buffering if present
            },
        )

    @app.post("/ask")
    def ask():
        # Quick Ask (SP2): a small syntax/stdlib question answered by the cheap
        # quick-ask model via the SAME injected run_fn as /run. The answer is
        # ephemeral — plain JSON, no SSE, nothing saved to the library.
        data = request.get_json(silent=True) or {}

        # Type-check before any .strip()/.lower() OR the problem-context slice
        # below (which sits outside the try/except): a non-string field must be
        # a clean 400, not a 500 (3.12), mirroring /run.
        type_err = _non_string_field_error(
            data,
            (("question", "Question"), ("language", "Language"),
             ("problem", "Problem")),
        )
        if type_err:
            return jsonify({"error": type_err}), 400

        question = (data.get("question") or "").strip()
        language = (data.get("language") or "").strip().lower() or "python"
        problem = data.get("problem") or ""

        # --- validation (reject before any Claude call, mirroring /run)
        if not question:
            return jsonify({"error": "A question is required."}), 400
        if len(question) > QUICK_ASK_MAX_QUESTION:
            return jsonify(
                {"error": f"Question too long (max {QUICK_ASK_MAX_QUESTION} chars)."}
            ), 400
        if language not in LANGUAGES:
            return jsonify({"error": f"Unknown language {language!r}."}), 400

        prompt = prompts.build_quick_ask(
            question,
            language=language,
            problem=problem[:QUICK_ASK_PROBLEM_CONTEXT_CAP],
        )
        try:
            answer = "".join(run_fn(prompt, model=config.quick_ask_model())).strip()
        except Exception as exc:  # noqa: BLE001 - surface as a clean 502, log the rest
            app.logger.exception("quick ask failed")
            return jsonify({"error": f"Quick Ask failed: {exc}"}), 502
        if not answer:
            # Same stance as /run's empty-stream guard: no text is a failure,
            # not an empty success.
            return jsonify({"error": "Claude returned an empty answer."}), 502
        return jsonify({"answer": answer})

    return app


# Module-level app for `flask run` / WSGI servers (real claude runner).
app = create_app()


if __name__ == "__main__":
    host = HOST
    port = PORT
    if not claude_cli.is_available():
        print(
            "WARNING: the `claude` CLI was not found on PATH. The page will load "
            "but runs will fail until Claude Code is installed/authenticated "
            "(or set LEETCOACH_CLAUDE_BIN)."
        )
    print(f"LeetCoach running at  http://{host}:{port}  (Ctrl-C to stop)")
    app.run(host=host, port=port, debug=False, threaded=True)
