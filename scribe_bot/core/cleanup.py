"""Weekly task cleanup, Sundays 5:30 PM (just before the weekly review). Nothing is deleted:
- done or removed tasks untouched for ARCHIVE_AFTER_DAYS leave the Done list (still in search and on their conversation);
- open tasks with no due date untouched for STALE_AFTER_DAYS get a "Still need this?" flag, shown on Today.
A flag counts only while the task is untouched since it was set (STALE), so any edit, Done or "Still need it" clears it,
and the question comes back only after another STALE_AFTER_DAYS of no activity.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from scribe_bot.core import db, settings
from scribe_bot.core.scheduler import Missed, Schedule

ARCHIVE_AFTER_DAYS = 30
STALE_AFTER_DAYS = 30
STALE = "t.status = 'open' AND t.stale_at IS NOT NULL AND t.stale_at >= t.updated_at"   # flagged and not answered yet


def run(conn: sqlite3.Connection, at: datetime | None = None) -> dict[str, int]:
    at = at or datetime.now(timezone.utc)
    stamp = at.astimezone(timezone.utc).isoformat(timespec="seconds")

    def cutoff(days: int) -> str:
        return (at - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds")

    with db.transaction(conn):
        archived = conn.execute(
            "UPDATE task SET archived_at = ? WHERE status IN ('done', 'dropped') AND archived_at IS NULL AND updated_at < ?",
            (stamp, cutoff(ARCHIVE_AFTER_DAYS))).rowcount
        stale = conn.execute(
            "UPDATE task SET stale_at = ? WHERE status = 'open' AND inbox = 0 AND due IS NULL AND updated_at < ? "
            "AND (stale_at IS NULL OR stale_at < updated_at)", (stamp, cutoff(STALE_AFTER_DAYS))).rowcount
    return {"archived": archived, "stale": stale}


def _job(missed: Missed) -> None:
    conn = db.connect(settings.load().db_path)
    try:
        run(conn)
    finally:
        conn.close()


SCHEDULES = [Schedule("task-cleanup", hour=17, minute=30, weekday=6, run=_job)]
