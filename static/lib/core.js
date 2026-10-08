/* LeetCoach core helpers (SP5): pure functions shared by static/app.js and
 * the zero-dependency node tests in tests/js/.
 *
 * No DOM, no network, no globals beyond the export below. Loaded in the page
 * with a plain <script src> (strict CSP: script-src 'self', no bundler) and
 * exposed as window.LeetCoachCore; in node `require()` returns the same object
 * through the UMD-style guard at the bottom of this file.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  } else {
    root.LeetCoachCore = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---- strings ------------------------------------------------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  // marked hands an angle-bracket autolink's href over already entity-escaped
  // (`<https://x/?a&b>` -> "https://x/?a&amp;b"); escaping it again produced a
  // broken "&amp;amp;" URL (C10). Decode the basic entities first.
  function unescapeEntities(s) {
    return String(s)
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#0*39;/g, "'")
      .replace(/&#x0*27;/gi, "'")
      .replace(/&amp;/g, "&");
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
  function modeLabel(folder) {
    var m = { answers: "Answer", answer: "Answer", learning: "Learning", guided: "Guided",
      reviews: "Code Review", review: "Code Review" };
    return m[folder] || humanize(folder);
  }
  // Accepts a file extension (py/cpp/java) or a wire language (python/cpp/java).
  function langLabel(ext) {
    var m = { py: "Python", python: "Python", cpp: "C++", java: "Java" };
    return m[ext] || "—";
  }
  var LANGUAGES = ["python", "cpp", "java"];
  function otherLanguages(lang) {
    return LANGUAGES.filter(function (l) { return l !== lang; });
  }

  // ---- time -----------------------------------------------------------------
  function relTime(mtime, nowMs) {
    if (!mtime) return "";
    var d = (nowMs == null ? Date.now() : nowMs) / 1000 - mtime;
    if (d < 45) return "now";
    if (d < 3600) return Math.max(1, Math.round(d / 60)) + "m";
    if (d < 86400) return Math.round(d / 3600) + "h";
    if (d < 7 * 86400) return Math.round(d / 86400) + "d";
    try {
      return new Date(mtime * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch (e) { return ""; }
  }
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
    if (ss === 60) { m += 1; ss = 0; }
    return m + ":" + (ss < 10 ? "0" : "") + ss;
  }
  // Local YYYY-MM-DD. NOT toISOString (UTC would misbucket near midnight).
  function dayKey(date) {
    var y = date.getFullYear();
    var m = date.getMonth() + 1;
    var d = date.getDate();
    return y + "-" + (m < 10 ? "0" : "") + m + "-" + (d < 10 ? "0" : "") + d;
  }
  function parseDayKey(k) {
    var p = String(k).split("-");
    return new Date(+p[0], (+p[1]) - 1, +p[2]);
  }

  // ---- model ids (SP5 model chip) -------------------------------------------
  // "claude-opus-5-5" -> "Opus 5.5"; "claude-sonnet-4-5-20250929" -> "Sonnet
  // 4.5"; "claude-3-5-haiku-20241022" -> "Haiku 3.5"; "opus" -> "Opus";
  // anything else is shown as-is (the raw id is the honest fallback).
  var FAMILIES = { fable: "Fable", opus: "Opus", sonnet: "Sonnet", haiku: "Haiku" };
  function modelLabel(id) {
    var raw = String(id || "").trim();
    if (!raw) return "";
    var s = raw.toLowerCase().replace(/\[[^\]]*\]$/, "");
    if (FAMILIES[s]) return FAMILIES[s];
    var m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(s);
    if (m && FAMILIES[m[1]]) return FAMILIES[m[1]] + " " + m[2] + (m[3] ? "." + m[3] : "");
    m = /^claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)(?:-\d{8})?$/.exec(s);
    if (m && FAMILIES[m[3]]) return FAMILIES[m[3]] + " " + m[1] + (m[2] ? "." + m[2] : "");
    return raw;
  }

  // ---- run lifecycle ----------------------------------------------------------
  // B14: a 409 ("identical run already in progress") right after Stop is the
  // server still tearing the old run down. Retry with doubling backoff.
  var RETRY_DELAYS = [400, 800, 1600, 3200];
  function retryDelay(attempt) {
    return attempt >= 0 && attempt < RETRY_DELAYS.length ? RETRY_DELAYS[attempt] : -1;
  }

  // SP5 fix R8: the /run response decision - ms to wait before re-POSTing,
  // or -1 to hand `status` to the caller. Only a 409 is retried, and only
  // while the run still owns the UI.
  function runRetryDelay(status, attempt, isCurrent) {
    if (status !== 409 || !isCurrent) return -1;
    return retryDelay(attempt);
  }

  // SP5 fix R8: what a POST /run/cancel answer means for Stop. A 200
  // {"cancelled": false} is the server saying the run already committed to
  // saving (SP4 M1) - keep reading for its `done`. Anything else (cancelled,
  // unknown run, network failure) means the run is over: show it as stopped.
  function cancelOutcome(status, body) {
    return status === 200 && !!body && body.cancelled === false ? "committed" : "cancelled";
  }

  // SP5 fix R8/B2: the finish kind for a terminal SSE event name, or null for
  // a non-terminal / unknown one. A `cancelled` event is a user Stop the
  // server honoured - the neutral "stopped" state, never "Run failed".
  function runEventKind(name) {
    if (name === "done") return "done";
    if (name === "error") return "error";
    if (name === "cancelled") return "stopped";
    return null;
  }

  // SP5 fix R4: would replacing the editor's text with a run's captured
  // problem lose a DIFFERENT, non-empty draft? (Whitespace-only differences
  // and an empty editor never need asking.)
  function needsReplaceConfirm(current, incoming) {
    var cur = String(current || "").trim();
    var inc = String(incoming || "").trim();
    return !!cur && !!inc && cur !== inc;
  }

  // D11: human text for an SSE `phase` payload.
  function phaseText(p) {
    p = p || {};
    if (p.phase === "verifying") {
      if (p.i && p.n) {
        return { label: "Verifying " + p.i + "/" + p.n,
                 note: "verifying sample " + p.i + " of " + p.n + "…" };
      }
      return { label: "Verifying", note: "verifying against the samples…" };
    }
    if (p.phase === "saving") return { label: "Saving", note: "saving to your library…" };
    return { label: "Streaming", note: "streaming…" };
  }

  // B16: is the markdown currently inside an unclosed ``` / ~~~ fence? (The
  // last code block is then partial: not highlighted, marked `.partial`.)
  //
  // SP5 fix R3: a fence may be indented (a code block inside a list item,
  // "1. step\n\n    ```python"), like parsing.py's indentation-aware match.
  // A closing fence counts when it is indented at most 3 columns past its
  // opener (or less - a dedent ends the list item, and the block with it);
  // a ``` indented further is code INSIDE the block (e.g. a docstring example).
  function hasOpenFence(md) {
    var lines = String(md || "").replace(/\r\n?/g, "\n").split("\n");
    var open = null; // { ch, len, indent }
    for (var i = 0; i < lines.length; i++) {
      var m = /^([ \t]*)(`{3,}|~{3,})(.*)$/.exec(lines[i]);
      if (!m) continue;
      var indent = m[1].replace(/\t/g, "    ").length;
      var ch = m[2].charAt(0);
      var len = m[2].length;
      if (!open) {
        if (ch === "`" && m[3].indexOf("`") !== -1) continue; // not a fence
        open = { ch: ch, len: len, indent: indent };
      } else if (ch === open.ch && len >= open.len && !m[3].trim() &&
                 indent <= open.indent + 3) {
        open = null;
      }
    }
    return open !== null;
  }

  // B16: ms to wait before the next throttled render (0 = render now).
  function throttleDelay(lastMs, nowMs, interval) {
    if (!lastMs) return 0;
    var wait = interval - (nowMs - lastMs);
    return wait > 0 ? wait : 0;
  }

  // D11: is a scroll container (close enough to) the bottom?
  function isNearBottom(box, threshold) {
    if (!box) return true;
    threshold = threshold == null ? 80 : threshold;
    return box.scrollHeight - (box.scrollTop + box.clientHeight) <= threshold;
  }

  // D11: is a live edge (the bottom of the streamed output, in viewport px)
  // within `threshold` px of the visible bottom of its scroll container?
  function nearEdge(edgeBottom, viewBottom, threshold) {
    threshold = threshold == null ? 80 : threshold;
    return edgeBottom - viewBottom <= threshold;
  }

  // D7: the library-relative path ("answers/x/y.md") of an absolute saved
  // path from the `done` payload, matched against the /library listing.
  function libRelPath(abs, files) {
    var a = String(abs || "").replace(/\\/g, "/");
    var best = "";
    (files || []).forEach(function (f) {
      var p = String((f && f.path) || f || "");
      if (!p) return;
      var hit = a === p || a.slice(-(p.length + 1)) === "/" + p;
      if (hit && p.length > best.length) best = p;
    });
    return best;
  }

  // ---- verdicts (B18) ---------------------------------------------------------
  // From the sandbox verdict line ("✓ Sample tests PASS (...)") - mirrors
  // app.verdict_from_text on the server.
  function verdictFromLine(line) {
    var s = String(line || "");
    if (!s) return "";
    var m = /Sample tests (PASS|FAIL|ERROR)\b/.exec(s);
    return m ? m[1].toLowerCase() : "not_verified";
  }
  function verdictInfo(v) {
    switch (v) {
      case "pass": return { cls: "pass", glyph: "✓", label: "Samples passed" };
      case "fail": return { cls: "fail", glyph: "✗", label: "Samples failed" };
      case "error": return { cls: "fail", glyph: "✗", label: "Code errored on the samples" };
      case "not_verified": return { cls: "warn", glyph: "!", label: "Not auto-verified" };
      default: return { cls: "none", glyph: "—", label: "No verification (study notes)" };
    }
  }

  // ---- library-derived runs (Cycle 10; moved here for tests) ------------------
  var CODE_EXT = { py: 1, cpp: 1, cc: 1, cxx: 1, c: 1, java: 1, cs: 1, js: 1, ts: 1, go: 1, rs: 1, rb: 1, kt: 1, swift: 1 };

  // "<stem>[__<tier>][__<n>]" -> parts. A trailing "__<digits>" is the
  // storage SLOT (a re-run that did not overwrite), never a tier (A8).
  var SLOT_RE = /__(\d+)$/;
  function splitRunName(base) {
    var rest = String(base || "");
    var slot = 0;
    var m = SLOT_RE.exec(rest);
    if (m) { slot = +m[1]; rest = rest.slice(0, m.index); }
    var us = rest.indexOf("__");
    return {
      stem: us === -1 ? rest : rest.slice(0, us),
      tier: us === -1 ? "" : rest.slice(us + 2),
      slot: slot,
    };
  }

  // Group files into runs by (mode-folder, topic, base name). SP6/A8: one
  // run per saved run - each tier and each "__N" slot is its own run; only an
  // Answer's .md + code file (same base name) share one.
  function deriveRuns(files) {
    var map = {};
    var order = [];
    (files || []).forEach(function (f) {
      var parts = f.path.split("/");
      if (parts.length < 3) return; // only <mode>/<topic>/<file> entries are runs
      var modeFolder = parts[0];
      var topicSeg = parts[1];
      var fname = parts[parts.length - 1];
      var dot = fname.lastIndexOf(".");
      var ext = dot === -1 ? "" : fname.slice(dot + 1).toLowerCase();
      var base = dot === -1 ? fname : fname.slice(0, dot);
      var nm = splitRunName(base);
      var stem = nm.stem;
      var tier = nm.tier;
      // learning writes into "<type>_learning" — drop the mode artifact.
      var topicRaw = topicSeg.replace(/_learning$/, "");
      var key = modeFolder + "|" + parts.slice(1, -1).join("/") + "|" + base;
      var run = map[key];
      if (!run) {
        run = {
          modeFolder: modeFolder, topicRaw: topicRaw, stemRaw: stem, slot: nm.slot,
          tier: "", langExt: "", savedAt: 0, mdPath: "", mdAt: 0, verdict: "",
          difficulty: "", title: "", number: null, files: [],
        };
        map[key] = run;
        order.push(run);
      }
      run.files.push(f);
      var mt = typeof f.mtime === "number" ? f.mtime : 0;
      if (mt > run.savedAt) run.savedAt = mt;
      if (ext === "md") {
        // the newest doc represents the run (and carries its verdict)
        if (!run.mdPath || mt > run.mdAt) {
          run.mdPath = f.path;
          run.mdAt = mt;
          run.verdict = f.verdict || "";
        }
      } else if (CODE_EXT[ext] && !run.langExt) run.langExt = ext;
      if (tier && !run.tier) run.tier = tier;
      if (f.difficulty && !run.difficulty) run.difficulty = String(f.difficulty);
      if (f.title && !run.title) run.title = String(f.title);
      if (typeof f.number === "number" && run.number === null) run.number = f.number;
    });
    return order.map(function (run) {
      var pt = problemTitle({ title: run.title, number: run.number, stem: run.stemRaw });
      return {
        modeFolder: run.modeFolder,
        mode: modeLabel(run.modeFolder),
        topicRaw: run.topicRaw,
        topic: humanize(run.topicRaw),
        stemRaw: run.stemRaw,
        problem: pt.title,
        number: pt.number,
        problemLabel: pt.label,
        tier: run.tier,
        slot: run.slot,
        difficulty: run.difficulty,
        language: langLabel(run.langExt),
        langExt: run.langExt,
        savedAt: run.savedAt,
        mdPath: run.mdPath,
        verdict: run.verdict,
        files: run.files,
      };
    });
  }

  // B19: the files `DELETE /library/file?scope=run` removes for `path` - its
  // same-name siblings in the same folder (mirrors RUN_SIBLING_EXTENSIONS).
  var RUN_SIBLING_EXT = { md: 1, py: 1, cpp: 1, java: 1, txt: 1 };
  function runSiblings(files, path) {
    var p = String(path || "");
    var slash = p.lastIndexOf("/");
    var dir = slash === -1 ? "" : p.slice(0, slash);
    var name = p.slice(slash + 1);
    var dot = name.lastIndexOf(".");
    var base = dot === -1 ? name : name.slice(0, dot);
    var out = [];
    (files || []).forEach(function (f) {
      var fp = String(f.path || f);
      var s = fp.lastIndexOf("/");
      if ((s === -1 ? "" : fp.slice(0, s)) !== dir) return;
      var n = fp.slice(s + 1);
      var d = n.lastIndexOf(".");
      if (d === -1 || n.slice(0, d) !== base) return;
      if (fp === p || RUN_SIBLING_EXT[n.slice(d + 1).toLowerCase()]) out.push(fp);
    });
    if (out.indexOf(p) === -1 && p) out.unshift(p);
    return out;
  }

  // ---- stats (Cycle 10; moved here for tests) ---------------------------------
  function computeStats(runs, now) {
    now = now || new Date();
    runs = runs || [];
    var probSet = {};
    var byMode = {};
    var byLanguage = {};
    var byTopic = {};
    var dayCounts = {};
    runs.forEach(function (run) {
      var title = run.problem || run.stemRaw || "";
      if (title) probSet[title] = true;
      var mode = run.mode || "Other";
      byMode[mode] = (byMode[mode] || 0) + 1;
      var lang = (run.language && run.language !== "—") ? run.language : "Unknown";
      byLanguage[lang] = (byLanguage[lang] || 0) + 1;
      var topic = run.topic || "Uncategorized";
      byTopic[topic] = (byTopic[topic] || 0) + 1;
      if (typeof run.savedAt === "number" && run.savedAt > 0) {
        var k = dayKey(new Date(run.savedAt * 1000));
        dayCounts[k] = (dayCounts[k] || 0) + 1;
      }
    });
    var today = dayCounts[dayKey(now)] || 0;
    var thisWeek = 0;
    for (var i = 0; i < 7; i++) {
      thisWeek += dayCounts[dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - i))] || 0;
    }
    var currentStreak = 0;
    var cursor = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (!dayCounts[dayKey(cursor)]) {
      cursor = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
      if (!dayCounts[dayKey(cursor)]) cursor = null;
    }
    while (cursor && dayCounts[dayKey(cursor)]) {
      currentStreak++;
      cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() - 1);
    }
    var days = Object.keys(dayCounts).sort();
    var longestStreak = 0;
    var runLen = 0;
    var prev = null;
    days.forEach(function (k) {
      if (prev === null) runLen = 1;
      else {
        var diff = Math.round((parseDayKey(k) - parseDayKey(prev)) / 86400000);
        runLen = diff === 1 ? runLen + 1 : 1;
      }
      if (runLen > longestStreak) longestStreak = runLen;
      prev = k;
    });
    var heatmap = [];
    for (var j = 118; j >= 0; j--) {
      var hk = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - j));
      heatmap.push({ date: hk, count: dayCounts[hk] || 0 });
    }
    return {
      total: runs.length,
      distinctProblems: Object.keys(probSet).length,
      today: today,
      thisWeek: thisWeek,
      currentStreak: currentStreak,
      longestStreak: longestStreak,
      byMode: byMode,
      byLanguage: byLanguage,
      byTopic: byTopic,
      heatmap: heatmap,
    };
  }
  function heatBucket(c) {
    if (c <= 0) return 0;
    if (c === 1) return 1;
    if (c === 2) return 2;
    if (c <= 4) return 3;
    return 4;
  }

  // ---- difficulty (SP6 / D1: the Diff column) ------------------------------------
  var DIFF = { easy: { cls: "easy", label: "Easy" }, medium: { cls: "med", label: "Medium" },
               hard: { cls: "hard", label: "Hard" } };
  function diffInfo(d) {
    var hit = DIFF[String(d || "").trim().toLowerCase()];
    return hit ? { cls: hit.cls, label: hit.label } : null;
  }

  // ---- click-to-reveal (SP6 / D2) ---------------------------------------------------
  // The doc contract puts a "### Hint 1..4" ladder under ## Approach (Guided and
  // Learning) and the one solution block under ## Solution. After a render, each
  // hint section - and, in Guided docs, the Solution section; in Guided and
  // Learning docs, the Key insight section (it gives Hint 1 away) - is wrapped
  // in a closed <details> so the learner reveals it on purpose. The wrap only
  // MOVES nodes the hardened markdown renderer already built; model text never
  // goes through innerHTML here.
  //
  // Extents (SP6 fix I1): a hint owns the nodes up to the next heading of ANY
  // level or <hr>. When nothing but an H1/H2/<hr>/the end follows it (the model
  // skipped the fixed "### Walkthrough"/"### Techniques" heading), it owns only
  // its first block, so Hint 4 never swallows the rest of ## Approach. An H2
  // section (Solution, Key insight) runs to the next H1/H2 or <hr>.
  //
  // A11y (SP6 fix M6): the heading is not nested in <summary>; it stays in the
  // document right before its <details>, visually hidden, so heading
  // navigation still finds every hint, and the summary shows its text in a
  // span.
  var HINT_RE = /^hint\s*(\d+)\b/i;
  var SOLUTION_RE = /^solution(?:\s*(?:[(:\-–—].*)?)?$/i;
  var INSIGHT_RE = /^key\s+insights?\b/i;
  var REVEAL_CUE = { hint: "Show hint", solution: "Show solution", insight: "Show key insight" };
  var HIDDEN_HEADING = "reveal-heading";
  function revealKind(tag, text, opts) {
    var t = String(tag || "").toUpperCase();
    var s = String(text || "").trim();
    if (t === "H3") {
      var m = HINT_RE.exec(s);
      if (m) return { key: "hint-" + (+m[1]), kind: "hint", level: 3 };
    }
    if (t === "H2" && opts && opts.revealSolution && SOLUTION_RE.test(s)) {
      return { key: "solution", kind: "solution", level: 2 };
    }
    if (t === "H2" && opts && opts.revealInsight && INSIGHT_RE.test(s)) {
      return { key: "insight", kind: "insight", level: 2 };
    }
    return null;
  }
  // Which sections a doc of `mode` hides (hints are always hidden).
  function revealsFor(mode) {
    var m = String(mode || "").toLowerCase();
    return { revealSolution: m === "guided", revealInsight: m === "guided" || m === "learning" };
  }
  function headingLevel(node) {
    if (!node || node.nodeType !== 1) return 0;
    var m = /^H([1-6])$/.exec(String(node.tagName || "").toUpperCase());
    return m ? +m[1] : 0;
  }
  function isRule(node) {
    return !!node && node.nodeType === 1 && String(node.tagName).toUpperCase() === "HR";
  }
  function revealExtent(h, info) {
    var extent = [];
    var stop = null;
    for (var n = h.nextSibling; n; n = n.nextSibling) {
      var lv = headingLevel(n);
      if (isRule(n) || (lv && (info.kind === "hint" || lv <= info.level))) { stop = n; break; }
      extent.push(n);
    }
    if (info.kind === "hint" && !(stop && headingLevel(stop) >= 3)) {
      // No sub-heading ends this hint: keep only its first block (plus any
      // whitespace text before it); the rest is the section's own text.
      var first = -1;
      for (var i = 0; i < extent.length; i++) {
        if (extent[i].nodeType === 1) { first = i; break; }
      }
      if (first !== -1) extent = extent.slice(0, first + 1);
    }
    return extent;
  }
  function applyReveals(container, doc, opts) {
    if (!container || !doc) return 0;
    opts = opts || {};
    var open = opts.open || {};
    var targets = [];
    Array.prototype.slice.call(container.childNodes).forEach(function (n) {
      if (n.nodeType !== 1) return;
      if ((" " + (n.className || "") + " ").indexOf(" " + HIDDEN_HEADING + " ") !== -1) return;
      var info = revealKind(n.tagName, n.textContent, opts);
      if (info) targets.push({ node: n, info: info });
    });
    targets.forEach(function (t) {
      var h = t.node;
      if (h.parentNode !== container) return;
      var extent = revealExtent(h, t.info);
      var details = doc.createElement("details");
      details.className = "reveal reveal-" + t.info.kind;
      details.setAttribute("data-reveal", t.info.key);
      var summary = doc.createElement("summary");
      summary.className = "reveal-sum";
      var label = doc.createElement("span");
      label.className = "reveal-title";
      label.textContent = h.textContent; // text only - never markup
      summary.appendChild(label);
      var cue = doc.createElement("span");
      cue.className = "reveal-cue";
      cue.textContent = REVEAL_CUE[t.info.kind];
      summary.appendChild(cue);
      details.appendChild(summary);
      var body = doc.createElement("div");
      body.className = "reveal-body";
      extent.forEach(function (x) { body.appendChild(x); });
      details.appendChild(body);
      container.insertBefore(details, h.nextSibling);
      h.className = ((h.className ? h.className + " " : "") + HIDDEN_HEADING + " sr-only");
      if (open[t.info.key]) details.open = true;
      if (typeof opts.onToggle === "function") {
        // SP6 fix M7: record the new state on the click itself (the native
        // toggle event fires a frame later - a re-render in between would
        // rebuild the reveal closed and flash it).
        summary.addEventListener("click", function () {
          opts.onToggle(t.info.key, !details.open);
        });
      }
    });
    return targets.length;
  }
  function isGuidedPath(p) {
    return /^guided\//.test(String(p || ""));
  }

  // ---- tier + display titles (SP6 fix O2 / O3) ------------------------------------
  // The code-quality tier is an Answer concept: Guided and Learning docs never
  // show (or log) one.
  function tierApplies(mode) {
    return String(mode || "").toLowerCase() === "answer";
  }
  // "1_two_sum" -> "1 Two Sum": a leading problem number on a derived (or
  // legacy) title is split off and shown separately.
  var NUM_PREFIX_RE = /^#?(\d{1,5})[.)]?\s+(?=\S)/;
  function problemTitle(o) {
    o = o || {};
    var number = typeof o.number === "number" && o.number > 0 ? o.number : null;
    var title = String(o.title || "").trim() || humanize(o.stem);
    var m = NUM_PREFIX_RE.exec(title);
    if (m) {
      title = title.slice(m[0].length);
      if (number === null) number = +m[1];
    }
    return { title: title, number: number, label: number !== null ? "#" + number + " · " + title : title };
  }

  // ---- summary actions (D7) -----------------------------------------------------
  function summaryActions(meta) {
    meta = meta || {};
    var acts = [];
    if (meta.mdPath) acts.push({ id: "open", label: "Open in Library", path: meta.mdPath });
    if (tierApplies(meta.mode) && meta.tier !== "optimal") {
      acts.push({ id: "optimal", label: "Re-run as Optimal", patch: { tier: "optimal" } });
    }
    // a Code Review is of one attempt in one language: no language re-runs
    if (meta.mode !== "review") otherLanguages(meta.language).forEach(function (l) {
      acts.push({ id: "lang-" + l, label: "Re-run in " + langLabel(l), patch: { lang: l } });
    });
    if (meta.mode && meta.mode !== "learning") {
      acts.push({ id: "learn", label: "Learn this topic", patch: { mode: "learning" } });
    }
    return acts;
  }

  // ---- platform + a11y + storage ------------------------------------------------
  function modKey(platform) {
    return /mac|iphone|ipad|ipod/i.test(String(platform || "")) ? "⌘" : "Ctrl";
  }
  // Focus-trap wrap: the index Tab (or Shift+Tab) moves to among `count`.
  function nextFocusIndex(current, count, backwards) {
    if (count <= 0) return -1;
    if (current < 0 || current >= count) return backwards ? count - 1 : 0;
    return backwards ? (current - 1 + count) % count : (current + 1) % count;
  }
  // IME guard: an Enter that only confirms a composition must not submit.
  function isComposingEnter(e) {
    return !!e && (e.isComposing === true || e.keyCode === 229);
  }
  // B15: localStorage wrapper. `getStore` returns the Storage (or throws -
  // private mode, blocked site data); every access is guarded so the page
  // works without it.
  function makePrefs(getStore, prefix) {
    prefix = prefix || "leetcoach.";
    function store() {
      try { return getStore(); } catch (e) { return null; }
    }
    return {
      get: function (key, fallback) {
        try {
          var s = store();
          var v = s ? s.getItem(prefix + key) : null;
          return v == null ? fallback : v;
        } catch (e) { return fallback; }
      },
      set: function (key, value) {
        try {
          var s = store();
          if (!s) return false;
          if (value == null || value === "") s.removeItem(prefix + key);
          else s.setItem(prefix + key, String(value));
          return true;
        } catch (e) { return false; }
      },
    };
  }

  // ---- SP7: practice loop (re-attempt, review queue, flashcards) -----------------
  // Display names of the run modes (the wire value "review" is Code Review).
  var MODE_NAMES = { answer: "Answer", learning: "Learning", guided: "Guided", review: "Code Review" };
  function modeName(mode) {
    var m = String(mode || "").toLowerCase();
    return MODE_NAMES[m] || cap(m);
  }
  // Modes with no code-quality tier (mirrors app.UNTIERED_MODES).
  function untiered(mode) {
    var m = String(mode || "").toLowerCase();
    return m === "learning" || m === "review";
  }

  // D4 Leitner rule, mirrored from problem_store.next_review for the grade
  // buttons' preview: solo -> next box (capped), hints -> same, peeked -> 1.
  var LEITNER_DAYS = [1, 3, 7, 14, 30];
  function leitnerPreview(box, grade) {
    var b = typeof box === "number" && box % 1 === 0 ? box : 1;
    b = Math.min(Math.max(b, 1), LEITNER_DAYS.length);
    var nb = grade === "solo" ? Math.min(b + 1, LEITNER_DAYS.length)
      : grade === "hints" ? b : grade === "peeked" ? 1 : 0;
    if (!nb) return null;
    return { box: nb, days: LEITNER_DAYS[nb - 1] };
  }
  function daysBetween(fromKey, toKey) {
    var a = parseDayKey(fromKey);
    var b = parseDayKey(toKey);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(fromKey)) || !/^\d{4}-\d{2}-\d{2}$/.test(String(toKey))) {
      return null;
    }
    if (isNaN(a.getTime()) || isNaN(b.getTime())) return null;
    return Math.round((b.getTime() - a.getTime()) / 86400000);
  }
  // "due today" / "overdue by 3 days" / "due tomorrow" / "due in 5 days".
  function dueLabel(due, today) {
    if (!due) return "not scheduled yet";
    var d = daysBetween(today, due);
    if (d === null) return "due " + due;
    if (d === 0) return "due today";
    if (d === 1) return "due tomorrow";
    if (d > 1) return "due in " + d + " days";
    return "overdue by " + (-d) + (d === -1 ? " day" : " days");
  }
  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  // The doc "Give up / show solution" reveals: the newest logged Answer or
  // Guided doc (they carry a solution), else the newest doc of any mode,
  // else the record's last listed .md.
  // SP8 fix M7: every candidate in that order (newest first within each
  // group, no duplicates), so a caller can fall back to the next one when a
  // doc was deleted. ``existing`` (optional: a path -> file map such as the
  // library listing, or an array of paths) drops docs that are no longer in
  // the library; the run log is append-only and still names deleted runs.
  var SOLUTION_MODES = { answer: 1, guided: 1 };
  function docCandidatesFor(record, existing) {
    record = record || {};
    var log = (record.log || []).slice();
    log.sort(function (a, b) { return String(a.ts || "").localeCompare(String(b.ts || "")); });
    function mdOf(entry) {
      var files = (entry && entry.files) || [];
      for (var i = 0; i < files.length; i++) {
        if (/\.md$/i.test(String(files[i]))) return String(files[i]);
      }
      return "";
    }
    var solutions = [];
    var others = [];
    log.forEach(function (e) {
      var md = mdOf(e);
      if (!md) return;
      (SOLUTION_MODES[String(e.mode || "")] ? solutions : others).push(md);
    });
    var runs = (record.runs || []).filter(function (p) { return /\.md$/i.test(String(p)); })
      .map(String);
    var ordered = solutions.reverse().concat(others.reverse(), runs.reverse());
    var has = null;
    if (Array.isArray(existing)) {
      has = {};
      existing.forEach(function (p) { has[String(p && p.path !== undefined ? p.path : p)] = 1; });
    } else if (existing && typeof existing === "object") {
      has = existing;
    }
    var seen = {};
    return ordered.filter(function (p) {
      if (seen[p]) return false;
      seen[p] = 1;
      return !has || Object.prototype.hasOwnProperty.call(has, p);
    });
  }
  function latestDocFor(record, existing) {
    return docCandidatesFor(record, existing)[0] || "";
  }

  // "Test my code" per-case / overall status -> label + style.
  var CASE_INFO = {
    pass: { label: "Passed", cls: "pass", glyph: "✓" },
    fail: { label: "Wrong answer", cls: "fail", glyph: "✗" },
    error: { label: "Error", cls: "fail", glyph: "!" },
    ran: { label: "Ran", cls: "none", glyph: "•" },
    not_verified: { label: "Not run", cls: "warn", glyph: "?" },
    not_supported: { label: "Not supported", cls: "warn", glyph: "?" },
  };
  function caseInfo(status) {
    return CASE_INFO[status] || CASE_INFO.not_verified;
  }

  // A Python starter that follows the sandbox contract (read the sample
  // `Input:` text on stdin, print the `Output:` JSON-style).
  var PY_STARTER =
    "import ast\nimport json\nimport re\nimport sys\n\n\n" +
    "def solve(args):\n" +
    "    # args holds the named inputs, e.g. args[\"nums\"], args[\"target\"]\n" +
    "    return None\n\n\n" +
    "if __name__ == \"__main__\":\n" +
    "    text = sys.stdin.read()\n" +
    "    # `nums = [2,7,11,15], target = 9` -> {\"nums\": [2, 7, 11, 15], \"target\": 9}\n" +
    "    pairs = re.findall(r\"(\\w+)\\s*=\\s*(.+?)(?=,\\s*\\w+\\s*=|$)\", text.strip(), re.S)\n" +
    "    args = {name: ast.literal_eval(value.strip()) for name, value in pairs}\n" +
    "    print(json.dumps(solve(args), separators=(\",\", \":\")))\n";
  function starterCode(language) {
    return language === "python" ? PY_STARTER : "";
  }

  // Tab / Shift+Tab in a code textarea. No selection: Tab inserts `unit` at
  // the caret, Shift+Tab removes up to one unit before the caret's line start.
  // A selection: every line it touches is indented / outdented. Returns the
  // new { value, start, end } (pure - the caller writes it back).
  function indentText(value, start, end, outdent, unit) {
    value = String(value || "");
    unit = unit || "    ";
    start = Math.max(0, Math.min(start | 0, value.length));
    end = Math.max(start, Math.min(end | 0, value.length));
    var lineStart = value.lastIndexOf("\n", start - 1) + 1;
    if (!outdent && start === end) {
      return { value: value.slice(0, start) + unit + value.slice(end), start: start + unit.length,
        end: start + unit.length };
    }
    // a selection ending right at a line start does not include that line
    var lastEnd = end > start && value.charAt(end - 1) === "\n" ? end - 1 : end;
    var blockEnd = value.indexOf("\n", lastEnd);
    if (blockEnd === -1) blockEnd = value.length;
    var lines = value.slice(lineStart, blockEnd).split("\n");
    var delta0 = 0;
    var total = 0;
    var out = lines.map(function (line, i) {
      var d;
      if (!outdent) {
        d = unit.length;
        line = unit + line;
      } else {
        var m = /^( +|\t)/.exec(line);
        var remove = 0;
        if (m) remove = m[1] === "\t" ? 1 : Math.min(m[1].length, unit.length);
        d = -remove;
        line = line.slice(remove);
      }
      if (i === 0) delta0 = d;
      total += d;
      return line;
    });
    var next = value.slice(0, lineStart) + out.join("\n") + value.slice(blockEnd);
    var ns = Math.max(lineStart, start + delta0);
    var ne = start === end ? ns : Math.max(ns, end + total);
    return { value: next, start: ns, end: ne };
  }

  // Wrap-around deck navigation.
  function wrapIndex(i, n, step) {
    if (n <= 0) return 0;
    return (((i + step) % n) + n) % n;
  }

  // The notes editor's visible state line.
  function notesStatus(state, info) {
    info = info || {};
    if (state === "saving") return "Saving…";
    if (state === "dirty") return "Unsaved changes";
    if (state === "saved") return "Saved";
    if (state === "error") return "Not saved" + (info.reason ? " (" + info.reason + ")" : "") + " — retry";
    return "";
  }

  // SP7 fix: one save queue per problem's notes, shared by every notes editor
  // of that problem. One request at a time; text pushed while a save is in
  // flight waits (only the newest is kept) and is sent when it lands - even if
  // the editor that pushed it is gone. `send(text, keepalive, done)` must call
  // done(err) once; `cb(err, savedText)` gets the text actually sent (newer
  // pushes coalesce). flushNow() (pagehide) sends the waiting text right away
  // with keepalive: the page will not live to see the in-flight save land.
  function makeSaveQueue(send) {
    var active = 0;
    var next = null;
    var latest = null;
    function run(job) {
      active++;
      var finished = false;
      var done = function (err) {
        if (finished) return;
        finished = true;
        active--;
        job.cbs.forEach(function (cb) { cb(err || null, job.text); });
        if (!active && next) {
          var n = next;
          next = null;
          run(n);
        }
      };
      try {
        send(job.text, job.keepalive, done);
      } catch (e) {
        done(e || new Error("send failed"));
      }
    }
    return {
      push: function (text, keepalive, cb) {
        latest = text;
        if (next) {
          next.text = text;
          next.keepalive = next.keepalive || !!keepalive;
          if (cb) next.cbs.push(cb);
          return;
        }
        var job = { text: text, keepalive: !!keepalive, cbs: cb ? [cb] : [] };
        if (active) next = job;
        else run(job);
      },
      flushNow: function () {
        if (!next) return;
        var n = next;
        next = null;
        n.keepalive = true;
        run(n);
      },
      pending: function () { return active > 0 || !!next; },
      latest: function () { return latest; },
    };
  }

  // The local day the review queue's labels count from: the server's `today`
  // while it is still that day here, else the local date (a tab left open
  // past midnight; the caller also re-fetches /review).
  function reviewToday(review, fetchedDay, now) {
    var local = dayKey(now || new Date());
    return review && review.today && fetchedDay === local ? review.today : local;
  }

  // ---- hardened marked renderer (KEEP the policy; moved here for tests) ---------
  // Claude's output is untrusted markdown rendered via innerHTML. marked v12
  // dropped `sanitize`, so raw HTML is escaped (visible as text), links must be
  // http(s), and images must be inline data:image/ URIs (no network fetch).
  function hardenedRenderer() {
    return {
      html: function (token) {
        var raw = typeof token === "string" ? token : (token && token.text) || "";
        return escapeHtml(raw);
      },
      link: function (href, title, text) {
        var h = unescapeEntities(String(href || ""));
        if (!/^https?:/i.test(h)) return text || escapeHtml(h);
        var attr = escapeHtml(h).replace(/"/g, "&quot;");
        return '<a href="' + attr + '" rel="noopener" target="_blank">' +
          (text || escapeHtml(h)) + "</a>";
      },
      image: function (href, title, text) {
        var h = unescapeEntities(String(href || ""));
        if (!/^data:image\//i.test(h)) return text || "";
        var attr = escapeHtml(h).replace(/"/g, "&quot;");
        return '<img src="' + attr + '" alt="' + (text || "") + '">';
      },
    };
  }

  // ---- SP8 / D6: follow-up questions on a saved doc -------------------------
  // The question as the server will see it (trimmed), or why it can't be sent.
  function followupCheck(text, max) {
    var q = String(text == null ? "" : text).trim();
    if (!q) return { ok: false, message: "Type a question about this doc first." };
    if (max && q.length > max) {
      return { ok: false, message: "That question is too long (max " + max + " characters)." };
    }
    return { ok: true, question: q };
  }
  var FOLLOWUP_FALLBACK_NOTES = {
    no_session: "no saved session for this doc, so a fresh call read the doc",
    resume_failed: "the saved session could not be resumed, so a fresh call read the doc",
    unsupported: "this claude CLI has no --resume, so a fresh call read the doc",
  };
  // A `phase` event's source ("resume" | "fallback" + reason) as a chip.
  function followupSource(p) {
    if (!p || typeof p !== "object" || !p.source) return null;
    if (p.source === "resume") {
      return { kind: "resume", label: "Resumed study session",
        note: "Claude still has the conversation that wrote this doc" };
    }
    return { kind: "fallback", label: "Answered from the doc",
      note: FOLLOWUP_FALLBACK_NOTES[p.reason] || "" };
  }
  function followupDoneText(done) {
    var heading = done && typeof done.heading === "string" ? done.heading.replace(/^#+\s*/, "") : "";
    if (!heading) return "Added to the doc.";
    var how = done.source === "resume" ? "resumed the study session" : "answered from the doc";
    return "Added to the doc under “" + heading + "” (" + how + ").";
  }

  return {
    followupCheck: followupCheck,
    followupSource: followupSource,
    followupDoneText: followupDoneText,
    escapeHtml: escapeHtml,
    unescapeEntities: unescapeEntities,
    cap: cap,
    humanize: humanize,
    extOf: extOf,
    firstLine: firstLine,
    midTrunc: midTrunc,
    modeLabel: modeLabel,
    langLabel: langLabel,
    otherLanguages: otherLanguages,
    relTime: relTime,
    fmtClock: fmtClock,
    fmtDuration: fmtDuration,
    dayKey: dayKey,
    parseDayKey: parseDayKey,
    modelLabel: modelLabel,
    retryDelay: retryDelay,
    runRetryDelay: runRetryDelay,
    cancelOutcome: cancelOutcome,
    runEventKind: runEventKind,
    needsReplaceConfirm: needsReplaceConfirm,
    phaseText: phaseText,
    hasOpenFence: hasOpenFence,
    throttleDelay: throttleDelay,
    isNearBottom: isNearBottom,
    nearEdge: nearEdge,
    libRelPath: libRelPath,
    verdictFromLine: verdictFromLine,
    verdictInfo: verdictInfo,
    CODE_EXT: CODE_EXT,
    splitRunName: splitRunName,
    deriveRuns: deriveRuns,
    runSiblings: runSiblings,
    computeStats: computeStats,
    heatBucket: heatBucket,
    diffInfo: diffInfo,
    revealKind: revealKind,
    revealsFor: revealsFor,
    applyReveals: applyReveals,
    tierApplies: tierApplies,
    problemTitle: problemTitle,
    isGuidedPath: isGuidedPath,
    summaryActions: summaryActions,
    modKey: modKey,
    nextFocusIndex: nextFocusIndex,
    isComposingEnter: isComposingEnter,
    makePrefs: makePrefs,
    hardenedRenderer: hardenedRenderer,
    modeName: modeName,
    untiered: untiered,
    LEITNER_DAYS: LEITNER_DAYS,
    leitnerPreview: leitnerPreview,
    daysBetween: daysBetween,
    dueLabel: dueLabel,
    plural: plural,
    latestDocFor: latestDocFor,
    docCandidatesFor: docCandidatesFor,
    caseInfo: caseInfo,
    starterCode: starterCode,
    indentText: indentText,
    wrapIndex: wrapIndex,
    notesStatus: notesStatus,
    makeSaveQueue: makeSaveQueue,
    reviewToday: reviewToday,
  };
});
