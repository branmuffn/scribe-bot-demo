"""Trash: conversations deleted in the app sit in Trash for 30 days, then are purged (SPEC §10 Retention).

Purging removes the conversation and everything that cascades from it (segments, speakers, decisions, people notes,
review items) plus its search rows and its tasks (Brian's DECISION Q1 A, 2026-10-20: no loose tasks left behind).
The recording and its audio are kept (retention: audio forever); deleting audio is a separate, explicit action.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from scribe_bot.core.db import transaction

TRASH_DAYS = 30


def purge(conn: sqlite3.Connection, at: datetime | None = None, days: int = TRASH_DAYS) -> int:
    at = at or datetime.now(timezone.utc)
    cutoff = (at - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM conversation WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with transaction(conn):
        conn.execute(f"DELETE FROM search_index WHERE conversation_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM review_item WHERE kind = 'inbox' AND ref_id IN (SELECT id FROM task WHERE conversation_id IN ({marks}))", ids)
        conn.execute(f"DELETE FROM task WHERE conversation_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM conversation WHERE id IN ({marks})", ids)
    return len(ids)
