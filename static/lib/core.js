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
    var m = { answers: "Answer", answer: "Answer", learning: "Learning", guided: "Guided" };
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
  function hasOpenFence(md) {
    var lines = String(md || "").split("\n");
    var open = null; // { ch, len }
    for (var i = 0; i < lines.length; i++) {
      var m = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(lines[i]);
      if (!m) continue;
      var ch = m[1].charAt(0);
      var len = m[1].length;
      if (!open) {
        if (ch === "`" && m[2].indexOf("`") !== -1) continue; // not a fence
        open = { ch: ch, len: len };
      } else if (ch === open.ch && len >= open.len && !m[2].trim()) {
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

  // Group files into runs by (mode-folder, topic, stem-before "__").
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
      var us = base.indexOf("__");
      var stem = us === -1 ? base : base.slice(0, us);
      var tier = us === -1 ? "" : base.slice(us + 2);
      // learning writes into "<type>_learning" — drop the mode artifact.
      var topicRaw = topicSeg.replace(/_learning$/, "");
      var key = modeFolder + "|" + topicRaw + "|" + stem;
      var run = map[key];
      if (!run) {
        run = {
          modeFolder: modeFolder, topicRaw: topicRaw, stemRaw: stem,
          tier: "", langExt: "", savedAt: 0, mdPath: "", mdAt: 0, verdict: "", files: [],
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

  // ---- summary actions (D7) -----------------------------------------------------
  function summaryActions(meta) {
    meta = meta || {};
    var acts = [];
    if (meta.mdPath) acts.push({ id: "open", label: "Open in Library", path: meta.mdPath });
    if (meta.mode && meta.mode !== "learning" && meta.tier !== "optimal") {
      acts.push({ id: "optimal", label: "Re-run as Optimal", patch: { tier: "optimal" } });
    }
    otherLanguages(meta.language).forEach(function (l) {
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

  return {
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
    phaseText: phaseText,
    hasOpenFence: hasOpenFence,
    throttleDelay: throttleDelay,
    isNearBottom: isNearBottom,
    nearEdge: nearEdge,
    libRelPath: libRelPath,
    verdictFromLine: verdictFromLine,
    verdictInfo: verdictInfo,
    CODE_EXT: CODE_EXT,
    deriveRuns: deriveRuns,
    runSiblings: runSiblings,
    computeStats: computeStats,
    heatBucket: heatBucket,
    summaryActions: summaryActions,
    modKey: modKey,
    nextFocusIndex: nextFocusIndex,
    isComposingEnter: isComposingEnter,
    makePrefs: makePrefs,
    hardenedRenderer: hardenedRenderer,
  };
});
