"""Pause, resume and cancel processing from the app (Brian, 2026-11-21: "pause a 2-hour talk while I use the computer,
resume at night, or cancel and start over later").

Pause sets 'pipeline_paused' = '1'. The worker stops at the next safe point: within ~30 s while transcribing (the pieces
done so far are kept, Resume carries on), within ~2 s while separating speakers (that step starts over), or after the
current short step. Nothing new starts while paused. Resume tonight stores 'pipeline_resume_at'; the worker lifts the
pause then. Cancel (only while paused, before the transcript is saved into the talk) sends the recording back to the
Inbox as it was and deletes the half-done files.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from scribe_bot.core.db import transaction

NIGHT = time(1)   # "Resume tonight" / "Tonight at 1 am" (Brian, 2026-11-21: 11 PM he may be up, 1 AM he never is)


def _set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM setting WHERE key = ?", (key,))
    else:
        conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, value))


def tonight(now: datetime | None = None) -> datetime:
    now = (now or datetime.now()).astimezone()
    at = now.replace(hour=NIGHT.hour, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def pause(conn: sqlite3.Connection) -> None:
    _set(conn, "pipeline_paused", "1")
    _set(conn, "pipeline_resume_at", None)


def resume(conn: sqlite3.Connection, when: str = "now", now: datetime | None = None) -> None:
    if when == "tonight":
        _set(conn, "pipeline_paused", "1")
        _set(conn, "pipeline_resume_at", tonight(now).astimezone(timezone.utc).isoformat(timespec="seconds"))
    else:
        _set(conn, "pipeline_paused", "0")
        _set(conn, "pipeline_resume_at", None)


def state(conn: sqlite3.Connection) -> dict:
    get = lambda k: (conn.execute("SELECT value FROM setting WHERE key = ?", (k,)).fetchone() or [None])[0]  # noqa: E731
    at = get("pipeline_resume_at")
    return {"paused": get("pipeline_paused") == "1", "resume_at": at,
            "resume_label": datetime.fromisoformat(at).astimezone().strftime("%-I %p").lower() if at else None}


def can_cancel(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute("SELECT kind, status FROM job WHERE id = ?", (job_id,)).fetchone()
    if not row or row["kind"] != "recording" or row["status"] not in ("pending", "failed"):
        return False
    return not conn.execute("SELECT 1 FROM stage_run WHERE job_id = ? AND stage = 'file' AND status = 'done'", (job_id,)).fetchone()


def cancel(conn: sqlite3.Connection, job_id: int, jobs_dir: Path) -> int | None:
    """Back to the Inbox. Returns the recording id, or None if this job can't be cancelled (running, or too far along)."""
    if not can_cancel(conn, job_id):
        return None
    rec_id = conn.execute("SELECT recording_id FROM job WHERE id = ?", (job_id,)).fetchone()[0]
    with transaction(conn):
        conn.execute("DELETE FROM stage_run WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM job WHERE id = ?", (job_id,))
        for (cid,) in conn.execute("SELECT id FROM conversation WHERE recording_id = ?", (rec_id,)).fetchall():
            conn.execute("DELETE FROM review_item WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM search_index WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM segment WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM speaker WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM notification WHERE link = ?", (f"/?job={job_id}",))
            conn.execute("DELETE FROM conversation WHERE id = ?", (cid,))
        conn.execute("UPDATE recording SET status = 'awaiting_approval' WHERE id = ?", (rec_id,))
        item = conn.execute("SELECT id FROM review_item WHERE kind = 'approval' AND ref_id = ? ORDER BY id DESC LIMIT 1",
                            (rec_id,)).fetchone()
        if item:
            conn.execute("UPDATE review_item SET status = 'open', resolved_at = NULL WHERE id = ?", (item[0],))
    shutil.rmtree(jobs_dir / str(job_id), ignore_errors=True)
    return rec_id


# ---------- Process at a set time (Brian, 2026-11-21: "a later to process later at a set time", apart from Remind me) ----------
# The Inbox item keeps 'process_at' (UTC), 'process_ai' (1/0) and 'process_type' in its payload and is snoozed until
# then; the worker approves it when the time comes, exactly as if Brian had tapped AI notes / Transcript only.

def approve_recording(conn: sqlite3.Connection, item: sqlite3.Row, ai: bool, meeting_type: str | None = None) -> None:
    """The one place a waiting recording is sent for processing (the Inbox buttons and Process at both use it)."""
    from scribe_bot.pipeline import ingest
    ingest.approve(conn, item["ref_id"], ai=ai, meeting_type=meeting_type)


def schedule(conn: sqlite3.Connection, rid: int, at_utc: str, ai: bool, meeting_type: str | None) -> None:
    import json
    r = conn.execute("SELECT payload FROM review_item WHERE id = ?", (rid,)).fetchone()
    p = json.loads(r["payload"] or "{}")
    p.update(process_at=at_utc, process_ai=1 if ai else 0, process_type=meeting_type or None)
    with transaction(conn):
        conn.execute("UPDATE review_item SET payload = ?, snoozed_until = ? WHERE id = ?", (json.dumps(p), at_utc, rid))


def unschedule(conn: sqlite3.Connection, rid: int) -> None:
    import json
    r = conn.execute("SELECT payload FROM review_item WHERE id = ?", (rid,)).fetchone()
    p = {k: v for k, v in json.loads(r["payload"] or "{}").items() if k not in ("process_at", "process_ai", "process_type")}
    with transaction(conn):
        conn.execute("UPDATE review_item SET payload = ?, snoozed_until = NULL WHERE id = ?", (json.dumps(p), rid))


def process_due(conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    """Called every worker tick: send the recordings whose Process at time has come. Returns their recording ids."""
    import json

    from scribe_bot.core.db import now as utc_now
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    done = []
    for r in conn.execute("SELECT * FROM review_item WHERE kind = 'approval' AND status = 'open' AND conversation_id IS NULL "
                          "AND ref_id IS NOT NULL AND json_extract(payload, '$.process_at') <= ?", (stamp,)).fetchall():
        p = json.loads(r["payload"] or "{}")
        with transaction(conn):
            approve_recording(conn, r, bool(p.get("process_ai")), p.get("process_type"))
            conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE id = ?", (utc_now(), r["id"]))
        done.append(r["ref_id"])
    return done


def overnight_next(conn: sqlite3.Connection, now: datetime | None = None) -> int | None:
    """Overnight backlog (Brian, 2026-11-22: "run through as many recordings as it can every night"). From 1 to 7 AM,
    when nothing is running or waiting, send the oldest Inbox recording as Transcript only (speech to text + who spoke,
    no AI, no cost). One at a time, so it simply stops starting new ones at 7 AM. Recordings with a Process at time are
    left for that time. Returns the recording id sent, or None."""
    import json

    from scribe_bot.core.db import now as utc_now
    from scribe_bot.pipeline import pace
    if not pace.night_window(now) or pace.heat_paused(conn):
        return None
    if conn.execute("SELECT 1 FROM job WHERE status IN ('pending', 'running') AND kind = 'recording'").fetchone():
        return None
    r = conn.execute("SELECT * FROM review_item WHERE kind = 'approval' AND status = 'open' AND conversation_id IS NULL "
                     "AND ref_id IS NOT NULL AND json_extract(payload, '$.process_at') IS NULL ORDER BY ref_id LIMIT 1").fetchone()
    if not r:
        return None
    p = json.loads(r["payload"] or "{}")
    with transaction(conn):
        approve_recording(conn, r, False, p.get("process_type"))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE id = ?", (utc_now(), r["id"]))
    return r["ref_id"]
