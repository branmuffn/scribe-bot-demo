"""Short names for lists (Q5 B+C, Brian 2026-11-21: long talk and task names ran off the screen everywhere).

One small call on the cheap model per talk, sent only the talk's title and its task titles (no transcript):
a 2-4 word name for the talk and a short version (up to 6 words) of each task. The full titles stay as they are and
are still shown on the talk page and in the task pop-up. Private talks are never sent (they keep their quick title).
"""
from __future__ import annotations

import json
import re
import sqlite3

from scribe_bot.ai import router
from scribe_bot.core.db import transaction

TASK = "extract"
KIND = "ai_short"
TALK_MAX, TASK_MAX = 40, 60
SYSTEM = ("You shorten titles for a phone list. Reply with JSON only: {\"talk\": str, \"tasks\": {\"<id>\": str}}.\n"
          "- talk: 2 to 4 words naming the topic, e.g. \"Budget review\", \"Project check-in\". No dates.\n"
          "- each task: at most 6 words, starting with the action, keeping the key person or thing, e.g. "
          "\"Certification form with Sam\", \"Book venue before trip\". No due dates (shown separately).\n"
          "- Plain words, no quotes, no trailing period. Keep names exactly as written.")


def _clean(v, cap: int) -> str | None:
    if not isinstance(v, str):
        return None
    v = re.sub(r"\s+", " ", v).strip().strip("\"'“”").rstrip(".").strip()
    return v[:cap].rstrip() or None


def needs(conn: sqlite3.Connection, cid: int) -> bool:
    c = conn.execute("SELECT private, summary, short_title FROM conversation WHERE id = ? AND deleted_at IS NULL", (cid,)).fetchone()
    return bool(c and not c["private"] and c["summary"] and not c["short_title"])


def shorten(conn: sqlite3.Connection, cid: int, *, approved: bool = True, factory: router.ProviderFactory = router.default_factory,
            chain: list | None = None) -> dict:
    c = conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone()
    if c is None or c["deleted_at"] or c["private"]:
        return {"status": "skipped"}
    tasks = conn.execute("SELECT id, title FROM task WHERE conversation_id = ?", (cid,)).fetchall()
    user = json.dumps({"talk": c["title"], "tasks": {str(t["id"]): t["title"] for t in tasks}}, ensure_ascii=False)
    if chain is None:
        chain = [router.HAIKU] + [x for x in router.task_config(conn, TASK)["chain"] if list(x) != router.HAIKU]
    try:
        call = router.run(conn, TASK, SYSTEM, user, sent_summary=f"conversation #{cid} title and {len(tasks)} task titles (short names)",
                          conversation_id=cid, area=c["area"], approved=approved, factory=factory, chain=chain, max_tokens=4000)
    except (router.NeedsApproval, router.Blocked, router.CallFailed) as e:
        return {"status": "failed", "error": str(e)}
    m = re.search(r"\{.*\}", call.text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not data:   # a reply cut off part-way: keep what arrived (the talk name, then each finished task)
        t = re.search(r'"talk"\s*:\s*"([^"]+)"', call.text or "")
        data = {"talk": t.group(1) if t else None,
                "tasks": dict(re.findall(r'"(\d+)"\s*:\s*"([^"]+)"', call.text or ""))}
    talk = _clean(data.get("talk"), TALK_MAX)
    short_tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    ids = {t["id"] for t in tasks}
    with transaction(conn):
        conn.execute("UPDATE conversation SET short_title = ? WHERE id = ?", (talk or "", cid))
        for k, v in short_tasks.items():
            s = _clean(v, TASK_MAX)
            if s and str(k).isdigit() and int(k) in ids:
                conn.execute("UPDATE task SET short_title = ? WHERE id = ?", (s, int(k)))
    return {"status": "done" if talk else "failed", "cost_usd": round(call.cost_usd, 6)}


def backlog(conn: sqlite3.Connection) -> list[int]:
    """Older talks with AI notes but no short name yet (Brian approved the one-time redo, about a cent in total)."""
    return [r[0] for r in conn.execute("SELECT id FROM conversation WHERE deleted_at IS NULL AND private = 0 AND summary IS NOT NULL "
                                       "AND summary != '' AND short_title IS NULL ORDER BY id")]


def stage(ctx) -> str:
    done = [shorten(ctx.conn, cid) for cid in backlog(ctx.conn)[:200]]
    return json.dumps({"talks": len(done), "cost_usd": round(sum(d.get("cost_usd", 0) for d in done), 6)})


PIPELINE = [("short", stage)]


RETRY_S = 1800


def maybe_queue(conn: sqlite3.Connection) -> int | None:
    """Called by the worker loop (not by tick, so tests never reach a real AI): queue one short-names job when talks
    are missing a short name, at most every 30 minutes, never twice at once."""
    from datetime import datetime, timedelta, timezone

    from scribe_bot.core import queue
    from scribe_bot.core.db import now
    if not backlog(conn) or conn.execute("SELECT 1 FROM job WHERE kind = ? AND status IN ('pending', 'running')", (KIND,)).fetchone():
        return None
    last = conn.execute("SELECT value FROM setting WHERE key = 'short_names_tried'").fetchone()
    if last:
        try:
            if datetime.fromisoformat(last[0]) > datetime.now(timezone.utc) - timedelta(seconds=RETRY_S):
                return None
        except ValueError:
            pass
    with transaction(conn):
        conn.execute("INSERT INTO setting (key, value) VALUES ('short_names_tried', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (now(),))
    return queue.enqueue(conn, KIND, payload={})
