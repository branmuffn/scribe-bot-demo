"""Markdown exports (Brian 2026-10-22): a conversation, a person, or a filtered list of conversations as one .md file,
ready to paste or upload into a chatbot. Built from the database, so edits (names, words, tasks) are included.
The file is made on the M14 and downloaded to Brian's own device; nothing is sent anywhere."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from scribe_bot.ai import extract
from scribe_bot.ai.prompts import MEETING_TYPES

ME = 1


# The app's names for areas ('personal' shows as Home). app.py reads this same list for the pages.
AREAS = {"work": "Work", "personal": "Home", "school": "School", "other": "Other"}


def _mmss(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _people(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, name FROM person")}


def _when(ts: str) -> str:
    """'Tue Nov 25, 2026, 3:24 pm' in the M14's local time, as the app shows it (R5-16: the file said UTC)."""
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ts or ""
    dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone()
    return dt.strftime("%a %b %-d, %Y, %-I:%M %p")  # F48: "AM/PM" like every fixed label (Brian writes "7 AM")


def conversation_md(conn: sqlite3.Connection, cid: int, transcript: bool = True, level: int = 1) -> str:
    c = conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone()
    names = _people(conn)
    nums = extract.speaker_numbers(conn, cid)
    spk = {r["id"]: names.get(r["person_id"]) or f"Speaker {nums.get(r['id'], '?')}"
           for r in conn.execute("SELECT id, person_id FROM speaker WHERE conversation_id = ?", (cid,))}
    h = "#" * level
    out = [f"{h} {c['title']}", "",
           f"- When: {_when(c['started_at'] or c['created_at'])}",
           f"- Length: {round((c['duration_s'] or 0) / 60)} min",
           f"- Type: {MEETING_TYPES.get(c['type'], c['type'] or 'unknown')} · Area: {AREAS.get(c['area'], c['area'] or 'not sorted')}",
           f"- Speakers: {', '.join(sorted(set(spk.values()))) or 'none'}", ""]
    if c["summary"]:
        out += [f"{h}# Summary", "", c["summary"], ""]
    tasks = conn.execute("SELECT * FROM task WHERE conversation_id = ? ORDER BY status, due IS NULL, due, id", (cid,)).fetchall()
    if tasks:
        out += [f"{h}# Tasks", ""]
        for t in tasks:
            who = "Me" if t["owner_person_id"] == ME else names.get(t["owner_person_id"], "Unassigned")
            box = "x" if t["status"] == "done" else " "
            note = ", not needed" if t["closed_as"] == "not_needed" else (", due " + t["due"] if t["due"] else "")
            out.append(f"- [{box}] {t['title']} ({who}{note})")
        out.append("")
    decisions = conn.execute("SELECT text FROM decision WHERE conversation_id = ? ORDER BY id", (cid,)).fetchall()
    if decisions:
        out += [f"{h}# Decisions", "", *(f"- {d[0]}" for d in decisions), ""]
    facts = conn.execute("SELECT person_name, person_id, value FROM person_fact WHERE conversation_id = ? "
                         "AND coalesce(confirmed, 1) = 1 ORDER BY id", (cid,)).fetchall()
    if facts:
        out += [f"{h}# People notes", "", *(f"- {names.get(f['person_id']) or f['person_name']}: {f['value']}" for f in facts), ""]
    if transcript:
        lines = conn.execute("SELECT start_s, speaker_id, text FROM segment WHERE conversation_id = ? ORDER BY idx", (cid,)).fetchall()
        if lines:
            # A blank line after each: Markdown runs single line breaks together into one paragraph, so every line
            # starts with its time and name only when it's a paragraph of its own (Brian 2026-10-22). The PDF stays tight.
            out += [f"{h}# Transcript", ""]
            for x in lines:
                out += [f"[{_mmss(x['start_s'])}] **{spk.get(x['speaker_id'], 'Unknown')}:** {x['text']}", ""]
    return "\n".join(out)


def person_md(conn: sqlite3.Connection, pid: int) -> str:
    p = conn.execute("SELECT * FROM person WHERE id = ?", (pid,)).fetchone()
    out = [f"# {p['name']}", ""]
    if p["notes"]:
        out += [p["notes"], ""]
    facts = conn.execute("SELECT type, value FROM person_fact WHERE person_id = ? AND coalesce(confirmed, 1) = 1 "
                         "ORDER BY type, id", (pid,)).fetchall()
    if facts:
        out += ["## Notes", "", *(f"- {f['type']}: {f['value']}" for f in facts), ""]
    open_ = conn.execute("SELECT t.title, t.due, t.owner_person_id FROM task t WHERE t.status = 'open' AND (t.owner_person_id = ? "
                         "OR (t.owner_person_id = ? AND EXISTS (SELECT 1 FROM speaker s WHERE s.conversation_id = t.conversation_id "
                         "AND s.person_id = ?)))", (pid, ME, pid)).fetchall() if pid != ME else []
    if open_:
        out += ["## Open items", "", *(f"- {'I owe' if t['owner_person_id'] == ME else 'They owe'}: {t['title']}"
                                       f"{' (due ' + t['due'] + ')' if t['due'] else ''}" for t in open_), ""]
    convs = conn.execute("SELECT c.id FROM conversation c WHERE c.deleted_at IS NULL AND EXISTS (SELECT 1 FROM speaker s "
                         "WHERE s.conversation_id = c.id AND s.person_id = ?) ORDER BY coalesce(c.started_at, c.created_at) DESC",
                         (pid,)).fetchall()
    if convs:
        out += ["## Conversations", ""] + [conversation_md(conn, r[0], transcript=False, level=3) for r in convs]
    return "\n".join(out)


def filename(title: str, ext: str = "md") -> str:
    return (re.sub(r"[^\w .()-]+", "_", title).strip()[:80] or "Scribe Bot") + "." + ext


_PLAIN = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "•": "-", "·": "-",
                        " ": " ", "→": "->", "✓": "v"})


_TURN = re.compile(r"\[\d+:\d\d\] \*\*")   # a transcript line: "[12:05] **Name:** text"
_SPEAKER = re.compile(r"^(\[\d+:\d\d\] )\*\*(.+?):\*\* (.*)$")
_MARK = re.compile(r"(\*\*|__|--|~~)")


def _plain_md(text: str) -> str:
    """R5-09: the PDF reads **, __, -- and ~~ as bold, italics, underline and strike, and drops them, so "we -- I
    think -- ship" lost its dashes and came out underlined. Only the speaker's name is meant to be bold."""
    def esc(t: str) -> str:
        return _MARK.sub(r"\\\1", t)
    m = _SPEAKER.match(text)
    return f"{m[1]}**{esc(m[2])}:** {esc(m[3])}" if m else esc(text)


def _latin(text: str) -> str:
    """The PDF's built-in fonts cover Western European letters; other characters become their nearest plain form."""
    return text.translate(_PLAIN).encode("latin-1", "replace").decode("latin-1")


def to_pdf(md: str, title: str) -> bytes:
    """The same content as the .md export, laid out as a tidy PDF: headings, bullets, bold speaker names."""
    from fpdf import FPDF

    pdf = FPDF(format="Letter")
    pdf.set_title(_latin(title))
    pdf.set_creator("Scribe Bot")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    sizes = {1: 18, 2: 14, 3: 12, 4: 11}
    rows = md.split("\n")
    for i, raw in enumerate(rows):
        line = _latin(raw.rstrip())
        if not line:
            if 0 < i < len(rows) - 1 and _TURN.match(rows[i - 1]) and _TURN.match(rows[i + 1]):
                continue   # the blank line the .md needs between transcript lines: no gap in the PDF
            pdf.ln(2)
            continue
        if line == "---":
            pdf.ln(3)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(4)
            continue
        level = len(line) - len(line.lstrip("#"))
        if level and line[level:level + 1] == " ":
            pdf.ln(3 if level > 1 else 0)
            pdf.set_font("Helvetica", "B", sizes.get(level, 11))
            pdf.multi_cell(0, sizes.get(level, 11) * 0.5, line[level + 1:], new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
            continue
        pdf.set_font("Helvetica", "", 10)
        indent = 4 if line.startswith("- ") else 0
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(0, 5, _plain_md(line), markdown=True, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
