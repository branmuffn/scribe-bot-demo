"""Talk-time analytics (F10) and Brian's speaking stats, counted from the transcript lines themselves.

Lines are the source of truth (not speaker.talk_seconds), so moving a line to another speaker updates the numbers.
A "turn" is a run of consecutive lines by the same speaker. No AI call; nothing leaves the M14.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from scribe_bot.core.people import ME


def by_speaker(conn: sqlite3.Connection, conversation_id: int) -> dict[int, dict]:
    """speaker id -> {"seconds", "turns"} for one conversation. Empty if it has no lines yet."""
    out: dict[int, dict] = {}
    prev = object()
    for r in conn.execute("SELECT speaker_id, start_s, end_s FROM segment WHERE conversation_id = ? ORDER BY idx",
                          (conversation_id,)):
        s = out.setdefault(r["speaker_id"], {"seconds": 0.0, "turns": 0})
        s["seconds"] += max(r["end_s"] - r["start_s"], 0)
        if r["speaker_id"] != prev:
            s["turns"] += 1
        prev = r["speaker_id"]
    return out


def with_person(conn: sqlite3.Connection, person_id: int) -> dict | None:
    """Across every conversation this person is in: how many, total talk, their share and Brian's share."""
    row = conn.execute(
        "SELECT count(DISTINCT g.conversation_id) AS n, coalesce(sum(g.end_s - g.start_s), 0) AS total, "
        "coalesce(sum(CASE WHEN s.person_id = ? THEN g.end_s - g.start_s END), 0) AS theirs, "
        "coalesce(sum(CASE WHEN s.person_id = ? THEN g.end_s - g.start_s END), 0) AS mine "
        "FROM segment g LEFT JOIN speaker s ON s.id = g.speaker_id JOIN conversation c ON c.id = g.conversation_id "
        "WHERE c.deleted_at IS NULL AND g.conversation_id IN (SELECT conversation_id FROM speaker WHERE person_id = ?)",
        (person_id, ME, person_id)).fetchone()
    if not row["n"] or row["total"] <= 0:
        return None
    return {"conversations": row["n"], "seconds": row["total"],
            "their_share": row["theirs"] / row["total"], "my_share": row["mine"] / row["total"]}


def hours_label(seconds: float) -> str:
    """'45 m', '2 h 10 m'."""
    m = int(round(seconds / 60))
    return f"{m // 60} h {m % 60} m" if m >= 60 else f"{max(m, 1)} m"


# ---------- Speaking stats for Brian (free, local; no AI) ----------

FILLERS = ("um", "uh", "er", "you know", "i mean", "kind of", "sort of", "basically")
_FILLER_RE = re.compile(r"\b(" + "|".join(re.escape(f) for f in FILLERS) + r")\b", re.IGNORECASE)
PACE = (140, 180)   # words per minute that is normal in conversation (120–160 is for presenting)
SLOW_DOWN_ABOVE = 190
LONGEST_OK_S = 120  # a stretch longer than this without a break gets a tip
FILLERS_OK_PER_MIN = 2
QUIET_AFTER_S = 300  # talking this long without asking anything gets a tip
EARLY_UNTIL = 3     # a trend from fewer conversations than this is labeled "Early numbers"


def _speaking_from(rows: list[sqlite3.Row], person_id: int) -> dict | None:
    """rows: one conversation's lines in order, with person_id. None if the person said nothing."""
    words = fillers = questions = 0
    seconds = longest = 0.0
    counts: dict[str, int] = {}
    run_start = run_end = None
    for r in [*rows, None]:
        mine = r is not None and r["person_id"] == person_id
        if run_start is not None and not mine:                      # a turn of theirs just ended
            longest = max(longest, run_end - run_start)
            run_start = None
        if not mine:
            continue
        if run_start is None:
            run_start = r["start_s"]
        run_end = r["end_s"]
        seconds += max(r["end_s"] - r["start_s"], 0)
        words += len(r["text"].split())
        questions += r["text"].count("?")
        for f in _FILLER_RE.findall(r["text"]):
            counts[f.lower()] = counts.get(f.lower(), 0) + 1
            fillers += 1
    if not words or seconds <= 0:
        return None
    minutes = seconds / 60
    return {"seconds": seconds, "words": words, "wpm": words / minutes, "longest": longest, "questions": questions,
            "fillers": fillers, "fillers_per_min": fillers / minutes,
            "counts": counts, "top_fillers": sorted(counts.items(), key=lambda kv: -kv[1])[:3]}


def _lines(conn: sqlite3.Connection, where: str, args: tuple) -> dict[int, list]:
    out: dict[int, list] = {}
    for r in conn.execute(
            "SELECT g.conversation_id, g.start_s, g.end_s, g.text, s.person_id FROM segment g "
            "LEFT JOIN speaker s ON s.id = g.speaker_id JOIN conversation c ON c.id = g.conversation_id "
            f"WHERE c.deleted_at IS NULL AND {where} ORDER BY g.conversation_id, g.idx", args):
        out.setdefault(r["conversation_id"], []).append(r)
    return out


def speaking(conn: sqlite3.Connection, conversation_id: int, person_id: int = ME) -> dict | None:
    """Brian's speaking stats for one conversation: pace, longest stretch, questions, filler words."""
    return _speaking_from(_lines(conn, "g.conversation_id = ?", (conversation_id,)).get(conversation_id, []), person_id)


def pace_by_speaker(conn: sqlite3.Connection, conversation_id: int) -> dict[int, float]:
    """speaker id -> words per minute in one conversation, for showing others' pace next to Brian's."""
    out: dict[int, list[float]] = {}
    for r in conn.execute("SELECT speaker_id, start_s, end_s, text FROM segment WHERE conversation_id = ?", (conversation_id,)):
        w = out.setdefault(r["speaker_id"], [0, 0.0])
        w[0] += len(r["text"].split())
        w[1] += max(r["end_s"] - r["start_s"], 0)
    return {sid: words / (secs / 60) for sid, (words, secs) in out.items() if words and secs > 0}


def speaking_trend(conn: sqlite3.Connection, days: int, person_id: int = ME) -> dict | None:
    """The same stats combined across conversations from the last `days` days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    per = [s for rows in _lines(conn, "coalesce(c.started_at, c.created_at) >= ?", (since,)).values()
           if (s := _speaking_from(rows, person_id))]
    if not per:
        return None
    seconds = sum(s["seconds"] for s in per)
    words, fillers = sum(s["words"] for s in per), sum(s["fillers"] for s in per)
    counts: dict[str, int] = {}
    for s in per:
        for f, n in s["counts"].items():
            counts[f] = counts.get(f, 0) + n
    return {"conversations": len(per), "seconds": seconds, "words": words, "wpm": words / (seconds / 60),
            "longest": max(s["longest"] for s in per), "questions": sum(s["questions"] for s in per),
            "fillers": fillers, "fillers_per_min": fillers / (seconds / 60),
            "top_fillers": sorted(counts.items(), key=lambda kv: -kv[1])[:3]}


def tips(s: dict) -> list[str]:
    """Plain rule-of-thumb tips from the numbers (no AI)."""
    out = []
    if s["wpm"] > SLOW_DOWN_ABOVE:
        out.append(f"You spoke at {s['wpm']:.0f} words a minute; slowing toward {PACE[1]} gives people time to follow. "
                   "Pause after each key point.")
    elif s["wpm"] < PACE[0]:
        out.append(f"You spoke at {s['wpm']:.0f} words a minute; a little faster (toward {PACE[0]}) can sound more confident.")
    if s["longest"] > LONGEST_OK_S:
        out.append(f"Your longest stretch was {s['longest'] / 60:.0f} min without a break. Try point → reason → example, "
                   "then hand it back with a question.")
    if s["fillers_per_min"] >= FILLERS_OK_PER_MIN:
        top = ", ".join(f"“{f}”" for f, _ in s["top_fillers"])
        out.append(f"About {s['fillers_per_min']:.1f} filler words a minute ({top}). A silent pause works better than a filler.")
    if s["seconds"] > QUIET_AFTER_S and s["questions"] == 0:
        out.append("You asked no questions. One open question (“What am I missing?”) invites others in.")
    return out


def levels(s: dict) -> dict[str, str]:
    """Good ('ok') / Watch ('warn') for each speaking tile, from the same thresholds as tips(). Speaking is coaching, so
    nothing is ever a Problem. Pace is Watch outside the normal 140–180 (the slow-down tip waits until above 190)."""
    return {"pace": "ok" if PACE[0] <= s["wpm"] <= PACE[1] else "warn",
            "longest": "warn" if s["longest"] > LONGEST_OK_S else "ok",
            "questions": "warn" if s["seconds"] > QUIET_AFTER_S and s["questions"] == 0 else "ok",
            "fillers": "warn" if s["fillers_per_min"] >= FILLERS_OK_PER_MIN else "ok"}
