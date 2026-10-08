/* LeetCoach frontend (application-shell UI).
 *
 * POSTs the run form to /run and consumes the SSE response with fetch +
 * ReadableStream (POST body is cleanest this way — no EventSource GET dance).
 * Text deltas accumulate and the markdown is re-rendered (throttled) with the
 * vendored `marked`; closed code blocks are syntax-highlighted with the
 * vendored `highlight.js` and wrapped in chrome. A terminal `done` event shows
 * the saved-summary card; `error`/abort surface a failure while KEEPING the
 * partial output. No runtime CDN dependency. Pure helpers live in
 * static/lib/core.js (window.LeetCoachCore) so node can unit-test them.
 *
 * SSE invariants (do NOT "improve"):
 *  - #output is the SINGLE node whose innerHTML is replaced per render, and
 *    its innerHTML is assigned ONLY inside render(). Renders are throttled to
 *    RENDER_INTERVAL_MS (B16) and deferred while the reader has text selected.
 *  - Streaming chrome (state pill / chips / live timer / Stop / caret / phase
 *    note) lives on SIBLING nodes around #output and is written with direct
 *    textContent/class writes — render() never touches it.
 *  - Code-block chrome + hljs are post-render passes over #output, then the
 *    SP6 click-to-reveal pass (core.applyReveals: hint sections and a Guided
 *    run's Solution move into <details>; nodes are moved, never re-parsed).
 *    Copy is handled by ONE delegated click listener per container, added once.
 *  - /run request body is { problem, mode, language, tier, model?, run_id };
 *    Stop POSTs /run/cancel { run_id }. Events: text deltas, `phase`, `meta`
 *    ({model}), terminal `done` / `error` / `cancelled` (a Stop the server
 *    honoured -> the neutral Stopped state); unknown names are ignored.
 *  - B13: every async callback of a run checks it is still `currentRun`
 *    before touching the UI, so a finishing old run can never clobber a new
 *    run's Stop button, flags or output.
 *  - Model/user text reaches the DOM only via el()/textContent (or the
 *    hardened marked renderer); innerHTML takes trusted literals only.
 */
(function () {
  "use strict";

  var core = window.LeetCoachCore;
  var $ = function (id) { return document.getElementById(id); };

  // ---- static SVG snippets (trusted literals, no user data) ---------------
  var COPY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>';
  var CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M5 13l4 4L19 7"/></svg>';
  var FOLDER_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2h9A1.5 1.5 0 0 1 21 8.5V18a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18Z"/></svg>';
  var ERR_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"><path d="M12 4 3 19h18Z"/><path d="M12 10v4"/><path d="M12 17h.01"/></svg>';

  var SAMPLE =
    "Two Sum\n\nGiven an array of integers nums and an integer target, return " +
    "indices of the two numbers such that they add up to target.\n\nYou may " +
    "assume that each input would have exactly one valid answer, and you may " +
    "not use the same element twice.\n\nExample:\nInput: nums = [2,7,11,15], " +
    "target = 9\nOutput: [0,1]\n\nConstraints:\n2 <= nums.length <= 10^4";

  var HLJS_LANG = { py: "python", cpp: "cpp", java: "java", json: "json" };
  var RENDER_INTERVAL_MS = 150;   // B16 stream render throttle
  var QA_TIMEOUT_MS = 60000;      // B20 Quick Ask client timeout
  var CANCEL_TIMEOUT_MS = 3000;   // SP4: an unresponsive server can't hang Stop

  // ---- core element handles ----------------------------------------------
  var appEl = $("app");
  var contentEl = $("content");
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
  var jumpPill = $("jump-latest");
  var runStatus = $("run-status");
  var toastEl = $("toast");

  var views = { console: $("view-console"), library: $("view-library"), stats: $("view-stats"),
    practice: $("view-practice") };

  // session lifecycle containers (all live inside #session as siblings)
  var sessionState = document.querySelector("[data-session-state]");
  var consoleIdle = document.querySelector(".console-idle");
  var runhead = document.querySelector(".runhead");
  var outwrap = document.querySelector(".outwrap");
  var streamFoot = document.querySelector(".stream-foot");
  var streamNote = streamFoot ? streamFoot.querySelector(".stream-note") : null;
  var summaryEl = document.querySelector(".summary");
  var errbox = document.querySelector(".errbox");
  var stopmark = document.querySelector(".stopmark");

  // run-header children (cached once; runhead is never re-created)
  var pulseEl = runhead.querySelector(".pulse");
  var rhLabel = runhead.querySelector(".rh-label");
  var rhChips = runhead.querySelector(".rh-chips");
  var timerEl = runhead.querySelector(".timer");
  var stopLbl = stopBtn ? stopBtn.querySelector(".stop-lbl") : null;

  // derived-data surfaces
  var topicsEl = $("topics");
  var recentsEl = $("recents");
  var recentTable = $("recent-table");
  var recentCount = document.querySelector("[data-recent-count]");

  // library two-pane
  var libTree = $("library-tree");
  var libViewer = $("library-viewer");
  var libViewerPath = $("library-viewer-path");
  var libViewerBody = $("library-viewer-body");
  var libViewerClose = $("library-viewer-close");
  var libViewerDelete = $("library-viewer-delete");
  var currentViewPath = null; // the library file currently open in the viewer

  // ---- marked XSS hardening (policy lives in core.hardenedRenderer) -------
  // Defense-in-depth: Claude's output is untrusted markdown rendered via
  // innerHTML. Raw HTML is escaped, links must be http(s), images must be
  // inline data:image/ URIs (no network fetch — offline promise).
  if (window.marked && typeof marked.use === "function") {
    marked.use({ renderer: core.hardenedRenderer() });
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
  var cap = core.cap;
  var humanize = core.humanize;
  var extOf = core.extOf;
  var midTrunc = core.midTrunc;
  var modeLabel = core.modeLabel;
  var langLabel = core.langLabel;
  function relTime(mtime) { return core.relTime(mtime); }
  function savedDate(mtime) {
    try {
      return new Date(mtime * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch (e) { return ""; }
  }
  function bySavedDesc(a, b) { return (b.savedAt || 0) - (a.savedAt || 0); }
  function typeBadge(ext) {
    return el("span", "tb " + (ext || ""), ext || "?");
  }
  function sleep(ms, signal) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(resolve, ms);
      if (signal) {
        signal.addEventListener("abort", function () {
          clearTimeout(t);
          var err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        }, { once: true });
      }
    });
  }
  function postJson(url, body, extra) {
    var opts = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
    if (extra) Object.keys(extra).forEach(function (k) { opts[k] = extra[k]; });
    return fetch(url, opts);
  }

  // ---- preferences (B15): localStorage, every access guarded --------------
  var prefs = core.makePrefs(function () { return window.localStorage; });

  // ---- platform glyphs (C10): ⌘ on Apple, Ctrl elsewhere ------------------
  var MOD = core.modKey(
    (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || ""
  );
  document.querySelectorAll("kbd[data-mod]").forEach(function (k) { k.textContent = MOD; });
  (function () {
    var sb = $("tb-search");
    if (sb) sb.title = "Search your library (" + MOD + " + K)";
  })();

  // ---- status: screen-reader live region + visible toasts -----------------
  function announce(msg) {
    if (!runStatus) return;
    runStatus.textContent = "";
    // A fresh text node after a tick so a repeated message is re-announced.
    setTimeout(function () { runStatus.textContent = msg; }, 30);
  }
  var toastTimer = null;
  function notify(msg, kind) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.className = "toast" + (kind ? " " + kind : "");
    toastEl.setAttribute("role", kind === "error" ? "alert" : "status");
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, kind === "error" ? 7000 : 4000);
  }
  if (toastEl) toastEl.addEventListener("click", function () { toastEl.hidden = true; });

  function copyText(text, btn) {
    var lbl = btn.querySelector(".lbl");
    if (lbl && !btn.getAttribute("data-label")) btn.setAttribute("data-label", lbl.textContent);
    function ok() {
      btn.classList.add("ok");
      if (lbl) lbl.textContent = "Copied";
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(function () {
        btn.classList.remove("ok");
        if (lbl) lbl.textContent = btn.getAttribute("data-label") || "Copy";
      }, 1400);
    }
    function fail() { notify("Could not copy to the clipboard.", "error"); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(String(text)).then(ok, fail);
    } else {
      fail();
    }
  }

  function flashEditor() {
    if (!editorEl) return;
    editorEl.classList.remove("flash");
    void editorEl.offsetWidth; // reflow so the animation restarts
    editorEl.classList.add("flash");
  }

  // ---- CLI availability + sign-in pill -----------------------------------
  (function claudeStatus() {
    var installed = document.body.dataset.claudeAvailable === "true";
    var loggedIn = document.body.dataset.claudeLoggedIn === "true";
    var ready = installed && loggedIn;
    var pill = $("claude-status");
    var dot = pill ? pill.querySelector(".dot") : null;
    var label = pill ? pill.querySelector(".tb-status-label") : null;
    if (dot) dot.classList.toggle("red", !ready);
    if (label) {
      label.textContent = ready
        ? "claude CLI ready"
        : installed ? "claude CLI signed out" : "claude CLI not found";
    }
    if (claudeWarning) {
      var missing = claudeWarning.querySelector('[data-banner="missing"]');
      var signedOut = claudeWarning.querySelector('[data-banner="signedout"]');
      if (missing) missing.hidden = installed;
      if (signedOut) signedOut.hidden = !(installed && !loggedIn);
      claudeWarning.hidden = ready;
    }
  })();

  // ---- segmented controls (aria-pressed, C9) ------------------------------
  function segButtons(group) {
    return document.querySelectorAll('.seg[data-seg="' + group + '"] .seg-btn');
  }
  function activeVal(group) {
    var on = document.querySelector('.seg[data-seg="' + group + '"] .seg-btn.on');
    return on ? on.getAttribute("data-val") : "";
  }
  // Select `val` in `group`; returns false (nothing changed) for an unknown value.
  function setSeg(group, val) {
    var btns = segButtons(group);
    var found = false;
    btns.forEach(function (b) { if (b.getAttribute("data-val") === val) found = true; });
    if (!found) return false;
    btns.forEach(function (b) {
      var on = b.getAttribute("data-val") === val;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    return true;
  }
  function clearSeg(group) {
    segButtons(group).forEach(function (b) {
      b.classList.remove("on");
      b.setAttribute("aria-pressed", "false");
    });
  }
  function syncTier() {
    var mode = activeVal("mode");
    var untiered = core.untiered(mode); // Learning / Code Review have no tier
    var tg = document.querySelector('.tgroup[data-seg="tier"]');
    if (tg) {
      tg.classList.toggle("disabled", untiered);
      tg.setAttribute("aria-disabled", untiered ? "true" : "false");
    }
    segButtons("tier").forEach(function (b) { b.disabled = untiered; });
    // SP7 / D5: Code Review takes the learner's attempt next to the problem.
    var rc = $("review-code-wrap");
    if (rc) rc.hidden = mode !== "review";
  }

  // B12: persist the picker as the default. On failure the picker reverts to
  // what the server still has, and the user is told (C10 visible failures).
  function saveModel(alias, previous) {
    function revert(reason) {
      if (previous) setSeg("model", previous); else clearSeg("model");
      notify("Could not save the model setting" + (reason ? " (" + reason + ")" : "") +
        ". It is unchanged.", "error");
    }
    postJson("/config/model", { model: alias }).then(function (resp) {
      if (resp.ok) return;
      return resp.json().catch(function () { return {}; }).then(function (d) {
        revert((d && d.error) || "HTTP " + resp.status);
      });
    }, function () { revert("network error"); });
  }
  document.querySelectorAll(".seg").forEach(function (seg) {
    seg.addEventListener("click", function (e) {
      var b = e.target.closest(".seg-btn");
      if (!b || !seg.contains(b) || b.disabled) return;
      var group = seg.getAttribute("data-seg");
      var previous = activeVal(group);
      var val = b.getAttribute("data-val");
      setSeg(group, val);
      if (group === "mode") syncTier();
      if (group === "model") {
        if (val !== previous) saveModel(val, previous);
      } else {
        prefs.set(group, val); // B15
      }
    });
  });
  // Reflect the server's current default model in the picker on load.
  (function initModel() {
    var current = document.body.dataset.claudeModel || "";
    if (current) setSeg("model", current); // custom/unknown id -> unselected
  })();
  // B15: restore mode / language / tier from the last session.
  ["mode", "lang", "tier"].forEach(function (g) {
    var v = prefs.get(g, "");
    if (v) setSeg(g, v);
  });
  syncTier();

  // ---- views (per-view scroll, C10) ---------------------------------------
  var currentView = "console";
  var viewScroll = { console: 0, library: 0, stats: 0, practice: 0 };
  function switchView(view) {
    if (!views[view]) return;
    if (view !== currentView && contentEl) viewScroll[currentView] = contentEl.scrollTop;
    var changed = view !== currentView;
    currentView = view;
    Object.keys(views).forEach(function (k) { if (views[k]) views[k].hidden = k !== view; });
    document.querySelectorAll("[data-view]").forEach(function (a) {
      var on = a.getAttribute("data-view") === view;
      a.classList.toggle("on", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    if (changed && contentEl) contentEl.scrollTop = viewScroll[view] || 0;
    closeDrawer(false);
    if (changed && view !== "practice") retirePracticeNotes(); // SP7 fix: one live notes editor
    if (view === "library") { loadLibrary(); restoreViewerNotes(); } // re-fetch on open: fresh after runs
    if (view === "stats") { refreshStats(); loadReview(); } // recompute + the review queue
    else if (changed) refreshReviewIfNewDay(); // SP7 fix: a stale "today" after midnight
  }
  document.querySelectorAll("[data-view]").forEach(function (a) {
    a.addEventListener("click", function () { switchView(a.getAttribute("data-view")); });
    a.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchView(a.getAttribute("data-view")); }
    });
  });

  // ---- filter pills (aria-pressed) -----------------------------------------
  function activatePill(pill) {
    var group = pill.parentNode;
    if (group) {
      group.querySelectorAll(".fp").forEach(function (x) {
        x.classList.remove("on");
        x.setAttribute("aria-pressed", "false");
      });
    }
    pill.classList.add("on");
    pill.setAttribute("aria-pressed", "true");
  }
  document.querySelectorAll("[data-lib-filter]").forEach(function (pill) {
    pill.addEventListener("click", function () { activatePill(pill); applyLibFilter(); });
  });
  document.querySelectorAll("[data-recent-filter]").forEach(function (pill) {
    pill.addEventListener("click", function () { activatePill(pill); applyRecentFilter(); });
  });

  // ---- composer tools + draft persistence (B15) ----------------------------
  var draftTimer = null;
  function saveDraftNow() {
    clearTimeout(draftTimer);
    draftTimer = null;
    prefs.set("draft", problemEl.value);
  }
  function setProblem(text) {
    problemEl.value = text;
    saveDraftNow();
  }
  problemEl.addEventListener("input", function () {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraftNow, 400);
  });
  (function restoreDraft() {
    var d = prefs.get("draft", "");
    if (d && !problemEl.value) problemEl.value = d;
  })();
  window.addEventListener("pagehide", function () { if (draftTimer) saveDraftNow(); });

  if (pasteBtn) {
    pasteBtn.addEventListener("click", function () {
      var blocked = "Clipboard access is blocked here — click the box and paste with " + MOD + "+V.";
      if (!navigator.clipboard || !navigator.clipboard.readText) {
        notify(blocked, "error");
        problemEl.focus();
        return;
      }
      navigator.clipboard.readText().then(function (text) {
        if (!text) { notify("The clipboard is empty.", "error"); return; }
        setProblem(text);
        problemEl.focus();
        flashEditor();
      }, function () {
        notify(blocked, "error");
        problemEl.focus();
      });
    });
  }
  if (sampleBtn) sampleBtn.addEventListener("click", function () { setProblem(SAMPLE); problemEl.focus(); });
  if (clearBtn) clearBtn.addEventListener("click", function () { setProblem(""); problemEl.focus(); });
  if (newRunBtn) newRunBtn.addEventListener("click", function () {
    if (isStreaming) { notify("A run is in progress — stop it first.", ""); return; }
    setProblem("");
    enterIdle();
    switchView("console");
    problemEl.focus();
  });

  // =========================================================================
  // Render pipeline — #output is the single re-rendered node.
  // =========================================================================
  // B16: highlight output is cached by (language, code) so a closed block is
  // highlighted once, not on every throttled render. hljs escapes its input;
  // the cached markup is exactly what hljs.highlightElement would assign.
  var hlCache = new Map();
  function highlightCode(container, skipLast) {
    if (!window.hljs) return;
    var blocks = container.querySelectorAll("pre code");
    blocks.forEach(function (code, i) {
      if (skipLast && i === blocks.length - 1) return; // still streaming in
      var m = /language-([\w+#-]+)/.exec(code.className || "");
      var lang = m ? m[1] : "";
      // Untagged / unknown languages render as plain text (no auto-detect).
      if (!lang || !hljs.getLanguage(lang)) return;
      var text = code.textContent;
      var key = lang + "\u0000" + text;
      var html = hlCache.get(key);
      if (html == null) {
        try {
          html = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
        } catch (e) { return; }
        if (hlCache.size > 300) hlCache.clear();
        hlCache.set(key, html);
      }
      code.innerHTML = html; // hljs-generated, escaped markup
      code.classList.add("hljs");
    });
  }

  // SP6 / D2: click-to-reveal for the doc in #output - every "### Hint N"
  // section, plus the ## Solution section of a Guided run and the ## Key
  // insight of a Guided / Learning one (core.revealsFor). Which ones the
  // learner opened survives the throttled re-renders of the same run.
  var outputReveal = { mode: "", open: {} };
  function revealOpts(state) {
    var which = core.revealsFor(state.mode);
    return {
      revealSolution: which.revealSolution,
      revealInsight: which.revealInsight,
      open: state.open,
      onToggle: function (key, isOpen) { state.open[key] = isOpen; },
    };
  }

  function render(md, final) {
    if (window.marked) {
      outputEl.innerHTML = marked.parse(md); // ONLY assignment site for #output
      var openLast = !final && core.hasOpenFence(md);
      highlightCode(outputEl, openLast);
      decorateCode(outputEl, openLast);
      core.applyReveals(outputEl, document, revealOpts(outputReveal));
    } else {
      outputEl.textContent = md;
    }
    if (!final) followLiveEdge();
  }

  // B16: throttled renders (at most one per RENDER_INTERVAL_MS) that wait while
  // the reader has a text selection inside #output (a re-render would wipe it).
  var lastRenderAt = 0;
  var renderTimer = null;
  var renderDirty = false;
  function selectionInOutput() {
    var sel = window.getSelection ? window.getSelection() : null;
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    return outputEl.contains(sel.anchorNode) || outputEl.contains(sel.focusNode);
  }
  function scheduleRender() {
    renderDirty = true;
    if (renderTimer) return;
    renderTimer = setTimeout(flushRender,
      core.throttleDelay(lastRenderAt, performance.now(), RENDER_INTERVAL_MS));
  }
  function flushRender() {
    renderTimer = null;
    if (!renderDirty) return;
    if (selectionInOutput()) { renderTimer = setTimeout(flushRender, 300); return; }
    renderDirty = false;
    lastRenderAt = performance.now();
    render(acc, false);
  }
  function cancelScheduledRender() {
    clearTimeout(renderTimer);
    renderTimer = null;
    renderDirty = false;
  }

  // Post-render pass: wrap each <pre> in .code/.code-head chrome with a lang
  // label + Copy button. Adds NO listeners (Copy is delegated, once, at init).
  function decorateCode(container, partialLast) {
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
      wrap.className = "code" + (partialLast && i === pres.length - 1 ? " partial" : "");
      var head = document.createElement("div");
      head.className = "code-head";
      var langSpan = el("span", "code-lang");
      langSpan.appendChild(el("span", "d"));
      langSpan.appendChild(document.createTextNode(lang || "text"));
      var copyBtn = el("button", "mini");
      copyBtn.type = "button";
      copyBtn.setAttribute("data-copy-code", "");
      copyBtn.setAttribute("data-label", "Copy");
      copyBtn.setAttribute("aria-label", "Copy code");
      copyBtn.innerHTML = COPY_SVG + '<span class="lbl">Copy</span>';
      head.appendChild(langSpan);
      head.appendChild(copyBtn);
      parent.insertBefore(wrap, pre);
      wrap.appendChild(head);
      wrap.appendChild(pre);
    });
  }

  function handleCopyCode(e) {
    var btn = e.target.closest("[data-copy-code]");
    if (!btn) return;
    var wrap = btn.closest(".code");
    var code = wrap && wrap.querySelector("code");
    copyText(code ? code.textContent : "", btn);
  }
  outputEl.addEventListener("click", handleCopyCode);
  if (libViewerBody) libViewerBody.addEventListener("click", handleCopyCode);

  // ---- D11: auto-follow the live edge + "Jump to latest" ------------------
  var following = true;
  function liveEdgeGapOk() {
    if (!contentEl || !streamFoot) return true;
    var edge = (streamFoot.hidden ? outwrap : streamFoot).getBoundingClientRect().bottom;
    return core.nearEdge(edge, contentEl.getBoundingClientRect().bottom, 80);
  }
  function scrollToLiveEdge(smooth) {
    if (!contentEl) return;
    var target = streamFoot.hidden ? outwrap : streamFoot;
    var delta = target.getBoundingClientRect().bottom - contentEl.getBoundingClientRect().bottom + 16;
    if (delta > 0) {
      if (smooth && contentEl.scrollBy) contentEl.scrollBy({ top: delta, behavior: "smooth" });
      else contentEl.scrollTop += delta;
    }
  }
  function followLiveEdge() {
    if (isStreaming && following && currentView === "console") scrollToLiveEdge(false);
  }
  if (contentEl) {
    contentEl.addEventListener("scroll", function () {
      if (!isStreaming || currentView !== "console") return;
      following = liveEdgeGapOk();
      if (jumpPill) jumpPill.hidden = following;
    }, { passive: true });
  }
  if (jumpPill) {
    jumpPill.addEventListener("click", function () {
      following = true;
      jumpPill.hidden = true;
      scrollToLiveEdge(true);
    });
  }

  // =========================================================================
  // Run lifecycle state machine (idle -> streaming -> done|error|stopped).
  // =========================================================================
  var isStreaming = false;
  var currentRun = null;     // B13: the run that owns the UI (null = none)
  var acc = "";              // the accumulator: acc += delta; scheduleRender()
  var timerId = null;
  var runStart = 0;
  var lastDuration = "";

  function startTimer() {
    runStart = performance.now();
    if (timerEl) timerEl.textContent = "0:00";
    timerId = setInterval(function () {
      if (timerEl) timerEl.textContent = core.fmtClock(performance.now() - runStart);
    }, 250);
  }
  function stopTimer() {
    if (timerId) { clearInterval(timerId); timerId = null; }
  }

  function setRunBtn(running) {
    runBtn.disabled = running;
    runBtn.classList.toggle("running", running);
    runBtn.setAttribute("aria-busy", running ? "true" : "false");
    runBtn.innerHTML = running
      ? '<span class="spin"></span> Running…'
      : 'Run <span class="g">&#9656;</span>';
  }
  function setStop(on, label) {
    if (!stopBtn) return;
    stopBtn.hidden = !on;
    stopBtn.disabled = !on;
    if (stopLbl) stopLbl.textContent = label || "Stop";
  }

  function setRunhead(state, label) {
    runhead.className = "runhead " + state;
    if (rhLabel) rhLabel.textContent = label;
    if (!pulseEl) return;
    pulseEl.className = state === "stream" ? "pulse" : "dot" + (state === "error" ? " red" : state === "stopped" ? " amber" : "");
  }
  function setPhase(p) {
    var t = core.phaseText(p);
    if (rhLabel) rhLabel.textContent = t.label;
    if (streamNote) streamNote.textContent = t.note;
    if (p && p.phase !== "streaming") announce(t.label);
  }

  function modelChipText(meta) {
    if (meta.modelId) return core.modelLabel(meta.modelId);
    return meta.modelAlias ? core.modelLabel(meta.modelAlias) : "Default model";
  }
  function buildRunChips(meta) {
    rhChips.textContent = "";
    rhChips.appendChild(chipEl(core.modeName(meta.mode), "grn"));
    rhChips.appendChild(chipEl(langLabel(meta.language), "mono"));
    if (meta.tier && core.tierApplies(meta.mode)) rhChips.appendChild(chipEl(cap(meta.tier), ""));
    var mc = chipEl(modelChipText(meta), "model" + (meta.modelId ? "" : " pending"));
    mc.id = "rh-model";
    mc.title = meta.modelId ? "Model: " + meta.modelId : "Requested model (waiting for the CLI to confirm)";
    rhChips.appendChild(mc);
  }
  function setRunModel(meta, modelId) {
    if (!modelId || typeof modelId !== "string") return;
    meta.modelId = modelId;
    var mc = $("rh-model");
    if (mc) {
      mc.textContent = core.modelLabel(modelId);
      mc.title = "Model: " + modelId;
      mc.classList.remove("pending");
    }
  }

  function hideTerminals() {
    summaryEl.hidden = true;
    errbox.hidden = true;
    stopmark.hidden = true;
  }
  function setSessionState(s) {
    if (sessionState) sessionState.textContent = s;
  }
  function endStreamingChrome() {
    isStreaming = false;
    stopTimer();
    streamFoot.hidden = true;
    if (jumpPill) jumpPill.hidden = true;
    outputEl.setAttribute("aria-busy", "false");
    setStop(false);
    setRunBtn(false);
  }

  function enterIdle() {
    endStreamingChrome();
    consoleIdle.hidden = false;
    runhead.hidden = true;
    outwrap.hidden = true;
    hideTerminals();
    setSessionState("idle");
  }

  function enterStreaming(meta) {
    isStreaming = true;
    following = true;
    if (jumpPill) jumpPill.hidden = true;
    consoleIdle.hidden = true;
    hideTerminals();
    setRunhead("stream", "Starting");
    if (streamNote) streamNote.textContent = "starting…";
    buildRunChips(meta);
    runhead.hidden = false;
    outwrap.hidden = false;
    streamFoot.hidden = false;
    outputEl.setAttribute("aria-busy", "true");
    setSessionState("streaming");
    setRunBtn(true);
    setStop(true);
    cancelScheduledRender();
    outputReveal = { mode: meta.mode, open: {} };
    render("", false); // clears #output through the single assignment site
    startTimer();
    announce("Run started: " + core.modeName(meta.mode) + ", " + langLabel(meta.language) + ".");
  }

  function enterDone(payload, meta) {
    endStreamingChrome();
    lastDuration = core.fmtDuration(performance.now() - runStart);
    setRunhead("done", "Finished");
    setSessionState("done");
    buildSummary(payload || {}, meta);
    summaryEl.hidden = false;
    outwrap.hidden = !acc;
    var n = (payload.paths || []).length;
    var v = core.verdictFromLine(payload.verification);
    announce("Run finished. Saved " + n + (n === 1 ? " file" : " files") + "." +
      (v ? " " + core.verdictInfo(v).label + "." : ""));
    loadLibrary(); // refresh recents / table / topics / tree with the new files
    loadReview();  // SP7: a first run of a problem schedules its review
  }

  function enterError(msg) {
    endStreamingChrome();
    setRunhead("error", "Failed");
    setSessionState("error");
    buildErrbox(msg);
    errbox.hidden = false;
    outwrap.hidden = !acc; // keep the partial output; hide an empty shell
    announce("Run failed. " + (msg || ""));
  }

  function enterStopped() {
    endStreamingChrome();
    setRunhead("stopped", "Stopped");
    setSessionState("stopped");
    buildStopmark();
    stopmark.hidden = false;
    outwrap.hidden = !acc; // keep the partial output
    announce("Run stopped.");
  }

  // B13: the single exit for a run. A run that no longer owns the UI (it was
  // stopped and a new one started) changes nothing.
  function finish(run, kind, data) {
    if (currentRun !== run) return;
    currentRun = null;
    cancelScheduledRender();
    render(acc, true); // final full render: last chunk never lost, highlights all
    if (kind === "done") enterDone(data || {}, run.meta);
    else if (kind === "stopped") enterStopped();
    else enterError(data);
  }

  // ---- terminal-state card builders --------------------------------------
  function savedRow(path) {
    var row = el("div", "savedrow");
    row.appendChild(typeBadge(extOf(path)));
    var fp = el("div", "fpath");
    var mt = midTrunc(path, 8);
    fp.appendChild(el("span", "a", mt.a));
    if (mt.b) fp.appendChild(el("span", "b", mt.b));
    fp.title = path;
    row.appendChild(fp);
    var btn = el("button", "mini");
    btn.type = "button";
    btn.setAttribute("aria-label", "Copy path " + path);
    btn.innerHTML = COPY_SVG + '<span class="lbl">Copy path</span>';
    btn.addEventListener("click", function () { copyText(path, btn); });
    row.appendChild(btn);
    return row;
  }

  function verdictChipVariant(v) {
    var cls = core.verdictInfo(v).cls;
    return cls === "pass" ? "mint" : cls === "fail" ? "red" : cls === "warn" ? "amber" : "";
  }

  // D7: re-run the same problem (captured at run start) with a patch.
  // SP5 fix R4: the editor may hold a DIFFERENT draft by now (the user
  // started the next problem); replacing it asks first instead of clobbering.
  function rerun(meta, patch, trigger) {
    if (isStreaming) return;
    if (meta.problem && core.needsReplaceConfirm(problemEl.value, meta.problem)) {
      confirmDialog({
        title: "Replace the problem in the editor?",
        lines: ["The editor holds a different problem than this run's.",
          "Re-running puts “" + (meta.title || core.firstLine(meta.problem)) +
          "” back in the editor and runs it."],
        note: "Your current draft will be replaced.",
        ok: "Replace and run",
        returnFocus: trigger,
      }).then(function (ok) { if (ok) applyRerun(meta, patch); });
      return;
    }
    applyRerun(meta, patch);
  }
  function applyRerun(meta, patch) {
    if (isStreaming) return;
    if (patch.mode) setSeg("mode", patch.mode);
    if (patch.lang) setSeg("lang", patch.lang);
    if (patch.tier) setSeg("tier", patch.tier);
    ["mode", "lang", "tier"].forEach(function (g) { if (patch[g]) prefs.set(g, patch[g]); });
    syncTier();
    if (meta.problem) setProblem(meta.problem);
    runNow();
  }
  function openSavedDoc(absPath) {
    var go = function () {
      var rel = core.libRelPath(absPath, libFiles);
      if (!rel) { notify("That file is not in the library listing yet.", "error"); return; }
      switchView("library");
      openFile(rel);
    };
    if (core.libRelPath(absPath, libFiles)) go();
    else loadLibrary().then(go);
  }

  function buildSummary(payload, meta) {
    summaryEl.textContent = "";
    var paths = payload.paths || [];
    var topics = payload.topics || [];

    var top = el("div", "sum-top");
    var check = el("div", "sum-check");
    check.innerHTML = CHECK_SVG;
    top.appendChild(check);
    var mid = document.createElement("div");
    mid.appendChild(el("div", "t", payload.save_warning
      ? "Saved to a fallback location"
      : "Saved to your study library"));
    var subParts = [];
    if (meta.title) subParts.push(meta.title); // C10: captured at run start
    subParts.push(core.modeName(meta.mode));
    subParts.push(langLabel(meta.language));
    if (meta.tier && core.tierApplies(meta.mode)) subParts.push(cap(meta.tier)); // O2
    var sub = subParts.join(" · ");
    if (lastDuration) sub += " — completed in " + lastDuration;
    mid.appendChild(el("div", "s", sub));
    top.appendChild(mid);
    top.appendChild(el("span", "spacer"));
    top.appendChild(chipEl(paths.length + (paths.length === 1 ? " file" : " files"), "mint"));
    summaryEl.appendChild(top);

    var body = el("div", "sum-body");
    function row(label) {
      var r = el("div", "meta-row");
      r.appendChild(el("span", "meta-label", label));
      body.appendChild(r);
      return r;
    }
    if (payload.save_warning) {
      // B25: the normal save failed and the doc went to output/_unsorted/.
      row("Note").appendChild(chipEl(payload.save_warning, "amber"));
    }
    if (payload.model) setRunModel(meta, payload.model);
    row("Model").appendChild(chipEl(modelChipText(meta), "model"));
    if (payload.problem_type) row("Type").appendChild(chipEl(payload.problem_type, ""));
    if (topics.length) {
      var r2 = row("Topics");
      topics.forEach(function (t) { r2.appendChild(chipEl(t, "grn")); });
    }
    if (payload.verification) {
      var v = core.verdictFromLine(payload.verification);
      var vc = chipEl(payload.verification, verdictChipVariant(v));
      vc.title = core.verdictInfo(v).label;
      row("Verify").appendChild(vc);
    }
    if (paths.length) {
      var files = el("div", "files");
      paths.forEach(function (p) { files.appendChild(savedRow(p)); });
      body.appendChild(files);
    }
    // D7: next steps.
    var md = paths.filter(function (p) { return /\.md$/i.test(p); })[0] || "";
    var acts = core.summaryActions({
      mode: meta.mode, language: meta.language, tier: meta.tier, mdPath: md,
    });
    if (acts.length) {
      var bar = el("div", "sum-actions");
      bar.setAttribute("role", "group");
      bar.setAttribute("aria-label", "Next steps");
      acts.forEach(function (a) {
        var b = el("button", "btn" + (a.id === "open" ? " green" : ""), a.label);
        b.type = "button";
        b.setAttribute("data-action", a.id);
        b.addEventListener("click", function () {
          if (a.id === "open") openSavedDoc(a.path);
          else rerun(meta, a.patch, b);
        });
        bar.appendChild(b);
      });
      body.appendChild(bar);
    }
    summaryEl.appendChild(body);
  }

  function buildErrbox(msg) {
    errbox.textContent = "";
    var ic = el("div", "ic");
    ic.innerHTML = ERR_SVG;
    errbox.appendChild(ic);
    var d = document.createElement("div");
    d.appendChild(el("div", "t", "Run failed"));
    d.appendChild(el("div", "d", msg || "The run did not complete."));
    errbox.appendChild(d);
  }

  function buildStopmark() {
    stopmark.textContent = "";
    stopmark.appendChild(el("span", "sq"));
    stopmark.appendChild(el("span", null, "Stopped"));
    stopmark.appendChild(el("span", "s", "Partial output kept below. Nothing was saved."));
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
  function newRunId() {
    try {
      if (window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID().replace(/-/g, "");
      }
    } catch (e) { /* fall through */ }
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  // Best-effort server-side cancel (B14). keepalive lets it outlive a page
  // unload. Resolves true only when the server answers 200 {"cancelled": false}:
  // the run already committed to saving (SP4 review M1), so the caller should
  // let it finish rather than abort and show "Stopped" for a run that was saved.
  // An unresponsive server must not leave Stop hanging: the request is aborted
  // after CANCEL_TIMEOUT_MS and resolves false.
  function cancelRun(runId) {
    var timer = null;
    try {
      var ctl = new AbortController();
      timer = setTimeout(function () { ctl.abort(); }, CANCEL_TIMEOUT_MS);
      return fetch("/run/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: runId }),
        keepalive: true,
        signal: ctl.signal,
      }).then(function (r) {
        return r.json().catch(function () { return null; }).then(function (d) {
          return core.cancelOutcome(r.status, d) === "committed";
        });
      }).catch(function () { return false; }).finally(function () {
        clearTimeout(timer);
      });
    } catch (e) {
      clearTimeout(timer);
      return Promise.resolve(false);
    }
  }

  // Stop: ask the server to cancel first; if it already committed to saving,
  // keep reading so its `done` lands (M1). Otherwise abort the stream and hand
  // the UI back at once (B13) — a re-run no longer waits on the old request.
  function stopRun(run) {
    if (!run || currentRun !== run || run.stopping) return;
    run.stopping = true;
    setStop(true, "Stopping…");
    stopBtn.disabled = true;
    cancelRun(run.id).then(function (committed) {
      if (currentRun !== run) return;
      if (committed) {
        run.stopping = false;
        setStop(false);
        setPhase({ phase: "saving" });
        notify("Too late to stop — the run is already saving.", "");
        return;
      }
      run.stopped = true;
      run.controller.abort();
      finish(run, "stopped");
    });
  }
  if (stopBtn) stopBtn.addEventListener("click", function () { stopRun(currentRun); });

  // POST /run; a 409 right after Stop is the old run still tearing down on the
  // server, so retry with backoff (B14) before giving up.
  async function postRun(run, body) {
    for (var attempt = 0; ; attempt++) {
      var resp = await fetch("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: run.controller.signal,
      });
      var delay = core.runRetryDelay(resp.status, attempt, currentRun === run);
      if (delay < 0) return resp;
      setRunhead("stream", "Waiting");
      if (streamNote) {
        streamNote.textContent = "the previous identical run is still shutting down — retrying in " +
          (delay / 1000).toFixed(1) + "s (" + (attempt + 1) + "/4)…";
      }
      await sleep(delay, run.controller.signal);
    }
  }

  async function runNow() {
    if (isStreaming) return; // guard double-run

    var problem = problemEl.value.trim();
    if (!problem) {
      switchView("console");
      flashEditor();
      problemEl.focus();
      notify("Paste a problem first.", "");
      return;
    }
    if (currentView !== "console") switchView("console"); // C10

    var mode = activeVal("mode");
    var language = activeVal("lang");
    var tier = core.untiered(mode) ? "" : activeVal("tier");
    var model = activeVal("model");
    var attemptCode = "";
    if (mode === "review") {
      // SP7 / D5: Code Review needs the learner's code next to the problem.
      attemptCode = reviewCodeEl ? reviewCodeEl.value : "";
      if (!attemptCode.trim()) {
        notify("Paste your code to review first.", "");
        if (reviewCodeEl) reviewCodeEl.focus();
        return;
      }
    }
    // Wire contract: { problem, mode, language, tier, model?, run_id }. The
    // picker's model is sent per run (B12); the server maps the alias that
    // matches a pinned .env id back to that id (config.resolve_run_model).
    var body = { problem: problem, mode: mode, language: language, tier: tier };
    if (model) body.model = model;
    if (mode === "review") body.code = attemptCode;
    var run = {
      id: newRunId(),
      controller: new AbortController(),
      stopping: false,
      stopped: false,
      meta: {
        mode: mode, language: language, tier: tier,
        modelAlias: model, modelId: "",
        problem: problem, title: core.firstLine(problem), // C10: captured now
      },
    };
    body.run_id = run.id;

    currentRun = run;
    acc = "";
    enterStreaming(run.meta);

    try {
      var resp = await postRun(run, body);
      if (currentRun !== run) return;
      if (!resp.ok) {
        var err = await resp.json().catch(function () { return {}; });
        var msg = err.error || "Request rejected (" + resp.status + ").";
        if (resp.status === 409) {
          msg = "An identical run is still finishing on the server. Wait a few seconds and press Run again.";
        }
        finish(run, "error", msg);
        return;
      }

      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var feed = makeSseParser(function (ev) {
        if (currentRun !== run) return;
        if (ev.type === "text") {
          acc += typeof ev.data === "string" ? ev.data : String(ev.data);
          scheduleRender();
        } else if (ev.name === "phase") {
          setPhase(ev.data || {});
        } else if (ev.name === "meta") {
          if (ev.data && ev.data.model) setRunModel(run.meta, ev.data.model);
        } else {
          // done / error / cancelled (SP5 fix B2: a server-confirmed Stop is
          // the neutral "Stopped" state, never "Run failed"); others ignored.
          var kind = core.runEventKind(ev.name);
          if (kind === "done") finish(run, "done", ev.data || {});
          else if (kind === "stopped") { run.stopped = true; finish(run, "stopped"); }
          else if (kind === "error") {
            finish(run, "error", typeof ev.data === "string" ? ev.data : "Run failed.");
          }
        }
      });

      while (true) {
        var r = await reader.read();
        if (r.done) break;
        if (currentRun !== run) { try { reader.cancel(); } catch (e) { /* noop */ } break; }
        feed(decoder.decode(r.value, { stream: true }));
      }
      // B17: the stream closed without a terminal event — never report that as
      // a save. (A Stop that ended the stream gracefully was already handled.)
      if (run.stopped) finish(run, "stopped");
      else {
        finish(run, "error",
          "The stream ended unexpectedly before the run finished (the server closed the " +
          "connection). Partial output is kept below; nothing was confirmed saved.");
      }
    } catch (e) {
      if (e && e.name === "AbortError") {
        // Stop (already settled by stopRun) or page unload: nothing to show.
        if (run.stopped) finish(run, "stopped");
        return;
      }
      finish(run, "error", "Network error: " + ((e && e.message) || "the request failed") + ".");
    }
  }
  runBtn.addEventListener("click", function () { runNow(); });

  // Leaving the page mid-run: warn first (B15); when it really unloads,
  // cancel server-side so the claude process stops (don't burn usage).
  window.addEventListener("beforeunload", function (e) {
    if (!isStreaming) return;
    e.preventDefault();
    e.returnValue = "";
  });
  window.addEventListener("pagehide", function () {
    var run = currentRun;
    if (run) {
      cancelRun(run.id);
      run.controller.abort();
    }
    if (qaInflight) cancelQuickAsk("unload");
  });

  // ⌘/Ctrl + Enter anywhere runs — switching to the Console first (C10).
  // Suppressed while a dialog is open and for an IME-composition Enter (C9).
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === "Enter" || e.keyCode === 13)) {
      if (core.isComposingEnter(e)) return;
      if (document.querySelector(".overlay:not([hidden])")) return;
      e.preventDefault();
      if (currentView === "practice") { practiceTest(); return; } // SP7 / D3
      if (!isStreaming) runNow();
    }
  });

  // =========================================================================
  // Quick Ask — one-shot Q&A, independent of the run stream. Cancellable,
  // with a 60 s client timeout (B20). Errors render as PLAIN TEXT.
  // =========================================================================
  var qaInput = $("qa-input");
  var qaAskBtn = $("qa-ask");
  var qaAnswer = $("qa-answer");
  var qaStatus = $("qa-status");
  var qaToggle = $("qa-toggle");
  var qaBody = $("qa-body");
  var qaPanel = $("quickask");
  var qaInflight = null;

  if (qaAnswer) qaAnswer.addEventListener("click", handleCopyCode);

  function showQaAnswer(md) {
    qaAnswer.classList.remove("err");
    if (window.marked) {
      qaAnswer.innerHTML = marked.parse(md); // hardened renderer (see marked.use above)
      highlightCode(qaAnswer, false);
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
  function setQaBusy(busy) {
    qaAskBtn.textContent = busy ? "Cancel" : "Ask";
    qaAskBtn.classList.toggle("green", !busy);
    qaAskBtn.setAttribute("aria-label", busy ? "Cancel Quick Ask" : "Ask");
    if (qaStatus) {
      qaStatus.hidden = !busy;
      qaStatus.textContent = busy ? "Asking… (Esc or Cancel to stop; gives up after 60 s)" : "";
    }
    if (qaPanel) qaPanel.setAttribute("aria-busy", busy ? "true" : "false");
  }
  function cancelQuickAsk(reason) {
    var q = qaInflight;
    if (!q) return;
    q.reason = reason;
    postJson("/ask/cancel", { ask_id: q.id }, { keepalive: true }).catch(function () {});
    q.controller.abort();
  }

  async function quickAsk() {
    if (qaInflight) { cancelQuickAsk("cancel"); return; } // the button is Cancel while busy
    var question = qaInput.value.trim();
    if (!question) { qaInput.focus(); return; }

    var q = { id: newRunId(), controller: new AbortController(), reason: "" };
    qaInflight = q;
    setQaBusy(true);
    var timer = setTimeout(function () { cancelQuickAsk("timeout"); }, QA_TIMEOUT_MS);

    try {
      var resp = await postJson("/ask", {
        question: question,
        language: activeVal("lang"),
        problem: problemEl.value.trim(),
        ask_id: q.id,
      }, { signal: q.controller.signal });
      // SP5 fix R7: the answer has arrived - the 60 s timeout must not
      // throw it away now (only an explicit Cancel/unload still can).
      clearTimeout(timer);
      var data = await resp.json().catch(function () { return {}; });
      if (q.reason) throw Object.assign(new Error("aborted"), { name: "AbortError" });
      if (resp.ok) showQaAnswer(String(data.answer || ""));
      else showQaError(data.error || "Request rejected (" + resp.status + ").");
    } catch (e) {
      if (e && e.name === "AbortError") {
        if (q.reason === "timeout") {
          showQaError("Quick Ask timed out after 60 s and was cancelled. Try again or shorten the question.");
        }
        // reason "cancel": reported in #qa-status below (SP5 fix B4).
      } else {
        showQaError("Network error: " + ((e && e.message) || "the request failed") + ".");
      }
    } finally {
      clearTimeout(timer);
      if (qaInflight === q) qaInflight = null;
      setQaBusy(false);
      if (q.reason === "cancel") {
        // SP5 fix B4: say so where the "Asking…" line was; the next ask's
        // setQaBusy(true) replaces it. The stale answer box goes away.
        qaAnswer.hidden = true;
        if (qaStatus) {
          qaStatus.textContent = "Quick Ask cancelled.";
          qaStatus.hidden = false;
        }
      }
    }
  }

  if (qaAskBtn) qaAskBtn.addEventListener("click", quickAsk);
  if (qaInput) {
    // Plain Enter asks (not mid-IME-composition); modified Enter falls through
    // to the global ⌘/Ctrl+Enter run shortcut; Esc cancels an ask in flight.
    qaInput.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && qaInflight) {
        e.preventDefault();
        e.stopPropagation();
        cancelQuickAsk("cancel");
        return;
      }
      if ((e.key === "Enter" || e.keyCode === 13) && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
        if (core.isComposingEnter(e)) return;
        e.preventDefault();
        if (!qaInflight) quickAsk();
      }
    });
  }
  function setQaCollapsed(collapsed) {
    qaBody.hidden = collapsed;
    if (qaPanel) qaPanel.classList.toggle("collapsed", collapsed);
    qaToggle.textContent = collapsed ? "Show" : "Hide";
    qaToggle.setAttribute("aria-expanded", String(!collapsed));
  }
  if (qaToggle && qaBody) {
    qaToggle.addEventListener("click", function () {
      var collapsed = !qaBody.hidden;
      setQaCollapsed(collapsed);
      prefs.set("qaCollapsed", collapsed ? "1" : ""); // B15
    });
    if (prefs.get("qaCollapsed", "") === "1") setQaCollapsed(true);
  }

  // =========================================================================
  // Derived real data from /library ( recents · table · topics · tree ).
  // =========================================================================
  var libFiles = [];
  var libByPath = {};
  var currentRuns = [];
  var libLoaded = false; // C10: no Stats empty-state flash before the first load
  var libListed = false; // R5: a /library listing has succeeded at least once

  function openRun(run) {
    var path = run.mdPath || (run.files[0] && run.files[0].path);
    if (!path) return;
    switchView("library");
    openFile(path);
  }
  function runAriaLabel(run) {
    var diff = core.diffInfo(run.difficulty);
    return "Open " + run.problemLabel + (diff ? " (" + diff.label + ")" : "") + " — " + run.mode +
      (run.language !== "—" ? ", " + run.language : "") +
      (run.tier && core.tierApplies(run.mode) ? ", " + cap(run.tier) : "") + ", " +
      core.verdictInfo(run.verdict).label + ", saved " + relTime(run.savedAt);
  }

  function renderRecents(runs) {
    if (!recentsEl) return;
    recentsEl.textContent = "";
    var top = runs.slice().sort(bySavedDesc).slice(0, 7);
    if (!top.length) {
      recentsEl.appendChild(el("div", "rm", "No saved runs yet."));
      return;
    }
    top.forEach(function (run) {
      var rec = el("button", "rec");
      rec.type = "button";
      rec.setAttribute("aria-label", runAriaLabel(run));
      var info = core.verdictInfo(run.verdict);
      var dd = el("span", "dd v-" + info.cls);
      dd.setAttribute("aria-hidden", "true");
      rec.appendChild(dd);
      var rt = el("span", "rt");
      var rn = el("span", "rn");
      // SP6 fix O3: the number is its own badge, never part of the title.
      if (run.number !== null) rn.appendChild(el("span", "pnum", "#" + run.number));
      var mt = midTrunc(run.problem, 6);
      rn.appendChild(el("span", "a", mt.a));
      if (mt.b) rn.appendChild(el("span", "b", mt.b));
      rt.appendChild(rn);
      var sub = run.mode.toLowerCase() + " · " + (run.langExt || "—") + " · " + relTime(run.savedAt);
      rt.appendChild(el("span", "rm", sub));
      rec.appendChild(rt);
      rec.addEventListener("click", function () { openRun(run); });
      recentsEl.appendChild(rec);
    });
  }

  function setRecentCount(n) {
    if (recentCount) recentCount.textContent = n + " saved · output/";
  }

  // SP6 / D1: the difficulty parsed from the paste (or the doc header).
  function diffCell(difficulty) {
    var info = core.diffInfo(difficulty);
    if (!info) return el("span", "diff", "—");
    var cell = el("span", "diff " + info.cls);
    var dot = el("span", "d");
    dot.setAttribute("aria-hidden", "true");
    cell.appendChild(dot);
    cell.appendChild(document.createTextNode(info.label));
    return cell;
  }

  function statusCell(verdict) {
    var info = core.verdictInfo(verdict);
    var st = el("span", "tstatus v-" + info.cls);
    st.title = info.label;
    if (info.cls === "pass") st.innerHTML = CHECK_SVG;
    else st.textContent = info.glyph;
    st.appendChild(el("span", "sr-only", info.label));
    return st;
  }

  function renderRecentTable(runs) {
    if (!recentTable) return;
    recentTable.querySelectorAll(".trow").forEach(function (r) { r.remove(); });
    var sorted = runs.slice().sort(bySavedDesc);
    setRecentCount(sorted.length);
    if (!sorted.length) {
      var empty = el("div", "trow empty");
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
      row.setAttribute("role", "button");
      row.tabIndex = 0;
      row.setAttribute("aria-label", runAriaLabel(run));
      row.setAttribute("data-mode", run.mode.toLowerCase());
      row.setAttribute("data-verdict", run.verdict || "none");
      row.setAttribute("data-difficulty", (core.diffInfo(run.difficulty) || { cls: "none" }).cls);
      row.appendChild(diffCell(run.difficulty));
      var pc = el("div", "pcell");
      // SP5 fix B5: long names/slugs ellipsize in their column (style.css);
      // the full text stays one hover away.
      var pn = el("div", "pn");
      if (run.number !== null) pn.appendChild(el("span", "pnum", "#" + run.number)); // O3
      pn.appendChild(document.createTextNode(run.problem));
      pn.title = run.problemLabel;
      pc.appendChild(pn);
      var slug = run.stemRaw + (run.tier ? "__" + run.tier : "") + (run.slot ? "__" + run.slot : "");
      var pm = el("div", "pm", slug);
      pm.title = slug;
      pc.appendChild(pm);
      row.appendChild(pc);
      var topicCell = run.topic ? chipEl(run.topic, "mint") : el("span", "tcell", "—");
      if (run.topic) topicCell.title = run.topic;
      row.appendChild(topicCell);
      row.appendChild(chipEl(run.mode, ""));
      row.appendChild(run.langExt ? typeBadge(run.langExt) : el("span", "tcell", "—"));
      row.appendChild(el("span", "tcell", relTime(run.savedAt)));
      row.appendChild(statusCell(run.verdict));
      row.addEventListener("click", function () { openRun(run); });
      row.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRun(run); }
      });
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

  // C9: topic chips are real buttons that open search prefilled with the topic.
  function renderTopics(runs) {
    if (!topicsEl) return;
    topicsEl.textContent = "";
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
      topicsEl.appendChild(el("span", "topic empty", "No topics yet"));
      return;
    }
    list.slice(0, 10).forEach(function (item) {
      var chip = el("button", "topic");
      chip.type = "button";
      chip.setAttribute("aria-label", "Search the library for " + item.topic +
        " (" + item.count + (item.count === 1 ? " problem)" : " problems)"));
      chip.appendChild(document.createTextNode(item.topic + " "));
      var c = el("span", "c", String(item.count));
      c.setAttribute("aria-hidden", "true");
      chip.appendChild(c);
      chip.addEventListener("click", function () { openSearch(item.topic); });
      topicsEl.appendChild(chip);
    });
  }

  // =========================================================================
  // Stats — Stats view + Console streak badge, from currentRuns.
  // =========================================================================
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function setStatText(id, val) {
    var e = $(id);
    if (e) e.textContent = String(val);
  }

  function renderStreakBadge(stats) {
    var badge = $("streak-badge");
    if (!badge) return;
    badge.textContent = "";
    badge.hidden = false;
    var zero = stats.currentStreak <= 0;
    badge.classList.toggle("zero", zero);
    if (zero) {
      badge.appendChild(el("span", "sb-txt", "Start your streak today"));
      return;
    }
    var flame = el("span", "sb-flame", "🔥");
    flame.setAttribute("aria-hidden", "true");
    badge.appendChild(flame);
    var txt = stats.currentStreak + "-day streak";
    if (stats.today > 0) txt += " · " + stats.today + " today";
    badge.appendChild(el("span", "sb-txt", txt));
  }

  function renderTiles(stats) {
    setStatText("stat-streak", stats.currentStreak);
    setStatText("stat-streak-sub",
      stats.currentStreak === 0 ? "start today" : (stats.currentStreak === 1 ? "day" : "days"));
    setStatText("stat-longest", stats.longestStreak);
    setStatText("stat-longest-sub", stats.longestStreak === 1 ? "day best" : "days best");
    setStatText("stat-today", stats.today);
    setStatText("stat-today-sub", "this week: " + stats.thisWeek);
    setStatText("stat-total", stats.total);
    setStatText("stat-total-sub",
      stats.distinctProblems + (stats.distinctProblems === 1 ? " problem" : " problems"));
  }

  // GitHub-style: columns = weeks, rows = weekday.
  function renderHeatmap(heatmap) {
    var grid = $("hm-grid");
    var months = $("hm-months");
    if (!grid || !months) return;
    grid.textContent = "";
    months.textContent = "";
    if (!heatmap.length) return;

    var first = core.parseDayKey(heatmap[0].date);
    var pad = first.getDay();
    var cells = [];
    for (var p = 0; p < pad; p++) cells.push(null);
    heatmap.forEach(function (d) { cells.push(d); });
    var numCols = Math.ceil(cells.length / 7);

    grid.style.gridTemplateColumns = "repeat(" + numCols + ", var(--hm-cell))";
    months.style.gridTemplateColumns = "repeat(" + numCols + ", var(--hm-cell))";
    grid.setAttribute("role", "img");
    var active = heatmap.filter(function (d) { return d.count > 0; }).length;
    grid.setAttribute("aria-label", "Activity heatmap: " + active + " active days in the last 17 weeks");

    cells.forEach(function (d) {
      if (!d) { grid.appendChild(el("span", "hm-cell hm-pad")); return; }
      var cell = el("span", "hm-cell hm-" + core.heatBucket(d.count));
      cell.title = d.date + " · " + d.count + (d.count === 1 ? " run" : " runs");
      grid.appendChild(cell);
    });

    var lastMonth = -1;
    var lastLabelCol = -99;
    for (var c = 0; c < numCols; c++) {
      var rep = null;
      for (var r = 0; r < 7; r++) {
        var cc = cells[c * 7 + r];
        if (cc) { rep = cc; break; }
      }
      if (!rep) continue;
      var mo = core.parseDayKey(rep.date).getMonth();
      if (mo !== lastMonth) {
        lastMonth = mo;
        if (c - lastLabelCol >= 3) {
          var lbl = el("span", "hm-mo", MONTHS[mo]);
          lbl.style.gridColumnStart = String(c + 1);
          months.appendChild(lbl);
          lastLabelCol = c;
        }
      }
    }
  }

  function sortedEntries(map) {
    return Object.keys(map).map(function (k) {
      return { key: k, count: map[k] };
    }).sort(function (a, b) {
      return b.count - a.count || a.key.localeCompare(b.key);
    });
  }

  function renderBars(id, map) {
    var host = $(id);
    if (!host) return;
    host.textContent = "";
    var rows = sortedEntries(map);
    if (!rows.length) { host.appendChild(el("div", "bd-empty", "No data yet")); return; }
    var max = rows[0].count || 1;
    rows.forEach(function (row) {
      var r = el("div", "bd-row");
      r.appendChild(el("span", "bd-key", row.key));
      var track = el("span", "bd-track");
      track.setAttribute("aria-hidden", "true");
      var fill = el("span", "bd-fill");
      fill.style.width = Math.max(6, Math.round((row.count / max) * 100)) + "%";
      track.appendChild(fill);
      r.appendChild(track);
      r.appendChild(el("span", "bd-num", String(row.count)));
      host.appendChild(r);
    });
  }

  function renderTopicList(id, map) {
    var host = $(id);
    if (!host) return;
    host.textContent = "";
    var rows = sortedEntries(map).slice(0, 8);
    if (!rows.length) { host.appendChild(el("div", "bd-empty", "No topics yet")); return; }
    rows.forEach(function (row, i) {
      var r = el("div", "bd-trow");
      r.appendChild(el("span", "bd-rank", String(i + 1)));
      r.appendChild(el("span", "bd-tname", row.key));
      r.appendChild(el("span", "bd-num", String(row.count)));
      host.appendChild(r);
    });
  }

  // SP6 / A8: Stats come from the server (GET /stats: the run log, plus
  // legacy files with no log entry counted per saved run). If that request
  // fails, the page falls back to computing them from the /library listing.
  var serverStats = null;
  function loadStats() {
    return fetch("/stats")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) { serverStats = data && data.heatmap ? data : null; })
      .catch(function () { serverStats = null; })
      .then(refreshStats);
  }

  function refreshStats() {
    if (!libLoaded) return; // C10: no empty-state flash before /library answers
    var stats = serverStats || core.computeStats(currentRuns);
    renderStreakBadge(stats);
    var empty = $("stats-empty");
    var body = $("stats-body");
    if (!stats.total) {
      if (empty) empty.hidden = false;
      if (body) body.hidden = true;
      return;
    }
    if (empty) empty.hidden = true;
    if (body) body.hidden = false;
    renderTiles(stats);
    renderHeatmap(stats.heatmap);
    renderBars("bd-mode", stats.byMode);
    renderBars("bd-language", stats.byLanguage);
    renderTopicList("bd-topic", stats.byTopic);
  }

  var statsEmptyCta = $("stats-empty-cta");
  if (statsEmptyCta) statsEmptyCta.addEventListener("click", function () { switchView("console"); });

  // =========================================================================
  // Library two-pane.
  // =========================================================================
  var vwTitle, vwSub;
  (function buildViewerHead() {
    if (!libViewer || !libViewerPath) return;
    var head = libViewer.querySelector(".vw-head");
    if (!head) return;
    var main = document.createElement("div");
    main.className = "vw-main";
    vwTitle = el("h2", "vw-title");
    vwSub = el("div", "vw-sub");
    head.insertBefore(main, libViewerPath);
    main.appendChild(vwTitle);
    main.appendChild(vwSub);
    main.appendChild(libViewerPath);
  })();

  function fileMeta(path) {
    var parts = path.split("/");
    var fname = parts[parts.length - 1];
    var dot = fname.lastIndexOf(".");
    var ext = dot === -1 ? "" : fname.slice(dot + 1).toLowerCase();
    var base = dot === -1 ? fname : fname.slice(0, dot);
    var nm = core.splitRunName(base); // A8: a trailing __N is a slot, not a tier
    var stem = nm.stem;
    var tier = nm.tier;
    var modeFolder = parts.length >= 3 ? parts[0] : "";
    var topicRaw = parts.length >= 3 ? parts[1].replace(/_learning$/, "") : "";
    var f = libByPath[path];
    // SP6 fix O3: the problem record's title (number shown apart); a legacy
    // "1_two_sum" name loses its leading number.
    var pt = core.problemTitle({ title: f && f.title, number: f && f.number, stem: stem });
    return {
      title: pt.label || fname,
      ext: ext,
      tier: tier,
      topic: humanize(topicRaw),
      mode: modeFolder ? modeLabel(modeFolder) : "",
      mtime: f && f.mtime,
      verdict: (f && f.verdict) || "",
      difficulty: (f && f.difficulty) || "",
    };
  }

  function showFile(relPath, text) {
    if (viewerNotes) viewerNotes.flush(); // SP7 / D9: never drop a pending note
    var meta = fileMeta(relPath);
    if (vwTitle) vwTitle.textContent = meta.title;
    if (vwSub) {
      vwSub.textContent = "";
      var diff = core.diffInfo(meta.difficulty);
      if (diff) vwSub.appendChild(chipEl(diff.label, "diff-" + diff.cls));
      if (meta.topic) vwSub.appendChild(chipEl(meta.topic, "mint"));
      if (meta.mode) vwSub.appendChild(chipEl(meta.mode, ""));
      if (meta.ext === "md") {
        if (meta.tier && core.tierApplies(meta.mode)) vwSub.appendChild(chipEl(cap(meta.tier), ""));
      } else {
        var ll = langLabel(meta.ext);
        if (ll !== "—") vwSub.appendChild(chipEl(ll, ""));
      }
      if (meta.verdict) {
        var info = core.verdictInfo(meta.verdict);
        vwSub.appendChild(chipEl(info.glyph + " " + info.label, verdictChipVariant(meta.verdict)));
      }
      if (meta.mtime) vwSub.appendChild(chipEl("saved " + savedDate(meta.mtime), "mono"));
    }
    if (libViewerPath) libViewerPath.textContent = relPath;
    currentViewPath = relPath;

    libViewerBody.textContent = "";
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
    highlightCode(libViewerBody, false);
    decorateCode(libViewerBody, false);
    if (meta.ext === "md") {
      // SP6 / D2: hints (and a Guided doc's solution) start hidden here too.
      core.applyReveals(libViewerBody, document, revealOpts({ mode: meta.mode, open: {} }));
    }
    libViewer.hidden = false;
    libViewer.scrollTop = 0; // C10: a newly opened file starts at its top
    decorateViewer(relPath, meta); // SP7: Re-attempt / Flashcards / notes
  }

  function markTreeActive(relPath) {
    libTree.querySelectorAll(".filerow").forEach(function (row) {
      var on = !!relPath && row.getAttribute("data-path") === relPath;
      row.classList.toggle("on", on);
      if (on) row.setAttribute("aria-current", "true"); else row.removeAttribute("aria-current");
    });
  }

  function openFile(relPath) {
    return fetch("/library/file?path=" + encodeURIComponent(relPath))
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (text) {
        showFile(relPath, text);
        markTreeActive(relPath);
      })
      .catch(function (e) {
        notify("Could not open " + relPath.split("/").pop() + " (" + ((e && e.message) || "error") +
          "). It may have been moved or deleted.", "error");
      });
  }

  function renderTree(files) {
    libTree.textContent = "";
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
      grp.setAttribute("role", "group");
      var h = el("div", "grp-h");
      h.innerHTML = FOLDER_SVG;
      h.appendChild(document.createTextNode(" " + (folder || "(library root)")));
      grp.setAttribute("aria-label", folder || "library root");
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
        if (f.verdict) {
          var vi = core.verdictInfo(f.verdict);
          var vd = el("span", "fverdict v-" + vi.cls, vi.glyph);
          vd.title = vi.label;
          btn.appendChild(vd);
          btn.setAttribute("aria-label", name + " — " + vi.label);
        }
        btn.addEventListener("click", function () { openFile(f.path); });
        grp.appendChild(btn);
      });
      libTree.appendChild(grp);
    });
    applyLibFilter();
    markTreeActive(currentViewPath); // C10: keep the open file highlighted
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
    return fetch("/library")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        libLoaded = true;
        libListed = true;
        libFiles = (data && data.files) || [];
        libByPath = {};
        libFiles.forEach(function (f) { libByPath[f.path] = f; });
        currentRuns = core.deriveRuns(libFiles);
        renderRecents(currentRuns);
        renderRecentTable(currentRuns);
        renderTopics(currentRuns);
        renderTree(libFiles);
        return loadStats();
      })
      .catch(function (e) {
        libLoaded = true;
        var why = (e && e.message) || "network error";
        // SP5 fix R5: a refresh that fails keeps the library the page already
        // shows (a blip must not blank recents / table / tree) and says so.
        if (libListed) {
          notify("Could not refresh the library (" + why + "). Showing the last loaded list.", "error");
          return;
        }
        notify("Could not load the library (" + why + ").", "error");
        libFiles = [];
        libByPath = {};
        currentRuns = [];
        renderRecents([]);
        renderRecentTable([]);
        renderTopics([]);
        refreshStats();
        if (libTree) {
          libTree.textContent = "";
          libTree.appendChild(el("div", "grp-h", "Could not load the library (" + why + ")."));
        }
      });
  }

  function closeViewer() {
    resetViewerExtras();
    closeFollowupBox(); // SP8 / D6: stops a follow-up in flight
    libViewer.hidden = true;
    libViewerBody.textContent = "";
    currentViewPath = null;
    markTreeActive(null);
  }
  if (libViewerClose) libViewerClose.addEventListener("click", closeViewer);

  // B19: "Delete run" removes the doc AND its code file(s) (scope=run).
  if (libViewerDelete) {
    libViewerDelete.addEventListener("click", function () {
      var path = currentViewPath;
      if (!path) return;
      var targets = core.runSiblings(libFiles, path);
      confirmDialog({
        title: targets.length > 1 ? "Delete this run (" + targets.length + " files)?" : "Delete this file?",
        lines: targets,
        note: "This can't be undone.",
        ok: targets.length > 1 ? "Delete run" : "Delete file",
        returnFocus: libViewerDelete,
      }).then(function (yes) {
        if (!yes) return;
        libViewerDelete.disabled = true;
        fetch("/library/file?path=" + encodeURIComponent(path) + "&scope=run", { method: "DELETE" })
          .then(function (resp) {
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            return resp.json().catch(function () { return {}; });
          })
          .then(function (d) {
            var n = (d && d.paths && d.paths.length) || targets.length;
            closeViewer();
            loadLibrary();
            notify("Deleted " + n + (n === 1 ? " file." : " files."), "");
          })
          .catch(function (e) {
            notify("Could not delete that run (" + ((e && e.message) || "error") + ").", "error");
          })
          .then(function () { libViewerDelete.disabled = false; });
      });
    });
  }

  // =========================================================================
  // Overlays: shortcuts modal, ⌘K search palette, confirm dialog — all trap
  // focus (C9) and render user-derived text via el()/textContent only.
  // =========================================================================
  var shortcutsModal = $("shortcuts-modal");
  var shortcutsBtn = $("shortcuts-btn");
  var shortcutsClose = $("shortcuts-close");
  var searchPalette = $("search-palette");
  var searchBox = $("tb-search");
  var searchInput = $("search-input");
  var searchResults = $("search-results");
  var confirmModal = $("confirm-modal");
  var overlayReturnFocus = null;
  var searchRows = [];
  var searchActive = -1;

  var FOCUSABLE = 'button:not([disabled]):not([hidden]), [href], input:not([disabled]), ' +
    'select, textarea, [tabindex]:not([tabindex="-1"])';
  function focusablesIn(root) {
    return Array.prototype.filter.call(root.querySelectorAll(FOCUSABLE), function (n) {
      return n.offsetParent !== null || n === document.activeElement;
    });
  }
  // Keep Tab inside `root` (wrapping both ways).
  function trapTab(e, root) {
    if (e.key !== "Tab" || !root) return;
    var list = focusablesIn(root);
    if (!list.length) { e.preventDefault(); return; }
    var i = list.indexOf(document.activeElement);
    var next = core.nextFocusIndex(i, list.length, e.shiftKey);
    e.preventDefault();
    list[next].focus();
  }
  function openOverlay() {
    return document.querySelector(".overlay:not([hidden])");
  }

  function isTypingTarget(node) {
    if (!node) return false;
    var tag = (node.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || node.isContentEditable === true;
  }
  function restoreOverlayFocus() {
    var t = overlayReturnFocus;
    overlayReturnFocus = null;
    if (t && typeof t.focus === "function") { try { t.focus(); } catch (e) { /* noop */ } }
  }

  // ---- shortcuts modal ----------------------------------------------------
  function openShortcuts() {
    if (!shortcutsModal || !shortcutsModal.hidden) return;
    closeSearch();
    overlayReturnFocus = document.activeElement;
    shortcutsModal.hidden = false;
    if (shortcutsClose) shortcutsClose.focus();
  }
  function closeShortcuts() {
    if (!shortcutsModal || shortcutsModal.hidden) return;
    shortcutsModal.hidden = true;
    restoreOverlayFocus();
  }
  if (shortcutsBtn) shortcutsBtn.addEventListener("click", openShortcuts);
  if (shortcutsClose) shortcutsClose.addEventListener("click", closeShortcuts);
  if (shortcutsModal) {
    shortcutsModal.addEventListener("click", function (e) {
      if (e.target === shortcutsModal) closeShortcuts();
    });
  }

  // ---- confirm dialog ---------------------------------------------------------
  var confirmResolve = null;
  function confirmDialog(opts) {
    if (!confirmModal) return Promise.resolve(window.confirm(opts.title));
    // SP5 fix R7: a second dialog while one is open answers the first
    // "cancelled" (the safe choice) instead of leaving its promise hanging.
    if (confirmResolve) {
      var pending = confirmResolve;
      confirmResolve = null;
      pending(false);
    }
    closeSearch();
    closeShortcuts();
    $("confirm-title").textContent = opts.title;
    var body = $("confirm-body");
    body.textContent = "";
    if (opts.lines && opts.lines.length) {
      var ul = el("ul", "confirm-list");
      opts.lines.forEach(function (l) { ul.appendChild(el("li", null, l)); });
      body.appendChild(ul);
    }
    if (opts.note) body.appendChild(el("p", "confirm-note", opts.note));
    $("confirm-ok").textContent = opts.ok || "OK";
    // SP7: a non-destructive confirm (Show the solution) is not styled red.
    $("confirm-ok").className = "btn " + (opts.tone === "primary" ? "green" : "danger");
    // Replacing an open dialog keeps the focus target of the first one (the
    // active element is now the dialog's own Cancel button).
    if (opts.returnFocus || confirmModal.hidden) {
      overlayReturnFocus = opts.returnFocus || document.activeElement;
    }
    confirmModal.hidden = false;
    $("confirm-cancel").focus(); // the safe choice has focus
    return new Promise(function (resolve) { confirmResolve = resolve; });
  }
  function closeConfirm(answer) {
    if (!confirmModal || confirmModal.hidden) return;
    confirmModal.hidden = true;
    var r = confirmResolve;
    confirmResolve = null;
    restoreOverlayFocus();
    if (r) r(!!answer);
  }
  if (confirmModal) {
    $("confirm-ok").addEventListener("click", function () { closeConfirm(true); });
    $("confirm-cancel").addEventListener("click", function () { closeConfirm(false); });
    confirmModal.addEventListener("click", function (e) {
      if (e.target === confirmModal) closeConfirm(false);
    });
  }

  // ---- search palette -----------------------------------------------------
  function hintFor(parts) {
    return parts.filter(function (p) { return p && p !== "—"; }).join(" · ");
  }

  function buildSearchItems() {
    var items = [];
    var claimed = {};
    currentRuns.forEach(function (run) {
      var path = run.mdPath || (run.files[0] && run.files[0].path);
      if (!path) return;
      run.files.forEach(function (f) { claimed[f.path] = true; });
      var names = run.files.map(function (f) { return f.path.split("/").pop(); }).join(" ");
      items.push({
        title: run.problemLabel || run.stemRaw || path,
        hint: hintFor([run.mode, run.topic, run.language]),
        path: path,
        run: run,
        savedAt: run.savedAt || 0,
        hay: [run.problemLabel, run.topic, run.mode, run.language, run.stemRaw, run.langExt, names]
          .join(" ").toLowerCase(),
      });
    });
    libFiles.forEach(function (f) {
      if (claimed[f.path]) return;
      var meta = fileMeta(f.path);
      items.push({
        title: meta.title,
        hint: hintFor([meta.mode, meta.topic, langLabel(meta.ext)]) || f.path,
        path: f.path,
        run: null,
        savedAt: (f && f.mtime) || 0,
        hay: [meta.title, meta.topic, meta.mode, langLabel(meta.ext), f.path].join(" ").toLowerCase(),
      });
    });
    return items;
  }

  function filterSearch(query) {
    var all = buildSearchItems();
    var q = String(query || "").trim().toLowerCase();
    if (!q) {
      return all.slice().sort(function (a, b) { return b.savedAt - a.savedAt; }).slice(0, 8);
    }
    var terms = q.split(/\s+/);
    return all.filter(function (it) {
      return terms.every(function (t) { return it.hay.indexOf(t) !== -1; });
    }).slice(0, 40);
  }

  // C9: the input stays focused; the highlighted option is announced through
  // aria-activedescendant.
  function setSearchActive(i) {
    var rows = searchResults.querySelectorAll(".pal-row");
    if (!rows.length) {
      searchActive = -1;
      searchInput.removeAttribute("aria-activedescendant");
      return;
    }
    if (i < 0) i = 0;
    if (i > rows.length - 1) i = rows.length - 1;
    searchActive = i;
    for (var r = 0; r < rows.length; r++) {
      var on = r === i;
      rows[r].classList.toggle("on", on);
      rows[r].setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        searchInput.setAttribute("aria-activedescendant", rows[r].id);
        if (rows[r].scrollIntoView) rows[r].scrollIntoView({ block: "nearest" });
      }
    }
  }
  function moveSearchActive(delta) {
    var rows = searchResults.querySelectorAll(".pal-row");
    if (!rows.length) return;
    var next = searchActive + delta;
    if (next < 0) next = rows.length - 1;
    if (next > rows.length - 1) next = 0;
    setSearchActive(next);
  }

  function renderSearchResults(items) {
    searchRows = items;
    searchResults.textContent = "";
    if (!items.length) {
      searchResults.appendChild(el("div", "pal-none", "No matches"));
      searchActive = -1;
      searchInput.removeAttribute("aria-activedescendant");
      return;
    }
    items.forEach(function (it, i) {
      var row = el("div", "pal-row");
      row.id = "search-opt-" + i;
      row.setAttribute("role", "option");
      row.appendChild(typeBadge(extOf(it.path)));
      var main = el("div", "pal-main");
      main.appendChild(el("div", "pal-title", it.title));
      if (it.hint) main.appendChild(el("div", "pal-hint", it.hint));
      row.appendChild(main);
      row.addEventListener("click", function () { openSearchItem(it); });
      row.addEventListener("mousemove", function () {
        if (searchActive !== i) setSearchActive(i);
      });
      searchResults.appendChild(row);
    });
    setSearchActive(0);
  }

  function openSearchItem(it) {
    if (!it) return;
    closeSearch();
    if (it.run) {
      openRun(it.run);
    } else {
      switchView("library");
      openFile(it.path);
    }
  }

  function openSearch(prefill) {
    if (!searchPalette || !searchInput) return;
    var q = typeof prefill === "string" ? prefill : "";
    if (!searchPalette.hidden) {
      if (q) { searchInput.value = q; renderSearchResults(filterSearch(q)); }
      searchInput.focus();
      searchInput.select();
      return;
    }
    closeShortcuts();
    overlayReturnFocus = document.activeElement;
    searchPalette.hidden = false;
    searchInput.value = q;
    renderSearchResults(filterSearch(q));
    searchInput.focus();
  }
  function closeSearch() {
    if (!searchPalette || searchPalette.hidden) return;
    searchPalette.hidden = true;
    restoreOverlayFocus();
  }

  if (searchBox) {
    searchBox.addEventListener("click", function () { openSearch(); });
    searchBox.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " " || e.keyCode === 13 || e.keyCode === 32) {
        e.preventDefault(); openSearch();
      }
    });
  }
  if (searchPalette) {
    searchPalette.addEventListener("click", function (e) {
      if (e.target === searchPalette) closeSearch();
    });
  }
  if (searchInput) {
    searchInput.addEventListener("input", function () {
      renderSearchResults(filterSearch(searchInput.value));
    });
    searchInput.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveSearchActive(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveSearchActive(-1); }
      else if (e.key === "Enter" || e.keyCode === 13) {
        if (core.isComposingEnter(e)) return; // C9 IME guard
        e.preventDefault();
        openSearchItem(searchRows[searchActive] || searchRows[0]);
      }
    });
  }

  // =========================================================================
  // C11: sidebar drawer below 900 px.
  // =========================================================================
  var sidebarEl = $("sidebar");
  var drawerToggle = $("sidebar-toggle");
  var drawerBackdrop = $("drawer-backdrop");
  var narrowMq = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;
  function drawerOpen() { return !!appEl && appEl.classList.contains("drawer-open"); }
  function openDrawer() {
    if (!appEl || !(narrowMq && narrowMq.matches)) return;
    appEl.classList.add("drawer-open");
    if (drawerBackdrop) drawerBackdrop.hidden = false;
    if (drawerToggle) {
      drawerToggle.setAttribute("aria-expanded", "true");
      drawerToggle.setAttribute("aria-label", "Close the sidebar");
    }
    var first = sidebarEl && focusablesIn(sidebarEl)[0];
    if (first) setTimeout(function () { first.focus(); }, 30);
  }
  function closeDrawer(refocus) {
    if (!drawerOpen()) return;
    appEl.classList.remove("drawer-open");
    if (drawerBackdrop) drawerBackdrop.hidden = true;
    if (drawerToggle) {
      drawerToggle.setAttribute("aria-expanded", "false");
      drawerToggle.setAttribute("aria-label", "Open the sidebar");
      if (refocus !== false) drawerToggle.focus();
    }
  }
  if (drawerToggle) {
    drawerToggle.addEventListener("click", function () {
      if (drawerOpen()) closeDrawer(); else openDrawer();
    });
  }
  if (drawerBackdrop) drawerBackdrop.addEventListener("click", function () { closeDrawer(); });
  if (recentsEl) recentsEl.addEventListener("click", function () { closeDrawer(false); });
  if (narrowMq) {
    var onMq = function () { if (!narrowMq.matches) closeDrawer(false); };
    if (narrowMq.addEventListener) narrowMq.addEventListener("change", onMq);
    else if (narrowMq.addListener) narrowMq.addListener(onMq);
  }

  // =========================================================================
  // SP7 — the practice loop: code boxes (Tab indent), Code Review's attempt
  // box, the review queue (Due today + Stats), the re-attempt view, per-problem
  // notes and flashcards. Everything user- or model-derived is set via
  // el()/textContent; only saved Markdown docs go through the hardened marked.
  // =========================================================================
  var NOTES_CAP = 20000;
  var NOTES_DEBOUNCE_MS = 800;
  var reviewCodeEl = $("review-code");
  var lastReview = null; // the last GET /review answer
  var lastReviewDay = ""; // the local day that answer was fetched on

  // SP7 fix: the server's "today" only while it is still that day here - a
  // tab left open past midnight counts from the local date (and re-fetches).
  function todayKey() {
    return core.reviewToday(lastReview, lastReviewDay, new Date());
  }
  function problemLabelOf(rec) {
    rec = rec || {};
    return core.problemTitle({ title: rec.title, number: rec.number, stem: rec.id || "" }).label ||
      rec.id || "this problem";
  }
  function errorOf(resp) {
    return resp.json().catch(function () { return {}; }).then(function (d) {
      return (d && d.error) || "HTTP " + resp.status;
    });
  }

  // ---- code textareas: Tab / Shift+Tab indent; Esc then Tab leaves --------
  function attachCodeEditor(ta, onChange) {
    if (!ta) return;
    var escaped = false;
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { escaped = true; return; }
      if (e.key !== "Tab" || e.ctrlKey || e.metaKey || e.altKey) { escaped = false; return; }
      if (escaped) { escaped = false; return; } // keyboard users can leave the box
      e.preventDefault();
      var r = core.indentText(ta.value, ta.selectionStart, ta.selectionEnd, e.shiftKey);
      if (r.value === ta.value) return;
      ta.value = r.value;
      ta.setSelectionRange(r.start, r.end);
      if (onChange) onChange();
    });
    ta.addEventListener("blur", function () { escaped = false; });
    if (onChange) ta.addEventListener("input", onChange);
  }

  // Code Review's attempt box keeps its draft like the problem box does.
  if (reviewCodeEl) {
    var reviewDraftTimer = null;
    var saveReviewDraft = function () {
      clearTimeout(reviewDraftTimer);
      reviewDraftTimer = setTimeout(function () { prefs.set("reviewDraft", reviewCodeEl.value); }, 400);
    };
    attachCodeEditor(reviewCodeEl, saveReviewDraft);
    var rd = prefs.get("reviewDraft", "");
    if (rd && !reviewCodeEl.value) reviewCodeEl.value = rd;
    window.addEventListener("pagehide", function () { prefs.set("reviewDraft", reviewCodeEl.value); });
  }
  syncTier(); // reveal the attempt box when Code Review was the saved mode

  // ---- D9: the notes editor (library viewer + re-attempt view) -------------
  // Debounced PUT /problems/<id>/notes with a visible Saving / Saved / Not
  // saved state; a failed save keeps the text and offers Retry.
  // SP7 fix: every save of a problem's notes goes through that problem's one
  // queue (core.makeSaveQueue), so saves never race, and text typed while a
  // save is in flight is still sent after the editor closes. Only one editor
  // is live at a time: opening the re-attempt view retires the library
  // viewer's editor, and leaving it retires its own; the viewer's editor is
  // rebuilt from the server (or the queue's newer text) when the Library
  // shows again - so two editors never overwrite each other's text.
  var notesQueues = {}; // pid -> core.makeSaveQueue
  function notesQueue(pid) {
    if (!notesQueues[pid]) {
      notesQueues[pid] = core.makeSaveQueue(function (text, keepalive, done) {
        fetch("/problems/" + encodeURIComponent(pid) + "/notes", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes: text }),
          keepalive: !!keepalive,
        }).then(function (resp) {
          if (resp.ok) return done(null);
          return errorOf(resp).then(function (why) { done(new Error(why)); });
        }).catch(function (e) {
          done(new Error((e && e.message) || "network error"));
        });
      });
    }
    return notesQueues[pid];
  }
  function notesBusy(pid) {
    return !!(notesQueues[pid] && notesQueues[pid].pending());
  }
  // The notes to show for `pid`: a save that was still landing when the
  // record was fetched (`busyAtFetch`) or is now beats the server's copy.
  function knownNotes(pid, serverText, busyAtFetch) {
    var q = notesQueues[pid];
    if (q && q.latest() !== null && (busyAtFetch || q.pending())) return q.latest();
    return serverText || "";
  }
  function makeNotesEditor(host, pid, initial, label, busyAtFetch) {
    host.textContent = "";
    var taId = host.id + "-input";
    var head = el("div", "notes-head");
    var lab = el("label", "notes-label", label || "Your notes");
    lab.setAttribute("for", taId);
    head.appendChild(lab);
    var status = el("span", "notes-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    head.appendChild(status);
    var retry = el("button", "mini", "Retry");
    retry.type = "button";
    retry.hidden = true;
    head.appendChild(retry);
    host.appendChild(head);
    var ta = el("textarea", "notes-input");
    ta.id = taId;
    ta.maxLength = NOTES_CAP;
    ta.rows = 5;
    ta.spellcheck = true;
    ta.placeholder = "What tripped you up? The trick to remember next time…";
    ta.value = knownNotes(pid, initial, busyAtFetch);
    ta.setAttribute("aria-describedby", taId + "-count");
    host.appendChild(ta);
    var count = el("div", "notes-count");
    count.id = taId + "-count";
    host.appendChild(count);
    host.hidden = false;

    var queue = notesQueue(pid);
    var timer = null;
    var saved = ta.value;  // what the server holds, as far as this editor knows
    var queued = ta.value; // the newest text handed to the queue
    var dead = false;
    function setState(state, info) {
      status.textContent = core.notesStatus(state, info);
      status.className = "notes-status s-" + state;
      retry.hidden = state !== "error";
    }
    function setCount() {
      count.textContent = ta.value.length + " / " + NOTES_CAP;
      count.classList.toggle("near", ta.value.length > NOTES_CAP * 0.9);
    }
    function save(keepalive) {
      var wasDirty = !!timer;
      clearTimeout(timer);
      timer = null;
      if (dead) return;
      var text = ta.value;
      if (text === queued) { if (wasDirty && text === saved) setState("saved"); return; }
      queued = text;
      setState("saving");
      queue.push(text, keepalive, function (err, sentText) {
        if (err) {
          if (queued === text) queued = null; // Retry (or the next save) re-sends it
          if (dead) return;
          setState("error", { reason: err.message || "network error" });
          return;
        }
        saved = sentText;
        if (dead || timer) return; // newer typing waits for its own debounce
        if (ta.value === saved) setState("saved");
        else if (ta.value !== queued) save();
      });
    }
    ta.addEventListener("input", function () {
      setCount();
      setState("dirty");
      clearTimeout(timer);
      timer = setTimeout(save, NOTES_DEBOUNCE_MS);
    });
    ta.addEventListener("blur", function () { if (timer) save(); });
    retry.addEventListener("click", function () { save(); });
    setCount();
    setState(saved ? "saved" : "idle");
    return {
      // keepalive: the request outlives a closing tab (pagehide).
      flush: function (keepalive) { if (!dead) save(keepalive); },
      // Hands any unsent text to the queue (it is sent even after this),
      // then stops touching the DOM.
      destroy: function () {
        if (!dead) save(true);
        dead = true;
      },
      pid: pid,
    };
  }

  // ---- library viewer extras: Re-attempt, Flashcards (n), notes -----------
  var lvReattempt = $("library-viewer-reattempt");
  var lvCards = $("library-viewer-cards");
  var lvNotes = $("library-viewer-notes");
  var viewerNotes = null;
  var viewerToken = 0;
  function resetViewerExtras() {
    viewerToken++;
    if (viewerNotes) { viewerNotes.destroy(); viewerNotes = null; }
    if (lvNotes) { lvNotes.hidden = true; lvNotes.textContent = ""; }
    if (lvReattempt) lvReattempt.hidden = true;
    if (lvCards) lvCards.hidden = true;
  }
  function decorateViewer(relPath, meta) {
    resetViewerExtras();
    var token = viewerToken;
    var f = libByPath[relPath] || {};
    var pid = f.problem_id || "";
    if (pid && lvReattempt) lvReattempt.hidden = false;
    if (meta.ext === "md" && lvCards) {
      fetch("/flashcards?path=" + encodeURIComponent(relPath))
        .then(function (resp) { return resp.ok ? resp.json() : null; })
        .then(function (d) {
          if (token !== viewerToken || !d || !d.count) return;
          lvCards.textContent = "Flashcards (" + d.count + ")";
          lvCards.hidden = false;
        })
        .catch(function () { /* the button just stays hidden */ });
    }
    if (pid) loadViewerNotes(pid, token);
    syncFollowupBox(relPath, meta); // SP8 / D6
  }
  function loadViewerNotes(pid, token) {
    if (!lvNotes) return;
    var busy = notesBusy(pid);
    fetch("/problems/" + encodeURIComponent(pid))
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (rec) {
        if (token !== viewerToken) return;
        if (viewerNotes) viewerNotes.destroy();
        viewerNotes = makeNotesEditor(lvNotes, rec.id || pid, rec.notes || "",
          "Notes on " + problemLabelOf(rec), busy || notesBusy(rec.id || pid));
      })
      .catch(function () { /* no record: no notes editor */ });
  }
  // SP7 fix: the re-attempt view takes over this problem's notes - retire
  // the viewer's editor (its unsent text goes to the save queue) ...
  function retireViewerNotes() {
    if (!viewerNotes) return;
    viewerNotes.destroy();
    viewerNotes = null;
    viewerToken++; // a notes fetch still in flight must not rebuild it
    if (lvNotes) { lvNotes.hidden = true; lvNotes.textContent = ""; }
  }
  // ... and rebuild it from fresh notes when the Library shows again.
  function restoreViewerNotes() {
    if (viewerNotes || !currentViewPath) return;
    var f = libByPath[currentViewPath] || {};
    if (f.problem_id) loadViewerNotes(f.problem_id, viewerToken);
  }
  if (lvReattempt) {
    lvReattempt.addEventListener("click", function () {
      var f = currentViewPath && libByPath[currentViewPath];
      if (f && f.problem_id) openPractice(f.problem_id, { from: "library" });
    });
  }
  if (lvCards) {
    lvCards.addEventListener("click", function () {
      if (!currentViewPath) return;
      var t = fileMeta(currentViewPath).title;
      openFlashcards({ path: currentViewPath }, "From " + t, lvCards);
    });
  }
  function flushNotes() {
    if (viewerNotes) viewerNotes.flush(true);
    if (practiceNotes) practiceNotes.flush(true);
  }
  window.addEventListener("pagehide", function () {
    flushNotes();
    // The page is going away: a save waiting on an in-flight one goes now.
    Object.keys(notesQueues).forEach(function (pid) { notesQueues[pid].flushNow(); });
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flushNotes();
  });

  // ---- SP8 / D6: follow-up questions on the open doc ------------------------
  // POST /followup streams an answer (the study run's session resumed, or a
  // fresh call with the doc as context); the server appends it to the doc,
  // then the doc is re-rendered here. One follow-up at a time; it runs in its
  // own slot, so a study run may stream meanwhile. Opening another file or
  // closing the viewer stops it. Model text reaches the DOM only through the
  // hardened marked renderer; everything else is textContent.
  var fuBox = $("library-viewer-followup");
  var fuInput = $("followup-input");
  var fuAsk = $("followup-ask");
  var fuStop = $("followup-stop");
  var fuStatus = $("followup-status");
  var fuSource = $("followup-source");
  var fuAnswer = $("followup-answer");
  var FOLLOWUP_MAX = fuInput ? (parseInt(fuInput.getAttribute("maxlength"), 10) || 2000) : 2000;
  var followup = null;    // the follow-up in flight
  var fuShownPath = null; // the doc the box currently belongs to
  var fuRenderTimer = null;

  if (fuAnswer) fuAnswer.addEventListener("click", handleCopyCode);

  function fuSetBusy(busy) {
    if (!fuBox) return;
    fuAsk.disabled = busy;
    fuStop.hidden = !busy;
    fuStop.disabled = !busy;
    fuBox.setAttribute("aria-busy", busy ? "true" : "false");
  }
  function fuSetStatus(text, kind) {
    fuStatus.textContent = text || "";
    fuStatus.className = "fu-status" + (kind ? " " + kind : "");
  }
  function fuShowSource(src) {
    if (!src) { fuSource.hidden = true; return; }
    fuSource.textContent = src.label;
    fuSource.title = src.note || "";
    fuSource.className = "chip mono fu-source " + src.kind;
    fuSource.hidden = false;
  }
  function fuClearAnswer() {
    clearTimeout(fuRenderTimer);
    fuRenderTimer = null;
    fuAnswer.hidden = true;
    fuAnswer.textContent = "";
  }
  function fuRender(fu, final) {
    fuRenderTimer = null;
    if (fu.path !== fuShownPath || !fu.acc) return;
    fuAnswer.hidden = false;
    if (window.marked) {
      fuAnswer.innerHTML = marked.parse(fu.acc); // hardened renderer (marked.use above)
      var openLast = !final && core.hasOpenFence(fu.acc);
      highlightCode(fuAnswer, openLast);
      decorateCode(fuAnswer, openLast);
    } else {
      fuAnswer.textContent = fu.acc;
    }
  }
  function fuScheduleRender(fu) {
    if (fuRenderTimer) return;
    fuRenderTimer = setTimeout(function () { fuRender(fu, false); }, RENDER_INTERVAL_MS);
  }

  // Called for every file the viewer shows: the box is for Markdown docs, and
  // a different doc gets a clean box (a follow-up on the old one is stopped).
  function syncFollowupBox(relPath, meta) {
    if (!fuBox) return;
    if (followup && followup.path !== relPath) stopFollowup("switch");
    fuBox.hidden = meta.ext !== "md";
    if (fuShownPath === relPath) return; // a re-render of the same doc keeps its state
    fuShownPath = relPath;
    fuInput.value = "";
    fuSetStatus("");
    fuShowSource(null);
    fuClearAnswer();
  }
  function closeFollowupBox() {
    if (!fuBox) return;
    if (followup) stopFollowup("switch");
    fuShownPath = null;
    fuBox.hidden = true;
  }

  function fuFinish(fu, kind, data) {
    if (fu.finished) return;
    fu.finished = true;
    if (followup === fu) followup = null;
    fuSetBusy(false);
    var here = fu.path === fuShownPath;
    if (kind === "done") {
      var msg = core.followupDoneText(data);
      announce("Follow-up answered. " + msg);
      if (!here) { notify("Follow-up added to " + fu.path.split("/").pop() + ".", ""); return; }
      fuInput.value = "";
      fuClearAnswer();
      fuSetStatus(msg, "ok");
      // Re-render the doc so the appended section shows, and bring it into view.
      openFile(fu.path).then(function () {
        if (currentViewPath !== fu.path) return;
        var hs = libViewerBody.querySelectorAll("h2");
        var last = hs[hs.length - 1];
        if (last && last.scrollIntoView) last.scrollIntoView({ block: "start" });
      });
      return;
    }
    if (here) fuRender(fu, true);
    if (kind === "stopped") {
      if (fu.reason === "switch") return; // the learner moved on; nothing to say
      fuSetStatus("Stopped — nothing was added to the doc.", "");
      announce("Follow-up stopped. Nothing was added to the doc.");
      return;
    }
    var text = typeof data === "string" && data ? data : "The follow-up failed.";
    if (here) fuSetStatus(text + " Nothing was added to the doc.", "err");
    else notify(text, "error");
    announce("Follow-up failed. " + text);
  }

  // Stop / Esc: cancel on the server first; once it is appending to the doc
  // (too late) keep reading for `done`, otherwise abort at once.
  function stopFollowup(reason) {
    var fu = followup;
    if (!fu || fu.stopping) return;
    fu.stopping = true;
    fu.reason = reason || "stop";
    fuStop.disabled = true;
    if (fu.path === fuShownPath && reason !== "switch") fuSetStatus("Stopping…", "");
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, CANCEL_TIMEOUT_MS);
    fetch("/followup/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ followup_id: fu.id }),
      keepalive: true,
      signal: ctl.signal,
    }).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (d) {
        return core.cancelOutcome(r.status, d) === "committed";
      });
    }).catch(function () { return false; }).then(function (committed) {
      clearTimeout(timer);
      if (fu.finished) return;
      if (committed) {
        fu.stopping = false;
        if (fu.path === fuShownPath) fuSetStatus("Too late to stop — adding it to the doc…", "");
        return;
      }
      fu.stopped = true;
      fu.controller.abort();
      fuFinish(fu, "stopped");
    });
  }

  async function askFollowup() {
    if (!fuBox || followup || !currentViewPath) return;
    var check = core.followupCheck(fuInput.value, FOLLOWUP_MAX);
    if (!check.ok) { fuSetStatus(check.message, "err"); fuInput.focus(); return; }
    var fu = { id: newRunId(), path: currentViewPath, controller: new AbortController(),
      acc: "", stopping: false, stopped: false, finished: false, reason: "" };
    followup = fu;
    fuSetBusy(true);
    fuShowSource(null);
    fuClearAnswer();
    fuSetStatus("Asking…", "");
    announce("Asking a follow-up about this doc.");
    var body = { path: fu.path, question: check.question, followup_id: fu.id };
    var model = activeVal("model");
    if (model) body.model = model;
    try {
      var resp = await postJson("/followup", body, { signal: fu.controller.signal });
      if (fu.finished) return;
      if (!resp.ok) {
        var err = await resp.json().catch(function () { return {}; });
        var msg = err.error || "Request rejected (" + resp.status + ").";
        if (resp.status === 409) msg = "Another follow-up is still running. Stop it or wait for it first.";
        fuFinish(fu, "error", msg);
        return;
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var feed = makeSseParser(function (ev) {
        if (fu.finished) return;
        if (ev.type === "text") {
          fu.acc += typeof ev.data === "string" ? ev.data : String(ev.data);
          if (fu.path === fuShownPath) fuScheduleRender(fu);
          return;
        }
        if (ev.name === "phase") {
          var p = ev.data || {};
          var src = core.followupSource(p);
          if (src && fu.path === fuShownPath) {
            fuShowSource(src);
            fuSetStatus(src.kind === "resume" ? "Answering…" : "Answering — " + src.note + "…", "");
          } else if (p.phase === "saving" && fu.path === fuShownPath && !fu.stopping) {
            fuSetStatus("Adding the answer to the doc…", "");
          }
          return;
        }
        if (ev.name === "meta") return;
        var kind = core.runEventKind(ev.name);
        if (kind === "done") fuFinish(fu, "done", ev.data || {});
        else if (kind === "stopped") { fu.stopped = true; fuFinish(fu, "stopped"); }
        else if (kind === "error") {
          fuFinish(fu, "error", typeof ev.data === "string" ? ev.data : "The follow-up failed.");
        }
      });
      while (true) {
        var r = await reader.read();
        if (r.done) break;
        if (fu.finished) { try { reader.cancel(); } catch (e) { /* noop */ } break; }
        feed(decoder.decode(r.value, { stream: true }));
      }
      if (!fu.finished) {
        if (fu.stopped) fuFinish(fu, "stopped");
        else fuFinish(fu, "error", "The stream ended before the answer was saved.");
      }
    } catch (e) {
      if (e && e.name === "AbortError") {
        if (!fu.finished) fuFinish(fu, "stopped");
        return;
      }
      fuFinish(fu, "error", "Network error: " + ((e && e.message) || "the request failed") + ".");
    }
  }

  if (fuBox) {
    fuAsk.addEventListener("click", function () { askFollowup(); });
    fuStop.addEventListener("click", function () { stopFollowup("stop"); });
    fuInput.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === "Enter" || e.keyCode === 13)) {
        // Ctrl+Enter asks here (not the global Run shortcut).
        e.preventDefault();
        e.stopPropagation();
        if (core.isComposingEnter(e)) return;
        askFollowup();
      }
    });
    fuBox.addEventListener("keydown", function (e) {
      if ((e.key === "Escape" || e.keyCode === 27) && followup && followup.path === fuShownPath) {
        e.preventDefault();
        e.stopPropagation();
        stopFollowup("stop");
      }
    });
    window.addEventListener("pagehide", function () {
      if (followup) {
        postJson("/followup/cancel", { followup_id: followup.id }, { keepalive: true })
          .catch(function () {});
        followup.controller.abort();
      }
    });
  }

  // ---- D4: the review queue (Console "Due today" + Stats) ------------------
  var duePanel = $("due-panel");
  var dueList = $("due-list");
  var dueEmpty = $("due-empty");
  var dueCount = $("due-count");
  function loadReview() {
    return fetch("/review")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        lastReview = data || null;
        lastReviewDay = core.dayKey(new Date());
        renderDuePanel(lastReview);
        renderReviewStats(lastReview);
        renderPracticeDue(); // the open re-attempt view's "due …" label too
      })
      .catch(function () { /* the panel stays as it was */ });
  }
  // A tab that comes back on a later day (overnight, after sleep) refreshes
  // the queue: yesterday's "due tomorrow" is today's "due today".
  function refreshReviewIfNewDay() {
    if (lastReviewDay && lastReviewDay !== core.dayKey(new Date())) loadReview();
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refreshReviewIfNewDay();
  });
  window.addEventListener("focus", refreshReviewIfNewDay);
  function renderDuePanel(data) {
    if (!duePanel || !dueList) return;
    var counts = (data && data.counts) || {};
    var due = (data && data.due) || [];
    duePanel.hidden = !counts.scheduled && !due.length;
    if (dueCount) dueCount.textContent = String(due.length);
    $("due-title").setAttribute("aria-label", "Due today, " + core.plural(due.length, "problem"));
    dueList.textContent = "";
    dueEmpty.hidden = !!due.length;
    if (!due.length) {
      dueEmpty.textContent = "Nothing due — " + (data && data.next_due
        ? "the next review is " + core.dueLabel(data.next_due, data.today).replace(/^due /, "") + "."
        : "run a problem to schedule its first review.");
    }
    var MAX_SHOWN = 12;
    due.slice(0, MAX_SHOWN).forEach(function (item) {
      var li = el("div", "due-item");
      li.setAttribute("role", "listitem");
      var row = el("button", "due-row");
      row.type = "button";
      li.appendChild(row);
      var label = problemLabelOf(item);
      var when = core.dueLabel(item.due, data.today);
      row.setAttribute("aria-label", "Re-attempt " + label + " — " + when + ", box " + item.box);
      var diff = core.diffInfo(item.difficulty);
      var d = el("span", "diff " + (diff ? diff.cls : ""));
      var dot = el("span", "d");
      dot.setAttribute("aria-hidden", "true");
      d.appendChild(dot);
      d.appendChild(document.createTextNode(diff ? diff.label : "—"));
      row.appendChild(d);
      var name = el("span", "due-name");
      if (item.number != null) name.appendChild(el("span", "pnum", "#" + item.number));
      name.appendChild(document.createTextNode(item.title || item.id));
      row.appendChild(name);
      if (item.pattern) row.appendChild(chipEl(humanize(item.pattern), "mint"));
      row.appendChild(el("span", "due-when" + (item.overdue_days > 0 ? " overdue" : ""), when));
      row.appendChild(chipEl("box " + item.box, "mono"));
      row.addEventListener("click", function () { openPractice(item.id, { from: "console" }); });
      dueList.appendChild(li);
    });
    if (due.length > MAX_SHOWN) {
      dueList.appendChild(el("div", "due-more", "+ " + (due.length - MAX_SHOWN) + " more due"));
    }
  }
  function renderReviewStats(data) {
    if (!data) return;
    var counts = data.counts || {};
    setStatText("rv-due", counts.due || 0);
    setStatText("rv-today", counts.reviewed_today || 0);
    setStatText("rv-scheduled", counts.scheduled || 0);
    setStatText("rv-next", data.next_due ? core.dueLabel(data.next_due, data.today).replace(/^due /, "") : "—");
    var host = $("bd-box");
    if (!host) return;
    host.textContent = "";
    var byBox = counts.by_box || {};
    var max = 1;
    core.LEITNER_DAYS.forEach(function (_, i) { max = Math.max(max, byBox[String(i + 1)] || 0); });
    core.LEITNER_DAYS.forEach(function (days, i) {
      var n = byBox[String(i + 1)] || 0;
      var r = el("div", "bd-row");
      r.setAttribute("aria-label", "Box " + (i + 1) + " (every " + core.plural(days, "day") + "): " +
        core.plural(n, "problem"));
      r.appendChild(el("span", "bd-key", "Box " + (i + 1) + " · " + days + "d"));
      var track = el("span", "bd-track");
      track.setAttribute("aria-hidden", "true");
      var fill = el("span", "bd-fill");
      fill.style.width = (n ? Math.max(6, Math.round((n / max) * 100)) : 0) + "%";
      track.appendChild(fill);
      r.appendChild(track);
      r.appendChild(el("span", "bd-num", String(n)));
      host.appendChild(r);
    });
  }
  var dueCardsBtn = $("due-flashcards");
  if (dueCardsBtn) {
    dueCardsBtn.addEventListener("click", function () { openFlashcards({}, "Whole library", dueCardsBtn); });
  }
  var statsCardsBtn = $("stats-flashcards");
  if (statsCardsBtn) {
    statsCardsBtn.addEventListener("click", function () { openFlashcards({}, "Whole library", statsCardsBtn); });
  }

  // ---- D3: the re-attempt view ---------------------------------------------
  var prTitle = $("practice-title");
  var prSub = $("practice-sub");
  var prStatement = $("practice-statement");
  var prDue = $("practice-due");
  var prLang = $("practice-lang");
  var prCode = $("practice-code");
  var prStarter = $("practice-starter");
  var prSamples = $("practice-samples");
  var prCases = $("practice-cases");
  var prAddCase = $("practice-add-case");
  var prTest = $("practice-test");
  var prCancel = $("practice-cancel");
  var prResults = $("practice-results");
  var prGiveup = $("practice-giveup");
  var prGrade = $("practice-grade");
  var prGradeStatus = $("practice-grade-status");
  var prBox = $("practice-box");
  var prSolution = $("practice-solution");
  var prSolutionBody = $("practice-solution-body");
  var prSolutionPath = $("practice-solution-path");
  var prNotesHost = $("practice-notes");
  var practice = null; // { id, rec, from, testing, lastTest, peeked }
  var practiceNotes = null;
  var MAX_CASES = 10;
  var GRADE_LABELS = { solo: "Solved it solo", hints: "Needed hints", peeked: "Peeked at the solution" };

  function retirePracticeNotes() {
    if (practiceNotes) { practiceNotes.destroy(); practiceNotes = null; }
  }
  function draftKey(pid) { return "attempt." + pid; }
  function saveAttemptDraft() {
    if (!practice || !prCode) return;
    prefs.set(draftKey(practice.id), prCode.value);
    prefs.set(draftKey(practice.id) + ".lang", prLang ? prLang.value : "");
  }
  // SP7 fix: a test result belongs to the code + language it ran - once
  // either changes, the shown results and the summary a grade would carry
  // are stale, so both go (a run still in flight shows its result but is
  // not kept for the grade; see practiceTest).
  function invalidatePracticeTest() {
    if (!practice) return;
    practice.lastTest = null;
    if (!practice.testing && prResults && prResults.firstChild) {
      prResults.textContent = "";
      prResults.className = "pr-results";
    }
  }
  var attemptDraftTimer = null;
  attachCodeEditor(prCode, function () {
    invalidatePracticeTest();
    clearTimeout(attemptDraftTimer);
    attemptDraftTimer = setTimeout(saveAttemptDraft, 400);
  });
  window.addEventListener("pagehide", saveAttemptDraft);

  function renderGradeDescs() {
    var box = practice && practice.rec && practice.rec.review ? practice.rec.review.box : 1;
    if (prBox) prBox.textContent = "Box " + core.leitnerPreview(box, "hints").box + " of " + core.LEITNER_DAYS.length;
    if (!prGrade) return;
    prGrade.querySelectorAll("[data-grade]").forEach(function (b) {
      var g = b.getAttribute("data-grade");
      var p = core.leitnerPreview(box, g);
      var desc = b.querySelector("[data-grade-desc]");
      var text = p ? "→ box " + p.box + ", again in " + core.plural(p.days, "day") : "";
      if (desc) desc.textContent = text;
      b.setAttribute("aria-label", GRADE_LABELS[g] + ": " + text.replace("→ ", "moves to "));
      b.classList.toggle("suggest", g === "peeked" && !!(practice && practice.peeked));
    });
  }
  // Grade buttons: enabled once per re-attempt session (SP7 fix: grading
  // twice would move the Leitner box twice); a new session re-enables them.
  function setGradeButtons(on) {
    if (!prGrade) return;
    prGrade.querySelectorAll("[data-grade]").forEach(function (x) { x.disabled = !on; });
  }
  function renderPracticeDue() {
    if (!prDue) return;
    var rv = practice && practice.rec && practice.rec.review;
    prDue.textContent = rv ? core.dueLabel(rv.due, todayKey()) : "";
  }

  function addCaseRow(input, expected) {
    if (!prCases) return;
    if (prCases.children.length >= MAX_CASES) {
      notify("Up to " + MAX_CASES + " custom cases.", "");
      return;
    }
    var n = prCases.children.length + 1;
    var row = el("div", "pr-case");
    function field(lbl, cls, value, ph) {
      var wrap = el("label", "pr-case-f");
      wrap.appendChild(el("span", "pr-case-l", lbl));
      var ta = el("textarea", "pr-case-in " + cls);
      ta.rows = 2;
      ta.spellcheck = false;
      ta.maxLength = 10000;
      ta.placeholder = ph;
      ta.value = value || "";
      wrap.appendChild(ta);
      return wrap;
    }
    row.appendChild(field("Input (stdin)", "ci", input, "nums = [3,3], target = 6"));
    row.appendChild(field("Expected output (optional)", "ce", expected, "[0,1]"));
    var rm = el("button", "mini", "Remove");
    rm.type = "button";
    rm.setAttribute("aria-label", "Remove case " + n);
    rm.addEventListener("click", function () {
      var next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      renumberCases();
      var f = next ? next.querySelector("textarea") : prAddCase;
      if (f) f.focus();
    });
    row.appendChild(rm);
    prCases.appendChild(row);
    renumberCases();
    return row;
  }
  function renumberCases() {
    if (!prCases) return;
    Array.prototype.forEach.call(prCases.children, function (row, i) {
      row.setAttribute("aria-label", "Custom case " + (i + 1));
      var rm = row.querySelector("button");
      if (rm) rm.setAttribute("aria-label", "Remove case " + (i + 1));
    });
  }
  function collectCases() {
    if (!prCases) return [];
    var out = [];
    Array.prototype.forEach.call(prCases.children, function (row) {
      var ci = row.querySelector(".ci");
      var ce = row.querySelector(".ce");
      if (!ci || !ci.value.trim()) return; // an empty row is ignored
      var c = { input: ci.value };
      if (ce && ce.value.trim()) c.expected = ce.value;
      out.push(c);
    });
    return out;
  }
  if (prAddCase) {
    prAddCase.addEventListener("click", function () {
      var row = addCaseRow("", "");
      if (row) row.querySelector("textarea").focus();
    });
  }
  if (prStarter) {
    prStarter.addEventListener("click", function () {
      var lang = prLang ? prLang.value : "python";
      var starter = core.starterCode(lang);
      if (!starter) {
        notify("No starter for " + langLabel(lang) + " — only Python attempts can be tested here.", "");
        return;
      }
      var put = function () {
        prCode.value = starter;
        invalidatePracticeTest();
        saveAttemptDraft();
        prCode.focus();
      };
      if (prCode.value.trim()) {
        confirmDialog({
          title: "Replace your code with the starter?",
          lines: ["The starter reads one example's Input line and prints the answer."],
          note: "Your current code will be replaced.",
          ok: "Replace",
          returnFocus: prStarter,
        }).then(function (yes) { if (yes) put(); });
      } else put();
    });
  }
  if (prLang) {
    prLang.addEventListener("change", function () {
      invalidatePracticeTest();
      saveAttemptDraft();
    });
  }

  function openPractice(pid, opts) {
    opts = opts || {};
    // SP7 fix: retire the viewer's notes editor BEFORE the fetch, so its
    // unsent text is queued and the snapshot below knows a save is landing.
    retireViewerNotes();
    var notesWereBusy = notesBusy(pid);
    return fetch("/problems/" + encodeURIComponent(pid))
      .then(function (resp) {
        if (!resp.ok) throw new Error(resp.status === 404 ? "not found" : "HTTP " + resp.status);
        return resp.json();
      })
      .then(function (rec) {
        if (practice && practice.testing) cancelPracticeTest();
        retirePracticeNotes();
        var id = rec.id || pid;
        var from = opts.from || (currentView === "practice" && practice ? practice.from : currentView);
        practice = { id: id, rec: rec, from: from, testing: null, lastTest: null, peeked: false,
          graded: false };
        var label = problemLabelOf(rec);
        if (prTitle) prTitle.textContent = "Re-attempt: " + label;
        if (prSub) {
          var bits = [];
          var diff = core.diffInfo(rec.difficulty);
          if (diff) bits.push(diff.label);
          if (rec.pattern) bits.push(humanize(rec.pattern));
          bits.push("solve it from memory, test it, then grade yourself");
          prSub.textContent = bits.join(" · ");
        }
        if (prStatement) {
          prStatement.textContent = rec.statement ||
            "No statement was saved for this problem. Open its doc in the Library to read it.";
        }
        renderPracticeDue();
        var lang = prefs.get(draftKey(id) + ".lang", "") || activeVal("lang") || "python";
        if (prLang) prLang.value = /^(python|cpp|java)$/.test(lang) ? lang : "python";
        if (prCode) prCode.value = prefs.get(draftKey(id), "");
        if (prCases) prCases.textContent = "";
        if (prSamples) prSamples.checked = true;
        if (prResults) { prResults.textContent = ""; prResults.className = "pr-results"; }
        if (prGradeStatus) prGradeStatus.textContent = "";
        if (prSolution) prSolution.hidden = true;
        if (prSolutionBody) prSolutionBody.textContent = "";
        setPracticeTesting(false);
        setGradeButtons(true); // a new session may be graded (once)
        renderGradeDescs();
        if (prNotesHost) {
          practiceNotes = makeNotesEditor(prNotesHost, id, rec.notes || "", "Notes",
            notesWereBusy || notesBusy(id));
        }
        switchView("practice");
        if (contentEl) contentEl.scrollTop = 0;
        if (prCode) prCode.focus();
        announce("Re-attempt " + label + ". " + (prDue ? prDue.textContent : ""));
      })
      .catch(function (e) {
        if (currentView === "library") restoreViewerNotes(); // still on the Library
        notify("Could not open that problem (" + ((e && e.message) || "error") + ").", "error");
      });
  }

  function setPracticeTesting(on) {
    if (prTest) {
      prTest.disabled = !!on;
      prTest.textContent = on ? "Testing…" : "Test my code";
    }
    if (prCancel) { prCancel.hidden = !on; prCancel.disabled = false; }
    if (prResults) prResults.setAttribute("aria-busy", on ? "true" : "false");
  }
  function showPracticeMessage(text, kind) {
    if (!prResults) return;
    prResults.textContent = "";
    prResults.className = "pr-results" + (kind ? " " + kind : "");
    prResults.appendChild(el("div", "pr-msg", text));
  }
  function pre(label, text, cls) {
    var wrap = el("div", "pr-io" + (cls ? " " + cls : ""));
    wrap.appendChild(el("span", "pr-io-l", label));
    var p = el("pre", "pr-io-v");
    p.textContent = text === "" ? "(empty)" : text;
    wrap.appendChild(p);
    return wrap;
  }
  function renderPracticeResults(data) {
    if (!prResults) return;
    prResults.textContent = "";
    var overall = core.caseInfo(data.status);
    prResults.className = "pr-results r-" + overall.cls;
    var head = el("div", "pr-sum");
    head.appendChild(el("span", "pr-glyph", overall.glyph));
    head.appendChild(el("span", "pr-sum-t", (data.status === "pass" ? "All passed" : overall.label) +
      " — " + (data.summary || "")));
    if (data.cancelled) head.appendChild(chipEl("cancelled", "amber"));
    prResults.appendChild(head);
    var counters = { sample: 0, custom: 0 };
    (data.cases || []).forEach(function (c) {
      var info = core.caseInfo(c.status);
      var name = c.source === "sample" ? "Example " + (++counters.sample) : "Case " + (++counters.custom);
      var d = el("details", "pr-case-r c-" + info.cls);
      if (c.status !== "pass") d.open = true;
      var s = el("summary", "pr-case-s");
      s.appendChild(el("span", "pr-glyph", info.glyph));
      s.appendChild(el("span", "pr-case-n", name));
      s.appendChild(el("span", "pr-case-st", info.label));
      d.appendChild(s);
      d.appendChild(pre("Input", c.input || ""));
      if (c.expected != null) d.appendChild(pre("Expected", c.expected));
      d.appendChild(pre("Your output", c.got || "", c.status === "fail" ? "bad" : ""));
      if (c.stderr) d.appendChild(pre("stderr", c.stderr, "err"));
      if (c.note && c.status !== "pass") d.appendChild(el("div", "pr-note", c.note));
      prResults.appendChild(d);
    });
  }
  function practiceTest() {
    if (!practice || practice.testing || !prCode) return;
    var code = prCode.value;
    var language = prLang ? prLang.value : "python";
    if (language === "python" && !code.trim()) {
      notify("Write some code first.", "");
      prCode.focus();
      return;
    }
    var testId = newRunId();
    var p = practice;
    p.testing = testId;
    setPracticeTesting(true);
    showPracticeMessage("Running your code in the sandbox…", "busy");
    postJson("/attempt/test", {
      problem_id: p.id, code: code, language: language, cases: collectCases(),
      include_samples: prSamples ? prSamples.checked : true, test_id: testId,
    }).then(function (resp) {
      if (!resp.ok) {
        return errorOf(resp).then(function (why) {
          if (resp.status === 409) why = "A test run is already in progress — wait for it to finish.";
          throw new Error(why);
        });
      }
      return resp.json();
    }).then(function (data) {
      if (practice !== p) return;
      if (!data.supported) {
        showPracticeMessage(data.message || "Running this language is not supported yet.", "r-warn");
        announce(data.message || "Not supported yet.");
        return;
      }
      renderPracticeResults(data);
      var unchanged = prCode.value === code && (!prLang || prLang.value === language);
      if (data.total > 0 && !data.cancelled && unchanged) {
        p.lastTest = { language: language, passed: data.passed, total: data.total };
      }
      announce("Tests finished: " + (data.summary || core.caseInfo(data.status).label) + ".");
    }).catch(function (e) {
      if (practice !== p) return;
      var msg = (e && e.message) || "the request failed";
      showPracticeMessage("Could not test the code: " + msg, "r-fail");
      announce("Could not test the code: " + msg);
    }).then(function () {
      if (practice !== p) return;
      p.testing = null;
      setPracticeTesting(false);
    });
  }
  function cancelPracticeTest() {
    if (!practice || !practice.testing) return;
    if (prCancel) prCancel.disabled = true;
    postJson("/attempt/cancel", { test_id: practice.testing }).catch(function () { /* best effort */ });
  }
  if (prTest) prTest.addEventListener("click", practiceTest);
  if (prCancel) prCancel.addEventListener("click", cancelPracticeTest);

  if (prGiveup) {
    prGiveup.addEventListener("click", function () {
      if (!practice) return;
      var p = practice;
      confirmDialog({
        title: "Show the solution?",
        lines: ["Opens the latest saved doc for " + problemLabelOf(p.rec) + " below your code."],
        note: "Then grade it honestly: “Peeked at the solution” sends it back to box 1.",
        ok: "Show the solution",
        tone: "primary",
        returnFocus: prGiveup,
      }).then(function (yes) {
        if (!yes || practice !== p) return;
        // SP8 fix M7: skip docs no longer in the library listing, and fall
        // back to the next candidate when one 404s (deleted since the listing).
        var candidates = core.docCandidatesFor(p.rec, libListed ? libByPath : null);
        var NO_SOLUTION = "No saved solution exists for this problem - its saved docs were deleted " +
          "or it has none yet.";
        var path = "";
        function fetchNext(i) {
          if (i >= candidates.length) return Promise.resolve(null);
          return fetch("/library/file?path=" + encodeURIComponent(candidates[i]))
            .then(function (resp) {
              if (resp.status === 404) return fetchNext(i + 1);
              if (!resp.ok) throw new Error("HTTP " + resp.status);
              path = candidates[i];
              return resp.text();
            });
        }
        fetchNext(0)
          .then(function (text) {
            if (practice !== p) return;
            if (text === null) { notify(NO_SOLUTION); announce(NO_SOLUTION); return; }
            p.peeked = true;
            renderGradeDescs();
            if (prSolutionPath) prSolutionPath.textContent = path;
            prSolutionBody.textContent = "";
            prSolutionBody.innerHTML = window.marked ? marked.parse(text) : ""; // hardened renderer
            if (!window.marked) {
              var pr = el("pre");
              pr.textContent = text;
              prSolutionBody.appendChild(pr);
            }
            highlightCode(prSolutionBody, false);
            decorateCode(prSolutionBody, false);
            prSolution.hidden = false;
            var h = $("practice-solution-h");
            if (h) { h.tabIndex = -1; h.focus(); }
            announce("Solution shown.");
          })
          .catch(function (e) {
            notify("Could not open the solution (" + ((e && e.message) || "error") + ").", "error");
          });
      });
    });
  }

  if (prGrade) {
    prGrade.addEventListener("click", function (e) {
      var b = e.target.closest("[data-grade]");
      if (!b || !practice || b.disabled || practice.graded) return;
      var p = practice;
      var grade = b.getAttribute("data-grade");
      var body = { grade: grade };
      if (p.lastTest) body.attempt = p.lastTest;
      setGradeButtons(false);
      prGradeStatus.textContent = "Saving your grade…";
      postJson("/problems/" + encodeURIComponent(p.id) + "/grade", body)
        .then(function (resp) {
          if (!resp.ok) return errorOf(resp).then(function (why) { throw new Error(why); });
          return resp.json();
        })
        .then(function (res) {
          if (practice !== p) return;
          p.graded = true; // SP7 fix: a second click would move the box again
          p.rec.review = res.review;
          var when = core.dueLabel(res.review.due, todayKey());
          var msg = "Graded “" + GRADE_LABELS[grade] + "” — box " + res.review.box + ", next review " +
            when.replace(/^due /, "") + " (" + res.review.due + ").";
          prGradeStatus.textContent = msg;
          renderPracticeDue();
          renderGradeDescs();
          notify("Review scheduled: " + when + ".", "");
          loadReview();
          loadStats();
        })
        .catch(function (err) {
          var why = (err && err.message) || "network error";
          if (practice === p) prGradeStatus.textContent = "Could not save the grade (" + why + "). Try again.";
          notify("Could not save the grade (" + why + ").", "error");
        })
        .then(function () { if (practice === p && !p.graded) setGradeButtons(true); });
    });
  }
  var prBack = $("practice-back");
  if (prBack) {
    prBack.addEventListener("click", function () {
      var to = practice && practice.from && practice.from !== "practice" ? practice.from : "console";
      switchView(to);
    });
  }

  // ---- D10: the flashcards modal (flip review) -----------------------------
  var fcModal = $("flashcards-modal");
  var fcDeck = { cards: [], i: 0, flipped: false };
  function fcQuery(scope) {
    var q = [];
    if (scope.problem_id) q.push("problem_id=" + encodeURIComponent(scope.problem_id));
    if (scope.path) q.push("path=" + encodeURIComponent(scope.path));
    return q.length ? "?" + q.join("&") : "";
  }
  function renderCard() {
    var n = fcDeck.cards.length;
    var empty = $("fc-empty");
    var card = $("fc-card");
    var flip = $("fc-flip");
    ["fc-prev", "fc-next", "fc-flip"].forEach(function (id) { $(id).disabled = n === 0; });
    $("fc-prev").disabled = n < 2;
    $("fc-next").disabled = n < 2;
    if (!n) {
      empty.hidden = false;
      empty.textContent = "No flashcards yet — every study doc's “Flashcards” section shows up here.";
      card.hidden = true;
      $("fc-count").textContent = "";
      return;
    }
    empty.hidden = true;
    card.hidden = false;
    var c = fcDeck.cards[fcDeck.i];
    $("fc-q").textContent = c.q;
    $("fc-a").textContent = c.a;
    $("fc-answer").hidden = !fcDeck.flipped;
    flip.textContent = fcDeck.flipped ? "Hide answer" : "Show answer";
    flip.setAttribute("aria-expanded", fcDeck.flipped ? "true" : "false");
    var src = $("fc-src");
    src.textContent = "";
    var label = core.problemTitle({ title: c.title, number: c.number, stem: "" }).label;
    src.appendChild(document.createTextNode("From " + (label || c.path)));
    if (c.pattern) src.appendChild(chipEl(humanize(c.pattern), "mint"));
    src.title = c.path;
    $("fc-count").textContent = (fcDeck.i + 1) + " / " + n;
  }
  function fcLive(msg) {
    var live = $("fc-live");
    if (!live) return;
    live.textContent = "";
    setTimeout(function () { live.textContent = msg; }, 30);
  }
  function fcMove(step) {
    var n = fcDeck.cards.length;
    if (n < 2) return;
    fcDeck.i = core.wrapIndex(fcDeck.i, n, step);
    fcDeck.flipped = false;
    renderCard();
    fcLive("Card " + (fcDeck.i + 1) + " of " + n + ". " + fcDeck.cards[fcDeck.i].q);
  }
  function fcFlip() {
    if (!fcDeck.cards.length) return;
    fcDeck.flipped = !fcDeck.flipped;
    renderCard();
    if (fcDeck.flipped) fcLive("Answer: " + fcDeck.cards[fcDeck.i].a);
  }
  function openFlashcards(scope, label, trigger) {
    if (!fcModal) return;
    closeSearch();
    closeShortcuts();
    fetch("/flashcards" + fcQuery(scope || {}))
      .then(function (resp) {
        if (!resp.ok) return errorOf(resp).then(function (why) { throw new Error(why); });
        return resp.json();
      })
      .then(function (data) {
        fcDeck = { cards: (data && data.cards) || [], i: 0, flipped: false };
        $("fc-scope").textContent = label ? label + " · " + core.plural(fcDeck.cards.length, "card") : "";
        $("fc-download").setAttribute("href", "/flashcards.tsv" + fcQuery(scope || {}));
        $("fc-download").hidden = !fcDeck.cards.length;
        renderCard();
        if (fcModal.hidden) overlayReturnFocus = trigger || document.activeElement;
        fcModal.hidden = false;
        var flip = $("fc-flip");
        if (flip && !flip.disabled) flip.focus(); else $("fc-close").focus();
        fcLive(fcDeck.cards.length
          ? "Flashcards: " + core.plural(fcDeck.cards.length, "card") + ". Card 1: " + fcDeck.cards[0].q
          : "No flashcards yet.");
      })
      .catch(function (e) {
        notify("Could not load the flashcards (" + ((e && e.message) || "error") + ").", "error");
      });
  }
  function closeFlashcards() {
    if (!fcModal || fcModal.hidden) return;
    fcModal.hidden = true;
    restoreOverlayFocus();
  }
  if (fcModal) {
    $("fc-close").addEventListener("click", closeFlashcards);
    $("fc-flip").addEventListener("click", fcFlip);
    $("fc-prev").addEventListener("click", function () { fcMove(-1); });
    $("fc-next").addEventListener("click", function () { fcMove(1); });
    fcModal.addEventListener("click", function (e) { if (e.target === fcModal) closeFlashcards(); });
    fcModal.addEventListener("keydown", function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      var onControl = e.target && /^(BUTTON|A)$/.test(e.target.tagName || "");
      if (e.key === "ArrowLeft") { e.preventDefault(); fcMove(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); fcMove(1); }
      else if ((e.key === " " || e.key === "Enter") && !onControl) { e.preventDefault(); fcFlip(); }
    });
  }

  // ---- global overlay keys: Esc, focus trap, ⌘/Ctrl+K, "?" ------------------
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" || e.keyCode === 27) {
      if (confirmModal && !confirmModal.hidden) { e.preventDefault(); closeConfirm(false); return; }
      if (fcModal && !fcModal.hidden) { e.preventDefault(); closeFlashcards(); return; }
      if (searchPalette && !searchPalette.hidden) { e.preventDefault(); closeSearch(); return; }
      if (shortcutsModal && !shortcutsModal.hidden) { e.preventDefault(); closeShortcuts(); return; }
      if (drawerOpen()) { e.preventDefault(); closeDrawer(); return; }
      return;
    }
    if (e.key === "Tab") {
      var ov = openOverlay();
      if (ov) { trapTab(e, ov.querySelector('[role="dialog"], [role="alertdialog"]') || ov); return; }
      if (drawerOpen()) { trapTab(e, sidebarEl); return; }
      return;
    }
    if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === "k" || e.key === "K")) {
      if (confirmModal && !confirmModal.hidden) return;
      if (fcModal && !fcModal.hidden) return;
      e.preventDefault();
      openSearch();
      return;
    }
    if (e.key === "?" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (isTypingTarget(e.target) || isTypingTarget(document.activeElement)) return;
      if (confirmModal && !confirmModal.hidden) return;
      if (fcModal && !fcModal.hidden) return;
      e.preventDefault();
      openShortcuts();
    }
  });

  // ---- boot ---------------------------------------------------------------
  enterIdle();
  loadLibrary(); // stats / streak render once it answers (no empty flash)
  loadReview();  // SP7 / D4: the Console "Due today" panel
})();
