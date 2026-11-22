"""Delete-from-your-devices checklist (Brian, 2026-11-21). When the nightly Trash purge deletes a recording's audio,
the recording is added here so Brian can delete it from the Pixel (Recorder) or the M4 (Voice Memos) too. Scribe Bot
never touches those devices itself: the phone sync is one-way. One Discord note per night, a pop-up on Today
(Add to my cleanup list / Keep them on my devices), and the /cleanup checklist."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from scribe_bot.pipeline.voicememo import name_date

DEVICES = {"pixel": "Pixel · Recorder", "m4": "M4 · Voice Memos", "other": "Other uploads"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def device_of(source: str, title: str | None) -> str:
    if source == "recorder":
        return "pixel"
    return "m4" if name_date(title) else "other"


def add(conn: sqlite3.Connection, recording_ids: list[int]) -> int:
    """Called by the purge after the audio is gone. Merged recordings (made on the M14) are skipped: their original
    parts are added instead, since those are what's on the devices. Returns how many were added."""
    n = 0
    for rid in recording_ids:
        r = conn.execute("SELECT id, source, title, original_path, received_at, duration_s FROM recording WHERE id = ?",
                         (rid,)).fetchone()
        if not r or conn.execute("SELECT 1 FROM recording_part WHERE recording_id = ?", (rid,)).fetchone():
            continue
        title = r["title"] or (r["original_path"] or "").rsplit("/", 1)[-1] or "Recording"
        when = name_date(title)
        cur = conn.execute("INSERT OR IGNORE INTO device_cleanup (recording_id, device, title, recorded_at, duration_s, "
                           "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (rid, device_of(r["source"], title), title,
                            when.isoformat(timespec="seconds") if when else r["received_at"], r["duration_s"], _now(), _now()))
        n += cur.rowcount
    return n


def note(conn: sqlite3.Connection, added: int) -> None:
    """One Discord note per night that something was added (titles stay out of it, as with every Discord note)."""
    if not added:
        return
    from scribe_bot.discord_bot import notify
    per = dict(conn.execute("SELECT device, count(*) FROM device_cleanup WHERE status = 'ask' GROUP BY device").fetchall())
    where = ", ".join(f"{n} on the {DEVICES[d].replace(' · ', ' (')})" for d, n in per.items() if d != "other")
    notify.enqueue(conn, "bot", f"🧹 {added} recording{'s' if added != 1 else ''} deleted from Scribe Bot (30 days in Trash). "
                   f"Delete {'them' if added != 1 else 'it'} from your devices too{': ' + where if where else ''}.", "/cleanup")


def counts(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT status, count(*) FROM device_cleanup GROUP BY status").fetchall())


def checklist(conn: sqlite3.Connection) -> list[dict]:
    """To do, plus ones ticked in the last day (shown crossed out, so a mis-tap can be undone)."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT * FROM device_cleanup WHERE status = 'todo' OR (status = 'done' AND updated_at > ?) "
                        "ORDER BY device = 'pixel' DESC, device = 'm4' DESC, recorded_at", (since,)).fetchall()
    return [dict(r) for r in rows]


def set_status(conn: sqlite3.Connection, ids: list[int] | None, status: str, only: str | None = None) -> None:
    sql, args = "UPDATE device_cleanup SET status = ?, updated_at = ?", [status, _now()]
    if ids is not None:
        sql += f" WHERE id IN ({','.join('?' * len(ids))})"; args += ids
    else:
        sql += " WHERE 1"
    if only:
        sql += " AND status = ?"; args.append(only)
    conn.execute(sql, args)
