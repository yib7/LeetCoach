"""Study stats (SP6 / A8), computed server-side from the run log.

Every run-log entry (``.leetcoach/runs.jsonl``) is one activity at its own
``ts``. Library files that NO log entry names (libraries saved before the log
existed) still count: one activity per saved run, i.e. per
``<mode>/<topic>/<base name>`` group (an Answer's ``.md`` + code file are one
run), at the group's newest mtime. Nothing collapses on the stem: each tier
and each ``__<n>`` slot (a re-run) is its own run - the old client grouped by
the text before the first ``__``, so repeat days, tiers and slots collided
and a 4-day streak showed as 1 (A8).

Days are LOCAL calendar days of the machine (the browser on localhost shares
it). The returned shape matches ``LeetCoachCore.computeStats`` so the page
renders either one.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

HEATMAP_DAYS = 119  # 17 weeks

MODE_LABELS = {"answer": "Answer", "answers": "Answer", "learning": "Learning",
               "guided": "Guided", "review": "Code Review", "reviews": "Code Review"}
LANGUAGE_LABELS = {"python": "Python", "py": "Python", "cpp": "C++", "java": "Java"}
_CODE_EXT = {"py": "python", "cpp": "cpp", "java": "java"}
_SLOT = re.compile(r"__\d+$")
_NUM_PREFIX = re.compile(r"^\d+[-_]")


def humanize(text: str) -> str:
    """``hash_map`` -> ``Hash Map`` (mirrors core.humanize)."""
    words = re.sub(r"[_-]+", " ", str(text or "")).split()
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _mode_label(mode) -> str:
    mode = str(mode or "")
    return MODE_LABELS.get(mode, humanize(mode) or "Other")


def _entry_time(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _problem_key(text: str) -> str:
    return _NUM_PREFIX.sub("", str(text or "")).lower()


def _activities(entries, files):
    """``(epoch, mode, language, topic, problem_key)`` per run: logged ones,
    then legacy groups of files the log never named."""
    logged_files: set[str] = set()
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("mode") == "followup":
            continue  # SP8 / D6: a follow-up question is not a study run
        for f in e.get("files") or ():
            if isinstance(f, str):
                logged_files.add(f)
        ts = _entry_time(e.get("ts"))
        if ts is None:
            continue
        lang = LANGUAGE_LABELS.get(str(e.get("language") or ""), "Unknown")
        topic = humanize(e.get("pattern") or "") or "Uncategorized"
        out.append((ts, _mode_label(e.get("mode")), lang, topic,
                    _problem_key(e.get("problem_id") or "")))
    n_log = len(out)

    groups: dict[tuple, dict] = {}
    for f in files or ():
        path = str(f.get("path") or "")
        if not path or path in logged_files:
            continue
        parts = path.split("/")
        if len(parts) < 3:
            continue  # only <mode>/<topic>/<file> entries are runs
        name = parts[-1]
        base, dot, ext = name.rpartition(".")
        if not dot:
            base, ext = name, ""
        key = (parts[0], "/".join(parts[1:-1]), base)
        g = groups.setdefault(key, {"mtime": 0.0, "lang": None})
        mtime = f.get("mtime")
        if isinstance(mtime, (int, float)) and mtime > g["mtime"]:
            g["mtime"] = float(mtime)
        if ext.lower() in _CODE_EXT and g["lang"] is None:
            g["lang"] = _CODE_EXT[ext.lower()]
    for (mode_folder, topic_seg, base), g in groups.items():
        if not g["mtime"]:
            continue
        stem = _SLOT.sub("", base).split("__", 1)[0]
        topic = humanize(re.sub(r"_learning$", "", topic_seg.split("/")[0])) or "Uncategorized"
        lang = LANGUAGE_LABELS.get(g["lang"] or "", "Unknown")
        out.append((g["mtime"], _mode_label(mode_folder), lang, topic, _problem_key(stem)))
    return out, n_log


def _count(mapping: dict, key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def compute_stats(entries, files, *, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    today = now.date() if isinstance(now, datetime) else now
    acts, n_log = _activities(entries, files)
    by_mode: dict = {}
    by_lang: dict = {}
    by_topic: dict = {}
    problems: set[str] = set()
    days: dict[date, int] = {}
    for ts, mode, lang, topic, key in acts:
        _count(by_mode, mode)
        _count(by_lang, lang)
        _count(by_topic, topic)
        if key:
            problems.add(key)
        try:
            d = datetime.fromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError):
            continue
        days[d] = days.get(d, 0) + 1

    this_week = sum(days.get(today - timedelta(days=i), 0) for i in range(7))
    cursor = today if days.get(today) else today - timedelta(days=1)
    streak = 0
    while days.get(cursor):
        streak += 1
        cursor -= timedelta(days=1)
    longest = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev is not None and (d - prev).days == 1 else 1
        longest = max(longest, run)
        prev = d
    heatmap = []
    for back in range(HEATMAP_DAYS - 1, -1, -1):
        d = today - timedelta(days=back)
        heatmap.append({"date": d.isoformat(), "count": days.get(d, 0)})
    return {
        "total": len(acts),
        "distinctProblems": len(problems),
        "today": days.get(today, 0),
        "thisWeek": this_week,
        "currentStreak": streak,
        "longestStreak": longest,
        "byMode": by_mode,
        "byLanguage": by_lang,
        "byTopic": by_topic,
        "heatmap": heatmap,
        "sources": {"log": n_log, "legacy": len(acts) - n_log},
    }
