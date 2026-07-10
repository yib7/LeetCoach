/* LeetCoach frontend.
 *
 * POSTs the run form to /run and consumes the SSE response with fetch +
 * ReadableStream (POST body is cleanest this way — no EventSource GET dance).
 * Text deltas append live; the accumulated markdown is re-rendered with the
 * vendored `marked`, and code blocks are syntax-highlighted with the vendored
 * `highlight.js`. A terminal `done` event shows the classification + saved
 * paths; an `error` event surfaces the failure. No runtime CDN dependency.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var problemEl = $("problem");
  var modeEl = $("mode");
  var languageEl = $("language");
  var tierEl = $("tier");
  var tierField = $("tier-field");
  var runBtn = $("run");
  var stopBtn = $("stop");
  var pasteBtn = $("paste");
  var statusEl = $("status");
  var metaEl = $("meta");
  var outputEl = $("output");

  // No marked.setOptions needed: marked v12 ignores the old `highlight`
  // option (code blocks are highlighted post-render via hljs.highlightElement
  // in render()), and `breaks: false` is marked's default.

  // Defense-in-depth XSS hardening (this is a localhost tool, but Claude's
  // output is still untrusted markdown we render via innerHTML). marked v12
  // dropped the old `sanitize` option, so neutralize raw HTML at the renderer
  // level: any literal HTML block/inline in the markdown is escaped and shown
  // as text instead of being injected into the DOM. Markdown-generated tags
  // (headings, code, emphasis, links, etc.) still render normally.
  if (window.marked && typeof marked.use === "function") {
    marked.use({
      renderer: {
        // Raw HTML blocks/inline: emit them as escaped, visible text.
        html: function (token) {
          var raw = typeof token === "string" ? token : (token && token.text) || "";
          return escapeHtml(raw);
        },
        // Links: neutralize non-http(s) protocols (javascript:, data:, ...).
        // marked v12 renderer signature is positional: link(href, title, text)
        // where `text` is the already-rendered (escaped) inner HTML.
        link: function (href, title, text) {
          var h = String(href || "");
          if (!/^https?:/i.test(h)) return text || escapeHtml(h);
          var attr = escapeHtml(h).replace(/"/g, "&quot;");
          return '<a href="' + attr + '" rel="noopener" target="_blank">' +
            (text || escapeHtml(h)) + "</a>";
        },
      },
    });
  }

  // Claude availability banner (flag injected server-side into <body>).
  if (document.body.getAttribute("data-claude-available") === "false") {
    var warn = $("claude-warning");
    if (warn) warn.hidden = false;
  }

  // Learning mode has no tier — disable the selector when it's chosen.
  function syncTier() {
    var isLearning = modeEl.value === "learning";
    tierEl.disabled = isLearning;
    tierField.classList.toggle("disabled", isLearning);
  }
  modeEl.addEventListener("change", syncTier);
  syncTier();

  // Paste-from-clipboard.
  pasteBtn.addEventListener("click", function () {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      setStatus("Clipboard read is not available in this browser.", "warn");
      return;
    }
    navigator.clipboard.readText().then(
      function (text) {
        problemEl.value = text;
        problemEl.focus();
      },
      function () {
        setStatus("Could not read the clipboard (permission denied?).", "warn");
      }
    );
  });

  function setStatus(text, kind) {
    statusEl.hidden = !text;
    statusEl.textContent = text || "";
    statusEl.className = "status" + (kind ? " status-" + kind : "");
  }

  function render(md) {
    if (window.marked) {
      outputEl.innerHTML = marked.parse(md);
      if (window.hljs) {
        outputEl.querySelectorAll("pre code").forEach(function (block) {
          try { hljs.highlightElement(block); } catch (e) { /* noop */ }
        });
      }
    } else {
      // Last-resort: show raw text if the lib failed to load.
      outputEl.textContent = md;
    }
  }

  // Coalesce streaming re-renders onto animation frames. Re-parsing the full
  // accumulated markdown (plus re-highlighting every code block) on every SSE
  // delta is O(n^2) jank on long outputs — instead remember the latest text
  // and render at most once per frame. A direct render(acc) still runs when
  // the stream ends, so the final content is always complete (rAF does not
  // fire in background tabs).
  var renderPending = false;
  var renderLatest = "";
  function scheduleRender(md) {
    renderLatest = md;
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(function () {
      renderPending = false;
      render(renderLatest);
    });
  }

  function showMeta(payload) {
    var paths = (payload.paths || [])
      .map(function (p) { return "<code>" + escapeHtml(p) + "</code>"; })
      .join("<br>");
    var topics = (payload.topics || []).join(", ");
    metaEl.hidden = false;
    metaEl.innerHTML =
      '<div class="meta-row"><strong>Type:</strong> ' +
      escapeHtml(payload.problem_type || "—") +
      "</div>" +
      (topics
        ? '<div class="meta-row"><strong>Topics:</strong> ' + escapeHtml(topics) + "</div>"
        : "") +
      (paths ? '<div class="meta-row"><strong>Saved:</strong><br>' + paths + "</div>" : "");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function setRunning(on) {
    runBtn.disabled = on;
    runBtn.textContent = on ? "Running…" : "Run";
    stopBtn.hidden = !on;
    stopBtn.disabled = !on;
  }

  // Stop button: aborts the in-flight run (set per-run inside runNow). The
  // server cancels the Claude subprocess when the connection drops.
  var activeStop = null;
  stopBtn.addEventListener("click", function () {
    if (activeStop) activeStop();
  });

  // Parse the SSE wire format incrementally out of the fetch byte stream.
  // Emits {type:"text", data} for plain `data:` lines and
  // {type:"event", name, data} for `event:`+`data:` blocks.
  function makeSseParser(onEvent) {
    var buf = "";
    return function (chunk) {
      buf += chunk;
      var idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        var block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        var name = null;
        var dataLines = [];
        block.split("\n").forEach(function (line) {
          if (line.indexOf("event:") === 0) {
            name = line.slice(6).trim();
          } else if (line.indexOf("data:") === 0) {
            dataLines.push(line.slice(5).trim());
          }
        });
        if (!dataLines.length && name === null) continue;
        var raw = dataLines.join("\n");
        var parsed;
        try { parsed = JSON.parse(raw); } catch (e) { parsed = raw; }
        if (name === null) onEvent({ type: "text", data: parsed });
        else onEvent({ type: "event", name: name, data: parsed });
      }
    };
  }

  async function runNow() {
    var problem = problemEl.value.trim();
    if (!problem) {
      setStatus("Paste a problem first.", "warn");
      return;
    }
    var body = {
      problem: problem,
      mode: modeEl.value,
      language: languageEl.value,
      tier: tierEl.disabled ? "" : tierEl.value,
    };

    setRunning(true);
    setStatus("Asking Claude…", "info");
    metaEl.hidden = true;
    outputEl.innerHTML = "";
    var acc = "";

    // Abort the in-flight /run if the user navigates away or closes the tab, so
    // the server sees the connection drop and cancels the Claude subprocess
    // instead of letting it run to completion and burn subscription usage.
    var controller = new AbortController();
    function abortOnUnload() { controller.abort(); }
    window.addEventListener("pagehide", abortOnUnload);
    window.addEventListener("beforeunload", abortOnUnload);

    // A deliberate Stop is not an error — remember it so the AbortError
    // handler can show a neutral "Stopped." instead of a warning.
    var stoppedByUser = false;
    activeStop = function () {
      stoppedByUser = true;
      controller.abort();
    };

    try {
      var resp = await fetch("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!resp.ok) {
        var err = await resp.json().catch(function () { return {}; });
        setStatus(err.error || "Request rejected (" + resp.status + ").", "warn");
        setRunning(false);
        return;
      }

      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var feed = makeSseParser(function (ev) {
        if (ev.type === "text") {
          acc += ev.data;
          scheduleRender(acc);
        } else if (ev.name === "done") {
          setStatus("Done. Saved to your study library.", "ok");
          showMeta(ev.data || {});
        } else if (ev.name === "error") {
          setStatus(typeof ev.data === "string" ? ev.data : "Run failed.", "warn");
        }
      });

      while (true) {
        var r = await reader.read();
        if (r.done) break;
        feed(decoder.decode(r.value, { stream: true }));
      }
    } catch (e) {
      // An AbortError here is an intentional cancel (Stop button or page
      // unload), not a fault.
      if (e && e.name === "AbortError") {
        if (stoppedByUser) setStatus("Stopped.", "");
      } else {
        setStatus("Network error: " + (e && e.message), "warn");
      }
    } finally {
      window.removeEventListener("pagehide", abortOnUnload);
      window.removeEventListener("beforeunload", abortOnUnload);
      activeStop = null;
      // Final render outside the rAF path so the last chunk is never lost
      // (and partial output stays visible after a Stop).
      render(acc);
      setRunning(false);
    }
  }

  runBtn.addEventListener("click", runNow);
})();
