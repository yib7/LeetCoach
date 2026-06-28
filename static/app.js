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
  var pasteBtn = $("paste");
  var statusEl = $("status");
  var metaEl = $("meta");
  var outputEl = $("output");

  // Configure marked to hand code blocks to highlight.js when available.
  if (window.marked && window.hljs) {
    marked.setOptions({
      breaks: false,
      highlight: function (code, lang) {
        try {
          if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
          }
          return hljs.highlightAuto(code).value;
        } catch (e) {
          return code;
        }
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
  }

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

    try {
      var resp = await fetch("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
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
          render(acc);
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
      setStatus("Network error: " + e.message, "warn");
    } finally {
      setRunning(false);
    }
  }

  runBtn.addEventListener("click", runNow);
})();
