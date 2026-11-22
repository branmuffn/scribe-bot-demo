"""Undo bar: an undoable action saves the previous values of the rows it changes; Undo writes them back and fixes search.

Every undoable action is a soft change (status, rating, owner, text), so restoring the saved columns is a full undo.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timedelta

from scribe_bot.ai import extract
from scribe_bot.core import db, people

SHOW_FOR_S = 15          # the bar shows on pages rendered this soon after the action
UNDO_WITHIN_S = 600      # Undo still works this long after (a slow tap, a phone that slept)

# Columns each undoable action may change. Table names come from code only, never from a request.
COLUMNS = {
    "task": ("title", "owner_person_id", "due", "status", "rated", "inbox", "area", "confidence", "updated_at"),
    "decision": ("rated", "text", "people"),
    "conversation": ("title", "summary", "takeaways", "deleted_at"),
    "person_fact": ("person_id", "person_name", "confirmed", "value"),
    "segment": ("speaker_id", "text", "idx", "start_s", "end_s"),   # idx/times: split and join (2026-11-20)
    "speaker": ("talk_seconds", "person_id", "confirmed", "match_score"),
    "review_item": ("status", "resolved_at", "snoozed_until"),
    "notification": ("dismissed_at",),
    "recording": ("status",),   # Inbox Skip / Remove can be undone (2026-11-21)
}


def save(conn: sqlite3.Connection, label: str, rows: list[tuple[str, int]]) -> int:
    """Call before changing `rows`. Returns the undo id (pass it to `created` for rows the action adds)."""
    snap = []
    for table, rid in rows:
        r = conn.execute(f"SELECT {', '.join(COLUMNS[table])} FROM {table} WHERE id = ?", (rid,)).fetchone()
        if r is not None:
            snap.append({"table": table, "id": rid, "before": dict(r)})
    return conn.execute("INSERT INTO undo_action (label, snapshot, created_at) VALUES (?, ?, ?)",
                        (label, json.dumps(snap), db.now())).lastrowid


# Rows an action deletes outright (a person and their voiceprint): saved whole, put back first on Undo.
DELETABLE = ("person", "voiceprint", "segment")   # segment: "Join with line above" (put back after the restore)


def removed(conn: sqlite3.Connection, uid: int, table: str, where: str, args: tuple) -> None:
    """Call before deleting rows of a DELETABLE table: they're saved whole (blobs as base64) in the undo action."""
    assert table in DELETABLE
    rows = [{k: ({"b64": base64.b64encode(v).decode()} if isinstance(v, bytes) else v) for k, v in dict(r).items()}
            for r in conn.execute(f"SELECT * FROM {table} WHERE {where}", args)]
    a = conn.execute("SELECT removed FROM undo_action WHERE id = ?", (uid,)).fetchone()
    conn.execute("UPDATE undo_action SET removed = ? WHERE id = ?",
                 (json.dumps([*json.loads(a["removed"]), *({"table": table, "row": r} for r in rows)]), uid))


def transcript(conn: sqlite3.Connection, uid: int, cid: int) -> None:
    """Call before splitting or joining lines: the conversation's whole transcript is saved, and Undo puts it back
    exactly (line numbers, times, words, speakers). Rows are updated in place, so tasks pointing at lines keep them."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM segment WHERE conversation_id = ?", (cid,))]
    a = conn.execute("SELECT removed FROM undo_action WHERE id = ?", (uid,)).fetchone()
    conn.execute("UPDATE undo_action SET removed = ? WHERE id = ?",
                 (json.dumps([*json.loads(a["removed"]), {"table": "transcript", "cid": cid, "rows": rows}]), uid))


def _restore_transcript(conn: sqlite3.Connection, cid: int, rows: list[dict]) -> None:
    keep = {r["id"] for r in rows}
    conn.execute("UPDATE segment SET idx = -id WHERE conversation_id = ?", (cid,))   # free every line number first
    for (sid,) in conn.execute("SELECT id FROM segment WHERE conversation_id = ?", (cid,)).fetchall():
        if sid not in keep:   # a line the fix added
            conn.execute("DELETE FROM segment WHERE id = ?", (sid,))
            conn.execute("DELETE FROM search_index WHERE kind = 'segment' AND ref_id = ?", (sid,))
    for r in rows:
        cols = [c for c in r if c != "id"]
        if conn.execute("SELECT 1 FROM segment WHERE id = ?", (r["id"],)).fetchone():
            conn.execute(f"UPDATE segment SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?", [r[c] for c in cols] + [r["id"]])
        else:   # a line the fix joined into another
            conn.execute(f"INSERT INTO segment (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                         [r["id"]] + [r[c] for c in cols])
        _reindex(conn, "segment", r["id"])


def created(conn: sqlite3.Connection, uid: int, table: str, rid: int) -> None:
    row = conn.execute("SELECT created FROM undo_action WHERE id = ?", (uid,)).fetchone()
    conn.execute("UPDATE undo_action SET created = ? WHERE id = ?",
                 (json.dumps([*json.loads(row["created"]), {"table": table, "id": rid}]), uid))


def _since(seconds: int) -> str:
    return (datetime.fromisoformat(db.now()) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def latest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The action the bar offers to undo, if it just happened."""
    return conn.execute("SELECT * FROM undo_action WHERE undone_at IS NULL AND created_at >= ? ORDER BY id DESC LIMIT 1",
                        (_since(SHOW_FOR_S),)).fetchone()


def undo(conn: sqlite3.Connection, uid: int) -> bool:
    """Write the saved values back. False if it's too old or already undone."""
    a = conn.execute("SELECT * FROM undo_action WHERE id = ? AND undone_at IS NULL AND created_at >= ?",
                     (uid, _since(UNDO_WITHIN_S))).fetchone()
    if a is None:
        return False
    new_ids: dict[int, int] = {}   # deleted person id -> id it comes back as (ids are reused: a newer person may hold it)

    def person(pid):
        return new_ids.get(pid, pid)

    with db.transaction(conn):
        for c in json.loads(a["created"]):   # lines a split added go first, freeing their line numbers
            if c["table"] == "segment":
                conn.execute("DELETE FROM segment WHERE id = ?", (c["id"],))
                conn.execute("DELETE FROM search_index WHERE kind = 'segment' AND ref_id = ?", (c["id"],))
        removed_rows = json.loads(a["removed"])
        for d in removed_rows:
            if d["table"] == "transcript":
                _restore_transcript(conn, d["cid"], d["rows"])
        later = [d for d in removed_rows if d["table"] == "segment"]   # a joined line returns once the lines below moved back
        for d in [d for d in removed_rows if d["table"] not in ("segment", "transcript")]:   # deleted rows go back first
            row = {k: (base64.b64decode(v["b64"]) if isinstance(v, dict) else v) for k, v in d["row"].items()}
            if d["table"] == "voiceprint":
                row["person_id"] = person(row["person_id"])
            old = row["id"]
            if conn.execute(f"SELECT 1 FROM {d['table']} WHERE id = ?", (old,)).fetchone():
                del row["id"]   # taken since: comes back under a new id
            rid = conn.execute(f"INSERT INTO {d['table']} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               list(row.values())).lastrowid
            if d["table"] == "person":
                new_ids[old] = rid
                people.reindex(conn, rid)
        for s in json.loads(a["snapshot"]):
            before = dict(s["before"])
            for c in ("person_id", "owner_person_id"):
                if before.get(c) is not None:
                    before[c] = person(before[c])
            if s["table"] == "decision" and new_ids and before.get("people"):
                before["people"] = json.dumps([person(p) for p in json.loads(before["people"])])
            cols = [c for c in before if c in COLUMNS[s["table"]]]
            conn.execute(f"UPDATE {s['table']} SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                         [before[c] for c in cols] + [s["id"]])
            _reindex(conn, s["table"], s["id"])
        for d in later:
            row = d["row"]
            conn.execute(f"INSERT INTO segment ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
            _reindex(conn, "segment", row["id"])
        for c in json.loads(a["created"]):
            if c["table"] == "speaker":   # a speaker added for one line goes away again if no line uses it
                conn.execute("DELETE FROM speaker WHERE id = ? AND NOT EXISTS (SELECT 1 FROM segment WHERE speaker_id = ?)",
                             (c["id"], c["id"]))
        conn.execute("UPDATE undo_action SET undone_at = ? WHERE id = ?", (db.now(), uid))
    return True


def _reindex(conn: sqlite3.Connection, table: str, rid: int) -> None:
    """Make search match the restored row (same text the pipeline indexes)."""
    if table == "conversation":
        extract.reindex_ai_text(conn, rid)
        return
    kind, sql = {   # (search kind, SELECT text, conversation_id, still-visible)
        "task": ("task", "SELECT title, conversation_id, status != 'dropped' FROM task WHERE id = ?"),
        "decision": ("decision", "SELECT text, conversation_id, coalesce(rated, '') != 'wrong' FROM decision WHERE id = ?"),
        "person_fact": ("fact", "SELECT coalesce(person_name, '') || ': ' || value, conversation_id, coalesce(confirmed, 1) != 0 "
                                "FROM person_fact WHERE id = ?"),
        "segment": ("segment", "SELECT text, conversation_id, 1 FROM segment WHERE id = ?"),
    }.get(table, (None, None))
    if kind is None:
        return
    row = conn.execute(sql, (rid,)).fetchone()
    conn.execute("DELETE FROM search_index WHERE kind = ? AND ref_id = ?", (kind, rid))
    if row and row[2]:
        conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES (?, ?, ?, ?)",
                     (kind, rid, row[1], row[0]))
