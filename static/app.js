/* LeetCoach frontend (application-shell UI).
 *
 * POSTs the run form to /run and consumes the SSE response with fetch +
 * ReadableStream (POST body is cleanest this way — no EventSource GET dance).
 * Text deltas append live; the accumulated markdown is re-rendered with the
 * vendored `marked`, code blocks are syntax-highlighted with the vendored
 * `highlight.js` and wrapped in chrome. A terminal `done` event shows the
 * saved-summary card; `error`/abort surface a failure while KEEPING the partial
 * output. No runtime CDN dependency.
 *
 * SSE invariants (do NOT "improve"):
 *  - #output is the SINGLE node whose innerHTML is replaced once per frame,
 *    and its innerHTML is assigned ONLY inside render().
 *  - Streaming chrome (state pill / chips / live timer / Stop / caret) lives on
 *    SIBLING nodes around #output and is written with direct textContent/class
 *    writes — render() never touches it.
 *  - Code-block chrome + hljs are per-frame post-render passes over #output.
 *    Copy is handled by ONE delegated click listener added once at init.
 *  - /run request body stays { problem, mode, language, tier }.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- static SVG snippets (trusted literals, no user data) ---------------
  var COPY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>';
  var CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M5 13l4 4L19 7"/></svg>';
  var CHECK_TABLE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 13l4 4L19 7"/></svg>';
  var FOLDER_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2h9A1.5 1.5 0 0 1 21 8.5V18a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18Z"/></svg>';
  var ERR_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"><path d="M12 4 3 19h18Z"/><path d="M12 10v4"/><path d="M12 17h.01"/></svg>';

  var SAMPLE =
    "Two Sum\n\nGiven an array of integers nums and an integer target, return " +
    "indices of the two numbers such that they add up to target.\n\nYou may " +
    "assume that each input would have exactly one valid answer, and you may " +
    "not use the same element twice.\n\nExample:\nInput: nums = [2,7,11,15], " +
    "target = 9\nOutput: [0,1]\n\nConstraints:\n2 <= nums.length <= 10^4";

  var CODE_EXT = { py: 1, cpp: 1, cc: 1, cxx: 1, c: 1, java: 1, cs: 1, js: 1, ts: 1, go: 1, rs: 1, rb: 1, kt: 1, swift: 1 };
  var HLJS_LANG = { py: "python", cpp: "cpp", java: "java", json: "json" };

  // ---- core element handles ----------------------------------------------
  var problemEl = $("problem");
  var editorEl = $("editor");
  var runBtn = $("run");
  var stopBtn = $("stop");
  var pasteBtn = $("paste");
  var sampleBtn = $("sample");
  var clearBtn = $("clear");
  var newRunBtn = $("new-run");
  var outputEl = $("output");
  var claudeWarning = $("claude-warning");

  var viewConsole = $("view-console");
  var viewLibrary = $("view-library");

  // session lifecycle containers (all live inside #session as siblings)
  var sessionState = document.querySelector("[data-session-state]");
  var consoleIdle = document.querySelector(".console-idle");
  var runhead = document.querySelector(".runhead");
  var outwrap = document.querySelector(".outwrap");
  var streamFoot = document.querySelector(".stream-foot");
  var summaryEl = document.querySelector(".summary");
  var errbox = document.querySelector(".errbox");
  var stopmark = document.querySelector(".stopmark");

  // run-header children (cached once; runhead is never re-created)
  var pulseEl = runhead ? runhead.querySelector(".pulse") : null;
  var rhLabel = runhead ? runhead.querySelector(".rh-label") : null;
  var rhChips = runhead ? runhead.querySelector(".rh-chips") : null;
  var timerEl = runhead ? runhead.querySelector(".timer") : null;

  // derived-data surfaces
  var topicsEl = $("topics");
  var recentsEl = $("recents");
  var recentTable = $("recent-table");
  var recentCount = document.querySelector("[data-recent-count]");

  // segmented controls
  var tierGroup = document.querySelector('.tgroup[data-seg="tier"]');

  // library two-pane
  var libTree = $("library-tree");
  var libViewer = $("library-viewer");
  var libViewerPath = $("library-viewer-path");
  var libViewerBody = $("library-viewer-body");
  var libViewerClose = $("library-viewer-close");

  // ---- marked XSS hardening (KEEP EXACTLY) --------------------------------
  // Defense-in-depth: Claude's output is untrusted markdown rendered via
  // innerHTML. marked v12 dropped `sanitize`, so neutralize raw HTML at the
  // renderer level (escaped, visible as text) and drop non-http(s) links.
  if (window.marked && typeof marked.use === "function") {
    marked.use({
      renderer: {
        html: function (token) {
          var raw = typeof token === "string" ? token : (token && token.text) || "";
          return escapeHtml(raw);
        },
        link: function (href, title, text) {
          var h = String(href || "");
          if (!/^https?:/i.test(h)) return text || escapeHtml(h);
          var attr = escapeHtml(h).replace(/"/g, "&quot;");
          return '<a href="' + attr + '" rel="noopener" target="_blank">' +
            (text || escapeHtml(h)) + "</a>";
        },
        // LeetCoach is a local, offline tool with NO legitimate remote-image
        // use case. A network-loading <img> the browser auto-fetches on render
        // is a data-egress / tracking channel reachable via prompt-injected
        // problem text through Claude (incl. Haiku Quick Ask), and breaks the
        // "Local & offline" promise. So allow ONLY inline data:image/... sources
        // (SVG loaded via <img> can't run script and makes no request); render
        // ANY other src — http(s), protocol-relative //, javascript:, relative,
        // empty — as the alt text, never an <img>. marked v12 passes positional
        // (href, title, text) like `link` above and pre-escapes `text` (alt),
        // so it is emitted as-is exactly as `link` emits its `text`.
        image: function (href, title, text) {
          var h = String(href || "");
          if (!/^data:image\//i.test(h)) return text || "";
          var attr = escapeHtml(h).replace(/"/g, "&quot;");
          return '<img src="' + attr + '" alt="' + (text || "") + '">';
        },
      },
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ---- tiny DOM helpers ---------------------------------------------------
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function chipEl(text, variant) {
    return el("span", "chip" + (variant ? " " + variant : ""), text);
  }
  function cap(s) {
    s = String(s || "");
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }
  function humanize(s) {
    s = String(s || "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
    if (!s) return "";
    return s.split(" ").map(function (w) {
      return w.charAt(0).toUpperCase() + w.slice(1);
    }).join(" ");
  }
  function extOf(path) {
    var f = String(path);
    var slash = Math.max(f.lastIndexOf("/"), f.lastIndexOf("\\"));
    var name = slash === -1 ? f : f.slice(slash + 1);
    var dot = name.lastIndexOf(".");
    return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
  }
  function firstLine(text) {
    var lines = String(text || "").split("\n");
    for (var i = 0; i < lines.length; i++) {
      var t = lines[i].trim();
      if (t) return t.length > 60 ? t.slice(0, 60) + "…" : t;
    }
    return "";
  }
  // Middle-truncate: `.a` ellipsizes (flex-shrink), `.b` is a pinned tail so the
  // meaningful end (extension) always stays visible.
  function midTrunc(str, tail) {
    tail = tail || 6;
    str = String(str);
    if (str.length <= tail + 3) return { a: str, b: "" };
    return { a: str.slice(0, str.length - tail), b: str.slice(str.length - tail) };
  }
  function typeBadge(ext) {
    // Type class (tb md/py/cpp/java) is styled entirely in style.css.
    return el("span", "tb " + (ext || ""), ext || "?");
  }
  function modeLabel(folder) {
    var m = { answers: "Answer", answer: "Answer", learning: "Learning", guided: "Guided" };
    return m[folder] || humanize(folder);
  }
  function langLabel(ext) {
    // Accepts both a file extension (py/cpp/java — deriveRuns) and the run's
    // wire language value (python/cpp/java — the summary meta); cpp/java are
    // identical across both, only "python" vs "py" needs the extra key.
    var m = { py: "Python", python: "Python", cpp: "C++", java: "Java" };
    return m[ext] || "—";
  }
  function relTime(mtime) {
    if (!mtime) return "";
    var d = Date.now() / 1000 - mtime;
    if (d < 45) return "now";
    if (d < 3600) return Math.max(1, Math.round(d / 60)) + "m";
    if (d < 86400) return Math.round(d / 3600) + "h";
    if (d < 7 * 86400) return Math.round(d / 86400) + "d";
    try {
      return new Date(mtime * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch (e) { return ""; }
  }
  function savedDate(mtime) {
    try {
      return new Date(mtime * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch (e) { return ""; }
  }
  function bySavedDesc(a, b) { return (b.savedAt || 0) - (a.savedAt || 0); }

  function copyText(text, btn) {
    function ok() {
      btn.classList.add("ok");
      var lbl = btn.querySelector(".lbl");
      var prev = lbl ? lbl.textContent : "";
      if (lbl) lbl.textContent = "Copied";
      setTimeout(function () {
        btn.classList.remove("ok");
        if (lbl) lbl.textContent = prev;
      }, 1400);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(String(text)).then(ok, function () { /* noop */ });
    } else {
      ok();
    }
  }

  function flashEditor() {
    if (!editorEl) return;
    editorEl.classList.remove("flash");
    void editorEl.offsetWidth; // reflow so the animation restarts
    editorEl.classList.add("flash");
  }

  // ---- CLI availability pill ---------------------------------------------
  (function claudeStatus() {
    var ready = document.body.dataset.claudeAvailable === "true";
    var pill = $("claude-status");
    var dot = pill ? pill.querySelector(".dot") : null;
    var label = pill ? pill.querySelector(".tb-status-label") : null;
    if (ready) {
      if (dot) dot.classList.remove("red");
      if (label) label.textContent = "claude CLI ready";
      if (claudeWarning) claudeWarning.hidden = true;
    } else {
      if (dot) dot.classList.add("red");
      if (label) label.textContent = "claude CLI not found";
      if (claudeWarning) claudeWarning.hidden = false;
    }
  })();

  // ---- segmented controls -------------------------------------------------
  function activeVal(group) {
    var on = document.querySelector('.seg[data-seg="' + group + '"] .seg-btn.on');
    return on ? on.getAttribute("data-val") : "";
  }
  function syncTier() {
    if (tierGroup) tierGroup.classList.toggle("disabled", activeVal("mode") === "learning");
  }
  document.querySelectorAll(".seg").forEach(function (seg) {
    seg.addEventListener("click", function (e) {
      var b = e.target.closest(".seg-btn");
      if (!b || !seg.contains(b)) return;
      seg.querySelectorAll(".seg-btn").forEach(function (x) { x.classList.remove("on"); });
      b.classList.add("on");
      if (seg.getAttribute("data-seg") === "mode") syncTier();
    });
  });
  syncTier();

  // ---- Console <-> Library tabs ------------------------------------------
  function switchView(view) {
    if (viewConsole) viewConsole.hidden = view !== "console";
    if (viewLibrary) viewLibrary.hidden = view !== "library";
    document.querySelectorAll("[data-view]").forEach(function (a) {
      a.classList.toggle("on", a.getAttribute("data-view") === view);
    });
    if (view === "library") loadLibrary(); // re-fetch on open: fresh after runs
  }
  document.querySelectorAll("[data-view]").forEach(function (a) {
    a.addEventListener("click", function () { switchView(a.getAttribute("data-view")); });
    a.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchView(a.getAttribute("data-view")); }
    });
  });

  // ---- filter pills -------------------------------------------------------
  function activatePill(pill) {
    var group = pill.parentNode;
    if (group) group.querySelectorAll(".fp").forEach(function (x) { x.classList.remove("on"); });
    pill.classList.add("on");
  }
  document.querySelectorAll("[data-lib-filter]").forEach(function (pill) {
    pill.addEventListener("click", function () { activatePill(pill); applyLibFilter(); });
  });
  document.querySelectorAll("[data-recent-filter]").forEach(function (pill) {
    pill.addEventListener("click", function () { activatePill(pill); applyRecentFilter(); });
  });

  // ---- composer tools -----------------------------------------------------
  if (pasteBtn) {
    pasteBtn.addEventListener("click", function () {
      if (!navigator.clipboard || !navigator.clipboard.readText) {
        console.warn("Clipboard read is not available in this browser.");
        return;
      }
      navigator.clipboard.readText().then(function (text) {
        problemEl.value = text;
        problemEl.focus();
        flashEditor();
      }, function () {
        console.warn("Could not read the clipboard (permission denied?).");
      });
    });
  }
  if (sampleBtn) sampleBtn.addEventListener("click", function () { problemEl.value = SAMPLE; problemEl.focus(); });
  if (clearBtn) clearBtn.addEventListener("click", function () { problemEl.value = ""; problemEl.focus(); });
  if (newRunBtn) newRunBtn.addEventListener("click", function () {
    if (isStreaming) return;
    problemEl.value = "";
    enterIdle();
    switchView("console");
    problemEl.focus();
  });

  // =========================================================================
  // Render pipeline — #output is the single re-rendered node.
  // =========================================================================
  function render(md) {
    if (window.marked) {
      outputEl.innerHTML = marked.parse(md); // ONLY assignment site for #output
      if (window.hljs) {
        outputEl.querySelectorAll("pre code").forEach(function (block) {
          try { hljs.highlightElement(block); } catch (e) { /* noop */ }
        });
      }
      decorateCode(outputEl, isStreaming);
    } else {
      outputEl.textContent = md;
    }
  }

  // Coalesce streaming re-renders onto animation frames (re-parsing the full
  // accumulated markdown every delta is O(n^2) jank). A direct render() still
  // runs when the stream ends so the final content is always complete.
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

  // Post-render pass: wrap each <pre> in .code/.code-head chrome with a lang
  // label + Copy button. Rebuilt every frame (innerHTML was replaced) — adds NO
  // listeners (Copy is delegated on the container, once, at init).
  function decorateCode(container, streaming) {
    var pres = container.querySelectorAll("pre");
    pres.forEach(function (pre, i) {
      var parent = pre.parentNode;
      if (parent && parent.classList && parent.classList.contains("code")) return;
      var code = pre.querySelector("code");
      var lang = "";
      if (code) {
        var m = /language-([\w+#-]+)/.exec(code.className || "");
        if (m) lang = m[1];
      }
      var wrap = document.createElement("div");
      wrap.className = "code" + (streaming && i === pres.length - 1 ? " partial" : "");
      var head = document.createElement("div");
      head.className = "code-head";
      var langSpan = el("span", "code-lang");
      langSpan.innerHTML = '<span class="d"></span>';
      langSpan.appendChild(document.createTextNode(lang || "code"));
      var copyBtn = el("button", "mini");
      copyBtn.type = "button";
      copyBtn.setAttribute("data-copy-code", "");
      copyBtn.innerHTML = COPY_SVG + '<span class="lbl">Copy</span>';
      head.appendChild(langSpan);
      head.appendChild(copyBtn);
      parent.insertBefore(wrap, pre);
      wrap.appendChild(head);
      wrap.appendChild(pre);
    });
  }

  // ONE delegated Copy listener per container (added once). Survives the
  // per-frame innerHTML replacement of #output because it sits on the parent.
  function handleCopyCode(e) {
    var btn = e.target.closest("[data-copy-code]");
    if (!btn) return;
    var wrap = btn.closest(".code");
    var code = wrap && wrap.querySelector("code");
    copyText(code ? code.textContent : "", btn);
  }
  outputEl.addEventListener("click", handleCopyCode);
  if (libViewerBody) libViewerBody.addEventListener("click", handleCopyCode);

  // =========================================================================
  // Run lifecycle state machine (idle -> streaming -> done|error|stopped).
  // Streaming chrome is written on the runhead / stream-foot SIBLINGS.
  // =========================================================================
  var isStreaming = false;
  var acc = "";              // the accumulator: acc += delta; scheduleRender(acc)
  var activeStop = null;     // set per-run; Stop button calls it
  var timerId = null;
  var runStart = 0;
  var lastDuration = "";

  function fmtClock(ms) {
    var s = Math.floor(ms / 1000);
    var m = Math.floor(s / 60);
    var ss = s % 60;
    return m + ":" + (ss < 10 ? "0" : "") + ss;
  }
  function fmtDuration(ms) {
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    var m = Math.floor(s / 60);
    var ss = Math.round(s % 60);
    return m + ":" + (ss < 10 ? "0" : "") + ss;
  }
  function startTimer() {
    runStart = performance.now();
    if (timerEl) timerEl.textContent = "0:00";
    timerId = setInterval(function () {
      if (timerEl) timerEl.textContent = fmtClock(performance.now() - runStart);
    }, 250);
  }
  function stopTimer() {
    if (timerId) { clearInterval(timerId); timerId = null; }
  }

  function setRunBtn(running) {
    runBtn.disabled = running;
    runBtn.classList.toggle("running", running);
    runBtn.innerHTML = running
      ? '<span class="spin"></span> Running…'
      : 'Run <span class="g">&#9656;</span>';
  }
  function setStop(on) {
    if (!stopBtn) return;
    stopBtn.hidden = !on;
    stopBtn.disabled = !on;
  }

  function setRunhead(state, label) {
    runhead.className = "runhead " + state;
    if (rhLabel) rhLabel.textContent = label;
    if (!pulseEl) return;
    if (state === "stream") {
      pulseEl.className = "pulse";
      pulseEl.style.cssText = "";
    } else {
      pulseEl.className = "dot" + (state === "error" ? " red" : "");
      pulseEl.style.cssText = state === "stopped"
        ? "background:var(--amber);box-shadow:0 0 0 3px var(--amber-weak)"
        : "";
    }
  }

  function buildRunChips(meta) {
    if (!rhChips) return;
    rhChips.innerHTML = "";
    rhChips.appendChild(chipEl(cap(meta.mode), "grn"));
    rhChips.appendChild(chipEl(meta.language, "mono"));
    if (meta.tier) rhChips.appendChild(chipEl(cap(meta.tier), ""));
  }

  function hideTerminals() {
    summaryEl.hidden = true;
    errbox.hidden = true;
    stopmark.hidden = true;
  }

  function enterIdle() {
    isStreaming = false;
    stopTimer();
    consoleIdle.hidden = false;
    runhead.hidden = true;
    outwrap.hidden = true;
    streamFoot.hidden = true;
    hideTerminals();
    if (sessionState) sessionState.textContent = "idle";
    setRunBtn(false);
    setStop(false);
  }

  function enterStreaming(meta) {
    isStreaming = true;
    consoleIdle.hidden = true;
    hideTerminals();
    setRunhead("stream", "Running");
    buildRunChips(meta);
    runhead.hidden = false;
    outwrap.hidden = false;
    streamFoot.hidden = false;
    if (sessionState) sessionState.textContent = "streaming";
    setRunBtn(true);
    setStop(true);
    render(""); // clears #output through the single assignment site
    startTimer();
  }

  function enterDone(payload, meta) {
    isStreaming = false;
    stopTimer();
    lastDuration = fmtDuration(performance.now() - runStart);
    setRunhead("done", "Finished");
    streamFoot.hidden = true;
    setStop(false);
    if (sessionState) sessionState.textContent = "done";
    setRunBtn(false);
    buildSummary(payload || {}, meta);
    summaryEl.hidden = false;
    if (outwrap.hidden && acc) outwrap.hidden = false;
    loadLibrary(); // refresh recents / table / topics / tree with the new files
  }

  function enterError(msg) {
    isStreaming = false;
    stopTimer();
    setRunhead("error", "Failed");
    streamFoot.hidden = true;
    setStop(false);
    if (sessionState) sessionState.textContent = "error";
    setRunBtn(false);
    buildErrbox(msg);
    errbox.hidden = false;
    outwrap.hidden = !acc; // keep the partial output; hide an empty shell
  }

  function enterStopped() {
    isStreaming = false;
    stopTimer();
    setRunhead("stopped", "Stopped");
    streamFoot.hidden = true;
    setStop(false);
    if (sessionState) sessionState.textContent = "stopped";
    setRunBtn(false);
    buildStopmark();
    stopmark.hidden = false;
    outwrap.hidden = !acc; // keep the partial output
  }

  // ---- terminal-state card builders --------------------------------------
  function savedRow(path) {
    var row = el("div", "savedrow");
    row.appendChild(typeBadge(extOf(path)));
    var fp = el("div", "fpath");
    var mt = midTrunc(path, 8);
    fp.appendChild(el("span", "a", mt.a));
    if (mt.b) fp.appendChild(el("span", "b", mt.b));
    row.appendChild(fp);
    var btn = el("button", "mini");
    btn.type = "button";
    btn.innerHTML = COPY_SVG + '<span class="lbl">Copy path</span>';
    btn.addEventListener("click", function () { copyText(path, btn); });
    row.appendChild(btn);
    return row;
  }

  function buildSummary(payload, meta) {
    summaryEl.innerHTML = "";
    var paths = payload.paths || [];
    var topics = payload.topics || [];

    var top = el("div", "sum-top");
    var check = el("div", "sum-check");
    check.innerHTML = CHECK_SVG;
    top.appendChild(check);
    var mid = document.createElement("div");
    mid.appendChild(el("div", "t", "Saved to your study library"));
    var subParts = [];
    var title = firstLine(problemEl.value);
    if (title) subParts.push(title);
    subParts.push(cap(meta.mode));
    subParts.push(langLabel(meta.language));
    if (meta.tier) subParts.push(cap(meta.tier));
    var sub = subParts.join(" · ");
    if (lastDuration) sub += " — completed in " + lastDuration;
    mid.appendChild(el("div", "s", sub));
    top.appendChild(mid);
    top.appendChild(el("span", "spacer"));
    top.appendChild(chipEl(paths.length + (paths.length === 1 ? " file" : " files"), "mint"));
    summaryEl.appendChild(top);

    var body = el("div", "sum-body");
    if (payload.problem_type) {
      var r1 = el("div", "meta-row");
      r1.appendChild(el("span", "meta-label", "Type"));
      r1.appendChild(chipEl(payload.problem_type, ""));
      body.appendChild(r1);
    }
    if (topics.length) {
      var r2 = el("div", "meta-row");
      r2.appendChild(el("span", "meta-label", "Topics"));
      topics.forEach(function (t) { r2.appendChild(chipEl(t, "grn")); });
      body.appendChild(r2);
    }
    if (payload.verification) {
      var r3 = el("div", "meta-row");
      r3.appendChild(el("span", "meta-label", "Verify"));
      var pass = /^\s*✓/.test(payload.verification); // "✓ ..."
      r3.appendChild(chipEl(payload.verification, pass ? "mint" : ""));
      body.appendChild(r3);
    }
    if (paths.length) {
      var files = el("div", "files");
      paths.forEach(function (p) { files.appendChild(savedRow(p)); });
      body.appendChild(files);
    }
    summaryEl.appendChild(body);
  }

  function buildErrbox(msg) {
    errbox.innerHTML = "";
    var ic = el("div", "ic");
    ic.innerHTML = ERR_SVG;
    errbox.appendChild(ic);
    var d = document.createElement("div");
    d.appendChild(el("div", "t", "Run failed"));
    d.appendChild(el("div", "d", msg || "The run did not complete."));
    errbox.appendChild(d);
  }

  function buildStopmark() {
    stopmark.innerHTML = "";
    stopmark.appendChild(el("span", "sq"));
    stopmark.appendChild(el("span", null, "Stopped"));
    stopmark.appendChild(el("span", "s", "Partial output kept below."));
  }

  // ---- Stop button --------------------------------------------------------
  if (stopBtn) {
    stopBtn.addEventListener("click", function () { if (activeStop) activeStop(); });
  }

  // ---- SSE parser (KEEP EXACTLY) -----------------------------------------
  // Emits {type:"text", data} for `data:` lines and
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

  // ---- the run ------------------------------------------------------------
  async function runNow() {
    if (isStreaming) return; // guard double-run

    var problem = problemEl.value.trim();
    if (!problem) {
      flashEditor();
      problemEl.focus();
      return;
    }

    var mode = activeVal("mode");
    var language = activeVal("lang");
    var tierDisabled = mode === "learning";
    var tier = tierDisabled ? "" : activeVal("tier");
    // Wire contract — unchanged: { problem, mode, language, tier }.
    var body = { problem: problem, mode: mode, language: language, tier: tier };
    var meta = { mode: mode, language: language, tier: tier };

    acc = "";
    enterStreaming(meta);

    // Abort the in-flight /run if the user navigates away, so the server sees
    // the drop and cancels the Claude subprocess (don't burn subscription use).
    var controller = new AbortController();
    function abortOnUnload() { controller.abort(); }
    window.addEventListener("pagehide", abortOnUnload);
    window.addEventListener("beforeunload", abortOnUnload);

    var stoppedByUser = false;
    activeStop = function () { stoppedByUser = true; controller.abort(); };

    var terminal = false; // a terminal SSE event (done/error) already set state

    try {
      var resp = await fetch("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!resp.ok) {
        var err = await resp.json().catch(function () { return {}; });
        enterError(err.error || "Request rejected (" + resp.status + ").");
        terminal = true;
        return;
      }

      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var feed = makeSseParser(function (ev) {
        if (ev.type === "text") {
          acc += ev.data;
          scheduleRender(acc);
        } else if (ev.name === "done") {
          enterDone(ev.data || {}, meta);
          terminal = true;
        } else if (ev.name === "error") {
          enterError(typeof ev.data === "string" ? ev.data : "Run failed.");
          terminal = true;
        }
      });

      while (true) {
        var r = await reader.read();
        if (r.done) break;
        feed(decoder.decode(r.value, { stream: true }));
      }
    } catch (e) {
      // AbortError = intentional cancel (Stop button or page unload).
      if (e && e.name === "AbortError") {
        if (stoppedByUser) { enterStopped(); terminal = true; }
      } else {
        enterError("Network error: " + (e && e.message));
        terminal = true;
      }
    } finally {
      window.removeEventListener("pagehide", abortOnUnload);
      window.removeEventListener("beforeunload", abortOnUnload);
      activeStop = null;
      isStreaming = false;
      // Final full render outside the rAF path so the last chunk is never lost
      // and the last code block loses its `.partial` marker.
      render(acc);
      // Stream closed without a terminal event — settle to a sensible state
      // instead of a stuck spinner. A user Stop can end the stream *gracefully*
      // (reader resolves done rather than rejecting AbortError), so the catch
      // above never fires; check stoppedByUser here so Stop still reads as
      // "stopped", not a spurious "done".
      if (!terminal) {
        if (stoppedByUser) enterStopped();
        else if (acc) enterDone({ paths: [], topics: [] }, meta);
        else enterIdle();
      }
      setRunBtn(false);
      setStop(false);
    }
  }
  runBtn.addEventListener("click", runNow);

  // ⌘/Ctrl + Enter anywhere runs (guarded against double-run).
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === "Enter" || e.keyCode === 13)) {
      e.preventDefault();
      if (!isStreaming) runNow();
    }
  });

  // =========================================================================
  // Quick Ask — one-shot Haiku Q&A, fully independent of the run stream.
  // Answers are ephemeral (each replaces the last); errors render as PLAIN
  // TEXT via textContent, success through the same hardened marked pipeline
  // as render().
  // =========================================================================
  var qaInput = $("qa-input");
  var qaAskBtn = $("qa-ask");
  var qaAnswer = $("qa-answer");
  var qaToggle = $("qa-toggle");
  var qaBody = $("qa-body");
  var qaPanel = $("quickask");
  var qaBusy = false;

  // Same one-time delegated Copy listener the other markdown surfaces get.
  if (qaAnswer) qaAnswer.addEventListener("click", handleCopyCode);

  function showQaAnswer(md) {
    qaAnswer.classList.remove("err");
    if (window.marked) {
      qaAnswer.innerHTML = marked.parse(md); // hardened renderer (see marked.use above)
      if (window.hljs) {
        qaAnswer.querySelectorAll("pre code").forEach(function (b) {
          try { hljs.highlightElement(b); } catch (e) { /* noop */ }
        });
      }
      decorateCode(qaAnswer, false);
    } else {
      qaAnswer.textContent = md;
    }
    qaAnswer.hidden = false;
  }

  function showQaError(msg) {
    qaAnswer.classList.add("err");
    qaAnswer.textContent = msg; // server/network strings NEVER hit innerHTML
    qaAnswer.hidden = false;
  }

  async function quickAsk() {
    if (qaBusy) return; // one quick-ask in flight at a time
    var question = qaInput.value.trim();
    if (!question) return;

    qaBusy = true;
    qaAskBtn.disabled = true;
    qaAskBtn.textContent = "Asking…";

    // Own controller — completely independent of the main run's abort state.
    var controller = new AbortController();
    function abortOnUnload() { controller.abort(); }
    window.addEventListener("pagehide", abortOnUnload);
    window.addEventListener("beforeunload", abortOnUnload);

    try {
      var resp = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: question,
          language: activeVal("lang"),
          problem: problemEl.value.trim(),
        }),
        signal: controller.signal,
      });
      var data = await resp.json().catch(function () { return {}; });
      if (resp.ok) {
        showQaAnswer(String(data.answer || ""));
      } else {
        showQaError(data.error || "Request rejected (" + resp.status + ").");
      }
    } catch (e) {
      if (!(e && e.name === "AbortError")) {
        showQaError("Network error: " + (e && e.message));
      }
    } finally {
      window.removeEventListener("pagehide", abortOnUnload);
      window.removeEventListener("beforeunload", abortOnUnload);
      qaBusy = false;
      qaAskBtn.disabled = false;
      qaAskBtn.textContent = "Ask";
    }
  }

  if (qaAskBtn) qaAskBtn.addEventListener("click", quickAsk);
  if (qaInput) {
    // Plain Enter asks; modified Enter falls through to the global ⌘/Ctrl+Enter
    // run shortcut untouched.
    qaInput.addEventListener("keydown", function (e) {
      if ((e.key === "Enter" || e.keyCode === 13) && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
        e.preventDefault();
        quickAsk();
      }
    });
  }
  if (qaToggle && qaBody) {
    qaToggle.addEventListener("click", function () {
      var collapsed = !qaBody.hidden;
      qaBody.hidden = collapsed;
      if (qaPanel) qaPanel.classList.toggle("collapsed", collapsed);
      qaToggle.textContent = collapsed ? "Show" : "Hide";
      qaToggle.setAttribute("aria-expanded", String(!collapsed));
    });
  }

  // =========================================================================
  // Derived real data from /library ( recents · table · topics · tree ).
  // =========================================================================
  var libFiles = [];
  var libByPath = {};
  var currentRuns = [];

  // Group files into runs by (mode-folder, topic, stem-before "__").
  function deriveRuns(files) {
    var map = {};
    var order = [];
    files.forEach(function (f) {
      var parts = f.path.split("/");
      if (parts.length < 3) return; // only <mode>/<topic>/<file> entries are runs
      var modeFolder = parts[0];
      var topicSeg = parts[1];
      var fname = parts[parts.length - 1];
      var dot = fname.lastIndexOf(".");
      var ext = dot === -1 ? "" : fname.slice(dot + 1).toLowerCase();
      var base = dot === -1 ? fname : fname.slice(0, dot);
      var us = base.indexOf("__");
      var stem = us === -1 ? base : base.slice(0, us);
      var tier = us === -1 ? "" : base.slice(us + 2);
      // learning writes into "<type>_learning" — drop the mode artifact so a
      // learning run's topic groups with the same topic in other modes.
      var topicRaw = topicSeg.replace(/_learning$/, "");
      var key = modeFolder + "|" + topicRaw + "|" + stem;
      var run = map[key];
      if (!run) {
        run = {
          modeFolder: modeFolder, topicRaw: topicRaw, stemRaw: stem,
          tier: "", langExt: "", savedAt: 0, mdPath: "", files: [],
        };
        map[key] = run;
        order.push(run);
      }
      run.files.push(f);
      if (typeof f.mtime === "number" && f.mtime > run.savedAt) run.savedAt = f.mtime;
      if (ext === "md") { if (!run.mdPath) run.mdPath = f.path; }
      else if (CODE_EXT[ext] && !run.langExt) run.langExt = ext;
      if (tier && !run.tier) run.tier = tier;
    });
    return order.map(function (run) {
      return {
        modeFolder: run.modeFolder,
        mode: modeLabel(run.modeFolder),
        topicRaw: run.topicRaw,
        topic: humanize(run.topicRaw),
        stemRaw: run.stemRaw,
        problem: humanize(run.stemRaw),
        tier: run.tier,
        language: langLabel(run.langExt),
        langExt: run.langExt,
        savedAt: run.savedAt,
        mdPath: run.mdPath,
        files: run.files,
      };
    });
  }

  function openRun(run) {
    var path = run.mdPath || (run.files[0] && run.files[0].path);
    if (!path) return;
    switchView("library");
    openFile(path);
  }

  function renderRecents(runs) {
    if (!recentsEl) return;
    recentsEl.innerHTML = "";
    var top = runs.slice().sort(bySavedDesc).slice(0, 7);
    if (!top.length) {
      recentsEl.appendChild(el("div", "rm", "No saved runs yet."));
      return;
    }
    top.forEach(function (run) {
      var rec = el("div", "rec");
      var dd = el("span", "dd");
      dd.style.background = "var(--tx4)"; // neutral — the app has no difficulty signal
      rec.appendChild(dd);
      var rt = el("div", "rt");
      var rn = el("div", "rn");
      var mt = midTrunc(run.problem, 6);
      rn.appendChild(el("span", "a", mt.a));
      if (mt.b) rn.appendChild(el("span", "b", mt.b));
      rt.appendChild(rn);
      var sub = run.mode.toLowerCase() + " · " + (run.langExt || "—") + " · " + relTime(run.savedAt);
      rt.appendChild(el("div", "rm", sub));
      rec.appendChild(rt);
      rec.addEventListener("click", function () { openRun(run); });
      recentsEl.appendChild(rec);
    });
  }

  function setRecentCount(n) {
    if (recentCount) recentCount.textContent = n + " saved · output/";
  }

  function renderRecentTable(runs) {
    if (!recentTable) return;
    recentTable.querySelectorAll(".trow").forEach(function (r) { r.remove(); });
    var sorted = runs.slice().sort(bySavedDesc);
    setRecentCount(sorted.length);
    if (!sorted.length) {
      var empty = el("div", "trow");
      empty.style.cursor = "default";
      empty.appendChild(el("span", "diff", "—"));
      var pc0 = el("div", "pcell");
      pc0.appendChild(el("div", "pn", "No runs saved yet"));
      pc0.appendChild(el("div", "pm", "Run a problem to populate output/"));
      empty.appendChild(pc0);
      recentTable.appendChild(empty);
      return;
    }
    sorted.forEach(function (run) {
      var row = el("div", "trow");
      row.setAttribute("data-mode", run.mode.toLowerCase());
      row.appendChild(el("span", "diff", "—")); // neutral difficulty column
      var pc = el("div", "pcell");
      pc.appendChild(el("div", "pn", run.problem));
      pc.appendChild(el("div", "pm", run.stemRaw + (run.tier ? "__" + run.tier : "")));
      row.appendChild(pc);
      row.appendChild(run.topic ? chipEl(run.topic, "mint") : el("span", "tcell", "—"));
      row.appendChild(chipEl(run.mode, ""));
      row.appendChild(run.langExt ? typeBadge(run.langExt) : el("span", "tcell", "—"));
      row.appendChild(el("span", "tcell", relTime(run.savedAt)));
      var st = el("span", "tstatus");
      st.innerHTML = CHECK_TABLE_SVG;
      row.appendChild(st);
      row.addEventListener("click", function () { openRun(run); });
      recentTable.appendChild(row);
    });
    applyRecentFilter();
  }

  function currentRecentFilter() {
    var on = document.querySelector("[data-recent-filter].on");
    return on ? on.getAttribute("data-recent-filter") : "all";
  }
  function applyRecentFilter() {
    if (!recentTable) return;
    var val = currentRecentFilter();
    recentTable.querySelectorAll(".trow").forEach(function (row) {
      var m = row.getAttribute("data-mode");
      if (!m) return; // empty-state row
      row.hidden = !(val === "all" || m === val);
    });
  }

  function renderTopics(runs) {
    if (!topicsEl) return;
    topicsEl.innerHTML = "";
    var sets = {};
    runs.forEach(function (run) {
      if (!run.topic) return;
      (sets[run.topic] = sets[run.topic] || {})[run.stemRaw] = true;
    });
    var list = Object.keys(sets).map(function (t) {
      return { topic: t, count: Object.keys(sets[t]).length };
    });
    list.sort(function (a, b) { return b.count - a.count || a.topic.localeCompare(b.topic); });
    if (!list.length) {
      topicsEl.appendChild(el("span", "topic", "No topics yet"));
      return;
    }
    list.slice(0, 10).forEach(function (item) {
      var chip = el("span", "topic");
      chip.appendChild(document.createTextNode(item.topic + " "));
      chip.appendChild(el("span", "c", String(item.count)));
      topicsEl.appendChild(chip);
    });
  }

  // =========================================================================
  // Library two-pane.
  // =========================================================================
  var vwTitle, vwSub;
  (function buildViewerHead() {
    if (!libViewer || !libViewerPath) return;
    var head = libViewer.querySelector(".vw-head");
    if (!head) return;
    var main = document.createElement("div");
    main.className = "vw-main"; // styled in style.css (.vw-main / .vw-title / .vw-sub)
    vwTitle = el("div", "vw-title");
    vwSub = el("div", "vw-sub");
    head.insertBefore(main, libViewerPath); // keep close button as the trailing sibling
    main.appendChild(vwTitle);
    main.appendChild(vwSub);
    main.appendChild(libViewerPath); // relocate the raw path under the humanized head
  })();

  function fileMeta(path) {
    var parts = path.split("/");
    var fname = parts[parts.length - 1];
    var dot = fname.lastIndexOf(".");
    var ext = dot === -1 ? "" : fname.slice(dot + 1).toLowerCase();
    var base = dot === -1 ? fname : fname.slice(0, dot);
    var us = base.indexOf("__");
    var stem = us === -1 ? base : base.slice(0, us);
    var tier = us === -1 ? "" : base.slice(us + 2);
    var modeFolder = parts.length >= 3 ? parts[0] : "";
    var topicRaw = parts.length >= 3 ? parts[1].replace(/_learning$/, "") : "";
    var f = libByPath[path];
    return {
      title: humanize(stem) || fname,
      ext: ext,
      tier: tier,
      topic: humanize(topicRaw),
      mode: modeFolder ? modeLabel(modeFolder) : "",
      mtime: f && f.mtime,
    };
  }

  function showFile(relPath, text) {
    var meta = fileMeta(relPath);
    if (vwTitle) vwTitle.textContent = meta.title;
    if (vwSub) {
      vwSub.innerHTML = "";
      if (meta.topic) vwSub.appendChild(chipEl(meta.topic, "mint"));
      if (meta.mode) vwSub.appendChild(chipEl(meta.mode, ""));
      if (meta.ext === "md") {
        if (meta.tier) vwSub.appendChild(chipEl(cap(meta.tier), ""));
      } else {
        var ll = langLabel(meta.ext);
        if (ll !== "—") vwSub.appendChild(chipEl(ll, ""));
      }
      if (meta.mtime) vwSub.appendChild(chipEl("saved " + savedDate(meta.mtime), "mono"));
    }
    if (libViewerPath) libViewerPath.textContent = relPath;

    libViewerBody.innerHTML = "";
    if (meta.ext === "md" && window.marked) {
      libViewerBody.innerHTML = marked.parse(text); // same hardened pipeline
    } else {
      var pre = document.createElement("pre");
      var code = document.createElement("code");
      if (HLJS_LANG[meta.ext]) code.className = "language-" + HLJS_LANG[meta.ext];
      code.textContent = text; // escaped by construction
      pre.appendChild(code);
      libViewerBody.appendChild(pre);
    }
    if (window.hljs) {
      libViewerBody.querySelectorAll("pre code").forEach(function (b) {
        try { hljs.highlightElement(b); } catch (e) { /* noop */ }
      });
    }
    decorateCode(libViewerBody, false);
    libViewer.hidden = false;
  }

  function markTreeActive(relPath) {
    var prev = libTree.querySelector(".filerow.on");
    if (prev) prev.classList.remove("on");
    var rows = libTree.querySelectorAll(".filerow");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-path") === relPath) { rows[i].classList.add("on"); break; }
    }
  }

  function openFile(relPath) {
    fetch("/library/file?path=" + encodeURIComponent(relPath))
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (text) {
        showFile(relPath, text);
        markTreeActive(relPath);
      })
      .catch(function (e) {
        console.warn("Could not open " + relPath + " (" + (e && e.message) + ").");
      });
  }

  function renderTree(files) {
    libTree.innerHTML = "";
    if (!files.length) {
      libTree.appendChild(el("div", "grp-h", "Nothing saved yet — run a problem to build your library."));
      return;
    }
    var groups = [];
    var byFolder = {};
    files.forEach(function (f) {
      var idx = f.path.lastIndexOf("/");
      var folder = idx === -1 ? "" : f.path.slice(0, idx);
      if (!(folder in byFolder)) { byFolder[folder] = []; groups.push(folder); }
      byFolder[folder].push(f);
    });
    groups.forEach(function (folder) {
      var grp = el("div", "grp");
      var h = el("div", "grp-h");
      h.innerHTML = FOLDER_SVG;
      h.appendChild(document.createTextNode(" " + (folder || "(library root)")));
      grp.appendChild(h);
      byFolder[folder].forEach(function (f) {
        var name = f.path.slice(f.path.lastIndexOf("/") + 1);
        var ext = extOf(name);
        var btn = el("button", "filerow");
        btn.type = "button";
        btn.setAttribute("data-path", f.path);
        btn.setAttribute("data-ext", ext);
        btn.title = f.path;
        btn.appendChild(typeBadge(ext));
        var fn = el("span", "fname");
        var mt = midTrunc(name, 8);
        fn.appendChild(el("span", "a", mt.a));
        if (mt.b) fn.appendChild(el("span", "b", mt.b));
        btn.appendChild(fn);
        btn.addEventListener("click", function () { openFile(f.path); });
        grp.appendChild(btn);
      });
      libTree.appendChild(grp);
    });
    applyLibFilter();
  }

  function currentLibFilter() {
    var on = document.querySelector("[data-lib-filter].on");
    return on ? on.getAttribute("data-lib-filter") : "all";
  }
  function applyLibFilter() {
    if (!libTree) return;
    var val = currentLibFilter();
    libTree.querySelectorAll(".grp").forEach(function (grp) {
      var any = false;
      grp.querySelectorAll(".filerow").forEach(function (row) {
        var ext = row.getAttribute("data-ext");
        var show = val === "all" || (val === "md" && ext === "md") || (val === "code" && ext !== "md");
        row.hidden = !show;
        if (show) any = true;
      });
      grp.hidden = !any;
    });
  }

  function loadLibrary() {
    fetch("/library")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        libFiles = (data && data.files) || [];
        libByPath = {};
        libFiles.forEach(function (f) { libByPath[f.path] = f; });
        currentRuns = deriveRuns(libFiles);
        renderRecents(currentRuns);
        renderRecentTable(currentRuns);
        renderTopics(currentRuns);
        renderTree(libFiles);
      })
      .catch(function (e) {
        libFiles = [];
        libByPath = {};
        currentRuns = [];
        renderRecents([]);
        renderRecentTable([]);
        renderTopics([]);
        if (libTree) {
          libTree.innerHTML = "";
          libTree.appendChild(el("div", "grp-h", "Could not load the library (" + (e && e.message) + ")."));
        }
      });
  }

  if (libViewerClose) {
    libViewerClose.addEventListener("click", function () {
      libViewer.hidden = true;
      libViewerBody.innerHTML = "";
      var on = libTree.querySelector(".filerow.on");
      if (on) on.classList.remove("on");
    });
  }

  // ---- boot ---------------------------------------------------------------
  enterIdle();
  loadLibrary();
})();
