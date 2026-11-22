"""People: find-or-create by name, merge duplicates, and spot likely duplicates.

Merging moves every reference (speakers, tasks, facts, decisions, voiceprints, aliases, search rows) onto one person
in a single transaction. Brian (person 1) can absorb others but is never merged away.
"""
from __future__ import annotations

import json
import math
import sqlite3
from array import array

from scribe_bot.core.db import now, transaction

ME = 1


def _blob_to_vec(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


def _vec_to_blob(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _normalise(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def aliases_of(row: sqlite3.Row) -> list[str]:
    return json.loads(row["aliases"] or "[]")


def find(conn: sqlite3.Connection, name: str) -> int | None:
    """An existing person whose name or alias matches (case-insensitive)."""
    key = name.strip().lower()
    for r in conn.execute("SELECT id, name, aliases FROM person ORDER BY id"):
        if r["name"].lower() == key or key in (a.lower() for a in aliases_of(r)):
            return r["id"]
    return None


def find_or_create(conn: sqlite3.Connection, name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Name needed")
    pid = find(conn, name)
    if pid is not None:
        return pid
    pid = conn.execute("INSERT INTO person (name, created_at) VALUES (?, ?)", (name, now())).lastrowid
    reindex(conn, pid)
    return pid


def reindex(conn: sqlite3.Connection, pid: int) -> None:
    p = conn.execute("SELECT name, aliases, notes FROM person WHERE id = ?", (pid,)).fetchone()
    conn.execute("DELETE FROM search_index WHERE kind = 'person' AND ref_id = ?", (pid,))
    if p:
        text = " ".join(x for x in [p["name"], *aliases_of(p), p["notes"] or ""] if x)
        conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('person', ?, NULL, ?)", (pid, text))


def preview(conn: sqlite3.Connection, from_id: int, into_id: int) -> dict:
    """What a merge would move: shown to Brian before he confirms."""
    one = lambda sql: conn.execute(sql, (from_id,)).fetchone()[0]  # noqa: E731
    src = conn.execute("SELECT name, aliases FROM person WHERE id = ?", (from_id,)).fetchone()
    dst = conn.execute("SELECT name FROM person WHERE id = ?", (into_id,)).fetchone()
    if src is None or dst is None:
        raise LookupError("No such person")
    return {
        "from_name": " / ".join([src["name"], *aliases_of(src)]), "into_name": dst["name"],
        "tasks": one("SELECT count(*) FROM task WHERE owner_person_id = ?"),
        "notes": one("SELECT count(*) FROM person_fact WHERE person_id = ?"),
        "conversations": one("SELECT count(DISTINCT conversation_id) FROM speaker WHERE person_id = ?"),
        "decisions": sum(1 for (p,) in conn.execute("SELECT people FROM decision") if from_id in json.loads(p or "[]")),
        "has_voice": bool(one("SELECT count(*) FROM voiceprint WHERE person_id = ?")),
    }


def merge(conn: sqlite3.Connection, from_id: int, into_id: int) -> None:
    """Fold from_id into into_id and delete from_id. Brian (1) can't be merged away."""
    if from_id == ME:
        raise ValueError("Brian can't be merged into someone else")
    if from_id == into_id:
        raise ValueError("Pick two different people")
    src = conn.execute("SELECT * FROM person WHERE id = ?", (from_id,)).fetchone()
    dst = conn.execute("SELECT * FROM person WHERE id = ?", (into_id,)).fetchone()
    if src is None or dst is None:
        raise LookupError("No such person")
    with transaction(conn):
        conn.execute("UPDATE speaker SET person_id = ? WHERE person_id = ?", (into_id, from_id))
        conn.execute("UPDATE task SET owner_person_id = ? WHERE owner_person_id = ?", (into_id, from_id))
        conn.execute("UPDATE person_fact SET person_id = ?, person_name = ? WHERE person_id = ?", (into_id, dst["name"], from_id))
        for did, people in conn.execute("SELECT id, people FROM decision").fetchall():
            ids = json.loads(people or "[]")
            if from_id in ids:
                merged = list(dict.fromkeys(into_id if i == from_id else i for i in ids))
                conn.execute("UPDATE decision SET people = ? WHERE id = ?", (json.dumps(merged), did))
        _merge_voiceprints(conn, from_id, into_id)
        names = [dst["name"], *aliases_of(dst)]
        new_aliases = [*aliases_of(dst)]
        for a in [src["name"], *aliases_of(src)]:
            if a.lower() not in (n.lower() for n in names) and not a.lower().startswith("speaker "):
                new_aliases.append(a)
                names.append(a)
        notes = "\n".join(x for x in (dst["notes"], src["notes"]) if x) or None
        last = max(x for x in (dst["last_spoken_at"], src["last_spoken_at"], "") if x is not None) or None
        area = dst["area"] if dst["area"] == src["area"] else "both"
        conn.execute("UPDATE person SET aliases = ?, notes = ?, last_spoken_at = ?, area = ? WHERE id = ?",
                     (json.dumps(new_aliases), notes, last, area, into_id))
        for r in conn.execute("SELECT id, payload FROM review_item WHERE status = 'open'").fetchall():
            p = json.loads(r["payload"] or "{}")
            if p.get("person_id") == from_id:
                p["person_id"] = into_id
                conn.execute("UPDATE review_item SET payload = ? WHERE id = ?", (json.dumps(p), r["id"]))
        conn.execute("DELETE FROM search_index WHERE kind = 'person' AND ref_id = ?", (from_id,))
        conn.execute("DELETE FROM person WHERE id = ?", (from_id,))
        reindex(conn, into_id)


def _merge_voiceprints(conn: sqlite3.Connection, from_id: int, into_id: int) -> None:
    """Sample-count-weighted average of the two normalised voiceprints (or just move the one that exists)."""
    src = conn.execute("SELECT * FROM voiceprint WHERE person_id = ?", (from_id,)).fetchall()
    if not src:
        return
    dst = conn.execute("SELECT * FROM voiceprint WHERE person_id = ?", (into_id,)).fetchall()
    rows = [*dst, *src]
    total = sum(r["sample_count"] for r in rows) or 1
    vecs = [_blob_to_vec(r["embedding"]) for r in rows]
    combined = _normalise([sum(v[i] * r["sample_count"] for v, r in zip(vecs, rows)) / total for i in range(len(vecs[0]))])
    conn.execute("DELETE FROM voiceprint WHERE person_id IN (?, ?)", (from_id, into_id))
    conn.execute("INSERT INTO voiceprint (person_id, embedding, sample_count, updated_at) VALUES (?, ?, ?, ?)",
                 (into_id, _vec_to_blob(combined), total, now()))


def duplicate_suggestions(conn: sqlite3.Connection) -> list[dict]:
    """Pairs that are probably the same person: same first name, or one's name is the other's alias.
    Grouped by name/first-name key, so only people who share a key are compared (not every pair)."""
    rows = conn.execute("SELECT id, name, aliases FROM person WHERE stands_for_speaker IS NULL ORDER BY id").fetchall()   # unnamed voices wait in their own row
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        first = r["name"].split()[0].lower()
        keys = {r["name"].lower(), *(x.lower() for x in aliases_of(r))}
        if not first.startswith("speaker"):
            keys.add("first:" + first)
        for k in keys:
            groups.setdefault(k, []).append(i)
    pairs = sorted({(g[x], g[y]) for g in groups.values() for x in range(len(g)) for y in range(x + 1, len(g))})
    out, seen = [], set()
    for i, j in pairs:
        a, b = rows[i], rows[j]
        keep, drop = (a, b) if a["id"] == ME or a["id"] < b["id"] else (b, a)
        if drop["id"] == ME:
            keep, drop = drop, keep
        key = (drop["id"], keep["id"])
        if key not in seen:
            seen.add(key)
            out.append({"from_id": drop["id"], "from_name": drop["name"], "into_id": keep["id"], "into_name": keep["name"]})
    return out


def delete(conn: sqlite3.Connection, pid: int) -> dict:
    """Delete a person. Their transcript lines become an unnamed speaker, their voiceprint goes, tasks they owned go to
    the Inbox (as Brian's, to sort), notes about them lose the link. Brian (1) can't be deleted.
    Call inside a transaction; for Undo, save undo_rows() and the person + voiceprint rows first (web/undo.py)."""
    if pid == ME:
        raise ValueError("You can't delete yourself")
    p = conn.execute("SELECT * FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise LookupError("No such person")
    counts = {"lines": conn.execute("SELECT count(*) FROM segment g JOIN speaker s ON s.id = g.speaker_id WHERE s.person_id = ?",
                                    (pid,)).fetchone()[0],
              "tasks": conn.execute("SELECT count(*) FROM task WHERE owner_person_id = ?", (pid,)).fetchone()[0]}
    conn.execute("UPDATE speaker SET person_id = NULL, confirmed = 0, match_score = NULL WHERE person_id = ?", (pid,))
    conn.execute("UPDATE task SET owner_person_id = ?, inbox = 1, updated_at = ? WHERE owner_person_id = ?", (ME, now(), pid))
    conn.execute("UPDATE person_fact SET person_id = NULL WHERE person_id = ?", (pid,))
    for did, ids in conn.execute("SELECT id, people FROM decision").fetchall():
        ids = json.loads(ids or "[]")
        if pid in ids:
            conn.execute("UPDATE decision SET people = ? WHERE id = ?", (json.dumps([i for i in ids if i != pid]), did))
    conn.execute("DELETE FROM voiceprint WHERE person_id = ?", (pid,))
    conn.execute("DELETE FROM search_index WHERE kind = 'person' AND ref_id = ?", (pid,))
    conn.execute("DELETE FROM person WHERE id = ?", (pid,))
    return {"name": p["name"], **counts}


def undo_rows(conn: sqlite3.Connection, pid: int) -> list[tuple[str, int]]:
    """The rows delete() changes, for the undo snapshot."""
    rows = [("speaker", r[0]) for r in conn.execute("SELECT id FROM speaker WHERE person_id = ?", (pid,))]
    rows += [("task", r[0]) for r in conn.execute("SELECT id FROM task WHERE owner_person_id = ?", (pid,))]
    rows += [("person_fact", r[0]) for r in conn.execute("SELECT id FROM person_fact WHERE person_id = ?", (pid,))]
    rows += [("decision", d) for d, ids in conn.execute("SELECT id, people FROM decision") if pid in json.loads(ids or "[]")]
    return rows


# ---- People mentioned but not added yet (Brian, 2026-11-20) ----
# The AI saves facts about people who were talked about; a fact links to a person only when that name or nickname
# already exists (so a passing name never makes a junk person). These are the rest, grouped by name, for People.
UNLINKED = ("f.person_id IS NULL AND coalesce(trim(f.person_name), '') != '' AND lower(f.person_name) NOT LIKE 'speaker %' "
            "AND lower(f.person_name) NOT IN ('unknown', 'me', 'brian') AND coalesce(f.confirmed, 1) != 0 "
            "AND NOT EXISTS (SELECT 1 FROM conversation z WHERE z.id = f.conversation_id AND z.deleted_at IS NOT NULL)")


def mentioned(conn: sqlite3.Connection, hidden: list[str] | None = None) -> list[dict]:
    hide = {h.lower() for h in hidden or []}
    groups: dict[str, dict] = {}
    for f in conn.execute(f"SELECT f.id, trim(f.person_name) AS name, f.value, f.conversation_id, f.created_at "
                          f"FROM person_fact f WHERE {UNLINKED} ORDER BY f.created_at DESC"):
        key = f["name"].lower()
        if key in hide:
            continue
        g = groups.setdefault(key, {"name": f["name"], "facts": 0, "convs": set(), "last": f["created_at"], "samples": []})
        g["facts"] += 1
        g["convs"].add(f["conversation_id"])
        if len(g["samples"]) < 2:
            g["samples"].append(f["value"])
    out = [{**g, "convs": len(g["convs"])} for g in groups.values()]
    return sorted(out, key=lambda g: (-g["facts"], g["name"].lower()))


def unlinked_fact_ids(conn: sqlite3.Connection, name: str) -> list[int]:
    return [r[0] for r in conn.execute(f"SELECT f.id FROM person_fact f WHERE {UNLINKED} AND lower(trim(f.person_name)) = ?",
                                       (name.strip().lower(),))]


def link_mentioned(conn: sqlite3.Connection, name: str, pid: int, as_alias: bool = False) -> int:
    """Move every unlinked fact about `name` to person `pid`; with as_alias, `name` also becomes their nickname, so
    future mentions link by themselves."""
    ids = unlinked_fact_ids(conn, name)
    if ids:
        conn.execute(f"UPDATE person_fact SET person_id = ? WHERE id IN ({','.join('?' * len(ids))})", (pid, *ids))
    p = conn.execute("SELECT name, aliases FROM person WHERE id = ?", (pid,)).fetchone()
    if as_alias and p and name.strip().lower() != p["name"].lower():
        al = aliases_of(p)
        if name.strip().lower() not in (a.lower() for a in al):
            conn.execute("UPDATE person SET aliases = ? WHERE id = ?", (json.dumps(al + [name.strip()]), pid))
            reindex(conn, pid)
    return len(ids)
