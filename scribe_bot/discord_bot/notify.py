"""Contract for every milestone. M5 replaces the sending side; enqueue() is final.

enqueue() only writes a notification row; the worker's sender delivers queued rows to Discord, holding them during
quiet hours (11 PM-7 AM) and when Discord is unreachable.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from scribe_bot.core.db import now


# Brian DECISIONS 2026-10-22: Discord carries alerts (#scribe-alerts: the M14, backups, AI spend) plus two recording
# pings on #scribe-bot, "New recording waiting for your OK" and "Ready". Processing started and briefings pass
# discord=False and stay in the app's 🔔 list.
DISCORD_CHANNELS = ("alerts", "bot")


def enqueue(conn: sqlite3.Connection, channel: str, title: str, link: str | None = None, discord: bool = True) -> int:
    """channel: 'bot' (recording pings, briefings) or 'alerts'. Title must not contain content. Only DISCORD_CHANNELS are
    posted; anything else, or discord=False, is shown in the app only (stored as already 'sent' so the sender skips it)."""
    post = discord and channel in DISCORD_CHANNELS
    cur = conn.execute("INSERT INTO notification (channel, title, link, status, created_at) VALUES (?, ?, ?, ?, ?)",
                       (channel, title, link, "queued" if post else "sent", now()))
    return cur.lastrowid


# Notifications whose job is done move to Dismissed by themselves (Brian 2026-10-22: no reminders about things he has
# already handled). Each rule only touches current (not dismissed) rows; Undo is still one tap on Dismissed.
TIDY_RULES = (
    # "New recording waiting for your OK": nothing is waiting any more
    "title LIKE 'New recording waiting%' AND NOT EXISTS (SELECT 1 FROM review_item r WHERE r.kind = 'approval' "
    "AND r.status = 'open')",
    # Ready ping: Brian has opened that conversation since
    "link LIKE '/conversations/%' AND EXISTS (SELECT 1 FROM conversation c WHERE c.id = CAST(substr(link, 16) AS INTEGER) "
    "AND (c.opened_at >= notification.created_at OR c.deleted_at IS NOT NULL))",
    # "Processing started": that job has finished (the Ready ping follows)
    "link LIKE '/?job=%' AND EXISTS (SELECT 1 FROM job j WHERE j.id = CAST(substr(link, 7) AS INTEGER) "
    "AND j.status NOT IN ('pending', 'running'))",
    # Briefings and weekly reviews: replaced by the next one; stale after 18 h
    "(title LIKE '☀️%' OR title LIKE '🗓️%') AND created_at < :stale",
)


def tidy(conn: sqlite3.Connection) -> int:
    t = now()
    stale = (datetime.now(timezone.utc) - timedelta(hours=18)).isoformat(timespec="seconds")
    n = 0
    for rule in TIDY_RULES:   # read first: a page load only takes the write lock when something really changes
        args = {"t": t, "stale": stale}
        if conn.execute(f"SELECT 1 FROM notification WHERE dismissed_at IS NULL AND {rule} LIMIT 1", args).fetchone():
            n += conn.execute(f"UPDATE notification SET dismissed_at = :t WHERE dismissed_at IS NULL AND {rule}", args).rowcount
    return n
