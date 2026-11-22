"""Nightly plain-file export (SPEC §3.11, A5): every conversation as a Markdown file in ~/scribe-bot-data/export/.

Readable without Scribe Bot. Files are rewritten only when their content changes; trashed conversations' files are
removed. The export folder is inside the data folder, so it is also in the encrypted backups. It never leaves the M14
otherwise.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from scribe_bot.core.settings import Settings


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "untitled"


def _mmss(s: float) -> str:
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{int(s % 60):02d}" if s >= 3600 else f"{int(s // 60)}:{int(s % 60):02d}"


def filename(c: sqlite3.Row) -> str:
    day = (c["started_at"] or c["created_at"])[:10]
    return f"{day}-{c['id']}-{_slug(c['title'])}.md"


def render(conn: sqlite3.Connection, c: sqlite3.Row) -> str:
    cid = c["id"]
    people = conn.execute("SELECT p.name, s.talk_seconds FROM speaker s LEFT JOIN person p ON p.id = s.person_id "
                          "WHERE s.conversation_id = ? ORDER BY s.talk_seconds DESC", (cid,)).fetchall()
    meta = [f"- Date: {c['started_at'] or c['created_at']}"]
    if c["duration_s"]:
        meta.append(f"- Length: {round(c['duration_s'] / 60)} min")
    if people:
        meta.append("- People: " + ", ".join(p["name"] or "Unknown" for p in people))
    meta.append(f"- Area: {c['area'] or 'unknown'} · Type: {c['type'] or 'unknown'}"
                + (" · **Private** (never left the M14)" if c["private"] else ""))
    if c["processed_by"]:
        meta.append(f"- Processed by: {c['processed_by']}")
    out = [f"# {c['title']}", "", *meta, ""]
    if c["summary"]:
        out += ["## Summary", "", c["summary"], ""]
    takeaways = json.loads(c["takeaways"]) if c["takeaways"] else []
    if takeaways:
        out += ["## Key takeaways", "", *[f"- {t}" for t in takeaways], ""]

    def tasks(mine: bool) -> list[str]:
        rows = conn.execute("SELECT t.title, t.due, t.status, t.closed_as, p.name FROM task t JOIN person p ON p.id = t.owner_person_id "
                            f"WHERE t.conversation_id = ? AND t.owner_person_id {'=' if mine else '!='} 1 AND t.status != 'dropped' "
                            "ORDER BY t.id",
                            (cid,)).fetchall()
        return [f"- [{'x' if r['status'] == 'done' else ' '}] {r['title']}" + (" (not needed)" if r["closed_as"] else "")
                + ("" if mine else f" ({r['name']})") + (f" · due {r['due']}" if r["due"] else "") for r in rows]

    for heading, lines in (("You owe", tasks(True)), ("They owe you", tasks(False)),
                           ("Decisions", [f"- {r[0]}" for r in conn.execute(
                               "SELECT text FROM decision WHERE conversation_id = ? AND coalesce(rated, '') != 'wrong' ORDER BY id",
                               (cid,))]),
                           ("People notes", [f"- {r[0] or r[1] or 'Unknown'}: {r[2]}" for r in conn.execute(
                               "SELECT p.name, f.person_name, f.value FROM person_fact f LEFT JOIN person p ON p.id = f.person_id "
                               "WHERE f.conversation_id = ? AND coalesce(f.confirmed, 1) = 1 ORDER BY f.id", (cid,))])):
        if lines:
            out += [f"## {heading}", "", *lines, ""]
    segs = conn.execute("SELECT g.start_s, g.text, p.name, s.cluster FROM segment g LEFT JOIN speaker s ON s.id = g.speaker_id "
                        "LEFT JOIN person p ON p.id = s.person_id WHERE g.conversation_id = ? ORDER BY g.idx", (cid,)).fetchall()
    if segs:
        out += ["## Transcript", ""]
        for g in segs:
            who = g["name"] or (f"Speaker {g['cluster'] + 1}" if g["cluster"] is not None else "Unknown")
            out.append(f"**{who}** [{_mmss(g['start_s'])}]: {g['text']}  ")
        out.append("")
    return "\n".join(out)


def export_all(conn: sqlite3.Connection, cfg: Settings) -> dict:
    folder = cfg.data_dir / "export" / "conversations"
    folder.mkdir(parents=True, exist_ok=True)
    keep: set[Path] = set()
    written = 0
    for c in conn.execute("SELECT * FROM conversation WHERE deleted_at IS NULL ORDER BY id").fetchall():
        f = folder / filename(c)
        keep.add(f)
        text = render(conn, c)
        if not f.exists() or f.read_text() != text:
            tmp = f.with_suffix(".tmp")
            tmp.write_text(text)
            tmp.replace(f)
            written += 1
    removed = 0
    for f in folder.glob("*.md"):
        if f not in keep:  # trashed, or renamed (title changed)
            f.unlink()
            removed += 1
    return {"files": len(keep), "written": written, "removed": removed}
