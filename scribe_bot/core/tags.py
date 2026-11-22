"""Tags on talks (M4 A, Brian picked 2026-11-20): free tags you type; the AI suggests only tags that already exist
(it never invents one). A tag with no talks left is removed."""
from __future__ import annotations

import sqlite3

from scribe_bot.core.db import now

MAX_LEN = 40


def clean(name: str) -> str:
    return " ".join((name or "").replace(",", " ").split())[:MAX_LEN]


def names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM tag ORDER BY name COLLATE NOCASE")]


def counts(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.name, count(ct.conversation_id) AS n FROM tag t LEFT JOIN conversation_tag ct ON ct.tag_id = t.id "
        "LEFT JOIN conversation c ON c.id = ct.conversation_id AND c.deleted_at IS NULL GROUP BY t.id ORDER BY t.name COLLATE NOCASE")]


def of(conn: sqlite3.Connection, cid: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.name, ct.by_ai FROM conversation_tag ct JOIN tag t ON t.id = ct.tag_id WHERE ct.conversation_id = ? "
        "ORDER BY t.name COLLATE NOCASE", (cid,))]


def add(conn: sqlite3.Connection, cid: int, name: str, by_ai: bool = False) -> int | None:
    name = clean(name)
    if not name:
        return None
    row = conn.execute("SELECT id FROM tag WHERE name = ?", (name,)).fetchone()
    if row is None:
        if by_ai:
            return None   # the AI never makes a new tag
        tid = conn.execute("INSERT INTO tag (name, created_at) VALUES (?, ?)", (name, now())).lastrowid
    else:
        tid = row[0]
    conn.execute("INSERT OR IGNORE INTO conversation_tag (conversation_id, tag_id, by_ai, created_at) VALUES (?, ?, ?, ?)",
                 (cid, tid, 1 if by_ai else 0, now()))
    return tid


def remove(conn: sqlite3.Connection, cid: int, tid: int) -> None:
    conn.execute("DELETE FROM conversation_tag WHERE conversation_id = ? AND tag_id = ?", (cid, tid))
    conn.execute("DELETE FROM tag WHERE id = ? AND NOT EXISTS (SELECT 1 FROM conversation_tag WHERE tag_id = ?)", (tid, tid))


def keep(conn: sqlite3.Connection, cid: int, tid: int) -> None:
    """You agree with the AI's tag: it becomes yours."""
    conn.execute("UPDATE conversation_tag SET by_ai = 0 WHERE conversation_id = ? AND tag_id = ?", (cid, tid))
