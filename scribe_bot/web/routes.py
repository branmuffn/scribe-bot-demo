"""Pages and actions (SPEC §3.8, OUTPUT_DESIGN.md v1). Plain forms + redirects; HTMX boosts them (no page reloads).
Small item actions (done, remove, change person) swap only their section in place (hx-select), so the page never jumps.

Edits feed the quality scores (OUTPUT_DESIGN §5): done = right, edited, reassigned or deleted = wrong. People notes are
kept unless Brian removes them (confirmed NULL = accepted, 0 = removed).
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BeforeValidator

from scribe_bot.ai import accounts, costs, extract, jobs as ai_jobs, prompts, router as ai_router
from scribe_bot.backup.trash import TRASH_DAYS, purge_due as trash_purge_due
from scribe_bot.core.names import first_name
from scribe_bot.core import cleanup, cleanup_devices, db, queue, machine, people as people_svc, progress, tags as tags_svc, talk
from scribe_bot.discord_bot import notify, sender
from scribe_bot.pipeline import control, identify, ingest, merge as merge_svc, pace, peek, voicememo
from scribe_bot.web import export, inbox as inbox_svc, undo
from scribe_bot.web.app import area_of, local_date, local_datetime, me_label, public_demo, templates, worker_status


def _id_or_none(v):
    """A picker's "＋ Someone new…" / "Who is it?" choice isn't a number: treat it as no pick, so a typed name decides
    (Brian, 2026-11-22: naming someone new gave "error 422")."""
    if v is None or isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except ValueError:
        return None


def _id_list(raw: str) -> list[int]:
    """Ids from a form's "3,5,8" list, in order; anything that isn't a whole number is skipped."""
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


def _ids(raw: str) -> list[int]:
    """The same, each id once."""
    return list(dict.fromkeys(_id_list(raw)))


OptId = Annotated[int | None, Form(), BeforeValidator(_id_or_none)]   # a form field; validator inside Annotated so it runs

router = APIRouter()
ME = 1
AUDIO_TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".webm": "audio/webm", ".aac": "audio/aac", ".flac": "audio/flac"}
UPLOAD_TYPES = tuple(AUDIO_TYPES)
DEFAULT_CAP_USD = 10.0


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.cfg.db_path, any_thread=True)
    try:
        yield conn
    finally:
        conn.close()


Conn = Depends(get_conn)


AWAKE = "(r.snoozed_until IS NULL OR r.snoozed_until <= ?)"   # review items not put off until later
REVIEW_FROM = "review_item r LEFT JOIN conversation c ON c.id = r.conversation_id"


# Snooze ("Remind me") choices for Needs you items. Times are the M14's local time; stored as UTC ISO like db.now().
EVENING, MORNING = time(18), time(8)


def snooze_options(now: datetime | None = None) -> list[tuple[str, str]]:
    """(value, label) for the ⋯ menu. "This evening" only while it's still a while away (before 5 PM)."""
    now = (now or datetime.now()).astimezone()
    monday = now.date() + timedelta(days=(7 - now.weekday()) % 7 or 7)
    return [("1h", "In 1 hour"), *([("evening", "This evening (6 PM)")] if now.hour < 17 else []),
            ("tomorrow", "Tomorrow morning (8 AM)"), ("monday", f"Next Monday, {monday:%b} {monday.day} (8 AM)")]


def snooze_until(choice: str, at: str = "", now: datetime | None = None) -> str:
    """When a snoozed item comes back. choice: 1h, evening, tomorrow, monday, or pick (at = 'YYYY-MM-DDTHH:MM', local)."""
    now = (now or datetime.now()).astimezone()
    today = now.date()
    if choice == "1h":
        t = now + timedelta(hours=1)
    elif choice == "pick":
        try:
            t = datetime.fromisoformat(at).astimezone()   # naive = local time (DST-aware)
        except ValueError:
            raise HTTPException(400, "Pick a date and time")
        if t <= now:
            raise HTTPException(400, "Pick a time in the future")
    elif choice == "evening" and now.time() < EVENING:
        t = datetime.combine(today, EVENING).astimezone()
    elif choice == "monday":
        t = datetime.combine(today + timedelta(days=(7 - today.weekday()) % 7 or 7), MORNING).astimezone()
    else:   # tomorrow (and "evening" once it's past 6 PM)
        t = datetime.combine(today + timedelta(days=1), MORNING).astimezone()
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


templates.env.globals["snooze_options"] = snooze_options


TASKS_PER_PAGE = 100   # the rest behind "Show more": keeps big lists under the page-speed budget
PAGE = 50              # rows per "Show more" step on the other long lists


def paged(conn: sqlite3.Connection, sql: str, args, n: int, step: int = PAGE) -> tuple[list, int]:
    """The first n rows of sql (which has no LIMIT) and how many there are in all, for "Show more · 40 left".
    Counts only when there are more, so a short list costs one query."""
    n = max(step, min(n, 5000))
    rows = conn.execute(f"{sql} LIMIT {n + 1}", args).fetchall()
    if len(rows) <= n:
        return rows, len(rows)
    return rows[:n], conn.execute(f"SELECT count(*) FROM ({sql})", args).fetchone()[0]


def review_scope(area: str) -> tuple[str, list]:
    """The review items that count (Needs you, the Today tab, /review): not from a conversation in Trash, speakers that
    still exist, and the Work/Home toggle (an item uses its conversation's area; unfiled or none: shows under both)."""
    a, args = area_clause(area, "c.area")
    return f"{live('r')} AND (r.kind != 'speaker' OR EXISTS (SELECT 1 FROM speaker s WHERE s.id = r.ref_id)){a}", args


def put_off(conn: sqlite3.Connection, what: str) -> bool:
    """The M14 line or the older-recordings line on Today is snoozed (setting later:<what>)."""
    return setting(conn, f"later:{what}", "") > db.now()


def needs_total(conn: sqlite3.Connection, area: str, m14: list[str]) -> int:
    """The Today tab's number, the same as "Needs you · N": one per row there (each review item, plus the M14 line, the
    older-recordings line and the old-tasks line when they show)."""
    scope, args = review_scope(area)
    n = conn.execute(f"SELECT count(*) FROM {REVIEW_FROM} WHERE r.status = 'open' AND {AWAKE} AND {scope} AND NOT {IS_RECORDING}",
                     [db.now(), *args]).fetchone()[0]
    a, targs = area_clause(area, "t.area")
    n += bool(m14) and not put_off(conn, "m14")
    n += bool(conn.execute(f"SELECT 1 FROM task t WHERE {cleanup.STALE} AND {LIVE_TASK}{a} LIMIT 1", targs).fetchone())
    n += conn.execute(f"SELECT count(*) FROM task t WHERE {REMIND_DUE} AND {LIVE_TASK}{a}", [db.now(), *targs]).fetchone()[0]
    return n


# Recordings waiting for an OK live on the Inbox page (Brian, 2026-11-20), not under Needs you.
IS_RECORDING = "(r.kind = 'approval' AND json_extract(r.payload, '$.recording_id') IS NOT NULL)"


def inbox_count(conn: sqlite3.Connection) -> int:
    """The Inbox tab's number: recordings waiting for an OK (not snoozed) plus older ones never processed."""
    return conn.execute(f"SELECT count(*) FROM review_item r WHERE r.status = 'open' AND {AWAKE} AND {IS_RECORDING}",
                        (db.now(),)).fetchone()[0] + conn.execute("SELECT count(*) FROM recording WHERE status = 'backlog'"
                                                                  ).fetchone()[0]


REMIND_DUE = "t.status = 'open' AND t.remind_at <= ?"   # a task reminder whose time has come (Needs you)


def render(request: Request, name: str, nav: str, conn: sqlite3.Connection | None = None, **ctx) -> HTMLResponse:
    area = area_of(request)
    m14 = machine.needs_attention(conn) if conn else []
    if conn:
        notify.tidy(conn)   # done things leave the bell before its count is read
        trash_purge_due(conn)
    return templates.TemplateResponse(request, name, {"nav": nav, "area": area, "today": date.today().isoformat(),
                                                      "undo": undo.latest(conn) if conn else None,
                                                      "toast": NOTES.get(request.query_params.get("note", "")),
                                                      "today_label": date.today().strftime("%A, %B %-d"),
                                                      "needs_count": needs_total(conn, area, m14) if conn else 0,
                                                      "head_alert": bool(m14), "now_iso": db.now(),
                                                      "ring": status_ring(conn, bool(m14)) if conn else None,
                                                      "n_trash": conn.execute("SELECT count(*) FROM conversation WHERE deleted_at "
                                                                              "IS NOT NULL AND purge_at IS NULL").fetchone()[0] if conn else 0,
                                                      "n_older": conn.execute("SELECT count(*) FROM recording WHERE status = "
                                                                              "'backlog'").fetchone()[0] if conn else 0,
                                                      "bell_new": bell_new(conn) if conn else 0,
                                                      "demo_tour": tour_conversation(conn) if conn and public_demo() else None,
                                                      "inbox_count": inbox_count(conn) if conn else 0,
                                                      "gone_mark": gone_mark(conn) if conn else 0, **ctx})


def gone_mark(conn: sqlite3.Connection) -> int:
    """B7: the newest removal this page already reflects (base.html hands it to app.js, which asks /gone for newer ones)."""
    return conn.execute("SELECT coalesce(max(id), 0) FROM removal").fetchone()[0]


@router.get("/gone")
def gone(since: int = 0, conn: sqlite3.Connection = Conn) -> JSONResponse:
    """B7 (Brian, 2026-11-25): what was removed since this page was drawn, on any device: kinds and ids only, no
    content. app.js asks every 25 s (it's also the keep-alive) and takes those rows off the screen."""
    rows = conn.execute("SELECT id, kind, ref_id FROM removal WHERE id > ? ORDER BY id LIMIT 200", (since,)).fetchall()
    last = rows[-1][0] if rows else gone_mark(conn)
    return JSONResponse({"last": last, "gone": [{"k": r[1], "id": r[2]} for r in rows]},
                        headers={"Cache-Control": "no-store"})


def status_ring(conn: sqlite3.Connection, alert: bool) -> dict:
    """Top-bar status ring (Brian picked B, 2026-11-21): fills while a talk processes; ❚❚ while paused; a red dot when
    the M14 needs attention. Tapping it opens the status sheet (/status/sheet)."""
    jobs = progress.active_jobs(conn) if conn.execute(
        "SELECT 1 FROM job WHERE kind = 'recording' AND status IN ('running', 'pending') LIMIT 1").fetchone() else []
    run = next((j for j in jobs if j.get("started")), jobs[0] if jobs else None)
    return {"active": bool(jobs), "pct": run["percent"] if run else 0, "n": len(jobs), "alert": alert,
            "paused": control.state(conn)["paused"]}


@router.get("/status/ring", response_class=HTMLResponse)
def status_ring_fragment(request: Request, conn: sqlite3.Connection = Conn):
    m14 = machine.needs_attention(conn)
    return templates.TemplateResponse(request, "_ring.html", {"ring": status_ring(conn, bool(m14))})


@router.get("/status/sheet", response_class=HTMLResponse)
def status_sheet(request: Request, conn: sqlite3.Connection = Conn):
    """The ring's sheet: processing now, the M14, AI costs this month, processing history, Help."""
    jobs = progress.active_jobs(conn)
    m14 = machine.needs_attention(conn)
    d = machine.card(machine.latest(conn))
    cap = float(setting(conn, "global_cap_usd", str(DEFAULT_CAP_USD)))
    last = conn.execute("SELECT j.id, j.updated_at, j.recording_id, r.title, c.title AS ctitle FROM job j "
                        "LEFT JOIN recording r ON r.id = j.recording_id LEFT JOIN conversation c ON c.recording_id = j.recording_id "
                        "WHERE j.kind = 'recording' AND j.status = 'done' ORDER BY j.id DESC LIMIT 1").fetchone()
    took = ""
    if last:
        first = conn.execute("SELECT min(started_at) FROM stage_run WHERE job_id = ?", (last["id"],)).fetchone()[0]
        if first:
            took = progress.duration_label((datetime.fromisoformat(last["updated_at"]) - datetime.fromisoformat(first)).total_seconds())
    return templates.TemplateResponse(request, "_status_sheet.html", {
        "jobs": jobs, "ctl": control.state(conn), "m14": m14, "d": d, "spend": spend(conn, month_start()), "cap": cap,
        "last": last, "took": took, "n_cleanup": cleanup_devices.counts(conn).get("todo", 0)})


@router.get("/cleanup", response_class=HTMLResponse)
def cleanup_page(request: Request, conn: sqlite3.Connection = Conn):
    """Delete-from-your-devices checklist (Brian, 2026-11-21), grouped by device."""
    return render(request, "cleanup.html", "more", conn, rows=cleanup_devices.checklist(conn), devices=cleanup_devices.DEVICES)


@router.post("/cleanup/answer")
def cleanup_answer(request: Request, keep: int = Form(0), conn: sqlite3.Connection = Conn):
    """The Today pop-up: Add to my cleanup list (keep=0) or Keep them on my devices (keep=1)."""
    with db.transaction(conn):
        cleanup_devices.set_status(conn, None, "kept" if keep else "todo", only="ask")
    return RedirectResponse("/" if keep else "/cleanup", status_code=303)


@router.post("/cleanup/{cid}/{what}")
def cleanup_mark(request: Request, cid: int, what: str, conn: sqlite3.Connection = Conn):
    """Tick (done), untick (todo) or Keep (kept) one row."""
    if what not in ("done", "todo", "kept"):
        raise HTTPException(404)
    with db.transaction(conn):
        cleanup_devices.set_status(conn, [cid], what)
    return back(request, "/cleanup")


def bell_new(conn: sqlite3.Connection) -> int:
    """Notifications since you last opened 🔔 (the dot on the bell)."""
    return conn.execute("SELECT count(*) FROM notification WHERE created_at > ? AND dismissed_at IS NULL",
                        (setting(conn, "notifications_seen_at", ""),)).fetchone()[0]


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, tab: str = "current", sent: int = 0, n: int = PAGE, conn: sqlite3.Connection = Conn):
    """What Scribe Bot sent to Discord, newest first: Current (✕ dismisses) and Dismissed (Restore). Opening it clears
    the bell's dot; the new ones stay marked here."""
    tab = "dismissed" if tab == "dismissed" else "current"
    seen = setting(conn, "notifications_seen_at", "")
    sql = ("SELECT * FROM notification WHERE dismissed_at IS NOT NULL ORDER BY dismissed_at DESC, id DESC" if tab == "dismissed"
           else "SELECT * FROM notification WHERE dismissed_at IS NULL ORDER BY created_at DESC, id DESC")
    rows, total = paged(conn, sql, [], n)
    n_current, n_dismissed = conn.execute("SELECT count(*) - count(dismissed_at), count(dismissed_at) FROM notification").fetchone()
    with db.transaction(conn):
        set_setting(conn, "notifications_seen_at", db.now())
    return render(request, "notifications.html", "more", conn, rows=rows, total=total, seen=seen, tab=tab,
                  n_current=n_current, n_dismissed=n_dismissed, sent=bool(sent),
                  quiet=sender.in_quiet_hours(datetime.now().astimezone()))


@router.post("/notifications/{nid}/dismiss")
def notification_dismiss(request: Request, nid: int, conn: sqlite3.Connection = Conn):
    with db.transaction(conn):
        undo.save(conn, "Notification dismissed", [("notification", nid)])
        conn.execute("UPDATE notification SET dismissed_at = ? WHERE id = ? AND dismissed_at IS NULL", (db.now(), nid))
    return back(request, "/notifications")


@router.get("/notifications/{nid}/open")
def notification_open(nid: int, conn: sqlite3.Connection = Conn):
    """Tapping a notification opens its link and moves it to Dismissed (it's been seen)."""
    n = conn.execute("SELECT link FROM notification WHERE id = ?", (nid,)).fetchone()
    conn.execute("UPDATE notification SET dismissed_at = ? WHERE id = ? AND dismissed_at IS NULL", (db.now(), nid))
    ok = n and n["link"] and n["link"].startswith("/") and not n["link"].startswith(("//", "/\\"))   # R5-14: never off-site
    return RedirectResponse(n["link"] if ok else "/notifications", status_code=303)


@router.post("/notifications/dismiss-all")
def notifications_dismiss_all(request: Request, conn: sqlite3.Connection = Conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM notification WHERE dismissed_at IS NULL")]
    if ids:
        with db.transaction(conn):
            undo.save(conn, f"{len(ids)} notification{'s' if len(ids) != 1 else ''} dismissed", [("notification", i) for i in ids])
            conn.execute("UPDATE notification SET dismissed_at = ? WHERE dismissed_at IS NULL", (db.now(),))
    return back(request, "/notifications")


@router.post("/notifications/{nid}/restore")
def notification_restore(request: Request, nid: int, conn: sqlite3.Connection = Conn):
    with db.transaction(conn):
        conn.execute("UPDATE notification SET dismissed_at = NULL WHERE id = ?", (nid,))
    return back(request, "/notifications?tab=dismissed")


@router.post("/notifications/test")
def notifications_test(request: Request, conn: sqlite3.Connection = Conn):
    """Brian's check that pushes reach his phone: one harmless line to each Discord channel (the worker sends within
    ~5 s; quiet hours hold the #scribe-bot one until 7 AM). No content, like every notification."""
    if stop := demo_stop(request, "send", "/notifications"):
        return stop
    with db.transaction(conn):
        for channel, name in (("bot", "#scribe-bot"), ("alerts", "#scribe-alerts")):
            notify.enqueue(conn, channel, f"🔔 Test notification from Scribe Bot ({name})", "/notifications")
    return RedirectResponse("/notifications?sent=1", status_code=303)


def back(request: Request, default: str = "/", note: str | None = None) -> RedirectResponse:
    url = request.headers.get("referer") or default
    return RedirectResponse(with_note(url, note) if note else url, status_code=303)


# Short notes shown in the bar at the bottom after an action that runs in the background (Brian's bug list: an action
# with no visible progress). The page gets only a key (?note=...); the words come from here, never from the address.
NOTES = {
    # U1 (Brian, 2026-11-25): the Summarize note ran off the phone screen. Notes stay short (PREFERENCES §3) and wrap.
    "transcribing": "Transcribing. It shows under Talks › Processing.",
    "summarizing": "Summarizing. The talk shows Summarizing until its notes are ready.",
    # D-11: the public demo (scribe-bot-demo on Vercel) runs none of these; the button ends in one of these notes
    "demo-ai": "Demo: the AI doesn't run here. Nothing was sent or charged.",
    "demo-run": "Demo: nothing is transcribed or summarized here. Nothing was sent.",
    "demo-audio": "Demo: audio isn't processed here, so merging is off.",
    "demo-upload": "Demo: recordings can't be added here. Nothing was uploaded.",
    "demo-keys": "Demo: accounts and keys can't be added or tested here.",
    "demo-send": "Demo: nothing is sent from here.",
}


def demo_stop(request: Request, what: str, default: str = "/") -> RedirectResponse | None:
    """D-11: on the public demo, an action that would transcribe, call an AI, take a recording, use a key or send a
    message does nothing and goes back with a short note (no job is queued, so nothing sits "waiting" forever).
    Returns None everywhere else, so the real app is unchanged."""
    if not public_demo():
        return None
    ref = request.headers.get("referer") or ""
    if urlsplit(ref).path.endswith("/confirm"):   # F12: Confirm left you on the confirm box; go where the real app goes
        return RedirectResponse(with_note(default, f"demo-{what}"), status_code=303)
    return back(request, default, note=f"demo-{what}")


def with_note(url: str, note: str) -> str:
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "note"] + [("note", note)]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def area_clause(area: str, col: str) -> tuple[str, list]:
    """Work/Personal filter. Items with no area yet (e.g. Inbox tasks the AI couldn't file) show under both."""
    return ("", []) if area == "all" else (f" AND ({col} = ? OR {col} IS NULL)", [area])


def live(alias: str) -> str:
    """Rows from a conversation in Trash are hidden everywhere until it's restored (rows with no conversation stay)."""
    return f"NOT EXISTS (SELECT 1 FROM conversation z WHERE z.id = {alias}.conversation_id AND z.deleted_at IS NOT NULL)"


LIVE_TASK = live("t")


def setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))


def index(conn: sqlite3.Connection, kind: str, ref_id: int, conversation_id: int | None, text: str) -> None:
    unindex(conn, kind, ref_id)
    conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES (?, ?, ?, ?)",
                 (kind, ref_id, conversation_id, text))


def unindex(conn: sqlite3.Connection, kind: str, ref_id: int) -> None:
    conn.execute("DELETE FROM search_index WHERE kind = ? AND ref_id = ?", (kind, ref_id))


def local_midnight() -> str:
    """The start of today on the M14's clock, as UTC ISO: what "today" means on Costs and in Settings."""
    return datetime.combine(date.today(), time()).astimezone(timezone.utc).isoformat(timespec="seconds")


def month_start() -> str:
    d = datetime.now(timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def spend(conn: sqlite3.Connection, since: str) -> float:
    return conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM ledger_entry WHERE time >= ?", (since,)).fetchone()[0]


def people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name FROM person WHERE stands_for_speaker IS NULL ORDER BY id = 1 DESC, name").fetchall()


@router.get("/people/pick")
def people_pick(request: Request, sid: int = 0, cid: int = 0, conn: sqlite3.Connection = Conn) -> JSONResponse:
    """The person picker (Brian picked A, 2026-11-22 night): everyone A-Z with how often they appear, plus up to 5
    suggestions: a voice match for this speaker, people from talks the same week, then the people named most often."""
    rows = conn.execute(
        "SELECT p.id, p.name, count(DISTINCT s.conversation_id) AS n, max(coalesce(c.started_at, c.created_at)) AS last "
        "FROM person p LEFT JOIN speaker s ON s.person_id = p.id LEFT JOIN conversation c ON c.id = s.conversation_id AND c.deleted_at IS NULL "
        "WHERE p.stands_for_speaker IS NULL GROUP BY p.id ORDER BY p.name COLLATE NOCASE").fetchall()
    def sub(r):
        return (f"In {r['n']} talk{'s' if r['n'] != 1 else ''}" + (f" · last {local_date(r['last'])}" if r["last"] else "")) if r["n"] else "Not in a talk yet"
    everyone = [{"id": r["id"], "name": me_label(r["name"]) if r["id"] == ME else r["name"], "sub": sub(r)} for r in rows]
    by_id = {p["id"]: p for p in everyone}
    sug: list[dict] = []
    def add(pid, why):
        if pid in by_id and pid not in [x["id"] for x in sug] and len(sug) < 5:
            sug.append({**by_id[pid], "sub": why})
    if sid:   # voice match: this speaker's voice against everyone's voiceprint
        try:
            emb = identify.speaker_embedding(conn, sid, request.app.state.cfg)
            if emb:
                scores = sorted(((identify.cosine(emb, v), pid) for pid, v in identify.known_prints(conn).items()), reverse=True)
                for sc, pid in scores[:2]:
                    if sc >= 0.45:
                        add(pid, f"Voice match {round(sc * 100)}%")
        except Exception:   # no embeddings for this talk: just skip the voice suggestion
            pass
    if cid:   # people from talks within a week of this one
        for r in conn.execute(
                "SELECT s.person_id, count(*) AS k, max(coalesce(nullif(c.short_title, ''), c.title)) AS t FROM speaker s JOIN conversation c ON c.id = s.conversation_id "
                "JOIN conversation me ON me.id = ? WHERE c.id != me.id AND c.deleted_at IS NULL AND s.person_id IS NOT NULL AND s.person_id != 1 "
                "AND abs(julianday(coalesce(c.started_at, c.created_at)) - julianday(coalesce(me.started_at, me.created_at))) <= 7 "
                "GROUP BY s.person_id ORDER BY k DESC LIMIT 3", (cid,)):
            add(r["person_id"], f"Same week: {r['t']}")
    for r in sorted((x for x in rows if x["id"] != ME), key=lambda x: -x["n"])[:5]:
        add(r["id"], sub(r))
    return JSONResponse({"suggested": sug, "all": everyone})


# "N to name", the same everywhere (B10/B12, 2026-11-25): the Talks list counted speakers by a stored talk time of 20 s
# or more, the talk page counted every speaker who still has lines, so a talk could say "3 to name" in the list and
# "2 to name" on its page. Both now count unnamed speakers (not marked "Don't know") who still have lines.
N_UNNAMED_SQL = ("SELECT count(*) FROM speaker s WHERE s.conversation_id = c.id AND s.person_id IS NULL AND s.unknown = 0 "
                 "AND EXISTS (SELECT 1 FROM segment g WHERE g.speaker_id = s.id)")


def speakers_of(conn: sqlite3.Connection, conversation_id: int) -> list[dict]:
    """Speakers in order of first appearance, named by person or 'Speaker N'."""
    rows = conn.execute(
        "SELECT s.*, p.name AS person_name, (SELECT min(idx) FROM segment g WHERE g.speaker_id = s.id) AS first_idx "
        "FROM speaker s LEFT JOIN person p ON p.id = s.person_id WHERE s.conversation_id = ? "
        "ORDER BY first_idx IS NULL, first_idx", (conversation_id,)).fetchall()
    stats = talk.by_speaker(conn, conversation_id)          # from the lines, so re-assigned lines count (F10)
    secs = {r["id"]: stats[r["id"]]["seconds"] if stats else r["talk_seconds"] for r in rows if not stats or r["id"] in stats}
    total = sum(secs.values()) or 1
    nums = extract.speaker_numbers(conn, conversation_id)   # same "Speaker N" as the AI's summary uses
    # Colors come from each speaker's place among ALL speakers, so hiding an emptied one (merged into another, below)
    # doesn't shift everyone else's color. Speakers left with no lines are hidden (Undo brings them back).
    # Brian, 2026-11-21: colors by each speaker's id order (not order of first words), so a split that changes who talks
    # first no longer repaints everyone (the top chips and the lines disagreed until a reload).
    palette = list(PERSON_COLORS)
    rank = {sid: i for i, sid in enumerate(sorted(r["id"] for r in rows))}
    out = [{**dict(r), "color": r["color"] or palette[rank[r["id"]] % len(palette)],
            "name": r["person_name"] or f"Speaker {nums[r['id']]}", "seconds": secs.get(r["id"], 0),
            "share": secs.get(r["id"], 0) / total, "turns": stats.get(r["id"], {}).get("turns", 0)}
           for r in rows]
    return [s for s in out if not stats or s["id"] in stats] or out


# ---------- Today ----------

def nudge(t) -> str:
    """A ready-to-paste reminder for a late promise: a plain local template (no AI). Brian copies it; nothing is sent."""
    first = first_name(t["owner_name"]) if t["owner_name"] else "there"
    try:
        when = "by " + date.fromisoformat(str(t["due"])[:10]).strftime("%A, %B %-d")
    except ValueError:
        when = str(t["due"])
    return f"Hi {first}, quick check-in on “{t['title'].strip().rstrip('.')}”. We'd said {when}. Any update? Thanks!"


def week(request: Request, conn: sqlite3.Connection) -> dict:
    """Today's "Your week" card (last 7 days): conversations, your tasks done, your speaking. Each tile opens its page."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    a, args = area_clause(area_of(request), "c.area")
    ta, targs = area_clause(area_of(request), "t.area")
    return {"since": (date.today() - timedelta(days=7)).isoformat(), "speaking": talk.speaking_trend(conn, 7),
            "conversations": conn.execute("SELECT count(*) FROM conversation c WHERE c.deleted_at IS NULL "
                                          f"AND coalesce(c.started_at, c.created_at) >= ?{a}", [since, *args]).fetchone()[0],
            "done": conn.execute(f"SELECT count(*) FROM task t WHERE t.status = 'done' AND t.closed_as IS NULL AND t.owner_person_id = {ME} "
                                 f"AND t.updated_at >= ? AND {LIVE_TASK}{ta}", [since, *targs]).fetchone()[0]}


@router.get("/speaking", response_class=HTMLResponse)
def speaking_page(request: Request, conn: sqlite3.Connection = Conn):
    """Your speaking: pace, longest stretch, questions and fillers over the last 7 and 30 days (no AI)."""
    return render(request, "speaking.html", "more", conn, speaking={d: talk.speaking_trend(conn, d) for d in (7, 30)})


def night_window_utc(now: datetime | None = None) -> tuple[str, str]:
    """Last night, 9 PM to 7 AM (local), as UTC ISO strings. Talks count for the night they FINISHED in, not when they
    were queued (2026-11-23: 19 memos queued at 12:04 AM and done by 5:13 AM showed as "nothing" with a 1-7 AM queue
    window)."""
    now = (now or datetime.now()).astimezone()
    day = now.date() if now.hour >= 1 else now.date() - timedelta(days=1)
    start = datetime.combine(day - timedelta(days=1), time(21)).astimezone()
    end = datetime.combine(day, time(7)).astimezone()
    return tuple(x.astimezone(timezone.utc).isoformat(timespec="seconds") for x in (start, end))


# a recording job that finished (done or failed) inside a night window; args: start, end
NIGHT_JOB = "j.kind = 'recording' AND j.status IN ('done', 'failed') AND j.updated_at >= ? AND j.updated_at < ?"


def overnight_card(conn: sqlite3.Connection, now: datetime | None = None) -> dict | None:
    """S1 (Brian picked A, 2026-11-22): what ran last night (1-7 AM local), shown on Today until he dismisses it."""
    now = (now or datetime.now()).astimezone()
    start = now.replace(hour=1, minute=0, second=0, microsecond=0)
    if now < start:
        return None
    s_utc, e_utc = night_window_utc(now)
    if setting(conn, "overnight_seen", "") == now.date().isoformat():
        return None
    r = conn.execute("SELECT count(*) AS n, sum(j.status = 'done' AND EXISTS (SELECT 1 FROM conversation c WHERE c.recording_id = j.recording_id "
                     "AND c.deleted_at IS NULL)) AS ok, sum(j.status = 'failed') AS bad, max(j.updated_at) AS last, "
                     "coalesce(sum(r.duration_s), 0) AS secs FROM job j LEFT JOIN recording r ON r.id = j.recording_id "
                     f"WHERE {NIGHT_JOB}", (s_utc, e_utc)).fetchone()   # ok = talks still in Talks, so it matches the list
    if not r["n"]:
        return None
    return {"n": r["n"], "ok": r["ok"] or 0, "bad": r["bad"] or 0, "hours": round(r["secs"] / 3600, 1),
            "last": local_datetime(r["last"]).split(", ")[-1] if r["last"] else ""}


def overnight_seen(conn: sqlite3.Connection) -> None:
    """The card leaves Today, and a copy goes to Notifications › Dismissed so it can be opened again later
    (Brian, 2026-11-22). App only, never Discord; once per morning."""
    card = overnight_card(conn)
    set_setting(conn, "overnight_seen", date.today().isoformat())
    if card:
        nid = notify.enqueue(conn, "bot", f"Overnight: {card['ok']} recording{'s' if card['ok'] != 1 else ''} transcribed "
                             f"({card['hours']} h of audio)" + (f", {card['bad']} failed" if card["bad"] else ""),
                             f"/conversations?night={date.today().isoformat()}", discord=False)
        conn.execute("UPDATE notification SET dismissed_at = ? WHERE id = ?", (db.now(), nid))


@router.post("/overnight/dismiss")
def overnight_dismiss(request: Request, conn: sqlite3.Connection = Conn):
    overnight_seen(conn)
    return back(request, "/")


@router.get("/", response_class=HTMLResponse)
def today_page(request: Request, conn: sqlite3.Connection = Conn):
    area = area_of(request)
    a, args = area_clause(area, "t.area")
    today = date.today().isoformat()
    due = conn.execute(
        "SELECT t.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, "
        "(SELECT idx FROM segment g WHERE g.id = t.source_segment_id) AS line "   # F96: its link jumped to "#L" (no line)
        "FROM task t LEFT JOIN conversation c ON c.id = t.conversation_id "
        f"WHERE t.status = 'open' AND t.inbox = 0 AND t.owner_person_id = 1{a} "
        f"AND {LIVE_TASK} ORDER BY t.due IS NULL, t.due, t.id LIMIT 50", args).fetchall()
    late = [t for t in due if t["due"] and t["due"] <= today]
    due = late + [t for t in due if t not in late][:max(0, 3 - len(late))]   # Next up: everything due, topped up to 3
    overdue_theirs = conn.execute(   # promises to Brian that are now late: worth a nudge (drafted by Brian, never sent for him)
        "SELECT t.*, p.name AS owner_name, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) "
        "AS line FROM task t JOIN person p ON p.id = t.owner_person_id LEFT JOIN conversation c ON c.id = t.conversation_id "
        f"WHERE t.status = 'open' AND t.inbox = 0 AND t.owner_person_id != 1 AND t.due IS NOT NULL AND t.due < ?{a} "
        f"AND {LIVE_TASK} ORDER BY t.due", [today, *args]).fetchall()
    overdue_theirs = [{**dict(t), "nudge": nudge(t)} for t in overdue_theirs]
    cap = float(setting(conn, "global_cap_usd", str(DEFAULT_CAP_USD)))
    month = spend(conn, month_start())
    needs = needs_you(conn, area)
    n_inbox = inbox_count(conn)
    inbox_hours = (conn.execute("SELECT coalesce(sum(duration_s), 0) FROM recording WHERE status IN "
                                "('awaiting_approval', 'backlog')").fetchone()[0] or 0) / 3600
    ca, cargs = area_clause(area, "c.area")
    recent = conn.execute(   # redesign (Brian, 2026-11-21): the last few talks, one line each
        "SELECT c.id, c.title, c.started_at, c.created_at, c.duration_s, c.area, c.type, "
        "(SELECT count(*) FROM task t WHERE t.conversation_id = c.id AND t.status != 'dropped') AS n_tasks "
        f"FROM conversation c WHERE c.deleted_at IS NULL AND (c.status = 'extracted' OR {FINISHED}){ca} "
        "ORDER BY coalesce(c.started_at, c.created_at) DESC LIMIT 3", cargs).fetchall()
    n_waiting = conn.execute(f"SELECT count(*) FROM task t WHERE t.status = 'open' AND t.inbox = 0 AND t.owner_person_id != 1{a} "
                             f"AND {LIVE_TASK}", args).fetchone()[0]
    return render(request, "today.html", "today", conn, overnight=overnight_card(conn), due=due, overdue_theirs=overdue_theirs, **needs, week=week(request, conn),
                  recent=recent, n_waiting=n_waiting,
                  n_inbox=n_inbox, inbox_hours=inbox_hours, people=people(conn), cleanup=cleanup_devices.counts(conn),
                  cleanup_ask=conn.execute("SELECT device, title FROM device_cleanup WHERE status = 'ask' ORDER BY recorded_at").fetchall(),
                  jobs=progress.active_jobs(conn), month_spend=month, cap=cap,
                  spend_alert=cap and month / cap >= 0.8, worker=worker_status(request.app.state.cfg),
                  demo_mode=request.app.state.cfg.demo,
                  paused=setting(conn, "pipeline_paused", "0") == "1", show_help=setting(conn, "help_dismissed") != "1",
                  tour_conv=tour_conversation(conn))


def needs_you(conn: sqlite3.Connection, area: str) -> dict:
    """What Today's Needs you and Review › Open show (same rows, same count as the Today tab), plus what's snoozed
    (Today's Snoozed group, Review › Snoozed): snoozed review items and the snoozed M14 / older-recordings lines."""
    review, m14 = review_data(conn, area), machine.needs_attention(conn)
    snoozed = snoozed_rows(conn, area)
    if put_off(conn, "m14"):
        if m14:
            snoozed.append({"what": "M14 health", "title": "The M14 needs attention", "until": setting(conn, "later:m14"),
                            "now": "/needs/m14/now"})
        m14 = []
    a, args = area_clause(area, "t.area")
    stale = conn.execute(f"SELECT count(*) FROM task t WHERE {cleanup.STALE} AND {LIVE_TASK}{a}", args).fetchone()[0]
    now, reminders = db.now(), []
    for t in conn.execute(   # task reminders: due ones under Needs you, later ones in Snoozed
            "SELECT t.*, p.name AS owner_name, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) "
            "AS line FROM task t JOIN person p ON p.id = t.owner_person_id LEFT JOIN conversation c ON c.id = t.conversation_id "
            f"WHERE t.status = 'open' AND t.remind_at IS NOT NULL AND {LIVE_TASK}{a} ORDER BY t.remind_at", args):
        if t["remind_at"] <= now:
            reminders.append(t)
        else:
            snoozed.append({"what": "Task reminder", "title": t["title"], "until": t["remind_at"], "now": f"/tasks/{t['id']}/remind/now"})
    return {**review, "m14": m14, "stale": stale, "reminders": reminders, "snoozed": sorted(snoozed, key=lambda x: x["until"])}


@router.post("/tasks/{tid}/remind")
def task_remind(request: Request, tid: int, until: str = Form("tomorrow"), at: str = Form(""), conn: sqlite3.Connection = Conn):
    """Remind me about this task: at that time it shows under Needs you on Today (Snoozed until then)."""
    when = snooze_until(until, at)
    with db.transaction(conn):
        if not conn.execute("UPDATE task SET remind_at = ? WHERE id = ? AND status = 'open'", (when, tid)).rowcount:
            raise HTTPException(404, "That task isn't open any more")
    return back(request, "/tasks")


@router.post("/tasks/{tid}/remind/{what}")
def task_remind_now_or_clear(request: Request, tid: int, what: str, conn: sqlite3.Connection = Conn):
    """now: bring the reminder back under Needs you now. clear ("Got it"): drop the reminder; the task stays open."""
    if what not in ("now", "clear"):
        raise HTTPException(404)
    with db.transaction(conn):
        conn.execute("UPDATE task SET remind_at = ? WHERE id = ?", (db.now() if what == "now" else None, tid))
    return back(request, "/")


@router.post("/help/dismiss")
def dismiss_help(request: Request, conn: sqlite3.Connection = Conn):
    set_setting(conn, "help_dismissed", "1")
    return back(request, "/")


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, conn: sqlite3.Connection = Conn):
    demo = conn.execute("SELECT id FROM conversation WHERE title LIKE 'DEMO:%' AND deleted_at IS NULL").fetchone()
    from scribe_bot.web import test_inbox
    return render(request, "help.html", "help", conn, demo_id=demo[0] if demo else None, tour_conv=tour_conversation(conn),
                  test_recs=len(test_inbox.ours(conn, request.app.state.cfg)))


@router.post("/test-inbox/add")
def test_inbox_add(request: Request, more: str = Form(""), conn: sqlite3.Connection = Conn):
    """M7 (Brian, 2026-11-25): three labelled, made-up recordings in the Inbox to try its buttons on; R4-24: more=1
    adds 30 short ones so the Inbox has enough to scroll."""
    from scribe_bot.web import test_inbox
    test_inbox.add(conn, request.app.state.cfg, more=bool(more))
    return RedirectResponse("/inbox", status_code=303)


@router.post("/test-inbox/remove")
def test_inbox_remove(request: Request, conn: sqlite3.Connection = Conn):
    from scribe_bot.web import test_inbox
    test_inbox.remove(conn, request.app.state.cfg)
    return RedirectResponse("/help#test-recordings", status_code=303)


def tour_conversation(conn: sqlite3.Connection) -> int | None:
    """The conversation the tour opens: the DEMO one if loaded, else the newest."""
    row = conn.execute("SELECT id FROM conversation WHERE deleted_at IS NULL ORDER BY title LIKE 'DEMO:%' DESC, "
                       "coalesce(started_at, created_at) DESC LIMIT 1").fetchone()
    return row[0] if row else None


@router.post("/demo/load")
def demo_load(conn: sqlite3.Connection = Conn):
    from scribe_bot.web import demo
    return RedirectResponse(f"/conversations/{demo.load(conn)}", status_code=303)


@router.post("/demo/remove")
def demo_remove(conn: sqlite3.Connection = Conn):
    from scribe_bot.web import demo
    demo.remove(conn)
    return RedirectResponse("/help", status_code=303)


@router.get("/progress/active", response_class=HTMLResponse)
def progress_fragment(request: Request, conn: sqlite3.Connection = Conn):
    """The Today page's live Processing card (HTMX polls this every 15 s while something is running)."""
    return templates.TemplateResponse(request, "_progress.html", {"jobs": progress.active_jobs(conn)})


@router.get("/progress/talks", response_class=HTMLResponse)
def talks_now_fragment(request: Request, n: int = 0, conn: sqlite3.Connection = Conn):
    """Talks page "Processing now" (Brian, 2026-11-21). Fewer jobs than last time = one finished: reload the page so the
    finished talk moves into its date group."""
    jobs = progress.active_jobs(conn)
    if len(jobs) < n:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(request, "_talks_now.html", {"jobs": jobs})


@router.get("/processing", response_class=HTMLResponse)
def processing_history(request: Request, conn: sqlite3.Connection = Conn):
    """Processing history (Brian, 2026-11-21: "I tapped one, something else was processing and I couldn't tell what"):
    every recording job, newest first: what it was (quick title + first words), when you sent it, when it finished, how
    long it took, AI notes or transcript only."""
    rows = []
    active = {p["job_id"]: p for p in progress.active_jobs(conn)}   # B39: queue-aware times
    for j in conn.execute("SELECT j.id, j.status, j.payload, j.created_at, j.updated_at, j.recording_id, r.title AS rec_title, "
                          "r.duration_s, c.id AS cid, c.title, c.private, c.deleted_at, c.summary, pk.text AS peek FROM job j "
                          "LEFT JOIN recording r ON r.id = j.recording_id LEFT JOIN conversation c ON c.recording_id = j.recording_id "
                          "LEFT JOIN recording_peek pk ON pk.recording_id = j.recording_id "   # F88: was 2 reads a row
                          "WHERE j.kind = 'recording' ORDER BY j.id DESC LIMIT 60"):
        pay = json.loads(j["payload"] or "{}")
        first = conn.execute("SELECT min(started_at) FROM stage_run WHERE job_id = ?", (j["id"],)).fetchone()[0]
        p = active.get(j["id"]) if j["status"] in ("running", "pending") else None
        rows.append({"id": j["id"], "status": j["status"], "cid": j["cid"] if not j["deleted_at"] else None,
                     "title": j["title"] or progress._quick((j["peek"] or "").strip()) or j["rec_title"] or "Recording",
                     "preview": progress._preview((j["peek"] or "").strip()), "minutes": round((j["duration_s"] or 0) / 60),
                     "ai": bool(j["summary"]) or (pay.get("ai") is not False and not j["private"]), "sent": j["created_at"], "started": first,
                     "finished": j["updated_at"] if j["status"] in ("done", "failed") else None, "p": p,
                     "took": progress.duration_label((datetime.fromisoformat(j["updated_at"]) - datetime.fromisoformat(first)).total_seconds())
                     if first and j["status"] == "done" else ""})
    return render(request, "processing.html", "more", conn, rows=rows, ctl=control.state(conn), speed=pace.mode(conn),
                  pace_label=setting(conn, "processing_pace") or "", heat=pace.heat_paused(conn))


@router.get("/more", response_class=HTMLResponse)
def more_page(request: Request, conn: sqlite3.Connection = Conn):
    return render(request, "more.html", "more", conn)


# ---------- Conversations ----------

def like_q(raw: str | None) -> str:
    """A list's search box (N9, Brian picked ⭐ 2026-11-25): the words as a LIKE pattern, % and _ taken literally."""
    q = (raw or "").strip()[:100]
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if q else ""


def conv_where(request: Request, person, type, start, end, unsorted) -> tuple[list[str], list]:
    """The Conversations filters as SQL (the list and its export use the same ones)."""
    area = area_of(request)
    where, args = ["c.deleted_at IS NULL"], []
    if unsorted:   # finished but not filed as Work or Home yet: their own list, one tap to file each
        where.append(UNSORTED)
    elif area != "all":   # still being processed: under both until it's filed
        where.append(f"(c.area = ? OR (c.area IS NULL AND NOT {FINISHED}))"); args.append(area)
    if person:
        where.append("EXISTS (SELECT 1 FROM speaker s WHERE s.conversation_id = c.id AND s.person_id = ?)"); args.append(person)
    if type:
        where.append("c.type = ?"); args.append(type)
    tag = request.query_params.get("tag")
    if tag and tag.isdigit():   # M4: Filter › Tag
        where.append("EXISTS (SELECT 1 FROM conversation_tag ct WHERE ct.conversation_id = c.id AND ct.tag_id = ?)"); args.append(int(tag))
    if start:
        where.append("substr(coalesce(c.started_at, c.created_at), 1, 10) >= ?"); args.append(start)
    if end:
        where.append("substr(coalesce(c.started_at, c.created_at), 1, 10) <= ?"); args.append(end)
    q = like_q(request.query_params.get("q"))
    if q:   # N9: the search box above the list (title, summary or a tag)
        where.append("(c.title LIKE ? ESCAPE '\\' OR coalesce(c.short_title, '') LIKE ? ESCAPE '\\' OR coalesce(c.summary, '') LIKE ? ESCAPE '\\' "
                     "OR EXISTS (SELECT 1 FROM conversation_tag ct JOIN tag tg ON tg.id = ct.tag_id WHERE ct.conversation_id = c.id AND tg.name LIKE ? ESCAPE '\\'))")
        args += [q, q, q, q]
    return where, args


@router.get("/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, person: int | None = None, type: str | None = None,
                       start: str | None = None, end: str | None = None, trash: int = 0, unsorted: int = 0, n: int = PAGE,
                       order: str = "newest", conn: sqlite3.Connection = Conn):
    if trash:
        rows = [{**dict(r), "gone_on": gone_on(r["deleted_at"])} for r in conn.execute(
            "SELECT * FROM conversation WHERE deleted_at IS NOT NULL AND purge_at IS NULL ORDER BY deleted_at DESC")]
        skipped = [{**dict(r), "gone_on": gone_on(r["resolved_at"])} for r in conn.execute(   # Inbox Skip lands here too
            "SELECT r.id, r.title, r.duration_s, r.received_at, max(i.resolved_at) AS resolved_at FROM recording r "
            "JOIN review_item i ON i.kind = 'approval' AND i.ref_id = r.id AND i.status = 'dismissed' "
            "WHERE r.status = 'declined' AND r.audio_deleted_at IS NULL GROUP BY r.id ORDER BY resolved_at DESC")]
        # one list, newest first (2026-11-23: an Inbox delete sat in a 2nd group under the deleted talks, off screen on
        # the phone, so Brian thought it wasn't in Trash)
        items = sorted([{**r, "from_inbox": False, "when": r["deleted_at"]} for r in rows] +
                       [{**r, "from_inbox": True, "when": r["resolved_at"]} for r in skipped], key=lambda r: r["when"] or "", reverse=True)
        return render(request, "conversations.html", "conversations", conn, rows=rows, skipped=skipped, items=items, trash=True,
                      trash_days=TRASH_DAYS, f={})
    where, args = conv_where(request, person, type, start, end, unsorted)
    night_q = request.query_params.get("night", "")
    try:   # F70: a date that can't be (2026-02-30, from a mangled link) crashed the list; it's ignored now
        day = date.today() if night_q == "1" else date.fromisoformat(night_q) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", night_q) else None
    except ValueError:
        day = None
    night = day is not None
    if night:   # the overnight card (S1): just last night's talks; opening this list dismisses the card
        s_utc, e_utc = night_window_utc(datetime.combine(day, time(12)))   # a date = that morning (from Notifications)
        if day == date.today() and setting(conn, "overnight_seen", "") != day.isoformat():
            overnight_seen(conn)
        where.append(f"EXISTS (SELECT 1 FROM job j WHERE j.recording_id = c.recording_id AND {NIGHT_JOB})")
        args = [*args, s_utc, e_utc] if isinstance(args, list) else (*args, s_utc, e_utc)
    now_jobs = [] if unsorted or night else progress.active_jobs(conn)
    if now_jobs:   # Brian (2026-11-21): still processing = only in "Processing now" at the top, not in the dated list too
        where.append("NOT EXISTS (SELECT 1 FROM job j WHERE j.kind = 'recording' AND j.recording_id = c.recording_id "
                     "AND j.status IN ('running', 'pending'))")
    rows, total = paged(conn,
        "SELECT c.*, (SELECT group_concat(name, ', ') FROM (SELECT DISTINCT p.name FROM speaker s JOIN person p ON p.id = s.person_id "
        "             WHERE s.conversation_id = c.id AND p.id != 1)) AS people, "   # DISTINCT: two voices can be one person (Brian, 2026-11-22)
        f"({N_UNNAMED_SQL}) AS n_unnamed, "
        "(SELECT group_concat(t.name, '|') FROM conversation_tag ct JOIN tag t ON t.id = ct.tag_id WHERE ct.conversation_id = c.id) AS tags, "
        "(SELECT min(t.name COLLATE NOCASE) FROM conversation_tag ct JOIN tag t ON t.id = ct.tag_id WHERE ct.conversation_id = c.id) AS first_tag "
        f"FROM conversation c WHERE {' AND '.join(where)} ORDER BY {CONV_ORDERS.get(order, CONV_ORDERS['newest'])[1]}", args, n)
    n_unsorted = 0 if unsorted else conn.execute(f"SELECT count(*) FROM conversation c WHERE c.deleted_at IS NULL AND {UNSORTED}").fetchone()[0]
    tag = request.query_params.get("tag", "")
    return render(request, "conversations.html", "conversations", conn, rows=rows, total=total, people=people(conn),
                  unsorted=unsorted, n_unsorted=n_unsorted, all_tags=tags_svc.counts(conn), jobs=now_jobs, night=night,
                  ai_busy=ai_jobs.pending_ids(conn), night_label=("last night" if not night or day == date.today() else "the night before " + day.strftime("%b %-d")),
                  orders=CONV_ORDERS, f={"person": person, "type": type or "", "start": start or "", "end": end or "",
                                         "order": order if order in CONV_ORDERS else "newest", "tag": tag if tag.isdigit() else "",
                                         "q": (request.query_params.get("q") or "").strip()[:100]})


CONV_ORDERS = {   # Conversations › Sort: key -> (label, ORDER BY); SQL comes from here only, never from the request
    "newest": ("Newest", "coalesce(c.started_at, c.created_at) DESC"),
    "oldest": ("Oldest", "coalesce(c.started_at, c.created_at)"),
    "longest": ("Longest", "coalesce(c.duration_s, 0) DESC, coalesce(c.started_at, c.created_at) DESC"),
    "title": ("A–Z", "c.title COLLATE NOCASE, coalesce(c.started_at, c.created_at) DESC"),
    "tag": ("Group by tag", "first_tag IS NULL, first_tag COLLATE NOCASE, coalesce(c.started_at, c.created_at) DESC"),   # M4
}
# Finished talks: 'done' (AI notes) or 'transcribed' once its processing is over (Transcript only stays 'transcribed' for
# good; it only checked 'done', so transcript-only talks were never "Not sorted", never on Today's recent list and showed
# under every area filter). 'transcribed' while the AI step still runs is not finished yet.
FINISHED = ("(c.status = 'done' OR (c.status = 'transcribed' AND EXISTS (SELECT 1 FROM recording fr "
            "WHERE fr.id = c.recording_id AND fr.status = 'done')))")
UNSORTED = f"c.area IS NULL AND {FINISHED}"   # finished, not filed as Work or Home yet ("Not sorted")


@router.post("/recordings/{rid}/unskip")
def recording_unskip(request: Request, rid: int, conn: sqlite3.Connection = Conn):
    """Trash › Skipped recordings › Restore: back to the Inbox, as if it had never been skipped. Undo puts it back in
    Trash (2026-11-25)."""
    with db.transaction(conn):
        if _unskip_rows(conn, [rid]):
            undo.add(conn, undo.save(conn, "Restored", []), _unskip_rows(conn, [rid]))
            _unskip(conn, rid)
    return back(request, "/conversations?trash=1")


def _unskip_rows(conn: sqlite3.Connection, rids: list[int]) -> list[tuple[str, int]]:
    """What restoring skipped recordings changes (for Undo): each recording and its newest Inbox ask."""
    rows = []
    for rid in rids:
        if conn.execute("SELECT 1 FROM recording WHERE id = ? AND status = 'declined' AND audio_deleted_at IS NULL",
                        (rid,)).fetchone():
            rows.append(("recording", rid))
            item = conn.execute("SELECT id FROM review_item WHERE kind = 'approval' AND ref_id = ? ORDER BY id DESC LIMIT 1",
                                (rid,)).fetchone()
            if item:
                rows.append(("review_item", item[0]))
    return rows


def _unskip(conn: sqlite3.Connection, rid: int) -> None:
    if conn.execute("UPDATE recording SET status = 'awaiting_approval' WHERE id = ? AND status = 'declined' "
                    "AND audio_deleted_at IS NULL", (rid,)).rowcount:
        item = conn.execute("SELECT id FROM review_item WHERE kind = 'approval' AND ref_id = ? ORDER BY id DESC LIMIT 1",
                            (rid,)).fetchone()
        if item:
            conn.execute("UPDATE review_item SET status = 'open', resolved_at = NULL WHERE id = ?", (item[0],))


def gone_on(deleted_at: str | None) -> str | None:
    """When a conversation in Trash is removed for good (the nightly purge after TRASH_DAYS)."""
    return (datetime.fromisoformat(deleted_at) + timedelta(days=TRASH_DAYS)).isoformat() if deleted_at else None


def get_conversation(conn: sqlite3.Connection, cid: int) -> sqlite3.Row:
    c = conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone()
    if c is None or c["purge_at"]:   # purge_at: deleted forever, waiting out its Undo time
        raise HTTPException(404, "That talk isn't here any more (deleted for good, or the link is wrong)")
    return c


def _md(text: str, title: str, fmt: str = "md") -> Response:
    """A file download (.md or .pdf, made on the M14, saved on Brian's device). `attachment` = always a download."""
    body, kind = (export.to_pdf(text, title), "application/pdf") if fmt == "pdf" else (text, "text/markdown; charset=utf-8")
    return Response(body, media_type=kind,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(export.filename(title, fmt))}"})


EXPORT_MAX = 100   # conversations per list export


def _fmt(fmt: str) -> str:
    if fmt not in ("md", "pdf"):
        raise HTTPException(404, "Exports are .md or .pdf")
    return fmt


@router.post("/conversations/{cid}/tags")
def conversation_tag_add(request: Request, cid: int, name: str = Form(...), conn: sqlite3.Connection = Conn):
    get_conversation(conn, cid)
    for n in name.split(","):   # "budget, venue" adds both
        tags_svc.add(conn, cid, n)
    return back(request, f"/conversations/{cid}")


@router.post("/conversations/{cid}/tags/{tid}/remove")
def conversation_tag_remove(request: Request, cid: int, tid: int, conn: sqlite3.Connection = Conn):
    t = conn.execute("SELECT name FROM tag WHERE id = ?", (tid,)).fetchone()
    if not conn.execute("SELECT 1 FROM conversation_tag WHERE conversation_id = ? AND tag_id = ?", (cid, tid)).fetchone():
        return back(request, f"/conversations/{cid}")   # F75: removed already (a second tap): no second Undo of nothing
    with db.transaction(conn):
        if t is not None:   # U12 (Brian, 2026-11-25): Undo on every delete, tags included
            uid = undo.save(conn, "Tag removed", [])
            undo.removed(conn, uid, "tag", "id = ?", (tid,))
            undo.removed(conn, uid, "conversation_tag", "conversation_id = ? AND tag_id = ?", (cid, tid))
        tags_svc.remove(conn, cid, tid)
    return back(request, f"/conversations/{cid}")


@router.post("/conversations/{cid}/tags/{tid}/keep")
def conversation_tag_keep(request: Request, cid: int, tid: int, conn: sqlite3.Connection = Conn):
    tags_svc.keep(conn, cid, tid)
    return back(request, f"/conversations/{cid}")


@router.get("/conversations/export.{fmt}")
def conversations_export(request: Request, fmt: str, person: int | None = None, type: str | None = None, start: str | None = None,
                         end: str | None = None, order: str = "newest", transcript: int = 0, ids: str = "",
                         conn: sqlite3.Connection = Conn):
    """The Conversations list as filtered now, newest first by default; summaries only unless transcript=1. `ids`
    (Brian, 2026-11-20: Select › tick talks › Export) exports just the picked ones."""
    where, args = conv_where(request, person, type, start, end, 0)
    picked = _id_list(ids)[:EXPORT_MAX]
    if picked:
        where = ["c.deleted_at IS NULL", f"c.id IN ({','.join('?' * len(picked))})"]; args = picked
    ids = [r[0] for r in conn.execute(f"SELECT c.id FROM conversation c WHERE {' AND '.join(where)} ORDER BY "
                                      f"{CONV_ORDERS.get(order, CONV_ORDERS['newest'])[1]} LIMIT {EXPORT_MAX}", args)]
    body = "\n\n---\n\n".join(export.conversation_md(conn, i, transcript=bool(transcript)) for i in ids)
    return _md(f"# Scribe Bot export · {len(ids)} talk{'s' if len(ids) != 1 else ''}\n\n" + body, f"Scribe Bot {date.today().isoformat()}", _fmt(fmt))


@router.get("/conversations/{cid}/export.{fmt}")
def conversation_export(cid: int, fmt: str, transcript: int = 1, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    return _md(export.conversation_md(conn, cid, transcript=bool(transcript)), c["title"], _fmt(fmt))


@router.get("/people/{pid}/export.{fmt}")
def person_export(pid: int, fmt: str, conn: sqlite3.Connection = Conn):
    p = conn.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    return _md(export.person_md(conn, pid), p["name"], _fmt(fmt))


@router.post("/conversations/{cid}/opened")
def conversation_opened(cid: int, conn: sqlite3.Connection = Conn):
    """A prefetched talk page was actually shown: now it counts as opened."""
    with db.transaction(conn):
        conn.execute("UPDATE conversation SET opened_at = ? WHERE id = ? AND status IN ('done', 'transcribed')", (db.now(), cid))
    return Response(status_code=204)


@router.get("/conversations/{cid}", response_class=HTMLResponse)
def conversation_page(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    unopened = False
    if c["status"] in ("done", "transcribed") and (c["opened_at"] is None or c["opened_at"] < c["updated_at"]):
        # its "New" badge goes and its Ready ping tidies itself (notify.tidy); no write on ordinary re-visits.
        # A prefetch (the phone loading it the moment a finger touches the row, 2026-11-22) isn't an open: the page
        # itself reports the open if it's actually shown (POST /conversations/{id}/opened).
        if "prefetch" in request.headers.get("sec-purpose", ""):
            unopened = True
        else:
            with db.transaction(conn):
                conn.execute("UPDATE conversation SET opened_at = ? WHERE id = ?", (db.now(), cid))
    speakers = speakers_of(conn, cid)
    name_of = {s["id"]: s["name"] for s in speakers}
    segments = [{**dict(r), "name": name_of.get(r["speaker_id"], "Unknown")} for r in conn.execute(
        "SELECT * FROM segment WHERE conversation_id = ? ORDER BY idx", (cid,))]
    line = "(SELECT idx FROM segment g WHERE g.id = x.source_segment_id) AS line"
    tasks = conn.execute(
        f"SELECT x.*, p.name AS owner_name, {line} FROM task x JOIN person p ON p.id = x.owner_person_id "
        "WHERE x.conversation_id = ? AND x.status != 'dropped' ORDER BY x.id", (cid,)).fetchall()
    decisions = conn.execute(
        f"SELECT x.*, {line} FROM decision x WHERE x.conversation_id = ? AND coalesce(x.rated, '') != 'wrong' ORDER BY x.id",
        (cid,)).fetchall()
    facts = conn.execute(
        f"SELECT x.*, coalesce(p.name, x.person_name, 'Unknown') AS who, {line} FROM person_fact x "
        "LEFT JOIN person p ON p.id = x.person_id WHERE x.conversation_id = ? AND coalesce(x.confirmed, 1) != 0 "
        "ORDER BY who, x.id", (cid,)).fetchall()
    grouped: dict[str, list] = {}
    for f in facts:
        grouped.setdefault(f["who"], []).append(f)
    rec = conn.execute("SELECT title FROM recording WHERE id = ?", (c["recording_id"],)).fetchone()
    has_audio = playable_audio(conn, cid) is not None
    mine = [t for t in tasks if t["owner_person_id"] == ME]
    theirs = [t for t in tasks if t["owner_person_id"] != ME]
    # F132: "What to check" counts only what's still open (a done task read "1 you owe")
    check = {"mine": sum(t["status"] == "open" for t in mine), "theirs": sum(t["status"] == "open" for t in theirs), "notes": len(facts),
             "unsure": sum(1 for t in tasks if t["status"] == "open"
                           and (t["inbox"] or (t["confidence"] is not None and t["confidence"] < 0.6))),
             "unnamed": sum(1 for s in speakers if s["person_id"] is None and not s["unknown"])}
    preview = None if not segments else extract.estimate_cost(conn, cid, ending_private=bool(c["private"]))
    processing = progress.latest_for_recording(conn, c["recording_id"])
    ai_calls = conn.execute(
        "SELECT model, count(*) AS calls, coalesce(sum(cost_usd), 0) AS cost, max(time) AS last FROM ledger_entry "
        "WHERE conversation_id = ? AND status = 'ok' GROUP BY model", (cid,)).fetchall()
    pace = talk.pace_by_speaker(conn, cid)
    parts = merge_svc.parts_of(conn, c["recording_id"])   # a merged talk: where each recording starts
    # Brian, 2026-11-21: a talk with no AI notes (Transcript only / Private) showed 5 empty headings. It now gets one
    # short card instead (topic words + how it starts); the headings come back if AI notes are made later.
    bare = (not c["summary"] and not mine and not theirs and not decisions and not facts and bool(segments)
            and c["status"] != "transcribing")
    topics = inbox_svc.quick_title(" ".join(x["text"] for x in segments), n=6) if bare else ""
    opening = inbox_svc.preview([{"speaker": "", "text": " ".join(x["text"] for x in segments[:60])}], chars=1400) if bare else ""
    part = request.query_params.get("part")
    return render(request, "_tx_part.html" if part == "lines" else "conversation.html", "conversations", conn, c=c, unopened=unopened, parts=parts, bare=bare, topics=topics, opening=opening,
                  tx_inline=request.query_params.get("tx") == "1",
                  tags=tags_svc.of(conn, cid), tag_names=tags_svc.names(conn), detail_pending=ai_jobs.detail_pending(conn, cid),
                  notes_view="detailed" if request.query_params.get("notes") == "detailed" else "short", short_speaker_s=SHORT_SPEAKER_SECONDS, person_colors=PERSON_COLORS, gone_on=gone_on(c["deleted_at"]),
                  speakers=speakers, segments=segments,
                  mine=mine, theirs=theirs, check=check, recording_title=rec["title"] if rec else None,
                  decisions=decisions, facts=grouped, n_facts=len(facts), takeaways=json.loads(c["takeaways"] or "[]"),
                  people=people(conn), has_audio=has_audio, preview=preview, rerun_pending=ai_jobs.pending_for(conn, cid), compare_pending=ai_jobs.compare_pending(conn, cid),
                  compare_ready=extract.compared(conn, cid) is not None, compare_model=model_label(extract.other_model(conn, cid)[1]),
                  compare_queued=request.query_params.get("compare") == "queued",
                  rerun_queued=request.query_params.get("rerun") == "queued", processing=processing, ai_calls=ai_calls,
                  meeting_types=prompts.MEETING_TYPES, speaking=(sp := talk.speaking(conn, cid)), speaking_tips=talk.tips(sp) if sp else [],
                  others_pace=[(s["name"], w) for s in speakers if s["person_id"] != ME
                               and (w := pace.get(s["id"]))])


# ---------- Confirm before spending (two steps on the server: the first tap only shows the cost) ----------

MODEL_LABELS = (("claude-sonnet", "Claude Sonnet"), ("claude-haiku", "Claude Haiku"), ("claude-opus", "Claude Opus"),
                ("gemini", "Gemini"), ("gpt", "OpenAI"))
TOKENS_PER_AUDIO_MINUTE = 175   # ~130 spoken words a minute; plus ~2k tokens of instructions


def model_label(model: str | None) -> str:
    m = (model or "").lower()
    return next((label for key, label in MODEL_LABELS if m.startswith(key)), model or "")


def ai_label(processed_by: str | None) -> str:
    """'brian:claude-sonnet-4-5' (account:model, how the pipeline records it) -> 'Claude Sonnet'."""
    return model_label((processed_by or "").rsplit(":", 1)[-1])


def recorder_title(name: str | None) -> str:
    """Recorder file names use '_' for the time ("Oct 19 at 16_40"); show it as a time ("Oct 19 at 16:40")."""
    name = re.sub(r"\s_\s", " - ", name or "")          # Recorder saves "A - B" as "A _ B" in file names
    when = voicememo.name_date(name)
    if when:   # Apple Voice Memos names files by date: "20261113 201200" -> "Nov 13 at 20:12"
        name = voicememo.NAME_DATE.sub(f"{when:%b} {when.day} at {when:%H:%M}", name)
    return re.sub(r"\b(\d{1,2})_(\d{2})\b", r"\1:\2", name)


# F87 (500 pass): Costs and the Data Ledger showed the code's own words: task "extract", status "ok", "$0.0000", and
# token counts without commas ("24879 / 2306") beside the By talk list's "24,879 / 2,306".
LEDGER_TASKS = {"extract": "AI notes", "ping": "Key test", "console_match": "Matched to the Anthropic console"}
LEDGER_STATUS = {"ok": "Sent", "blocked": "Blocked, not sent", "failed": "Failed", "adjustment": "Matched"}


def usd(v, places: int = 2) -> str:
    """$0.0291 at 4 places; nothing spent is "$0", not "$0.0000"."""
    return f"${v:.{places}f}" if v else "$0"


templates.env.filters["model_label"] = model_label
templates.env.filters["ai_label"] = ai_label
templates.env.filters["ledger_task"] = lambda t: LEDGER_TASKS.get(t, (t or "").replace("_", " ").capitalize())
templates.env.filters["ledger_status"] = lambda s: LEDGER_STATUS.get(s, (s or "").capitalize())
templates.env.filters["usd"] = usd
templates.env.filters["or_list"] = lambda xs: " or ".join([", ".join(xs[:-1]), xs[-1]] if len(xs) > 2 else xs)   # F90: "A, B or C"
templates.env.filters["recorder_title"] = recorder_title
templates.env.globals["speaking_levels"] = talk.levels
templates.env.filters["area_label"] = lambda a: {"work": "Work", "personal": "Home", "school": "School", "other": "Other"}.get(a, a or "")   # Q2: "Home" everywhere
templates.env.globals["chart_keys"] = {c[0] for c in machine.CHARTS}   # M14 tiles that open their history chart


def fragment_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def confirm_page(request: Request, conn: sqlite3.Connection, nav: str, *, heading: str, cost: float, model: str | None,
                 tokens: int | None, detail: str, action: str, cancel: str, fields: dict | None = None,
                 blocked: str | None = None, type_pick: str | None = None) -> HTMLResponse:
    frag = fragment_request(request)
    return render(request, "confirm.html", nav, conn, layout="_bare.html" if frag else "base.html", fragment=frag,
                  heading=heading, cost=cost or 0.0, model=model_label(model), tokens=f"{round(tokens):,}" if tokens else None,
                  detail=detail, action=action, cancel=cancel, fields=fields or {}, blocked=blocked,
                  type_pick=type_pick, meeting_types=prompts.MEETING_TYPES)


def confirmed(confirm: str) -> bool:
    return confirm == "1"


@router.get("/conversations/{cid}/rerun/confirm", response_class=HTMLResponse)
def rerun_confirm(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    est = extract.estimate_cost(conn, cid, ending_private=bool(c["private"]))
    return confirm_page(request, conn, "conversations", heading="Re-run the AI on this talk?" if c["summary"] else "Summarize this talk?", cost=est["cost_usd_est"],
                        model=est["model"], tokens=est["tokens_in_est"], blocked=est["blocked"],
                        detail="Sends the transcript text (never the audio). Edits you already made are kept.",
                        action=f"/conversations/{cid}/rerun", cancel=f"/conversations/{cid}")


@router.post("/conversations/{cid}/rerun")
def rerun_ai(request: Request, cid: int, confirm: str = Form(""), end_private: str = Form(""),
             conn: sqlite3.Connection = Conn):
    """Re-run AI, only from the confirm step (Brian saw the cost), so the run counts as approved. A Private conversation
    runs only from its own confirm step, which ends Private first (the AI never runs on a Private one)."""
    c = get_conversation(conn, cid)
    if not confirmed(confirm):
        return RedirectResponse(f"/conversations/{cid}/rerun/confirm", status_code=303)
    if stop := demo_stop(request, "ai", f"/conversations/{cid}"):
        return stop
    if c["private"]:
        if end_private != "1":
            raise HTTPException(400, "Private talks go to the AI only after you end Private on the confirm step")
        with db.transaction(conn):
            conn.execute("UPDATE conversation SET private = 0, updated_at = ? WHERE id = ?", (db.now(), cid))
            if c["recording_id"]:
                conn.execute("UPDATE recording SET private = 0 WHERE id = ?", (c["recording_id"],))
    ai_jobs.queue_rerun(conn, cid, approved=True)
    return RedirectResponse(f"/conversations/{cid}?rerun=queued", status_code=303)


# ---------- Short / Detailed notes (Brian, 2026-11-20) ----------

@router.get("/conversations/{cid}/detail/confirm", response_class=HTMLResponse)
def detail_confirm(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    est = extract.detail_estimate(conn, cid)
    return confirm_page(request, conn, "conversations", heading="Make detailed notes?", cost=est["cost_usd"],
                        model=est["model"], tokens=est["tokens_in"],
                        blocked="private: stays on the M14" if c["private"] else None,
                        detail="A longer version by topic: what was said, reasons, numbers, open questions. Sends the "
                               "transcript text (never the audio). The short notes stay as they are.",
                        action=f"/conversations/{cid}/detail", cancel=f"/conversations/{cid}")


@router.post("/conversations/{cid}/detail")
def detail_make(request: Request, cid: int, confirm: str = Form(""), conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    if not confirmed(confirm):
        return RedirectResponse(f"/conversations/{cid}/detail/confirm", status_code=303)
    if stop := demo_stop(request, "ai", f"/conversations/{cid}"):
        return stop
    if c["private"]:
        raise HTTPException(400, "Private talks stay on the M14")
    ai_jobs.queue_detail(conn, cid)
    return RedirectResponse(f"/conversations/{cid}?notes=detailed", status_code=303)


# ---------- Compare with the other model (Brian, 2026-11-19) ----------

@router.get("/conversations/{cid}/compare/confirm", response_class=HTMLResponse)
def compare_confirm(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    est = extract.compare_estimate(conn, cid)
    return confirm_page(request, conn, "conversations", heading=f"Compare with {model_label(est['model'])}?",
                        cost=est["cost_usd"], model=est["model"], tokens=est["tokens_in"],
                        blocked="private: stays on the M14" if c["private"] else None,
                        detail="Runs only the AI step again on this transcript (no audio work). Your current notes stay "
                               "until you pick which version to keep.",
                        action=f"/conversations/{cid}/compare", cancel=f"/conversations/{cid}")


@router.post("/conversations/{cid}/compare")
def compare_ai(request: Request, cid: int, confirm: str = Form(""), conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    if not confirmed(confirm):
        return RedirectResponse(f"/conversations/{cid}/compare/confirm", status_code=303)
    if stop := demo_stop(request, "ai", f"/conversations/{cid}"):
        return stop
    if c["private"]:
        raise HTTPException(400, "Private talks never go to the AI")
    ai_jobs.queue_compare(conn, cid)
    return RedirectResponse(f"/conversations/{cid}?compare=queued", status_code=303)


@router.get("/conversations/{cid}/compare", response_class=HTMLResponse)
def compare_page(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    other = extract.compared(conn, cid)
    if not other:
        return RedirectResponse(f"/conversations/{cid}", status_code=303)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM person")}
    current = {
        "by": c["processed_by"], "title": c["title"], "summary": c["summary"],
        "tasks": [(t["title"], names.get(t["owner_person_id"], "")) for t in conn.execute(
            "SELECT title, owner_person_id FROM task WHERE conversation_id = ? AND status != 'dropped' ORDER BY id", (cid,))],
        "decisions": [r[0] for r in conn.execute("SELECT text FROM decision WHERE conversation_id = ? ORDER BY id", (cid,))],
        "facts": [(r[0], r[1]) for r in conn.execute(
            "SELECT coalesce(p.name, x.person_name, 'Unknown'), x.value FROM person_fact x LEFT JOIN person p ON p.id = x.person_id "
            "WHERE x.conversation_id = ? ORDER BY x.id", (cid,))]}
    d = other["data"]
    alt = {"by": other["by"], "title": d.get("title"), "summary": d["summary"],
           "tasks": [(t["title"], t.get("owner") or "") for t in d["tasks"] + d["waiting_on"]],
           "decisions": [x["text"] for x in d["decisions"]], "facts": [(f["person"], f["fact"]) for f in d["people_facts"]]}
    cost = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM ledger_entry WHERE conversation_id = ? AND status = 'ok' "
                        "AND model = ?", (cid, (c["processed_by"] or "").rsplit(":", 1)[-1])).fetchone()[0]
    return render(request, "compare.html", "conversations", conn, c=c, versions=[
        {"key": "current", "label": ai_label(current["by"]) or "Current", "note": "Now in use", "cost": cost, **current},
        {"key": "other", "label": ai_label(alt["by"]), "note": "New, not used yet", "cost": other["cost_usd"], **alt}])


@router.post("/conversations/{cid}/compare/keep")
def compare_keep(cid: int, which: str = Form(...), conn: sqlite3.Connection = Conn):
    get_conversation(conn, cid)
    if which not in ("current", "other"):
        raise HTTPException(400, "Pick current or other")
    extract.keep_compared(conn, cid, which)
    return RedirectResponse(f"/conversations/{cid}", status_code=303)


@router.post("/conversations/{cid}/summary")
def edit_summary(request: Request, cid: int, title: str | None = Form(None), summary: str | None = Form(None),
                 takeaways: str | None = Form(None), conn: sqlite3.Connection = Conn):
    """Fix the title, summary or key points in place, one field at a time (key points: one per line).
    Fields not sent stay as they are. Re-run AI replaces summary and key points again."""
    c = get_conversation(conn, cid)
    if title is not None and not title.strip():
        raise HTTPException(400, "A title is needed")
    points = None if takeaways is None else [
        ln.strip().lstrip("-•* ").strip() for ln in takeaways.splitlines() if ln.strip().lstrip("-•* ").strip()]
    label = "Renamed the talk" if title is not None and summary is None and takeaways is None else "Edited the summary"
    with db.transaction(conn):
        undo.save(conn, label, [("conversation", cid)])
        conn.execute("UPDATE conversation SET title = ?, summary = ?, takeaways = ?, updated_at = ? WHERE id = ?",
                     (title.strip() if title is not None else c["title"],
                      (summary.strip() or None) if summary is not None else c["summary"],
                      json.dumps(points) if points is not None else c["takeaways"], db.now(), cid))
        if title is not None and title.strip() != c["title"]:   # Q5: your own name wins; a long one is shortened again by the AI
            conn.execute("UPDATE conversation SET short_title = ? WHERE id = ?", (title.strip() if len(title.strip()) <= 40 else None, cid))
        extract.reindex_ai_text(conn, cid)
    return back(request, f"/conversations/{cid}")


@router.post("/conversations/{cid}/type")
def set_type(request: Request, cid: int, type: str = Form(...), conn: sqlite3.Connection = Conn):
    """F6: the meeting type picks the summary template for the next AI run (nothing re-runs by itself)."""
    if type not in prompts.MEETING_TYPES:
        raise HTTPException(400, "unknown meeting type")
    get_conversation(conn, cid)
    conn.execute("UPDATE conversation SET type = ?, type_chosen = 1, updated_at = ? WHERE id = ?", (type, db.now(), cid))
    return back(request, f"/conversations/{cid}")


@router.post("/settings/templates/{kind}")
def save_template(request: Request, kind: str, text: str = Form(""), reset: str = Form(""), conn: sqlite3.Connection = Conn):
    if kind not in prompts.MEETING_TYPES:
        raise HTTPException(404)
    if reset or text.strip() == prompts.TEMPLATES[kind]:
        conn.execute("DELETE FROM setting WHERE key = ?", (f"template:{kind}",))
    else:
        set_setting(conn, f"template:{kind}", text.strip())
    return RedirectResponse("/settings/templates", status_code=303)


@router.post("/conversations/{cid}/area")
def set_area(request: Request, cid: int, area: str = Form(...), conn: sqlite3.Connection = Conn):
    if area not in ("work", "personal", "school", "other"):
        raise HTTPException(400, "area must be work or personal")
    get_conversation(conn, cid)
    with db.transaction(conn):
        conn.execute("UPDATE conversation SET area = ?, area_confidence = 1, updated_at = ? WHERE id = ?", (area, db.now(), cid))
        conn.execute("UPDATE task SET area = ? WHERE conversation_id = ?", (area, cid))
    return back(request, f"/conversations/{cid}")


@router.post("/conversations/{cid}/delete")
def trash_conversation(cid: int, conn: sqlite3.Connection = Conn):
    """Trash: the conversation and everything from it (tasks, notes, review items) are hidden until restored, or
    removed for good after TRASH_DAYS. Undo works like any other change."""
    get_conversation(conn, cid)   # 404 if missing
    with db.transaction(conn):
        undo.save(conn, "Moved to Trash", [("conversation", cid)])   # Brian, 2026-11-22: the title made it "Moved…"
        # F118: a second tap (double tap, or the other device) leaves the first time alone, so it changes nothing
        conn.execute("UPDATE conversation SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                     (db.now(), db.now(), cid))
    return RedirectResponse("/conversations", status_code=303)


def _talk_ids(ids: str) -> list[int]:
    return _id_list(ids)[:200]


@router.get("/talks-bulk/ai", response_class=HTMLResponse)
def talks_bulk_ai_confirm(request: Request, ids: str = "", conn: sqlite3.Connection = Conn):
    """Talks › Select › AI notes (Brian, 2026-11-22): one confirm with the total cost. Talks that already have AI notes
    are left out (no paying twice)."""
    todo, have = [], 0
    for cid in _talk_ids(ids):
        c = conn.execute("SELECT summary FROM conversation WHERE id = ? AND deleted_at IS NULL", (cid,)).fetchone()
        if c is None:
            continue
        if c["summary"]:
            have += 1
        else:
            todo.append(cid)
    ests = [extract.estimate_cost(conn, cid) for cid in todo]
    n = len(todo)
    return confirm_page(request, conn, "conversations", heading=f"Summarize {n} talk{'s' if n != 1 else ''}?",
                        cost=sum(e["cost_usd_est"] or 0 for e in ests), model=ests[0]["model"] if ests else None,
                        tokens=sum(e["tokens_in_est"] or 0 for e in ests) or None,
                        blocked=None if n else "all of these already have AI notes",
                        detail="Sends each transcript's text (never the audio)."
                               + (f" {have} already ha{'ve' if have != 1 else 's'} AI notes and {'are' if have != 1 else 'is'} left out." if have else ""),
                        action="/talks-bulk", cancel="/conversations", fields={"ids": ",".join(map(str, todo)), "what": "ai"})


@router.post("/talks-bulk")
def talks_bulk(request: Request, ids: str = Form(...), what: str = Form(...), confirm: str = Form(""),
               conn: sqlite3.Connection = Conn):
    """Talks › Select › Delete or AI notes for several at once. Delete is one Undo; AI notes only from the confirm step."""
    cids = [cid for cid in _talk_ids(ids)
            if conn.execute("SELECT 1 FROM conversation WHERE id = ? AND deleted_at IS NULL", (cid,)).fetchone()]
    if what == "trash":
        with db.transaction(conn):
            undo.save(conn, f"Moved {len(cids)} talk{'s' if len(cids) != 1 else ''} to Trash", [("conversation", c) for c in cids])
            for cid in cids:
                conn.execute("UPDATE conversation SET deleted_at = ?, updated_at = ? WHERE id = ?", (db.now(), db.now(), cid))
        return RedirectResponse("/conversations", status_code=303)
    if what == "merge":
        if stop := demo_stop(request, "audio", "/conversations"):
            return stop
        try:
            new = merge_svc.merge_talks(conn, request.app.state.cfg, cids)
        except merge_svc.MergeError as e:
            raise HTTPException(400, str(e))
        return RedirectResponse(f"/conversations/{new}?merged={len(cids)}", status_code=303)
    if what == "ai":
        if not confirmed(confirm):
            return RedirectResponse(f"/talks-bulk/ai?ids={','.join(map(str, cids))}", status_code=303)
        if stop := demo_stop(request, "ai", "/conversations"):
            return stop
        for cid in cids:
            ai_jobs.queue_rerun(conn, cid, approved=True)
        return RedirectResponse("/conversations?note=summarizing", status_code=303)
    raise HTTPException(400, "Pick Summarize, Merge or Delete")


@router.post("/conversations/{cid}/restore")
def restore_conversation(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    """Trash › Restore. Undo puts it back in Trash (2026-11-25)."""
    c = get_conversation(conn, cid)
    if c["deleted_at"]:
        with db.transaction(conn):
            undo.save(conn, "Restored", [("conversation", cid)])
            conn.execute("UPDATE conversation SET deleted_at = NULL, updated_at = ? WHERE id = ?", (db.now(), cid))
    return back(request, f"/conversations/{cid}")


def audio_file(request: Request, path: str | None) -> FileResponse:
    """Only files inside the data folder, and only ones the database points to (no paths from the browser)."""
    if not path:
        raise HTTPException(404, "No audio")
    root = request.app.state.cfg.data_dir.resolve()
    p = Path(path).expanduser().resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise HTTPException(404, "No audio")
    # Kept in this device's cache for a day (Brian, 2026-11-21: audio was slow to start; "no-store" made the phone fetch
    # the start of the file again on every visit). "private" = only Brian's own browser keeps it, never a shared cache.
    return FileResponse(p, media_type=AUDIO_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=86400"})


def playable_audio(conn: sqlite3.Connection, cid: int) -> str | None:
    """The file a talk plays: the original while it's kept (30 days, D8), then the archive copy. Only formats browsers
    can play, and only a file that's there (R5-15: the player showed for a talk whose audio was gone, and ▶ and every
    timestamp did nothing). The talk page and the audio route both ask this, so they always agree."""
    row = conn.execute("SELECT r.vault_path, r.archive_path FROM conversation c JOIN recording r ON r.id = c.recording_id "
                       "WHERE c.id = ?", (cid,)).fetchone()
    return next((p for p in (row or []) if p and Path(p).suffix.lower() in AUDIO_TYPES and Path(p).is_file()), None)


@router.get("/audio/conversation/{cid}")
def conversation_audio(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    return audio_file(request, playable_audio(conn, cid))


@router.get("/audio/speaker/{sid}")
def speaker_clip(request: Request, sid: int, conn: sqlite3.Connection = Conn):
    row = conn.execute("SELECT clip_path FROM speaker WHERE id = ?", (sid,)).fetchone()
    return audio_file(request, row[0] if row else None)


@router.post("/speakers/{sid}/name")
def name_speaker(request: Request, sid: int, person_id: OptId = None, new_name: str = Form(""), special: str = Form(""),
                 conn: sqlite3.Connection = Conn):
    """Name a speaker once; the M2 pipeline then learns their voiceprint so they're recognised next time.
    special: "not" = "Not <name>" (back to its own Speaker N), "idk" = "Don't know" (stop asking) (Brian, 2026-11-22)."""
    if not conn.execute("SELECT 1 FROM speaker WHERE id = ?", (sid,)).fetchone():
        raise HTTPException(404, "That speaker isn't here any more")
    if special == "not" and not new_name.strip():
        return unassign_speaker(request, sid, conn)
    if special == "idk" and not new_name.strip():
        return speaker_unknown(request, sid, conn)
    if special == "idk_rest" and not new_name.strip():   # B11: "Don't know: this one and the rest"
        cid = conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (sid,)).fetchone()[0]
        return speakers_unknown_rest(request, cid, conn)
    with db.transaction(conn):
        person_id = person_from_form(conn, person_id, new_name)
        conn.execute("UPDATE speaker SET person_id = ?, confirmed = 1, unknown = 0 WHERE id = ?", (person_id, sid))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'speaker' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), sid))
    identify.enroll(conn, sid, person_id, cfg=request.app.state.cfg)  # adds their voice to the voiceprint
    cid = conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (sid,)).fetchone()[0]
    merge_same_person(conn, cid, sid, person_id)
    extract.relabel_speakers(conn, cid)
    return back(request, "/review")


def merge_same_person(conn: sqlite3.Connection, cid: int, sid: int, person_id: int | None) -> int | None:
    """Naming a speaker as someone who already has a speaker in this conversation folds this one into theirs: one
    chip, one color, combined talk time (Brian, 2026-11-20: two "Alex" chips in different colors). Undo splits them."""
    if not person_id:
        return None
    other = conn.execute("SELECT id FROM speaker WHERE conversation_id = ? AND person_id = ? AND id != ? ORDER BY "
                         "(SELECT min(idx) FROM segment g WHERE g.speaker_id = speaker.id) IS NULL, "
                         "(SELECT min(idx) FROM segment g WHERE g.speaker_id = speaker.id) LIMIT 1",
                         (cid, person_id, sid)).fetchone()
    if other is None:
        return None
    into = other[0]
    sp = conn.execute("SELECT talk_seconds FROM speaker WHERE id = ?", (sid,)).fetchone()
    with db.transaction(conn):
        undo.save(conn, "Merged two speakers named the same",
                  [("speaker", sid), ("speaker", into),
                   *(("segment", r[0]) for r in conn.execute("SELECT id FROM segment WHERE speaker_id = ?", (sid,)))])
        conn.execute("UPDATE segment SET speaker_id = ? WHERE speaker_id = ?", (into, sid))
        conn.execute("UPDATE speaker SET talk_seconds = coalesce(talk_seconds, 0) + ? WHERE id = ?",
                     (sp["talk_seconds"] or 0, into))
        conn.execute("UPDATE speaker SET talk_seconds = 0 WHERE id = ?", (sid,))
    return into


@router.post("/speakers/{sid}/unassign")
def unassign_speaker(request: Request, sid: int, conn: sqlite3.Connection = Conn):
    """"This isn't X": the speaker goes back to unnamed (and back into Review) so Brian can name them again. Undo (10
    minutes, 2026-11-25) puts the name back everywhere: the line names, the AI notes, tasks, notes and the voiceprint."""
    sp = conn.execute("SELECT * FROM speaker WHERE id = ?", (sid,)).fetchone()
    if sp is None:
        raise HTTPException(404, "That speaker isn't here any more")
    cid, pid = sp["conversation_id"], sp["person_id"]
    with db.transaction(conn):
        uid = undo.save(conn, "Name taken off", [
            ("speaker", sid), ("conversation", cid),
            *(("task", r[0]) for r in conn.execute("SELECT id FROM task WHERE conversation_id = ? AND owner_person_id = ?", (cid, pid))),
            *(("person_fact", r[0]) for r in conn.execute("SELECT id FROM person_fact WHERE conversation_id = ? AND person_id = ?",
                                                          (cid, pid)))])
        stand_in = conn.execute("SELECT id FROM person WHERE stands_for_speaker = ?", (sid,)).fetchone()
        if pid and sp["confirmed"]:
            undo.voiceprint(conn, uid, pid)
        if sp["person_id"] and sp["confirmed"]:   # take this voice back out of their voiceprint, so it stops matching
            identify.unenroll(conn, sid, sp["person_id"], cfg=request.app.state.cfg)
        conn.execute("UPDATE speaker SET person_id = NULL, confirmed = 0, match_score = NULL, unknown = 0 WHERE id = ?", (sid,))
        if sp["person_id"]:   # their name leaves this talk's summary, tasks and notes too (Brian, 2026-11-23)
            extract.unname_in_text(conn, sid, sp["person_id"])
        if not stand_in and (made := conn.execute("SELECT id FROM person WHERE stands_for_speaker = ?", (sid,)).fetchone()):
            undo.created(conn, uid, "person", made[0])   # the "Speaker N" stand-in their tasks moved to
        if not conn.execute("SELECT 1 FROM review_item WHERE kind = 'speaker' AND ref_id = ? AND status = 'open'",
                            (sid,)).fetchone():
            undo.created(conn, uid, "review_item", conn.execute(
                "INSERT INTO review_item (kind, conversation_id, ref_id, payload, created_at) VALUES ('speaker', ?, ?, ?, ?)",
                (cid, sid, json.dumps({"clip": sp["clip_path"]}), db.now())).lastrowid)
    return back(request, f"/conversations/{cid}")


def _close_speaker_reviews(conn: sqlite3.Connection, sids: list[int]) -> None:
    for sid in sids:
        conn.execute("UPDATE review_item SET status = 'dismissed', resolved_at = ? WHERE kind = 'speaker' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), sid))


@router.post("/speakers/{sid}/unknown")
def speaker_unknown(request: Request, sid: int, conn: sqlite3.Connection = Conn):
    """"Don't know" (Brian, 2026-11-22): stays "Speaker N", no longer asked about anywhere. Naming them later still works."""
    sp = conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (sid,)).fetchone()
    if sp is None:
        raise HTTPException(404, "That speaker isn't here any more")
    with db.transaction(conn):
        undo.save(conn, "Marked as don't know", [("speaker", sid), *(("review_item", r[0]) for r in conn.execute(
            "SELECT id FROM review_item WHERE kind = 'speaker' AND ref_id = ? AND status = 'open'", (sid,)))])
        conn.execute("UPDATE speaker SET unknown = 1 WHERE id = ? AND person_id IS NULL", (sid,))
        _close_speaker_reviews(conn, [sid])
    return back(request, f"/conversations/{sp['conversation_id']}")


@router.post("/conversations/{cid}/speakers-unknown")
def speakers_unknown_rest(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    """"Don't know the rest" for big group talks: every speaker still unnamed in this talk stops being asked about."""
    sids = [r[0] for r in conn.execute("SELECT id FROM speaker WHERE conversation_id = ? AND person_id IS NULL AND unknown = 0", (cid,))]
    if sids:
        with db.transaction(conn):
            undo.save(conn, f"{len(sids)} marked as don't know", [*(("speaker", i) for i in sids), *(("review_item", r[0]) for r in conn.execute(
                f"SELECT id FROM review_item WHERE kind = 'speaker' AND status = 'open' AND ref_id IN ({','.join('?' * len(sids))})", sids))])
            conn.execute(f"UPDATE speaker SET unknown = 1 WHERE id IN ({','.join('?' * len(sids))})", sids)
            _close_speaker_reviews(conn, sids)
    return back(request, f"/conversations/{cid}")


SHORT_SPEAKER_SECONDS = 30   # less talk than this: probably a split-off piece of another speaker (F7, 2026-10-21)


@router.post("/speakers/{sid}/fold")
def fold_speaker(request: Request, sid: int, into: int = Form(...), conn: sqlite3.Connection = Conn):
    """"Part of someone else": all of this speaker's lines (and talk time) move to another speaker in the same
    conversation. Voiceprints are untouched (a short piece is too little audio to learn from). Undo restores it."""
    sp = conn.execute("SELECT * FROM speaker WHERE id = ?", (sid,)).fetchone()
    if sp is None or into == sid or not conn.execute("SELECT 1 FROM speaker WHERE id = ? AND conversation_id = ?",
                                                     (into, sp["conversation_id"])).fetchone():
        raise HTTPException(400, "Pick another speaker from this talk")
    cid = sp["conversation_id"]
    with db.transaction(conn):
        undo.save(conn, "Moved a short speaker's lines",
                  [("speaker", sid), ("speaker", into),
                   *(("segment", r[0]) for r in conn.execute("SELECT id FROM segment WHERE speaker_id = ?", (sid,))),
                   *(("review_item", r[0]) for r in conn.execute(
                       "SELECT id FROM review_item WHERE kind = 'speaker' AND ref_id = ? AND status = 'open'", (sid,)))])
        conn.execute("UPDATE segment SET speaker_id = ? WHERE speaker_id = ?", (into, sid))
        conn.execute("UPDATE speaker SET talk_seconds = talk_seconds + ? WHERE id = ?", (sp["talk_seconds"] or 0, into))
        conn.execute("UPDATE speaker SET talk_seconds = 0 WHERE id = ?", (sid,))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'speaker' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), sid))
    return back(request, f"/conversations/{cid}")


@router.get("/segments/{seg_id}/edit", response_class=HTMLResponse)
def line_edit_panel(request: Request, seg_id: int, conn: sqlite3.Connection = Conn):
    """A line's ⋯ panel, loaded when first opened (keeps long transcripts light)."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "That line isn't here any more")
    # a bare fragment (U12): none of the page frame's counts are needed for one panel
    return templates.TemplateResponse(request, "_line_edit.html", {"s": seg, "speakers": speakers_of(conn, seg["conversation_id"])})


@router.post("/segments/{seg_id}/speaker")
def line_speaker(request: Request, seg_id: int, speaker_id: OptId = None, new_name: str = Form(""),
                 ids: str = Form(""), conn: sqlite3.Connection = Conn):
    """Fix who said one line, or a block of lines in a row (`ids`, Brian 2026-11-20: tap the name, pick who). Only
    those lines move (and their talk time); voiceprints are untouched: a few lines are too little audio."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "That line isn't here any more")
    cid = seg["conversation_id"]
    more = _ids(ids)   # F83: a line listed twice ("5,5") moved its talk time twice
    segs = [seg] + [r for r in (conn.execute("SELECT * FROM segment WHERE id = ? AND conversation_id = ?", (x, cid)).fetchone()
                                for x in more if x != seg_id) if r]
    before, lo, hi = _window(conn, segs)
    with db.transaction(conn):
        n = len(segs)
        uid = undo.save(conn, f"Changed who said line {seg['idx'] + 1}" if n == 1 else f"Changed who said {n} lines",
                        [*(("segment", g["id"]) for g in segs), *(("speaker", r[0]) for r in conn.execute(
                            "SELECT id FROM speaker WHERE conversation_id = ?", (cid,)))])
        speaker_id = _speaker_from_form(conn, cid, uid, speaker_id, new_name)
        for g in segs:
            if speaker_id != g["speaker_id"]:
                secs = max(0.0, g["end_s"] - g["start_s"])
                conn.execute("UPDATE speaker SET talk_seconds = max(0, talk_seconds - ?) WHERE id = ?", (secs, g["speaker_id"]))
                conn.execute("UPDATE speaker SET talk_seconds = talk_seconds + ? WHERE id = ?", (secs, speaker_id))
                conn.execute("UPDATE segment SET speaker_id = ? WHERE id = ?", (speaker_id, g["id"]))
    if _fast(request):
        return _fix_reply(conn, cid, before, lo, hi, uid)
    return back(request, f"/conversations/{cid}?tx=1#L{seg['idx']}")


def _fast(request: Request) -> bool:
    """Fixes made by tapping send X-Fast: 1 and get back only the lines that changed (Brian, 2026-11-20: every fix
    reloaded the whole talk page, about 1 KB per line, and the phone rebuilt every word: far too slow)."""
    return request.headers.get("x-fast") == "1"


def _window(conn: sqlite3.Connection, segs: list) -> tuple[set[int], float, float]:
    """The lines a fix can touch (these lines and one either side) and their time span, read before the fix."""
    idxs = [g["idx"] for g in segs]
    cid = segs[0]["conversation_id"]
    rows = conn.execute("SELECT id, start_s, end_s FROM segment WHERE conversation_id = ? AND idx BETWEEN ? AND ?",
                        (cid, min(idxs) - 1, max(idxs) + 1)).fetchall()
    return {r["id"] for r in rows}, min(r["start_s"] for r in rows), max(r["end_s"] for r in rows)


def _fix_reply(conn: sqlite3.Connection, cid: int, before: set[int], lo: float, hi: float, uid: int) -> JSONResponse:
    rows = conn.execute("SELECT id, idx, start_s, end_s, speaker_id, text FROM segment WHERE conversation_id = ? "
                        "AND start_s BETWEEN ? AND ? ORDER BY idx", (cid, lo, hi)).fetchall()
    if uid:   # F75: a fix that changed nothing (a second tap) offers no Undo of its own
        undo.drop_noops(conn, [uid])
    u = conn.execute("SELECT id, label FROM undo_action WHERE id = ?", (uid,)).fetchone() if uid else None
    return JSONResponse({"lines": [dict(r) for r in rows], "removed": sorted(before - {r["id"] for r in rows}),
                         "speakers": [[x["id"], x["name"], x["color"]] for x in speakers_of(conn, cid)],
                         "undo": {"id": u["id"], "label": u["label"]} if u else None})


def _word_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """A selection grown to whole words (a half-selected word counts as selected)."""
    start, end = max(0, min(start, len(text))), max(0, min(end, len(text)))
    if start > end:
        start, end = end, start
    caret = start == end
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    if caret:
        return start, start
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


REF_TABLES = undo.REF_TABLES   # rows that point at a transcript line (source_segment_id)


def _other_speaker(conn: sqlite3.Connection, cid: int, current: int | None) -> int | None:
    """In a two-person conversation, the other person (Brian, 2026-11-20: a fix in a 1:1 needs no picking)."""
    ids = [r[0] for r in conn.execute("SELECT DISTINCT speaker_id FROM segment WHERE conversation_id = ? "
                                      "AND speaker_id IS NOT NULL", (cid,))]
    others = [x for x in ids if x != current]
    return others[0] if len(ids) == 2 and len(others) == 1 else None


def _speaker_from_form(conn: sqlite3.Connection, cid: int, uid: int, speaker_id: int | None, new_name: str) -> int:
    if new_name.strip():
        pid = people_svc.find_or_create(conn, new_name)
        row = conn.execute("SELECT id FROM speaker WHERE conversation_id = ? AND person_id = ? ORDER BY id LIMIT 1",
                           (cid, pid)).fetchone()
        if row:
            return row["id"]
        sid = conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id, confirmed, talk_seconds) "
                           "VALUES (?, (SELECT coalesce(max(cluster), -1) + 1 FROM speaker WHERE conversation_id = ?), ?, 1, 0)",
                           (cid, cid, pid)).lastrowid
        undo.created(conn, uid, "speaker", sid)
        return sid
    if speaker_id and conn.execute("SELECT 1 FROM speaker WHERE id = ? AND conversation_id = ?", (speaker_id, cid)).fetchone():
        return speaker_id
    raise HTTPException(400, "Pick a speaker or type a name")


def _renumber(conn: sqlite3.Connection, cid: int) -> None:
    """Lines numbered 0, 1, 2... again in time order, after lines were added or joined."""
    ids = [r[0] for r in conn.execute("SELECT id FROM segment WHERE conversation_id = ? ORDER BY idx, start_s, id", (cid,))]
    conn.execute("UPDATE segment SET idx = -id WHERE conversation_id = ?", (cid,))
    conn.executemany("UPDATE segment SET idx = ? WHERE id = ?", list(enumerate(ids)))


def _merge_runs(conn: sqlite3.Connection, cid: int, lo: int, hi: int, any_line: bool = False) -> None:
    """Pieces of one original line that end up said by the same person, next to each other, become one line again
    (Brian, 2026-11-20: no "Brian: How / Brian: are you?" after a fix). Lines that were separate from the start stay
    separate (joining them made a second tap move too much); `any_line` (Join with line above) joins any two.
    Tasks, decisions and notes that pointed at a joined line point at the line it joined."""
    rows = conn.execute("SELECT * FROM segment WHERE conversation_id = ? AND idx BETWEEN ? AND ? ORDER BY idx", (cid, lo, hi)).fetchall()
    keep = None
    for r in rows:
        if keep is not None and r["speaker_id"] == keep["speaker_id"] and (any_line or
                (r["split_of"] or r["id"]) == (keep["split_of"] or keep["id"])):
            text = f"{keep['text']} {r['text']}".strip()
            conn.execute("UPDATE segment SET text = ?, end_s = max(end_s, ?) WHERE id = ?", (text, r["end_s"], keep["id"]))
            for t in REF_TABLES:
                conn.execute(f"UPDATE {t} SET source_segment_id = ? WHERE source_segment_id = ?", (keep["id"], r["id"]))
            conn.execute("DELETE FROM segment WHERE id = ?", (r["id"],))
            unindex(conn, "segment", r["id"])
            index(conn, "segment", keep["id"], cid, text)
            keep = conn.execute("SELECT * FROM segment WHERE id = ?", (keep["id"],)).fetchone()
        else:
            keep = r


def _reassign(conn: sqlite3.Connection, uid: int, seg: sqlite3.Row, a: int, b: int, new_sp: int) -> int:
    """Words a..b (by word number) of one line go to `new_sp`: the line splits into before / those words / after,
    times split by share of characters, talk time follows; then same-speaker neighbours join up. Returns the
    line number to show afterwards."""
    cid, words = seg["conversation_id"], seg["text"].split()
    a, b = max(0, min(a, len(words))), max(0, min(b, len(words)))
    pieces = [(" ".join(words[:a]), seg["speaker_id"]), (" ".join(words[a:b]), new_sp), (" ".join(words[b:]), seg["speaker_id"])]
    pieces = [(t, w) for t, w in pieces if t]
    total = sum(len(t) for t, _ in pieces) or 1
    dur, t0 = max(0.0, seg["end_s"] - seg["start_s"]), seg["start_s"]
    conn.execute("UPDATE segment SET idx = idx + ? WHERE conversation_id = ? AND idx > ?", (10_000_000, cid, seg["idx"]))
    for i, (t, who) in enumerate(pieces):
        t1 = seg["end_s"] if i == len(pieces) - 1 else round(t0 + dur * len(t) / total, 2)
        if i == 0:
            conn.execute("UPDATE segment SET text = ?, end_s = ?, speaker_id = ? WHERE id = ?", (t, t1, who, seg["id"]))
            index(conn, "segment", seg["id"], cid, t)
        else:
            rid = conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text, confidence, split_of) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (cid, seg["idx"] + i, t0, t1, who, t, seg["confidence"],
                                                                   seg["split_of"] or seg["id"])).lastrowid
            index(conn, "segment", rid, cid, t)
        if who != seg["speaker_id"]:
            conn.execute("UPDATE speaker SET talk_seconds = max(0, coalesce(talk_seconds, 0) - ?) WHERE id = ?", (t1 - t0, seg["speaker_id"]))
            conn.execute("UPDATE speaker SET talk_seconds = coalesce(talk_seconds, 0) + ? WHERE id = ?", (t1 - t0, who))
        t0 = t1
    _renumber(conn, cid)
    _merge_runs(conn, cid, seg["idx"] - 1, seg["idx"] + len(pieces))
    _renumber(conn, cid)
    t_moved = seg["start_s"] + dur * len(" ".join(words[:a])) / total   # where the moved words start
    at = conn.execute("SELECT idx FROM segment WHERE conversation_id = ? AND start_s <= ? ORDER BY start_s DESC, idx DESC LIMIT 1",
                      (cid, t_moved + 0.001)).fetchone()
    return at["idx"] if at else seg["idx"]


@router.post("/segments/{seg_id}/flip")
def line_flip(request: Request, seg_id: int, at: int = Form(...), end: int | None = Form(None),
              speaker_id: OptId = None, new_name: str = Form(""), conn: sqlite3.Connection = Conn):
    """Fix mode (Brian, 2026-11-20, replaces long-press selecting on the phone): tap the first word someone else
    said. Words `at` to the end of the line (or to `end`) go to that speaker: in a 1:1 the other person, otherwise
    the one picked. Same-speaker neighbours join up. No AI; Undo restores the whole transcript."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "That line isn't here any more")
    cid = seg["conversation_id"]
    before, lo, hi = _window(conn, [seg])
    with db.transaction(conn):
        uid = undo.save(conn, f"Fixed who said line {seg['idx'] + 1}",
                        [("speaker", r[0]) for r in conn.execute("SELECT id FROM speaker WHERE conversation_id = ?", (cid,))])
        undo.transcript(conn, uid, cid)
        if speaker_id or new_name.strip():
            sp = _speaker_from_form(conn, cid, uid, speaker_id, new_name)
        else:
            sp = _other_speaker(conn, cid, seg["speaker_id"])
            if sp is None:
                raise HTTPException(400, "Pick who said it")
        n = len(seg["text"].split())
        idx = _reassign(conn, uid, seg, at, n if end is None else end, sp)
    if _fast(request):
        return _fix_reply(conn, cid, before, lo, hi, uid)
    return RedirectResponse(f"/conversations/{cid}?tx=1#L{idx}", status_code=303)


@router.post("/segments/{seg_id}/split")
def line_split(request: Request, seg_id: int, start: int = Form(...), end: int = Form(...),
               speaker_id: OptId = None, new_name: str = Form(""), conn: sqlite3.Connection = Conn):
    """With a mouse (speaker fix A, 2026-11-20): select words, then "Split here" (they become their own line) or
    "Who said this?" (they also move to a speaker). Same-speaker neighbours join up afterwards."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "That line isn't here any more")
    cid, text = seg["conversation_id"], seg["text"]
    a, b = _word_bounds(text, start, end)
    wa, wb = len(text[:a].split()), len(text[:b].split())
    wants = bool(speaker_id) or bool(new_name.strip())
    if a == b:
        wb = len(text.split())
    if not wants and (wa == 0 and wb == len(text.split())):
        return RedirectResponse(f"/conversations/{cid}?tx=1#L{seg['idx']}", status_code=303)
    before, lo, hi = _window(conn, [seg])
    with db.transaction(conn):
        uid = undo.save(conn, f"Split line {seg['idx'] + 1}",
                        [("speaker", r[0]) for r in conn.execute("SELECT id FROM speaker WHERE conversation_id = ?", (cid,))])
        undo.transcript(conn, uid, cid)
        sp = _speaker_from_form(conn, cid, uid, speaker_id, new_name) if wants else seg["speaker_id"]
        if wants:
            idx = _reassign(conn, uid, seg, wa, wb, sp)
        else:   # a plain split keeps the speaker, so no joining up afterwards
            words = text.split()
            pieces = [p for p in (" ".join(words[:wa]), " ".join(words[wa:wb]), " ".join(words[wb:])) if p]
            total = sum(map(len, pieces)) or 1
            dur, t0 = max(0.0, seg["end_s"] - seg["start_s"]), seg["start_s"]
            conn.execute("UPDATE segment SET idx = idx + ? WHERE conversation_id = ? AND idx > ?", (10_000_000, cid, seg["idx"]))
            for i, t in enumerate(pieces):
                t1 = seg["end_s"] if i == len(pieces) - 1 else round(t0 + dur * len(t) / total, 2)
                if i == 0:
                    conn.execute("UPDATE segment SET text = ?, end_s = ? WHERE id = ?", (t, t1, seg_id))
                    index(conn, "segment", seg_id, cid, t)
                else:
                    rid = conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text, confidence, split_of) "
                                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (cid, seg["idx"] + i, t0, t1, seg["speaker_id"], t,
                                                                           seg["confidence"], seg["split_of"] or seg["id"])).lastrowid
                    index(conn, "segment", rid, cid, t)
                t0 = t1
            _renumber(conn, cid)
            idx = seg["idx"] + (1 if wa > 0 else 0)
    if _fast(request):
        return _fix_reply(conn, cid, before, lo, hi, uid)
    return RedirectResponse(f"/conversations/{cid}?tx=1#L{idx}", status_code=303)


@router.post("/segments/{seg_id}/join")
def line_join(request: Request, seg_id: int, conn: sqlite3.Connection = Conn):
    """"Join with line above": this line's words go onto the end of the line before it (that line's speaker keeps
    them, talk time follows). Undo puts the line back."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "That line isn't here any more")
    cid = seg["conversation_id"]
    prev = conn.execute("SELECT * FROM segment WHERE conversation_id = ? AND idx < ? ORDER BY idx DESC LIMIT 1",
                        (cid, seg["idx"])).fetchone()
    if prev is None:
        raise HTTPException(400, "The first line has no line above it")
    with db.transaction(conn):
        uid = undo.save(conn, f"Joined line {seg['idx'] + 1} to the line above",
                        [("speaker", r[0]) for r in conn.execute("SELECT id FROM speaker WHERE conversation_id = ?", (cid,))])
        undo.transcript(conn, uid, cid)
        secs = max(0.0, seg["end_s"] - seg["start_s"])
        if prev["speaker_id"] != seg["speaker_id"]:
            conn.execute("UPDATE speaker SET talk_seconds = max(0, coalesce(talk_seconds, 0) - ?) WHERE id = ?",
                         (secs, seg["speaker_id"]))
            conn.execute("UPDATE speaker SET talk_seconds = coalesce(talk_seconds, 0) + ? WHERE id = ?",
                         (secs, prev["speaker_id"]))
            conn.execute("UPDATE segment SET speaker_id = ? WHERE id = ?", (prev["speaker_id"], seg_id))
        _merge_runs(conn, cid, prev["idx"], seg["idx"], any_line=True)
        _renumber(conn, cid)
    return RedirectResponse(f"/conversations/{cid}?tx=1#L{prev['idx']}", status_code=303)


@router.post("/segments/{seg_id}/text")
def line_text(request: Request, seg_id: int, text: str = Form(...), heard: str = Form(""), correct: str = Form(""),
              conn: sqlite3.Connection = Conn):
    """Fix the words of one line (search follows). Optionally teach the glossary a mis-hearing for future AI runs."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "That line isn't here any more")
    if not text.strip():
        raise HTTPException(400, "A line can't be empty")
    before, lo, hi = _window(conn, [seg])
    with db.transaction(conn):
        uid = undo.save(conn, f"Edited line {seg['idx'] + 1}", [("segment", seg_id)])
        conn.execute("UPDATE segment SET text = ? WHERE id = ?", (text.strip(), seg_id))
        index(conn, "segment", seg_id, seg["conversation_id"], text.strip())
        if heard.strip() and correct.strip():
            add_glossary_variant(conn, correct.strip(), heard.strip())
    if _fast(request):   # Brian, 2026-11-20: a word fix reloaded the whole talk page and timed out on the phone
        return _fix_reply(conn, seg["conversation_id"], before, lo, hi, uid)
    return back(request, f"/conversations/{seg['conversation_id']}?tx=1#L{seg['idx']}")


def add_glossary_variant(conn: sqlite3.Connection, term: str, heard: str) -> None:
    """Add one mis-hearing to a term, keeping the ones it already has."""
    row = conn.execute("SELECT variants FROM glossary_term WHERE term = ?", (term,)).fetchone()
    variants = json.loads(row["variants"]) if row else []
    if heard.lower() not in (v.lower() for v in variants) and heard.lower() != term.lower():
        variants.append(heard)
    conn.execute("INSERT INTO glossary_term (term, variants, approved, created_at) VALUES (?, ?, 1, ?) "
                 "ON CONFLICT (term) DO UPDATE SET variants = excluded.variants, approved = 1", (term, json.dumps(variants), db.now()))


def person_from_form(conn: sqlite3.Connection, person_id: int | None, new_name: str) -> int:
    """A "Change person" / "Who is this?" form: a typed name wins (matched to an existing person, else created)."""
    if new_name.strip():
        return people_svc.find_or_create(conn, new_name)
    if not person_id or not conn.execute("SELECT 1 FROM person WHERE id = ?", (person_id,)).fetchone():
        raise HTTPException(400, "Pick a person or type a name")
    return person_id


# ---------- Undo bar ----------

@router.post("/undo/{uid}")
def undo_action(request: Request, uid: int, conn: sqlite3.Connection = Conn):
    try:
        done = undo.undo(conn, uid, cfg=request.app.state.cfg)
    except undo.Blocked as e:
        raise HTTPException(409, str(e))
    if not done:
        raise HTTPException(410, "Too late to undo that (or it was already undone)")
    return back(request)


# ---------- Tasks ----------

TABS = {"mine": "t.owner_person_id = 1 AND t.inbox = 0", "waiting": "t.owner_person_id != 1 AND t.inbox = 0",
        "inbox": "t.inbox = 1"}




def task_counts(request: Request, conn: sqlite3.Connection) -> dict[str, int]:
    """Open tasks per tab, for the Work/Personal toggle in effect (the tab numbers must match the lists)."""
    a, args = area_clause(area_of(request), "t.area")
    return {k: conn.execute(f"SELECT count(*) FROM task t WHERE {v} AND t.status = 'open' AND {LIVE_TASK}{a}", args).fetchone()[0]
            for k, v in TABS.items()}


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, tab: str = "mine", person: int | None = None, show: str = "open", n: int = TASKS_PER_PAGE,
               sort: str = "due", conn: sqlite3.Connection = Conn):
    if tab == "decisions":
        counts = task_counts(request, conn)
        decisions, total = decision_rows(request, conn, n)
        return render(request, "tasks.html", "tasks", conn, tab=tab, counts=counts, decisions=decisions, total=total)
    tab = tab if tab in TABS else "mine"
    a, args = area_clause(area_of(request), "t.area")
    where = ("t.inbox = 0" if show == "old" else TABS[tab]) + f" AND {LIVE_TASK}" + a   # Old: everyone's flagged tasks in one list
    if person:
        where += " AND (t.owner_person_id = ? OR EXISTS (SELECT 1 FROM speaker s WHERE s.conversation_id = t.conversation_id "
        where += "AND s.person_id = ?))"
        args += [person, person]
    q = like_q(request.query_params.get("q"))
    if q:   # N9: the search box above the list
        where += " AND (t.title LIKE ? ESCAPE '\\' OR coalesce(t.short_title, '') LIKE ? ESCAPE '\\')"
        args += [q, q]
    archived = request.query_params.get("archived") == "1"
    base, base_args = where, list(args)   # this tab, area and person, before the Open / Done / Old choice
    if show == "old":
        where += f" AND {cleanup.STALE}"
    elif show in ("done", "closed", "not_needed"):   # Closed view: All / Done / Not needed (N1)
        where += " AND t.status = 'done'" + ("" if archived else " AND t.archived_at IS NULL")
        where += {"done": " AND t.closed_as IS NULL", "not_needed": " AND t.closed_as = 'not_needed'"}.get(show, "")
    else:
        where += " AND t.status = 'open'"
    rows, total = paged(conn,
        "SELECT t.*, p.name AS owner_name, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) AS line, "
        "(SELECT min(tg.name COLLATE NOCASE) FROM conversation_tag ct JOIN tag tg ON tg.id = ct.tag_id WHERE ct.conversation_id = t.conversation_id) AS first_tag "
        "FROM task t JOIN person p ON p.id = t.owner_person_id LEFT JOIN conversation c ON c.id = t.conversation_id "
        f"WHERE {where} ORDER BY " + ("t.updated_at DESC" if show in ("done", "closed", "not_needed") else "t.created_at DESC" if sort == "added"
                                    # by talk / by tag (Brian picked A, 2026-11-22 night): newest talk first, then due date
                                    else "t.conversation_id IS NULL, coalesce(c.started_at, c.created_at) DESC, t.conversation_id, t.due IS NULL, t.due" if sort == "talk"
                                    else "first_tag IS NULL, first_tag COLLATE NOCASE, t.due IS NULL, t.due" if sort == "tag"
                                    else "t.due IS NULL, t.due, t.created_at DESC"),   # Sort (Brian, 2026-11-22)
        args, n, TASKS_PER_PAGE)
    counts = task_counts(request, conn)
    n_archived = conn.execute(f"SELECT count(*) FROM task t WHERE {base} AND t.status = 'done' AND t.archived_at IS NOT NULL",
                              base_args).fetchone()[0] if show in ("done", "closed", "not_needed") and not archived else 0
    return render(request, "tasks.html", "tasks", conn, rows=rows, tab=tab, counts=counts, people=people(conn), person=person, show=show,
                  n_archived=n_archived, total=total, sort=sort if sort in ("added", "talk", "tag") else "due",
                  q=(request.query_params.get("q") or "").strip()[:100])


@router.post("/tasks")
def add_task(request: Request, title: str = Form(...), owner: str = Form(""), due: str = Form(""), area: str = Form(""),
             conn: sqlite3.Connection = Conn):
    if not title.strip():
        raise HTTPException(400, "Title needed")
    if not owner.isdigit() or not conn.execute("SELECT 1 FROM person WHERE id = ?", (int(owner),)).fetchone():
        raise HTTPException(400, "Pick who it's for")
    owner = int(owner)
    t = db.now()
    with db.transaction(conn):
        tid = conn.execute("INSERT INTO task (title, owner_person_id, due, area, confidence, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, 1, ?, ?)",
                           (title.strip(), owner, due or None, area if area in ("work", "personal", "school", "other") else None, t, t)).lastrowid
        index(conn, "task", tid, None, title.strip())
    return back(request, "/tasks")


def task_or_404(conn: sqlite3.Connection, tid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM task WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise HTTPException(404, "That task isn't here any more")
    return row


@router.post("/tasks/{tid}/done")
def task_done(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    task_or_404(conn, tid)
    with db.transaction(conn):
        undo.save(conn, "Marked done", [("task", tid)])
        conn.execute("UPDATE task SET status = 'done', closed_as = NULL, closed_note = NULL, rated = coalesce(rated, 'right'), "
                     "updated_at = ? WHERE id = ?", (db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/not-needed")
def task_not_needed(request: Request, tid: int, note: str = Form(""), conn: sqlite3.Connection = Conn):
    """N1 (Brian, 2026-11-20): closed without being done (the event passed...). Kept in history, marked "Not needed",
    never counted as finished, no rating for the AI (it wasn't wrong). Undo or Reopen brings it back."""
    task_or_404(conn, tid)
    with db.transaction(conn):
        undo.save(conn, "Marked not needed", [("task", tid)])
        conn.execute("UPDATE task SET status = 'done', closed_as = 'not_needed', closed_note = ?, updated_at = ? WHERE id = ?",
                     (note.strip()[:200] or None, db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/keep")
def task_keep(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    """"Still need it": answers the weekly cleanup's question (asked again only after another month untouched)."""
    task_or_404(conn, tid)
    conn.execute("UPDATE task SET updated_at = ? WHERE id = ?", (db.now(), tid))
    return back(request, "/tasks?show=old")


@router.post("/tasks/{tid}/reopen")
def task_reopen(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    task_or_404(conn, tid)
    conn.execute("UPDATE task SET status = 'open', closed_as = NULL, closed_note = NULL, updated_at = ? WHERE id = ?", (db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/edit")
def task_edit(request: Request, tid: int, title: str = Form(...), owner: int = Form(ME), due: str = Form(""),
              conn: sqlite3.Connection = Conn):
    t = task_or_404(conn, tid)
    if not conn.execute("SELECT 1 FROM person WHERE id = ?", (owner,)).fetchone():   # R5-03: merged/deleted elsewhere
        raise HTTPException(409, "That person was merged or removed on another device. Reload and pick again.")
    changed = (title.strip(), owner, due or None) != (t["title"], t["owner_person_id"], t["due"])
    with db.transaction(conn):
        undo.save(conn, "Task edited", [("task", tid)])
        conn.execute("UPDATE task SET title = ?, owner_person_id = ?, due = ?, rated = ?, updated_at = ? WHERE id = ?",
                     (title.strip(), owner, due or None, "wrong" if changed and t["conversation_id"] else t["rated"],
                      db.now(), tid))
        if title.strip() != t["title"]:   # Q5: a short edit is its own short name; a long one shows in full (2 lines)
            conn.execute("UPDATE task SET short_title = ? WHERE id = ?", (title.strip() if len(title.strip()) <= 60 else None, tid))
        index(conn, "task", tid, t["conversation_id"], title.strip())
    return back(request, "/tasks")


@router.post("/tasks/{tid}/owner")
def task_owner(request: Request, tid: int, person_id: OptId = None, new_name: str = Form(""),
               conn: sqlite3.Connection = Conn):
    """Change person: moves the task between You owe and They owe you. An AI task given to the wrong person counts as wrong."""
    t = task_or_404(conn, tid)
    with db.transaction(conn):
        owner = person_from_form(conn, person_id, new_name)
        if owner != t["owner_person_id"]:
            name = conn.execute("SELECT name FROM person WHERE id = ?", (owner,)).fetchone()[0]
            undo.save(conn, f"Moved to {'you' if owner == ME else name}", [("task", tid)])
            conn.execute("UPDATE task SET owner_person_id = ?, rated = ?, updated_at = ? WHERE id = ?",
                         (owner, "wrong" if t["conversation_id"] else t["rated"], db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/delete")
def task_delete(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    task_or_404(conn, tid)
    with db.transaction(conn):
        undo.save(conn, "Task removed", [("task", tid), *(("review_item", r[0]) for r in conn.execute(   # Undo reopens it
            "SELECT id FROM review_item WHERE kind = 'inbox' AND ref_id = ? AND status = 'open'", (tid,)))])
        conn.execute("UPDATE task SET status = 'dropped', rated = 'wrong', inbox = 0, updated_at = ? WHERE id = ?", (db.now(), tid))
        unindex(conn, "task", tid)
        conn.execute("UPDATE review_item SET status = 'dismissed', resolved_at = ? WHERE kind = 'inbox' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks-bulk/{what}")
def tasks_bulk(request: Request, what: str, ids: str = Form(...), conn: sqlite3.Connection = Conn):
    """N4 / A3.2 (Brian, 2026-11-25): Tasks › pick several › Done, Not needed or Delete. One Undo brings them all back."""
    tids = [r[0] for r in (conn.execute("SELECT id FROM task WHERE id = ? AND status != 'dropped'", (i,)).fetchone()
                           for i in _ids(ids)) if r]
    if not tids or what not in ("done", "not-needed", "delete"):
        return back(request, "/tasks")
    n = f"{len(tids)} task{'s' if len(tids) != 1 else ''}"
    marks = ",".join("?" * len(tids))
    with db.transaction(conn):
        label = {"done": f"Marked {n} done", "not-needed": f"Marked {n} not needed", "delete": f"Removed {n}"}[what]
        items = [r[0] for r in conn.execute(
            f"SELECT id FROM review_item WHERE kind = 'inbox' AND status = 'open' AND ref_id IN ({marks})", tids)]
        undo.save(conn, label, [("task", t) for t in tids] + [("review_item", r) for r in items])
        if what == "done":
            conn.execute(f"UPDATE task SET status = 'done', closed_as = NULL, closed_note = NULL, rated = coalesce(rated, 'right'), "
                         f"updated_at = ? WHERE id IN ({marks})", (db.now(), *tids))
        elif what == "not-needed":
            conn.execute(f"UPDATE task SET status = 'done', closed_as = 'not_needed', closed_note = NULL, updated_at = ? "
                         f"WHERE id IN ({marks})", (db.now(), *tids))
        else:
            conn.execute(f"UPDATE task SET status = 'dropped', rated = 'wrong', inbox = 0, updated_at = ? WHERE id IN ({marks})",
                         (db.now(), *tids))
            for t in tids:
                unindex(conn, "task", t)
        if what != "done" or items:
            conn.execute(f"UPDATE review_item SET status = 'dismissed', resolved_at = ? WHERE kind = 'inbox' AND status = 'open' "
                         f"AND ref_id IN ({marks})", (db.now(), *tids))
    return back(request, "/tasks")


@router.post("/trash-bulk/{what}")
def trash_bulk(request: Request, what: str, ids: str = Form(...), conn: sqlite3.Connection = Conn):
    """N4 / A3.3 (Brian, 2026-11-25): Trash › pick several › Restore or Delete forever. Ids look like c12 (a talk) or r5
    (a recording skipped in the Inbox). Skipped recordings leave by themselves after 30 days, so Delete forever leaves
    them as they are. Both can be undone (Brian, 2026-11-25: "what if I do need to undo it"): Delete forever takes the
    talks out of Trash at once and deletes them for good once the Undo time is over (trash.purge_due; their audio goes
    in the nightly clean-up)."""
    picked = [x.strip() for x in ids.split(",")]
    cids = [int(x[1:]) for x in picked if x[:1] == "c" and x[1:].isdigit()]
    rids = [int(x[1:]) for x in picked if x[:1] == "r" and x[1:].isdigit()]
    cids = [c for c in dict.fromkeys(cids) if conn.execute(
        "SELECT 1 FROM conversation WHERE id = ? AND deleted_at IS NOT NULL AND purge_at IS NULL", (c,)).fetchone()]
    if what == "restore":
        rows = _unskip_rows(conn, list(dict.fromkeys(rids)))
        n = len(cids) + sum(1 for t, _ in rows if t == "recording")
        if n:
            with db.transaction(conn):
                uid = undo.save(conn, "Restored" if n == 1 else f"{n} items restored", [("conversation", c) for c in cids])
                undo.add(conn, uid, rows)
                for c in cids:
                    conn.execute("UPDATE conversation SET deleted_at = NULL, updated_at = ? WHERE id = ?", (db.now(), c))
                for t, r in rows:
                    if t == "recording":
                        _unskip(conn, r)
    elif what == "forever" and cids:
        at = (datetime.fromisoformat(db.now()) + timedelta(seconds=undo.UNDO_WITHIN_S + 60)).isoformat(timespec="seconds")
        with db.transaction(conn):
            undo.save(conn, "Deleted forever" if len(cids) == 1 else f"{len(cids)} talks deleted forever",
                      [("conversation", c) for c in cids])
            for c in cids:
                conn.execute("UPDATE conversation SET purge_at = ? WHERE id = ?", (at, c))
    return RedirectResponse("/conversations?trash=1", status_code=303)


@router.post("/tasks/{tid}/sure")
def task_sure(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    """"Unsure" → Looks right: the tag goes and the task leaves the Inbox."""
    task_or_404(conn, tid)
    with db.transaction(conn):
        undo.save(conn, "Marked as right", [("task", tid), *(("review_item", r[0]) for r in conn.execute(   # F74: Undo
            "SELECT id FROM review_item WHERE kind = 'inbox' AND ref_id = ? AND status = 'open'", (tid,)))])   # reopens it
        conn.execute("UPDATE task SET confidence = 1, inbox = 0, updated_at = ? WHERE id = ?", (db.now(), tid))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'inbox' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/file")
def task_file(request: Request, tid: int, area: str = Form(""), conn: sqlite3.Connection = Conn):
    """Inbox -> filed: Brian confirms the task (and optionally its area)."""
    task_or_404(conn, tid)
    with db.transaction(conn):
        conn.execute("UPDATE task SET inbox = 0, area = coalesce(?, area), updated_at = ? WHERE id = ?",
                     (area if area in ("work", "personal", "school", "other") else None, db.now(), tid))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'inbox' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), tid))
    return back(request, "/tasks?tab=inbox")


# ---------- Decisions ----------

def decision_rows(request: Request, conn: sqlite3.Connection, n: int = TASKS_PER_PAGE) -> tuple[list, int]:
    a, args = area_clause(area_of(request), "c.area")
    return paged(conn,
        "SELECT d.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, coalesce(c.started_at, c.created_at) AS at, "
        "(SELECT idx FROM segment g WHERE g.id = d.source_segment_id) AS line "
        "FROM decision d JOIN conversation c ON c.id = d.conversation_id "
        f"WHERE c.deleted_at IS NULL AND coalesce(d.rated, '') != 'wrong'{a} ORDER BY at DESC", args, n, TASKS_PER_PAGE)


@router.get("/decisions")
def decisions_page():
    return RedirectResponse("/tasks?tab=decisions", status_code=302)


def decision_or_404(conn: sqlite3.Connection, did: int) -> sqlite3.Row:
    """F77: Remove / Looks right on a decision that's gone (an old page) answered as if it worked."""
    d = conn.execute("SELECT * FROM decision WHERE id = ?", (did,)).fetchone()
    if d is None:
        raise HTTPException(404, "That decision isn't here any more")
    return d


@router.post("/decisions/{did}/wrong")
def decision_wrong(request: Request, did: int, conn: sqlite3.Connection = Conn):
    decision_or_404(conn, did)
    with db.transaction(conn):
        undo.save(conn, "Removed a decision", [("decision", did)])
        conn.execute("UPDATE decision SET rated = 'wrong' WHERE id = ?", (did,))
        unindex(conn, "decision", did)
    return back(request, "/decisions")


@router.post("/decisions/{did}/edit")
def decision_edit(request: Request, did: int, text: str = Form(...), conn: sqlite3.Connection = Conn):
    """Fix a decision's wording. Saved as rated 'right' (Brian's version), so a later AI re-run keeps it."""
    d = decision_or_404(conn, did)
    if not text.strip():
        raise HTTPException(400, "A decision can't be empty")
    with db.transaction(conn):
        undo.save(conn, "Edited a decision", [("decision", did)])
        conn.execute("UPDATE decision SET text = ?, rated = 'right' WHERE id = ?", (text.strip(), did))
        index(conn, "decision", did, d["conversation_id"], text.strip())
    return back(request, "/decisions")


@router.post("/decisions/{did}/right")
def decision_right(request: Request, did: int, conn: sqlite3.Connection = Conn):
    decision_or_404(conn, did)
    conn.execute("UPDATE decision SET rated = 'right' WHERE id = ?", (did,))
    return back(request, "/decisions")


# ---------- People ----------

# "You owe X": Brian's open tasks from conversations X spoke in (tasks don't record who they're for).
I_OWE_THEM = "EXISTS (SELECT 1 FROM speaker s WHERE s.conversation_id = t.conversation_id AND s.person_id = ?)"


PEOPLE_SORTS = {"recent": ("Last spoke", "p.last_spoken_at IS NULL, p.last_spoken_at DESC, p.name COLLATE NOCASE"),
                "talked": ("Most talked", "talked DESC, p.name COLLATE NOCASE"), "name": ("Name", "p.name COLLATE NOCASE")}
TALKED = "(SELECT coalesce(sum(s.talk_seconds), 0) FROM speaker s WHERE s.person_id = p.id)"


@router.get("/people", response_class=HTMLResponse)
def people_page(request: Request, q: str = "", sort: str = "name", n: int = PAGE, conn: sqlite3.Connection = Conn):
    """Filter by name or alias, sort by last spoke / most talked / name, a page at a time. The counts are worked out
    only for the people shown, so a long list stays quick."""
    area = area_of(request)
    sort = sort if sort in PEOPLE_SORTS else "name"   # A-Z first (Brian picked the #10 ideas, 2026-11-21)
    where, args = ["1"], []
    if area != "all":
        where.append("p.area IN (?, 'both')"); args.append(area)
    if q.strip():
        # F81: like the other lists' search boxes (N9): "_" or "%" in the box matched every person
        where.append("(p.name LIKE ? ESCAPE '\\' OR p.aliases LIKE ? ESCAPE '\\')"); args += [like_q(q)] * 2
    else:   # Brian, 2026-11-21: "Speaker 2 from ..." placeholders wait in one row at the top, not mixed into the list
        where.append("p.stands_for_speaker IS NULL")
    page, total = paged(conn, f"SELECT p.id, {TALKED} AS talked FROM person p WHERE {' AND '.join(where)} "
                              f"ORDER BY p.id = {ME} DESC, {PEOPLE_SORTS[sort][1]}", args, n)
    ids = [r["id"] for r in page]
    ta = "" if area == "all" else " AND t.area = ?"     # task counts follow the toggle too
    found = {r["id"]: r for r in conn.execute(
        f"SELECT p.*, {TALKED} AS talked, (SELECT count(*) FROM person_fact f WHERE f.person_id = p.id "
        f"AND coalesce(f.confirmed, 1) != 0 AND {live('f')}) AS n_facts, "
        f"(SELECT count(*) FROM task t WHERE t.owner_person_id = p.id AND t.status = 'open' AND {LIVE_TASK}{ta}) AS n_owed, "
        f"(SELECT count(*) FROM task t WHERE t.owner_person_id = {ME} AND t.status = 'open' AND p.id != {ME} AND {LIVE_TASK}{ta} AND "
        f"{I_OWE_THEM.replace('?', 'p.id')}) AS n_i_owe FROM person p WHERE p.id IN ({','.join('?' * len(ids)) or 'NULL'})",
        ([] if area == "all" else [area, area]) + ids)}
    dups = people_svc.duplicate_suggestions(conn)
    return render(request, "people.html", "people", conn, rows=[found[i] for i in ids if i in found], total=total, q=q, sort=sort,
                  sorts=PEOPLE_SORTS, duplicates=dups[:10], more_duplicates=len(dups) - 10,
                  mentioned=people_svc.mentioned(conn, json.loads(setting(conn, "mentioned_hidden", "[]"))) if not q.strip() else [],
                  everyone=people(conn), unnamed=[] if q.strip() else unnamed_voices(conn))


def unnamed_voices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Speakers the AI gave tasks or notes to before they had a name (placeholder people), newest talk first."""
    return conn.execute(
        "SELECT p.id AS pid, s.id AS sid, s.clip_path, s.talk_seconds, c.id AS cid, "
        "coalesce(nullif(c.short_title, ''), c.title) AS conv_title, coalesce(c.started_at, c.created_at) AS at, "
        f"(SELECT count(*) FROM task t WHERE t.owner_person_id = p.id AND t.status = 'open' AND {LIVE_TASK}) AS n_tasks, "
        "(SELECT count(*) FROM person_fact f WHERE f.person_id = p.id) AS n_facts, "
        "(SELECT count(*) FROM speaker x WHERE x.conversation_id = c.id AND x.id <= s.id) AS num "
        "FROM person p JOIN speaker s ON s.id = p.stands_for_speaker JOIN conversation c ON c.id = s.conversation_id "
        "WHERE c.deleted_at IS NULL ORDER BY at DESC").fetchall()


@router.post("/people/mentioned")
def people_mentioned(request: Request, name: str = Form(...), action: str = Form(...), person_id: OptId = 0,
                     conn: sqlite3.Connection = Conn):
    """Mentioned, not added yet: Add (new person; their facts move over), Same as (an existing person; the name
    becomes their nickname), or Hide (never list this name again; the facts stay inside their conversations)."""
    name = name.strip()
    if not name or action not in ("add", "same", "hide"):
        raise HTTPException(400, "Name and action needed")
    with db.transaction(conn):
        if action == "hide":
            hidden = json.loads(setting(conn, "mentioned_hidden", "[]"))
            set_setting(conn, "mentioned_hidden", json.dumps(sorted(set(hidden) | {name})))
            return back(request, "/people")
        ids = people_svc.unlinked_fact_ids(conn, name)
        if action == "add":
            pid = people_svc.find(conn, name)
            uid = undo.save(conn, "Added to People", [("person_fact", i) for i in ids])
            if pid is None:
                pid = people_svc.find_or_create(conn, name)
                undo.created(conn, uid, "person", pid)
            people_svc.link_mentioned(conn, name, pid)
            return RedirectResponse(f"/people/{pid}", status_code=303)
        target = conn.execute("SELECT id, name, aliases FROM person WHERE id = ?", (person_id,)).fetchone()
        if not target:
            raise HTTPException(400, "Pick who it is")
        undo.save(conn, f"{name} is {target['name']}", [("person_fact", i) for i in ids])   # Undo moves the facts back
        people_svc.link_mentioned(conn, name, person_id, as_alias=True)
    return back(request, "/people")


@router.post("/people")
def add_person(request: Request, name: str = Form(...), area: str = Form("both"), conn: sqlite3.Connection = Conn):
    if not name.strip():
        raise HTTPException(400, "Name needed")
    with db.transaction(conn):
        pid = conn.execute("INSERT INTO person (name, area, created_at) VALUES (?, ?, ?)",
                           (name.strip(), area if area in ("work", "personal", "school", "other", "both") else "both", db.now())).lastrowid
        index(conn, "person", pid, None, name.strip())
    return RedirectResponse(f"/people/{pid}", status_code=303)


@router.get("/people/merge-many", response_class=HTMLResponse)
def merge_many_page(request: Request, ids: str = "", conn: sqlite3.Connection = Conn):
    """People › Select › Merge (Brian, 2026-11-21, "like selecting emails in Gmail"): pick whose name to keep."""
    picked = [r for r in (conn.execute("SELECT id, name FROM person WHERE id = ?", (i,)).fetchone() for i in _ids(ids)) if r]
    if len(picked) < 2:
        return RedirectResponse("/people", status_code=303)
    keep = next((p["id"] for p in picked if p["id"] == ME), picked[0]["id"])
    # F98 (500 pass): what each one brings, for every row (it was only on the rows not ticked at first, so ticking
    # another name left the counts on the wrong row and the first name had none)
    previews = {p["id"]: people_svc.preview(conn, p["id"], p["id"]) for p in picked}
    return render(request, "merge_many.html", "people", conn, picked=picked, keep=keep, previews=previews,
                  ids=",".join(str(p["id"]) for p in picked))


@router.get("/people/{pid}", response_class=HTMLResponse)
def person_page(request: Request, pid: int, n: int = PAGE, conn: sqlite3.Connection = Conn):
    p = conn.execute("SELECT * FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    facts = conn.execute(
        "SELECT f.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title FROM person_fact f LEFT JOIN conversation c ON c.id = f.conversation_id "
        f"WHERE f.person_id = ? AND coalesce(f.confirmed, 1) != 0 AND {live('f')} ORDER BY coalesce(f.date, f.created_at) DESC",
        (pid,)).fetchall()
    ta, targs = area_clause(area_of(request), "t.area")
    ca, cargs = area_clause(area_of(request), "c.area")
    open_tasks = ("SELECT t.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) AS line "
                  f"FROM task t LEFT JOIN conversation c ON c.id = t.conversation_id WHERE t.status = 'open' AND {LIVE_TASK} AND {{}}" + ta +
                  " ORDER BY t.due IS NULL, t.due, t.id")
    they_owe = conn.execute(open_tasks.format("t.owner_person_id = ?"), (pid, *targs)).fetchall()
    i_owe = conn.execute(open_tasks.format(f"t.owner_person_id = {ME} AND {I_OWE_THEM}"),
                         (pid, *targs)).fetchall() if pid != ME else []
    convs, n_convs = paged(conn,
        "SELECT DISTINCT c.* FROM conversation c JOIN speaker s ON s.conversation_id = c.id WHERE s.person_id = ? "
        f"AND c.deleted_at IS NULL{ca} ORDER BY coalesce(c.started_at, c.created_at) DESC", (pid, *cargs), n)
    voice = conn.execute("SELECT sum(sample_count) FROM voiceprint WHERE person_id = ?", (pid,)).fetchone()[0]
    return render(request, "person.html", "people", conn, ai_busy=ai_jobs.pending_ids(conn), p=p, facts=facts, they_owe=they_owe, i_owe=i_owe, convs=convs, n_convs=n_convs,
                  talk_time=talk.with_person(conn, pid) if pid != ME else None,
                  speaking={d: talk.speaking_trend(conn, d) for d in (7, 30)} if pid == ME else None,
                  aliases=", ".join(json.loads(p["aliases"] or "[]")), voice=voice, people=people(conn))


@router.get("/people/{pid}/merge", response_class=HTMLResponse)
def merge_preview(request: Request, pid: int, into: int, conn: sqlite3.Connection = Conn):
    """Step 1 of a merge: show what moves before Brian confirms."""
    if pid == ME:
        raise HTTPException(400, "Brian can't be merged into someone else")
    try:
        pv = people_svc.preview(conn, pid, into)
    except LookupError:
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    return render(request, "merge.html", "people", conn, pv=pv, from_id=pid, into_id=into)


@router.post("/people/{pid}/merge")
def merge_people(pid: int, into: int = Form(...), conn: sqlite3.Connection = Conn):
    """Merge two people. Undo (10 minutes, 2026-11-25) brings the merged-away person back with everything they had."""
    try:
        with db.transaction(conn):
            uid = undo.save(conn, "People merged", [])
            _merge_with_undo(conn, uid, pid, into)
            undo.after(conn, uid, "person", into)
    except LookupError:
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/people/{into}", status_code=303)


@router.post("/people/{pid}/not-same")
def people_not_same(pid: int, into: int = Form(...), later: int = Form(0), conn: sqlite3.Connection = Conn):
    """A "Possibly the same person" suggestion: Don't merge (never suggest the pair again) or ✕ (hide it for 30 days).
    Undo brings the suggestion back."""
    if pid == into:
        raise HTTPException(400, "Pick two different people")
    if not all(conn.execute("SELECT 1 FROM person WHERE id = ?", (i,)).fetchone() for i in (pid, into)):
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    # F76: a second tap (or the other device) added the pair again, so one Undo brought nothing back. Already hidden
    # for good, or hidden for 30 days and ✕ tapped again: nothing to do.
    if conn.execute("SELECT 1 FROM person_not_same WHERE a = ? AND b = ? AND (until IS NULL OR (? AND until > ?))",
                    (min(pid, into), max(pid, into), 1 if later else 0, db.now())).fetchone():
        return RedirectResponse("/people", status_code=303)
    with db.transaction(conn):
        uid = undo.save(conn, "Suggestion hidden for 30 days" if later else "Marked as different people", [])
        undo.created(conn, uid, "person_not_same", people_svc.not_same(conn, pid, into, 30 if later else None))
    return RedirectResponse("/people", status_code=303)


@router.post("/people/merge-many")
def merge_many(ids: str = Form(...), into: int = Form(...), conn: sqlite3.Connection = Conn):
    """People › Select › Merge: one Undo brings them all back (2026-11-25)."""
    others = [i for i in _ids(ids) if i != into and conn.execute("SELECT 1 FROM person WHERE id = ?", (i,)).fetchone()]
    if ME in others:
        raise HTTPException(400, "You can't be merged into someone else; keep your own name")
    if not conn.execute("SELECT 1 FROM person WHERE id = ?", (into,)).fetchone():
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    if others:
        with db.transaction(conn):
            uid = undo.save(conn, f"{len(others) + 1} people merged", [])
            for pid in others:
                _merge_with_undo(conn, uid, pid, into)
            undo.after(conn, uid, "person", into)
    return RedirectResponse(f"/people/{into}", status_code=303)


def _merge_with_undo(conn: sqlite3.Connection, uid: int, pid: int, into: int) -> None:
    """Merge person `pid` into `into` inside the caller's transaction, saving what Undo needs first: the rows that move,
    the person who goes, and both voiceprints (merge combines them into one)."""
    undo.add(conn, uid, people_svc.merge_undo_rows(conn, pid, into))
    undo.removed(conn, uid, "person", "id = ?", (pid,))
    first = not any(d.get("table") == "voiceprint_clear" and d.get("person_id") == into
                    for d in json.loads(conn.execute("SELECT removed FROM undo_action WHERE id = ?", (uid,)).fetchone()[0]))
    if first:   # the kept person's voiceprint as it was before the first merge
        undo.voiceprint(conn, uid, into)
    undo.voiceprint(conn, uid, pid)
    people_svc.merge(conn, pid, into, own_transaction=False)


@router.post("/people/delete-many")
def delete_many(ids: str = Form(...), conn: sqlite3.Connection = Conn):
    """People › Select › Remove: one Undo brings them all back (10 minutes)."""
    pids = [i for i in _ids(ids) if i != ME and conn.execute("SELECT 1 FROM person WHERE id = ?", (i,)).fetchone()]
    if not pids:
        return RedirectResponse("/people", status_code=303)
    with db.transaction(conn):
        rows = [r for pid in pids for r in people_svc.undo_rows(conn, pid)]
        uid = undo.save(conn, f"Removed {len(pids)} {'person' if len(pids) == 1 else 'people'}", rows)
        for pid in pids:
            _delete_with_undo(conn, uid, pid)
    return RedirectResponse("/people", status_code=303)


@router.post("/people/{pid}/delete")
def delete_person(pid: int, conn: sqlite3.Connection = Conn):
    """Delete a person (people.delete says what happens to their lines and tasks). Undo works for 10 minutes."""
    p = conn.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    if pid == ME:
        raise HTTPException(400, "You can't delete yourself")
    with db.transaction(conn):
        _delete_with_undo(conn, undo.save(conn, "Person deleted", people_svc.undo_rows(conn, pid)), pid)
    return RedirectResponse("/people", status_code=303)


def _delete_with_undo(conn: sqlite3.Connection, uid: int, pid: int) -> None:
    """Delete one person, keeping their row and voiceprint in Undo action uid."""
    undo.removed(conn, uid, "person", "id = ?", (pid,))
    undo.removed(conn, uid, "voiceprint", "person_id = ?", (pid,))
    people_svc.delete(conn, pid)


# The same 8 colors the talk-time bar uses automatically (app.css .talk), so a picked color never clashes.
PERSON_COLORS = {"#2563eb": "Blue", "#ea580c": "Orange", "#16a34a": "Green", "#9333ea": "Purple", "#db2777": "Pink",
                 "#0891b2": "Teal", "#ca8a04": "Gold", "#dc2626": "Red"}


@router.post("/speakers/{sid}/color")
def speaker_color(request: Request, sid: int, color: str = Form(""), conn: sqlite3.Connection = Conn):
    """A speaker's color in this one conversation (talk bar + chip). Empty = automatic."""
    row = conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise HTTPException(404, "That speaker isn't here any more")
    conn.execute("UPDATE speaker SET color = ? WHERE id = ?", (color if color in PERSON_COLORS else None, sid))
    return back(request, f"/conversations/{row[0]}")


@router.post("/people/{pid}/edit")
def edit_person(request: Request, pid: int, name: str = Form(...), aliases: str = Form(""), area: str = Form("both"),
                notes: str = Form(""), conn: sqlite3.Connection = Conn):
    if not name.strip():
        raise HTTPException(400, "A name is needed")
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    p = conn.execute("SELECT name, stands_for_speaker FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise HTTPException(404, "That person isn't here any more (merged or deleted)")
    named_speaker = None
    if p["stands_for_speaker"] and name.strip() != p["name"]:
        # A placeholder ("Speaker 2 from Budget sync on Oct 20") given a real name: that names the speaker, everywhere.
        named_speaker = p["stands_for_speaker"]
        cid = conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (named_speaker,)).fetchone()[0]
        other = people_svc.find(conn, name)
        if other is not None and other != pid:   # someone who already exists: their tasks, notes and decisions join them
            people_svc.merge(conn, pid, other)
            identify.enroll(conn, named_speaker, other, cfg=request.app.state.cfg)
            extract.relabel_speakers(conn, cid)
            if request.headers.get("HX-Request"):   # Edit autosaves in the background: tell the page to go there itself
                return Response(headers={"HX-Redirect": f"/people/{other}"})
            return RedirectResponse(f"/people/{other}", status_code=303)   # this page is gone
    with db.transaction(conn):
        conn.execute("UPDATE person SET name = ?, aliases = ?, area = ?, notes = ? WHERE id = ?",
                     (name.strip(), json.dumps(alias_list), area if area in ("work", "personal", "school", "other", "both") else "both",
                      notes.strip() or None, pid))
        index(conn, "person", pid, None, " ".join([name.strip(), *alias_list, notes.strip()]))
        if named_speaker:
            conn.execute("UPDATE person SET stands_for_speaker = NULL WHERE id = ?", (pid,))
        else:   # the new name replaces the old one in earlier summaries, tasks and notes too (no AI)
            extract.rename_in_text(conn, pid, p["name"], name)
    if named_speaker:
        identify.enroll(conn, named_speaker, pid, cfg=request.app.state.cfg)
        extract.relabel_speakers(conn, cid)
    return back(request, f"/people/{pid}")


@router.post("/facts/{fid}/confirm")
def confirm_fact(request: Request, fid: int, ok: int = Form(...), conn: sqlite3.Connection = Conn):
    if not conn.execute("SELECT 1 FROM person_fact WHERE id = ?", (fid,)).fetchone():   # F77: an old page said nothing
        raise HTTPException(404, "That note isn't here any more")
    with db.transaction(conn):
        if not ok:
            undo.save(conn, "Removed a note", [("person_fact", fid)])
        conn.execute("UPDATE person_fact SET confirmed = ? WHERE id = ?", (1 if ok else 0, fid))
        if not ok:
            unindex(conn, "fact", fid)
    return back(request)


@router.post("/facts/{fid}/edit")
def fact_edit(request: Request, fid: int, value: str = Form(...), conn: sqlite3.Connection = Conn):
    """Fix the wording of a people note in place."""
    f = conn.execute("SELECT * FROM person_fact WHERE id = ?", (fid,)).fetchone()
    if f is None:
        raise HTTPException(404, "That note isn't here any more")
    if not value.strip():
        raise HTTPException(400, "A note can't be empty")
    with db.transaction(conn):
        undo.save(conn, "Edited a note", [("person_fact", fid)])
        conn.execute("UPDATE person_fact SET value = ?, confirmed = 1 WHERE id = ?", (value.strip(), fid))
        index(conn, "fact", fid, f["conversation_id"], f"{f['person_name'] or ''}: {value.strip()}")
    return back(request)


@router.post("/facts/{fid}/kind")
def fact_kind(request: Request, fid: int, kind: str = Form(...), conn: sqlite3.Connection = Conn):
    """Change what kind of note it is (Birthday, Family, ...) from the note's pop-up."""
    if kind not in prompts.FACT_TYPES:
        raise HTTPException(400, "Pick one of the kinds in the list")
    if not conn.execute("SELECT 1 FROM person_fact WHERE id = ?", (fid,)).fetchone():
        raise HTTPException(404, "That note isn't here any more")
    with db.transaction(conn):
        undo.save(conn, "Changed a note's kind", [("person_fact", fid)])
        conn.execute("UPDATE person_fact SET type = ? WHERE id = ?", (kind, fid))
    return back(request)


@router.post("/facts/{fid}/person")
def fact_person(request: Request, fid: int, person_id: OptId = None, new_name: str = Form(""),
                conn: sqlite3.Connection = Conn):
    """Change person on a people note (the AI attached it to the wrong person). The note stays kept."""
    if not conn.execute("SELECT 1 FROM person_fact WHERE id = ?", (fid,)).fetchone():
        raise HTTPException(404, "That note isn't here any more")
    with db.transaction(conn):
        pid = person_from_form(conn, person_id, new_name)
        name = conn.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()[0]
        undo.save(conn, f"Moved a note to {name}", [("person_fact", fid)])
        conn.execute("UPDATE person_fact SET person_id = ?, person_name = ? WHERE id = ?", (pid, name, fid))
    return back(request)


# ---------- Review ----------

# "Oct 23 at 09_07", also at the end of a renamed one ("Intro with Sam - Oct 14 at 11_30", or 11-30 / 11:30)
# F10: a phone on a 12-hour clock adds AM/PM ("Nov 12 at 2_15 PM"), which read as 2:15 in the morning
RECORDER_NAME = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2}) at (\d{1,2})[_:\-](\d{2})\b(?:\s?([AaPp])\.?[Mm]\b\.?)?")


def recorded_at(title: str | None, received_at: str | None) -> datetime:
    """When a recording was made, for sorting (Brian, 2026-11-20: the waiting list looked random because files
    synced together share one received time). Google Recorder names files "Oct 23 at 09_07" (phone time, no year);
    anything else falls back to when it arrived."""
    try:
        got = datetime.fromisoformat(received_at) if received_at else datetime.now(timezone.utc)
    except ValueError:
        got = datetime.now(timezone.utc)
    if got.tzinfo is None:
        got = got.replace(tzinfo=timezone.utc)
    m = RECORDER_NAME.search((title or "").strip())
    if not m:
        return voicememo.name_date(title) or got   # Apple Voice Memos: "20261113 201200"
    try:
        hour = int(m[3])
        if m[5] and 1 <= hour <= 12:
            hour = hour % 12 + (12 if m[5] in "Pp" else 0)
        when = datetime.strptime(f"{m[1]} {m[2]} {got.astimezone().year} {hour}:{m[4]}", "%b %d %Y %H:%M").astimezone()
    except ValueError:
        return got
    if when > got + timedelta(days=2):   # "Dec 30" that arrived in January: last year
        when = when.replace(year=when.year - 1)
    return when


def review_data(conn: sqlite3.Connection, area: str = "all", snoozed: bool = False) -> dict:
    """Open review items grouped by kind, ready to act on: the awake ones (Needs you), or the snoozed ones."""
    scope, args = review_scope(area)
    items = conn.execute(
        f"SELECT r.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title FROM {REVIEW_FROM} WHERE r.status = 'open' AND {scope} "
        f"AND {'NOT ' if snoozed else ''}{AWAKE} ORDER BY r.created_at", [*args, db.now()]).fetchall()
    speakers, inbox, approvals, glossary, recordings = [], [], [], [], []
    rt = None   # F79: the M14's step times, read once for every card (they were read again for each recording)
    for r in items:
        payload = json.loads(r["payload"] or "{}")
        if r["kind"] == "speaker":
            s = conn.execute("SELECT * FROM speaker WHERE id = ?", (r["ref_id"],)).fetchone()
            if s:
                speakers.append({"r": r, "s": s, "payload": payload})
        elif r["kind"] == "inbox":
            t = conn.execute("SELECT t.*, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) AS line FROM task t "
                             "WHERE t.id = ?", (r["ref_id"],)).fetchone()
            inbox.append({"r": r, "t": t, "payload": payload})
        elif r["kind"] == "approval" and payload.get("recording_id"):
            dur = conn.execute("SELECT duration_s, title, received_at FROM recording WHERE id = ?",
                               (payload["recording_id"],)).fetchone()
            d = dur[0] if dur and dur[0] else (payload.get("minutes") or 0) * 60
            rt = rt or progress.rates(conn)
            recordings.append({"r": r, "payload": payload,
                               "when": recorded_at(payload.get("title") or (dur["title"] if dur else None),
                                                   dur["received_at"] if dur else None),
                               "est": {**progress.choices(conn, [d], rt)[0],
                                                                   "cost": payload.get("ai_cost") or ai_cost(d)},
                               "type_guess": "learning" if LEARNING_WORDS.search(payload.get("title") or "") else ""})
        elif r["kind"] == "approval":
            est = payload.get("estimate") or {}
            approvals.append({"r": r, "payload": payload, "cost": est.get("cost_usd", estimate(conn, r["conversation_id"]))})
        else:
            g = conn.execute("SELECT * FROM glossary_term WHERE id = ?", (r["ref_id"],)).fetchone()
            glossary.append({"r": r, "g": g, "payload": payload, "variants": json.loads(g["variants"] or "[]") if g else []})
    recordings.sort(key=lambda x: x["when"], reverse=True)   # newest recording first
    older = [] if snoozed else sorted(
        (dict(r) for r in conn.execute("SELECT id, title, duration_s, received_at FROM recording WHERE status = 'backlog' "
                                       "ORDER BY received_at DESC, id DESC")),
        key=lambda r: recorded_at(r["title"], r["received_at"]), reverse=True)
    if recordings or older:
        rt = rt or progress.rates(conn)
    for r, e in zip(older, progress.choices(conn, [r["duration_s"] for r in older], rt) if older else []):
        r["est"] = {**e, "cost": ai_cost(r["duration_s"])}
    return {"speakers": speakers, "inbox": inbox, "approvals": approvals, "glossary": glossary, "recordings": recordings,
            "older": older, "recordings_total": totals(recordings), "older_total": totals(older),
            "m14_wait": progress.queue_wait_s(conn, r=rt) if recordings or older else 0}


def review_label(conn: sqlite3.Connection, r: sqlite3.Row) -> tuple[str, str]:
    """(what it is, which one) for a review item in the Snoozed group and the Review log."""
    payload = json.loads(r["payload"] or "{}")
    if r["kind"] == "speaker":
        return "Speaker to name", r["conv_title"] or "Conversation"
    if r["kind"] == "inbox":
        t = conn.execute("SELECT title FROM task WHERE id = ?", (r["ref_id"],)).fetchone()
        return "Unsure task", t[0] if t else payload.get("title") or "Task"
    if r["kind"] == "approval" and payload.get("recording_id"):
        return "Recording waiting for your OK", recorder_title(payload.get("title")) or "Recording"
    if r["kind"] == "approval":
        return "Cloud approval", r["conv_title"] or payload.get("task") or "AI run"
    g = conn.execute("SELECT term FROM glossary_term WHERE id = ?", (r["ref_id"],)).fetchone()
    return "Glossary suggestion", g[0] if g else payload.get("term") or "Term"


def snoozed_rows(conn: sqlite3.Connection, area: str) -> list[dict]:
    """Snoozed review items for Today's Snoozed group: still in view, with when each comes back."""
    scope, args = review_scope(area)
    rows = conn.execute(f"SELECT r.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title FROM {REVIEW_FROM} WHERE r.status = 'open' AND NOT {AWAKE} "
                        f"AND {scope} ORDER BY r.snoozed_until", [db.now(), *args]).fetchall()
    return [{"what": what, "title": title, "until": r["snoozed_until"], "now": f"/review/{r['id']}/now"}
            for r in rows for what, title in [review_label(conn, r)]]


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, tab: str = "open", n: int = PAGE, conn: sqlite3.Connection = Conn):
    """Review is the log, like an email inbox: nothing just disappears. Open = exactly Today's Needs you; Snoozed (with
    when each comes back); Done and Dismissed (when each was resolved; dismissed ones can be restored)."""
    area = area_of(request)
    tab = tab if tab in REVIEW_TABS else "open"
    scope, args = review_scope(area)
    needs = needs_you(conn, area)
    counts = {k: conn.execute(f"SELECT count(*) FROM {REVIEW_FROM} WHERE r.status = ? AND {scope}", [k, *args]).fetchone()[0]
              for k in ("done", "dismissed")}
    counts["snoozed"] = len(needs["snoozed"])          # "open" is the Today tab's number (needs_count in every page)
    ctx = {"tab": tab, "counts": counts, "people": people(conn)}
    if tab == "open":
        return render(request, "review.html", "review", conn, **ctx, **needs)
    if tab == "snoozed":
        data = review_data(conn, area, snoozed=True)
        return render(request, "review.html", "review", conn, **ctx, **data,
                      lines=[x for x in needs["snoozed"] if x["now"].startswith(("/needs/", "/tasks/"))])
    rows, total = paged(conn, f"SELECT r.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title FROM {REVIEW_FROM} WHERE r.status = ? AND {scope} "
                        "ORDER BY coalesce(r.resolved_at, r.created_at) DESC", [tab, *args], n)
    return render(request, "review.html", "review", conn, **ctx, log=[log_row(conn, r) for r in rows], total=total)


def _local_day(ts) -> str:
    try:
        return datetime.fromisoformat(str(ts)).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return ""


@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, sort: str = "newest", show: str = "all", has: str = "", length: str = "", kind: str = "",
               source: str = "", min_m: str = "", max_m: str = "", start: str = "", end: str = "", q: str = "",
               conn: sqlite3.Connection = Conn):
    """Recordings waiting to be processed: quick titles and previews, sort, and the Filter panel (FX-A: dropdowns,
    several at once, each shown as a chip you can ✕), with the same Transcribe + AI / Transcript Only / Skip buttons."""
    sort = sort if sort in inbox_svc.SORTS else "newest"
    asked = {"has": has, "length": length, "kind": kind, "source": source}
    if show in inbox_svc.OLD_SHOW:   # old links (?show=long) still work
        k, v = inbox_svc.OLD_SHOW[show]
        asked[k] = asked[k] or v
    chosen = {k: v for k, v in asked.items() if v and v in inbox_svc.FX[k][1]}
    data = review_data(conn, "all")
    items = inbox_items(request, conn, data)
    # M3 A (Brian picked, 2026-11-20): length from/to (slider + number boxes) and recorded from/to dates
    lo = int(min_m) if min_m.isdigit() else None
    hi = int(max_m) if max_m.isdigit() else None
    start = start if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start or "") else ""
    end = end if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end or "") else ""

    def in_range(x) -> bool:
        m, d = (x["duration_s"] or 0) / 60, _local_day(x["when"])
        return ((lo is None or m >= lo) and (hi is None or m <= hi) and (not start or (d and d >= start))
                and (not end or (d and d <= end)))
    q = q.strip()[:100]
    words = q.lower()

    def found(x) -> bool:   # N9: the search box above the list (the title, the quick title or the opening words)
        return not words or any(words in (x.get(k) or "").lower() for k in ("title", "quick_title", "preview_full", "preview"))
    ranged = [x for x in items if in_range(x) and found(x)]
    shown = inbox_svc.sort(inbox_svc.fx_filter(ranged, chosen), sort)
    counts = {k: {v: sum(inbox_svc.fx_match(x, k, v) for x in inbox_svc.fx_filter(items, {a: b for a, b in chosen.items() if a != k}))
                  for v in opts} for k, (_, opts) in inbox_svc.FX.items()}
    rng = {"min_m": str(lo) if lo is not None else "", "max_m": str(hi) if hi is not None else "", "start": start, "end": end, "q": q}

    def url(**change):
        q = {**chosen, **rng, "sort": sort, **change}
        return "/inbox" + ("?" + urlencode({k: v for k, v in q.items() if v and not (k == "sort" and v == "newest")}) if any(
            v and not (k == "sort" and v == "newest") for k, v in q.items()) else "")
    chips = [(inbox_svc.FX[k][1][v], url(**{k: ""})) for k, v in chosen.items()]
    if lo is not None or hi is not None:
        chips.append((f"{lo or 0}–{hi if hi is not None else '∞'} min", url(min_m="", max_m="")))
    if start or end:
        chips.append((f"{start or '…'} to {end or 'now'}", url(start="", end="")))
    longest = max([int((x["duration_s"] or 0) / 60) + 1 for x in items] or [60])
    return render(request, "inbox.html", "inbox", conn, items=shown, total=len(items), counts=counts, sort=sort,
                  rng=rng, longest=longest, n_on=len(chosen) + (lo is not None or hi is not None) + bool(start or end),
                  chosen=chosen, fx=inbox_svc.FX, chips=chips, clear_url=url(**{k: "" for k in [*chosen, *rng]}),
                  sorts=inbox_svc.SORTS, hours=sum(x["duration_s"] or 0 for x in items) / 3600,
                  m14_wait=data["m14_wait"], meeting_types=prompts.MEETING_TYPES,
                  recordings_total=data["recordings_total"], n_ready=len(data["recordings"]))


@router.post("/inbox/merge")
def inbox_merge(request: Request, ids: str = Form(...), conn: sqlite3.Connection = Conn):
    """Merge before processing (Brian, 2026-11-21): the picked recordings become one, waiting in the Inbox."""
    if stop := demo_stop(request, "audio", "/inbox"):
        return stop
    try:
        new_id = merge_svc.merge(conn, request.app.state.cfg, _id_list(ids))
    except merge_svc.MergeError as e:
        raise HTTPException(400, str(e))
    with db.transaction(conn):   # Undo splits it back while it's still waiting (2026-11-25), like Unmerge
        n = conn.execute("SELECT count(*) FROM recording_part WHERE recording_id = ?", (new_id,)).fetchone()[0]
        undo.created(conn, undo.save(conn, f"{n} recordings merged", []), "inbox_merge", new_id)
    return RedirectResponse(f"/inbox#rec-{new_id}", status_code=303)


@router.post("/inbox/{rec_id}/unmerge")
def inbox_unmerge(request: Request, rec_id: int, conn: sqlite3.Connection = Conn):
    try:
        merge_svc.unmerge(conn, request.app.state.cfg, rec_id)
    except merge_svc.MergeError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse("/inbox", status_code=303)


def inbox_items(request: Request, conn: sqlite3.Connection, data: dict) -> list[dict]:
    rows = {r["id"]: r for r in conn.execute("SELECT id, source, original_path, title, duration_s, received_at FROM recording "
                                            "WHERE status IN ('awaiting_approval', 'backlog')")}
    folders = {request.app.state.cfg.data_dir / "inbound"} | {Path(r["original_path"]).parent for r in rows.values()
                                                               if r["original_path"] and r["original_path"] != "/x"}
    uploads = request.app.state.cfg.data_dir / "uploads"   # first: a transcript added by hand wins over a synced one
    index = inbox_svc.transcript_index([uploads, *sorted(folders - {uploads})])
    quick = peek.peeks(conn)
    items = []
    for x in data["recordings"]:
        items.append({"rec_id": x["payload"]["recording_id"], "x": x, "older": False, "when": x["when"],
                      "title": x["payload"].get("title"), "duration_s": (x["payload"].get("minutes") or 0) * 60})
    for r in data["older"]:
        items.append({"rec_id": r["id"], "r": r, "older": True, "when": recorded_at(r["title"], r["received_at"]),
                      "title": r["title"], "duration_s": r["duration_s"], "ai_cost": ai_cost(r["duration_s"])})
    merged = {}
    for r in conn.execute("SELECT recording_id, count(*) AS n, group_concat(title, ' + ') AS t FROM recording_part GROUP BY recording_id"):
        merged[r["recording_id"]] = {"n": r["n"], "titles": r["t"]}
    for it in items:
        it["merged"] = merged.get(it["rec_id"])
        rec = rows.get(it["rec_id"])
        if rec and rec["duration_s"]:
            it["duration_s"] = rec["duration_s"]
        it["source"] = inbox_svc.SOURCES.get(rec["source"], rec["source"]) if rec else ""
        path = inbox_svc.find(index, it["title"], rec["original_path"] if rec else None)
        if not path and rec and rec["original_path"] and voicememo.words(Path(rec["original_path"])):
            path = Path(rec["original_path"])   # an Apple Voice Memo: its transcript is inside the .m4a
            it["source"] = "Voice Memos"
        it["transcript"] = path
        it["quick_title"] = it["preview"] = it["preview_full"] = ""
        it["peek"] = False
        if not path and it["rec_id"] in quick:   # T1: from the first 2 minutes (a Recorder .txt, T2, wins when there is one)
            it["quick_title"] = inbox_svc.quick_title(quick[it["rec_id"]], min_count=1)
            it["preview"] = inbox_svc.preview([{"speaker": "", "text": quick[it["rec_id"]]}])
            it["preview_full"] = inbox_svc.preview([{"speaker": "", "text": quick[it["rec_id"]]}], chars=4000)
            it["peek"] = True
        if path:
            try:
                text = inbox_svc.read(path, 200_000)
                it["quick_title"] = inbox_svc.quick_title(text)
                it["preview"] = inbox_svc.preview(inbox_svc.parse(text))
                it["preview_full"] = inbox_svc.preview(inbox_svc.parse(text), chars=1800)
            except OSError:
                it["transcript"] = None
    return items


@router.get("/inbox/{rid}/transcript", response_class=HTMLResponse)
def inbox_transcript(request: Request, rid: int, conn: sqlite3.Connection = Conn):
    """The whole Recorder transcript for a waiting recording, unedited, on one clean page (Brian, 2026-11-20)."""
    it = next((x for x in inbox_items(request, conn, review_data(conn, "all")) if x["rec_id"] == rid), None)
    if not it or not it["transcript"]:
        raise HTTPException(404, "No transcript for that recording")
    text = inbox_svc.read(it["transcript"])
    return render(request, "inbox_transcript.html", "inbox", conn, it=it, turns=inbox_svc.parse(text),
                  words=len(text.split()))


@router.post("/inbox/{rid}/transcript")
def inbox_add_transcript(request: Request, rid: int, text: str = Form(""), file: UploadFile | None = File(None),
                         conn: sqlite3.Connection = Conn):
    """Add (or replace) a waiting recording's transcript by pasting it or picking a .txt (Apple Voice Memos, Recorder,
    anything; Brian, 2026-11-20). Saved as uploads/<recording name>.txt, so it wins over the 2-minute guess and over a
    synced .txt. Only the Inbox title, preview and "Read transcript" use it: processing still makes its own."""
    rec = conn.execute("SELECT original_path, title FROM recording WHERE id = ? AND status IN ('awaiting_approval', 'backlog')",
                       (rid,)).fetchone()
    if rec is None:
        raise HTTPException(404, "That recording isn't waiting in the Inbox")
    body = text.strip()
    if not body and file is not None and file.filename:
        if Path(file.filename).suffix.lower() not in (".txt", ".text", ".md"):
            raise HTTPException(400, "Pick a .txt file, or paste the text")
        body = file.file.read(2_000_000).decode("utf-8", errors="replace").strip()
    if not body:
        raise HTTPException(400, "Paste the transcript or pick a .txt file")
    stem = Path(rec["original_path"]).stem if rec["original_path"] else (rec["title"] or f"recording {rid}")
    dest = request.app.state.cfg.data_dir / "uploads" / f"{re.sub(r'[^\w .()-]', '_', stem).strip() or rid}.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body + "\n", encoding="utf-8")
    return RedirectResponse(f"/inbox#rec-{rid}", status_code=303)


@router.get("/inbox/{rid}/transcript.txt")
def inbox_transcript_txt(request: Request, rid: int, conn: sqlite3.Connection = Conn):
    it = next((x for x in inbox_items(request, conn, review_data(conn, "all")) if x["rec_id"] == rid), None)
    if not it or not it["transcript"]:
        raise HTTPException(404)
    if it["transcript"].suffix.lower() != ".txt":   # a Voice Memo's transcript, from inside the .m4a
        return Response(inbox_svc.read(it["transcript"]), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(it['transcript'].stem + '.txt')}"})   # R5-05
    return FileResponse(it["transcript"], media_type="text/plain; charset=utf-8", filename=it["transcript"].name)


REVIEW_TABS = {"open": "Open", "snoozed": "Snoozed", "done": "Done", "dismissed": "Dismissed"}


def log_row(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    """One resolved review item for the log: what, which, what Brian did and when, and where it led."""
    what, title = review_label(conn, r)
    payload = json.loads(r["payload"] or "{}")
    recording = r["kind"] == "approval" and payload.get("recording_id")
    did = {"speaker": ("Named", "Skipped"), "inbox": ("Kept", "Removed"), "glossary": ("Added", "Dismissed"),
           "approval": ("Approved", "Deleted" if recording else "Declined")}[r["kind"]][r["status"] == "dismissed"]
    cid = r["conversation_id"] or (recording and (conn.execute("SELECT id FROM conversation WHERE recording_id = ? "
                                                              "AND deleted_at IS NULL", (payload["recording_id"],)).fetchone() or [None])[0])
    return {"id": r["id"], "what": what, "title": title, "did": did, "at": r["resolved_at"] or r["created_at"],
            "href": f"/conversations/{cid}" if cid else "/settings/glossary" if r["kind"] == "glossary" else None,
            "status": r["status"]}


@router.post("/review/{rid}/later")
def review_later(request: Request, rid: int, until: str = Form("tomorrow"), at: str = Form(""),
                 conn: sqlite3.Connection = Conn):
    """Remind me later: the item moves to Snoozed (still listed on Today and in Review) until the time Brian picked."""
    review_or_404(conn, rid)
    conn.execute("UPDATE review_item SET snoozed_until = ? WHERE id = ?", (snooze_until(until, at), rid))
    return back(request, "/")


@router.post("/review/{rid}/process-at")
def review_process_at(request: Request, rid: int, when: str = Form("tonight"), at: str = Form(""), ai: str = Form("0"),
                      meeting_type: str = Form(""), conn: sqlite3.Connection = Conn):
    """Process at a set time (Brian, 2026-11-21): the recording waits, then is sent at that time with AI notes or
    Transcript only. AI notes here count as confirmed: the sheet shows the cost on the same line."""
    r = review_or_404(conn, rid)
    if r["kind"] != "approval" or r["conversation_id"] or not r["ref_id"]:
        raise HTTPException(400, "Only a waiting recording can be processed later")
    if stop := demo_stop(request, "run", "/inbox"):
        return stop
    at_utc = (control.tonight().astimezone(timezone.utc).isoformat(timespec="seconds") if when == "tonight"
              else snooze_until("pick", at))
    control.schedule(conn, rid, at_utc, ai == "1", meeting_type if meeting_type in prompts.MEETING_TYPES else None)
    return back(request, "/inbox")


@router.post("/review/{rid}/process-at/cancel")
def review_process_at_cancel(request: Request, rid: int, conn: sqlite3.Connection = Conn):
    review_or_404(conn, rid)
    control.unschedule(conn, rid)
    return back(request, "/inbox")


@router.post("/review/{rid}/now")
def review_now(request: Request, rid: int, conn: sqlite3.Connection = Conn):
    review_or_404(conn, rid)
    conn.execute("UPDATE review_item SET snoozed_until = NULL WHERE id = ?", (rid,))
    return back(request, "/review")


NEEDS_LINES = ("m14", "older")   # Today lines that aren't review items: the M14's health and the older recordings


@router.post("/needs/{what}/later")
def needs_later(request: Request, what: str, until: str = Form("tomorrow"), at: str = Form(""),
                conn: sqlite3.Connection = Conn):
    """Snooze the M14-health line or the older-recordings line on Today, like any Needs you item."""
    if what not in NEEDS_LINES:
        raise HTTPException(404)
    set_setting(conn, f"later:{what}", snooze_until(until, at))
    return back(request, "/")


@router.post("/needs/{what}/now")
def needs_now(request: Request, what: str, conn: sqlite3.Connection = Conn):
    if what not in NEEDS_LINES:
        raise HTTPException(404)
    conn.execute("DELETE FROM setting WHERE key = ?", (f"later:{what}",))
    return back(request, "/")


@router.get("/inbox-bulk/ai", response_class=HTMLResponse)
def inbox_bulk_ai_confirm(request: Request, ids: str = "", conn: sqlite3.Connection = Conn):
    """R5-24: Inbox › swipe › Summarize and Inbox › Select › Summarize sent recordings to the paid AI without showing the
    cost (the select bar even said "the cost is confirmed per recording"). One confirm with the total, as on Talks.
    Private recordings stay on the M14 (transcript only) and aren't counted."""
    todo, cost, minutes, private = [], 0.0, 0.0, 0
    for rec_id in _talk_ids(ids):
        rec = conn.execute("SELECT status, duration_s, private FROM recording WHERE id = ?", (rec_id,)).fetchone()
        r = conn.execute("SELECT payload FROM review_item WHERE kind = 'approval' AND status = 'open' AND ref_id = ? "
                         "AND conversation_id IS NULL ORDER BY id DESC LIMIT 1", (rec_id,)).fetchone()
        if rec is None or (r is None and rec["status"] != "backlog"):
            continue   # already sent or gone
        p = json.loads(r["payload"] or "{}") if r else {}
        todo.append(rec_id)
        if p.get("private") or rec["private"]:
            private += 1
            continue
        cost += p["ai_cost"] if r and p.get("ai_cost") is not None else ai_cost(rec["duration_s"])
        minutes += (rec["duration_s"] or 0) / 60
    n = len(todo)
    model = ai_router.task_config(conn, "extract")["chain"][0][1]
    return confirm_page(request, conn, "inbox", heading=f"Summarize {n} recording{'s' if n != 1 else ''}?", cost=cost,
                        model=model, tokens=round(minutes * TOKENS_PER_AUDIO_MINUTE + 2000 * (n - private), -2) if minutes else None,
                        blocked=None if n else "these were already sent or are gone",
                        detail="Speech-to-text runs on the M14. Then each transcript's text goes to "
                               f"{model_label(model)} for the summary and tasks. The audio never leaves the M14."
                               + (f" {private} Private recording{'s stay' if private != 1 else ' stays'} on the M14 (transcript only)." if private else ""),
                        action="/inbox/bulk", cancel="/inbox", fields={"ids": ",".join(map(str, todo)), "what": "ai"})


@router.post("/inbox/bulk")
def inbox_bulk(request: Request, ids: str = Form(...), what: str = Form(...), meeting_type: str = Form(""),
               confirm: str = Form(""), conn: sqlite3.Connection = Conn):
    """Several Inbox recordings at once (Brian, 2026-11-22: picking older ones and tapping Transcript did nothing, they
    needed "Get it ready" first). An older recording is got ready on the way, so AI notes / Transcript only / Delete
    work on any mix. AI notes only from a button that shows the cost (confirm=1) or the confirm step (R5-24)."""
    if what not in ("ai", "local", "skip"):
        raise HTTPException(400, "Pick Summarize, Transcribe or Delete")
    if what == "ai" and not confirmed(confirm):
        return RedirectResponse(f"/inbox-bulk/ai?ids={','.join(x.strip() for x in ids.split(',') if x.strip().isdigit())}", status_code=303)
    if what != "skip" and (stop := demo_stop(request, "run", "/inbox")):
        return stop
    for rec_id in _id_list(ids):
        ingest.backlog_to_review(conn, rec_id, request.app.state.cfg)   # does nothing unless it's an older recording
        r = conn.execute("SELECT id FROM review_item WHERE kind = 'approval' AND status = 'open' AND ref_id = ? "
                         "AND conversation_id IS NULL ORDER BY id DESC LIMIT 1", (rec_id,)).fetchone()
        if not r:
            continue
        if what == "skip":
            review_dismiss(request, r["id"], conn)
        else:
            review_approve(request, r["id"], ai="1" if what == "ai" else "0", confirm="1", meeting_type=meeting_type, conn=conn)
    return back(request, "/inbox", note=None if what == "skip" else "transcribing")


@router.post("/recordings/{rec_id}/review")
def older_to_review(request: Request, rec_id: int, conn: sqlite3.Connection = Conn):
    """Older recordings: put one in Review like a new recording (nothing runs until Brian approves it there)."""
    if not ingest.backlog_to_review(conn, rec_id, request.app.state.cfg):
        raise HTTPException(404, "Not an older recording (already processed or missing)")
    return back(request, "/review")


def ai_cost(duration_s: float | None) -> float:
    """The AI summary's estimated cost for a recording this long (the same rate the ask-first request uses)."""
    return round((duration_s or 0) / 60 * ingest.AI_USD_PER_AUDIO_MINUTE, 3)


def totals(rows: list[dict]) -> dict | None:
    """All of these together ("All 14: ~4 h 10 min · ~$0.80"), when there's more than one."""
    if len(rows) < 2:
        return None
    return {k: sum(r["est"][k] for r in rows) for k in ("ai_s", "local_s", "cost")}


def estimate(conn: sqlite3.Connection, conversation_id: int | None) -> float | None:
    """Cost preview before a cloud run."""
    if conversation_id is None:
        return None
    return extract.estimate_cost(conn, conversation_id)["cost_usd_est"]


def review_or_404(conn: sqlite3.Connection, rid: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM review_item WHERE id = ?", (rid,)).fetchone()
    if r is None:
        raise HTTPException(404, "That item isn't waiting any more")
    return r


@router.post("/review/{rid}/restore")
def review_restore(request: Request, rid: int, conn: sqlite3.Connection = Conn):
    """Dismissed by mistake: back under Needs you as it was. A skipped recording waits for its OK again; a removed Inbox
    task comes back to the Inbox."""
    r = review_or_404(conn, rid)
    if r["status"] != "dismissed":
        raise HTTPException(400, "Only dismissed items can be restored")
    with db.transaction(conn):
        if r["kind"] == "approval" and not r["conversation_id"] and r["ref_id"]:
            if not conn.execute("UPDATE recording SET status = 'awaiting_approval' WHERE id = ? AND status = 'declined'",
                                (r["ref_id"],)).rowcount:
                raise HTTPException(409, "That recording has been processed since, so there's nothing to restore")
        if r["kind"] == "inbox" and r["ref_id"]:
            t = conn.execute("SELECT * FROM task WHERE id = ?", (r["ref_id"],)).fetchone()
            if t and t["status"] == "dropped":
                conn.execute("UPDATE task SET status = 'open', inbox = 1, rated = NULL, updated_at = ? WHERE id = ?",
                             (db.now(), t["id"]))
                index(conn, "task", t["id"], t["conversation_id"], t["title"])
        conn.execute("UPDATE review_item SET status = 'open', resolved_at = NULL, snoozed_until = NULL WHERE id = ?", (rid,))
    return back(request, "/review?tab=dismissed")


@router.post("/review/{rid}/dismiss")
def review_dismiss(request: Request, rid: int, conn: sqlite3.Connection = Conn):
    r = review_or_404(conn, rid)
    if r["status"] == "dismissed":   # F75: a second tap (or an old page): skipped already, no second Undo of nothing
        return back(request, "/review")
    if r["status"] != "open":   # F75: an old page: it was approved or answered since; Skip would rewrite that as skipped
        raise HTTPException(409, "That was already done on another page or device. Reload to see where it is now.")
    with db.transaction(conn):
        undo.save(conn, "Moved to Trash" if r["kind"] == "approval" else "Skipped", [("review_item", rid)] + ([("recording", r["ref_id"])] if r["kind"] == "approval" and r["ref_id"] else []))
        if r["kind"] == "approval" and not r["conversation_id"] and r["ref_id"]:  # a waiting recording: leave it unprocessed
            conn.execute("UPDATE recording SET status = 'declined' WHERE id = ? AND status = 'awaiting_approval'", (r["ref_id"],))
        conn.execute("UPDATE review_item SET status = 'dismissed', resolved_at = ? WHERE id = ?", (db.now(), rid))
    return back(request, "/review")


def spends_money(r: sqlite3.Row, ai: str) -> bool:
    """Approving this review item would send text to a paid AI."""
    if r["kind"] != "approval":
        return False
    return bool(r["conversation_id"]) or ai != "0"


# A Recorder title with one of these words starts the approve step's type picker on Learning (Brian can change it).
LEARNING_WORDS = re.compile(r"\b(class|lecture|sermon|podcast|talk|training|course|seminar|webinar|lesson|learning)\b", re.I)


@router.get("/review/{rid}/confirm", response_class=HTMLResponse)
def review_confirm(request: Request, rid: int, meeting_type: str = "", conn: sqlite3.Connection = Conn):
    r = review_or_404(conn, rid)
    if r["status"] != "open":   # F82: Back, or an old link, asked again to pay for one that was sent or skipped since
        raise HTTPException(409, "Already approved: Home shows how far along it is." if r["status"] == "done" else
                            "That was moved to Trash since. Restore it there first." if r["kind"] == "approval"
                            and not r["conversation_id"] else "That was dismissed since. Restore it under Review › Dismissed first.")
    if not spends_money(r, "1"):
        raise HTTPException(400, "Nothing to confirm")
    if r["conversation_id"]:
        est = extract.estimate_cost(conn, r["conversation_id"])
        return confirm_page(request, conn, "review", heading="Run the AI on this talk?", cost=est["cost_usd_est"],
                            model=est["model"], tokens=est["tokens_in_est"], blocked=est["blocked"],
                            detail="Sends the transcript text (never the audio).", action=f"/review/{rid}/approve", cancel="/review")
    payload = json.loads(r["payload"] or "{}")
    model = ai_router.task_config(conn, "extract")["chain"][0][1]
    minutes = payload.get("minutes") or 0
    dur = conn.execute("SELECT duration_s FROM recording WHERE id = ?", (r["ref_id"],)).fetchone()
    m14 = round(progress.estimate(conn, dur[0])["total_s"] / 60) if dur and dur[0] else payload.get("m14_minutes")
    return confirm_page(request, conn, "review", heading="Transcribe and summarise with AI?", cost=payload.get("ai_cost") or 0.0,
                        model=model, tokens=round(minutes * TOKENS_PER_AUDIO_MINUTE + 2000, -2) if minutes else None,
                        detail=f"Speech-to-text runs on the M14 (about {m14} min). Then the transcript text goes to "
                               f"{model_label(model)} for the summary and tasks. The audio never leaves the M14.",
                        action=f"/review/{rid}/approve", cancel="/review", fields={"ai": "1"},
                        type_pick=meeting_type if meeting_type in prompts.MEETING_TYPES else "")


@router.post("/review/{rid}/approve")
def review_approve(request: Request, rid: int, ai: str = Form("1"), confirm: str = Form(""), meeting_type: str = Form(""),
                   conn: sqlite3.Connection = Conn):
    """Glossary suggestion -> approved term. Cloud approval -> the AI run is queued as approved (dismiss = transcript only).
    Waiting recording (ask-first mode) -> processing queued; ai=0 means transcript only. Anything that spends money must
    come from the confirm step (confirm=1), otherwise Brian is sent there first."""
    r = review_or_404(conn, rid)
    if r["status"] == "done":   # F72: a second tap or an old page: it was sent already, so nothing is queued (or paid) twice
        return back(request, "/review")
    if r["status"] != "open":
        raise HTTPException(409, "That recording was moved to Trash since. Restore it there first."
                            if r["kind"] == "approval" and not r["conversation_id"] else "That was dismissed since. Restore it under Review › Dismissed first.")
    if spends_money(r, ai) and not confirmed(confirm):
        return RedirectResponse(f"/review/{rid}/confirm", status_code=303)
    if r["kind"] == "approval" and (stop := demo_stop(request, "ai" if r["conversation_id"] else "run", "/review")):
        return stop
    with db.transaction(conn):
        if r["kind"] == "approval" and not r["conversation_id"] and r["ref_id"]:
            control.approve_recording(conn, r, ai != "0", meeting_type if meeting_type in prompts.MEETING_TYPES else None)
        if r["kind"] == "glossary" and r["ref_id"]:
            conn.execute("UPDATE glossary_term SET approved = 1 WHERE id = ?", (r["ref_id"],))
        if r["kind"] == "approval" and r["conversation_id"]:
            ai_jobs.queue_rerun(conn, r["conversation_id"], approved=True)
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE id = ?", (db.now(), rid))
    note = ("transcribing" if r["kind"] == "approval" and not r["conversation_id"] and r["ref_id"]
            else "summarizing" if r["kind"] == "approval" and r["conversation_id"] else None)
    return back(request, "/review", note=note)


# ---------- Search ----------

def fts_query(q: str) -> str:
    """User text -> safe FTS5 query: every word quoted (no operators), prefix match on the last one."""
    words = re.findall(r"[\w'-]+", q)
    if not words:
        return ""
    parts = ['"' + w.replace('"', "") + '"' for w in words]
    parts[-1] += "*"
    return " ".join(parts)


SEARCH_TABS = {"all": "All", "segment": "Lines", "task": "Tasks", "decision": "Decisions", "fact": "Notes", "person": "People"}
SEARCH_GROUPS = 20   # conversations per "Show more" step on Search
HITS_SHOWN = 3       # best hits shown per conversation; "All N in this conversation" opens the rest
HISTORY_SHOWN = 10   # recent searches under the box
COMMON_HITS = 5000   # a word this common ("okay", "the") isn't grouped: the newest 100 hits instead (grouping reads every hit)
SEARCH_FROM = "FROM search_index s LEFT JOIN conversation c ON c.id = s.conversation_id"
SNIPPET = "snippet(search_index, 3, '[[', ']]', ' … ', 12)"
HIT_LINE = (   # where it was said, so a result opens right at that transcript line
    "CASE s.kind WHEN 'segment' THEN (SELECT idx FROM segment g WHERE g.id = s.ref_id) "
    "WHEN 'task' THEN (SELECT g.idx FROM task x JOIN segment g ON g.id = x.source_segment_id WHERE x.id = s.ref_id) "
    "WHEN 'decision' THEN (SELECT g.idx FROM decision x JOIN segment g ON g.id = x.source_segment_id WHERE x.id = s.ref_id) "
    "WHEN 'fact' THEN (SELECT g.idx FROM person_fact x JOIN segment g ON g.id = x.source_segment_id WHERE x.id = s.ref_id) END")
# F130: a line by a voice not named yet says "Speaker N" (the talk's own numbering), not nothing at all
HIT_SPEAKER = ("(SELECT coalesce(p.name, 'Speaker ' || (SELECT count(*) FROM speaker x WHERE x.conversation_id = sp.conversation_id "
               "AND x.id <= sp.id)) FROM segment g JOIN speaker sp ON sp.id = g.speaker_id LEFT JOIN person p ON p.id = sp.person_id "
               "WHERE s.kind = 'segment' AND g.id = s.ref_id)")


def search_where(request: Request, query: str, f: dict) -> tuple[str, list]:
    """The MATCH plus every filter (Work/Home, Trash, with, said by, dates, one conversation); the tab is added by the caller."""
    a, args = area_clause(area_of(request), "c.area")   # unfiled conversations and rows with none (people, captures): both
    where, args = [f"search_index MATCH ? AND (c.deleted_at IS NULL){a}"], [query, *args]
    if f["person"]:
        where.append("(EXISTS (SELECT 1 FROM speaker sp WHERE sp.conversation_id = s.conversation_id AND sp.person_id = ?) "
                     "OR (s.kind = 'person' AND s.ref_id = ?))")
        args += [f["person"], f["person"]]
    if f["said"]:
        where.append("s.kind = 'segment' AND EXISTS (SELECT 1 FROM segment g JOIN speaker sp ON sp.id = g.speaker_id "
                     "WHERE g.id = s.ref_id AND sp.person_id = ?)")
        args.append(f["said"])
    for key, op in (("start", ">="), ("end", "<=")):   # dates leave out people and captures (they have no meeting date)
        if f[key]:
            where.append(f"substr(coalesce(c.started_at, c.created_at), 1, 10) {op} ?")
            args.append(f[key])
    if f["conv"]:
        where.append("s.conversation_id = ?")
        args.append(f["conv"])
    return " AND ".join(where), args


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", type: str = "all", person: int | None = None, said: int | None = None,
                start: str = "", end: str = "", sort: str = "best", conv: int | None = None, n: int = SEARCH_GROUPS,
                conn: sqlite3.Connection = Conn):
    """Results grouped by conversation (best hits first, "All N in this conversation" for the rest), tabs per kind with
    their counts, and filters. Two passes over the hits whatever the library size: counts + groups, then snippets."""
    tab = type if type in SEARCH_TABS else "all"
    f = {"person": person, "said": said, "start": start, "end": end, "conv": conv,
         "sort": sort if sort in ("best", "new", "old") else "best"}
    query = fts_query(q)
    if query and n == SEARCH_GROUPS and not conv and remembering(conn):   # a new search, not "Show more" or one conversation
        with db.transaction(conn):
            conn.execute("INSERT INTO search_history (q, last_at) VALUES (?, ?) "
                         "ON CONFLICT (q) DO UPDATE SET q = excluded.q, last_at = excluded.last_at", (q.strip()[:200], db.now()))
    # Recent searches: a drop-down under the box after a search; the list itself (with Show more) on the empty page.
    recent = conn.execute(f"SELECT q FROM search_history ORDER BY last_at DESC LIMIT {HISTORY_SHOWN}").fetchall() if query else []
    ctx = {"q": q, "tab": tab, "f": f, "search_tabs": SEARCH_TABS, "people": people(conn), "counts": {}, "groups": [], "other": [],
           "recent": recent, "history_on": remembering(conn)}
    if not query:
        history, total = paged(conn, "SELECT q, last_at FROM search_history ORDER BY last_at DESC", [],
                               HISTORY_SHOWN if n == SEARCH_GROUPS else n, step=HISTORY_SHOWN)
        return render(request, "search.html", "search", conn, **ctx, history=history, history_total=total)
    where, args = search_where(request, query, f)
    kind, kargs = ("", []) if tab == "all" else (" AND s.kind = ?", [tab])
    n_hits = conn.execute("SELECT count(*) FROM search_index WHERE search_index MATCH ?", (query,)).fetchone()[0]   # index only
    if n_hits > COMMON_HITS and not conv:   # stays fast however common the word: newest first, stops after 100
        hits = conn.execute(f"SELECT s.kind, s.ref_id, s.conversation_id AS cid, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, {SNIPPET} AS snip, "
                            f"{HIT_LINE} AS line, {HIT_SPEAKER} AS said_by {SEARCH_FROM} WHERE {where}{kind} "
                            "ORDER BY s.rowid DESC LIMIT 100", [*args, *kargs]).fetchall()
        return render(request, "search.html", "search", conn, **{**ctx, "common": n_hits, "hits": hits})
    if conv:   # one conversation: every hit, in transcript order
        rows, total = paged(conn, f"SELECT s.kind, s.ref_id, s.conversation_id AS cid, {SNIPPET} AS snip, {HIT_LINE} AS line, "
                            f"{HIT_SPEAKER} AS said_by {SEARCH_FROM} WHERE {where}{kind} ORDER BY line IS NULL, line", [*args, *kargs], n)
        c = conn.execute("SELECT id, title FROM conversation WHERE id = ?", (conv,)).fetchone()
        return render(request, "search.html", "search", conn, **{**ctx, "conv": c, "hits": rows, "total": total})
    per = conn.execute(f"SELECT s.conversation_id AS cid, s.kind, count(*) AS hits, min(s.rank) AS best, c.title, c.area, c.status, "
                       f"coalesce(c.started_at, c.created_at) AS at {SEARCH_FROM} WHERE {where} GROUP BY s.conversation_id, s.kind",
                       args).fetchall()
    counts = {k: sum(r["hits"] for r in per if k in ("all", r["kind"])) for k in SEARCH_TABS}
    groups: dict[int, dict] = {}
    for r in per:
        if r["cid"] is not None and r["kind"] != "person" and tab in ("all", r["kind"]):
            g = groups.setdefault(r["cid"], {"id": r["cid"], "title": r["title"], "at": r["at"], "hits": 0, "best": r["best"], "rows": [],
                                             "area": r["area"], "status": r["status"]})
            g["hits"] += r["hits"]
            g["best"] = min(g["best"], r["best"])
    order = sorted(groups.values(), key=lambda g: g["best"]) if f["sort"] == "best" else \
        sorted(groups.values(), key=lambda g: g["at"] or "", reverse=f["sort"] == "new")
    shown = order[:max(SEARCH_GROUPS, min(n, 1000))]
    if shown:
        ids = [g["id"] for g in shown]
        marks = ",".join("?" * len(ids))
        top = [r[0] for r in conn.execute(
            "SELECT hid FROM (SELECT s.rowid AS hid, row_number() OVER (PARTITION BY s.conversation_id ORDER BY s.rank) AS k "
            f"{SEARCH_FROM} WHERE {where}{kind} AND s.kind != 'person' AND s.conversation_id IN ({marks})) WHERE k <= {HITS_SHOWN}",
            [*args, *kargs, *ids])]
        for h in conn.execute(f"SELECT s.kind, s.ref_id, s.conversation_id AS cid, {SNIPPET} AS snip, {HIT_LINE} AS line, "
                              f"{HIT_SPEAKER} AS said_by FROM search_index s WHERE search_index MATCH ? "
                              f"AND s.rowid IN ({','.join('?' * len(top))}) ORDER BY s.rank", [query, *top]):
            groups[h["cid"]]["rows"].append(h)
        for r in conn.execute("SELECT cid, group_concat(name, ', ') AS names FROM (SELECT DISTINCT s.conversation_id AS cid, p.name FROM speaker s "
                              f"JOIN person p ON p.id = s.person_id WHERE p.id != 1 AND s.conversation_id IN ({marks})) "
                              "GROUP BY cid", ids):
            groups[r["cid"]]["people"] = r["names"]
    other = []   # not in a conversation: people, captures, tasks you added yourself (top of the first page)
    if tab in ("all", "person", "task") and n == SEARCH_GROUPS and not (said or start or end):
        other = conn.execute(f"SELECT s.kind, s.ref_id, {SNIPPET} AS snip {SEARCH_FROM} WHERE {where}{kind} "
                             "AND (s.conversation_id IS NULL OR s.kind = 'person') ORDER BY s.kind != 'person', s.rank LIMIT 10",
                             [*args, *kargs]).fetchall()
    return render(request, "search.html", "search", conn, **{**ctx, "counts": counts, "groups": shown, "n_groups": len(order),
                                                             "other": other})


def remembering(conn: sqlite3.Connection) -> bool:
    return setting(conn, "search_history", "on") == "on"


@router.get("/search/advanced", response_class=HTMLResponse)
def search_advanced(request: Request, q: str = "", type: str = "all", person: int | None = None, said: int | None = None,
                    start: str = "", end: str = "", sort: str = "best", conn: sqlite3.Connection = Conn):
    """The fields of the desktop advanced search (Brian picked A4, 2026-11-23), loaded when ☷ opens; filled with the
    search being shown, if any."""
    return templates.TemplateResponse(request, "_adv_search.html", {
        "q": q, "tab": type if type in SEARCH_TABS else "all", "search_tabs": SEARCH_TABS, "people": people(conn),
        "f": {"person": person, "said": said, "start": start, "end": end, "sort": sort}})


@router.get("/search/history")
def search_history_page():
    """Recent searches live in Search itself now (old links land there)."""
    return RedirectResponse("/search", status_code=303)


@router.post("/search/history/remove")
def search_history_remove(request: Request, q: str = Form(...), conn: sqlite3.Connection = Conn):
    with db.transaction(conn):
        conn.execute("DELETE FROM search_history WHERE q = ?", (q,))
    return back(request, "/search")


@router.post("/search/history/clear")
def search_history_clear(request: Request, conn: sqlite3.Connection = Conn):
    with db.transaction(conn):
        conn.execute("DELETE FROM search_history")
    return back(request, "/search")


@router.post("/search/history/keep")
def search_history_keep(request: Request, on: str = Form("on"), conn: sqlite3.Connection = Conn):
    """Remember my searches: on, or off (which also clears what's there)."""
    with db.transaction(conn):
        set_setting(conn, "search_history", "on" if on == "on" else "off")
        if on != "on":
            conn.execute("DELETE FROM search_history")
    return back(request, "/search")


# ---------- Capture ----------

def save_upload(dest_dir: Path, upload: UploadFile, allowed: tuple[str, ...] | None = None) -> Path:
    name = Path(upload.filename or "upload").name  # drop any folders the browser sent
    name = re.sub(r"[^\w .()-]", "_", name).strip() or "upload"
    if allowed and Path(name).suffix.lower() not in allowed:
        raise HTTPException(400, f"Unsupported file type; use one of {', '.join(allowed)}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest, n = dest_dir / name, 1
    while dest.exists():
        dest = dest_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    with dest.open("wb") as f:
        while chunk := upload.file.read(1 << 20):
            f.write(chunk)
    return dest


@router.get("/capture", response_class=HTMLResponse)
def capture_page(request: Request, saved: str = "", conn: sqlite3.Connection = Conn):
    recent = conn.execute("SELECT * FROM capture ORDER BY created_at DESC LIMIT 20").fetchall()
    return render(request, "capture.html", "capture", conn, recent=recent, saved=saved)


@router.post("/capture/note")
def capture_note(text: str = Form(...), conn: sqlite3.Connection = Conn):
    if not text.strip():
        raise HTTPException(400, "Empty note")
    with db.transaction(conn):
        cid = conn.execute("INSERT INTO capture (kind, text, created_at) VALUES ('text', ?, ?)", (text.strip(), db.now())).lastrowid
        index(conn, "capture", cid, None, text.strip())
    return RedirectResponse("/capture?saved=note", status_code=303)


TRANSCRIPT_TYPES = (".txt",)


@router.post("/capture/known")
async def capture_known(request: Request, conn: sqlite3.Connection = Conn):
    """Before uploading, the browser sends each file's SHA-256 (Brian, 2026-11-23: a duplicate only showed up after its
    whole upload). Files Scribe Bot already has are skipped without sending them. Older never-processed recordings
    are left out here, so their upload still moves them into the Inbox."""
    try:
        body = await request.json()
        given = body.get("hashes", []) if isinstance(body, dict) else []
    except ValueError:   # R5-04: not JSON at all (an old page, a cut-off request): nothing known, upload as usual
        given = []
    hashes = [h for h in (given if isinstance(given, list) else []) if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h)][:500]
    out = {}
    for h in hashes:
        row = conn.execute("SELECT id, status FROM recording WHERE sha256 = ? AND audio_deleted_at IS NULL", (h,)).fetchone()
        if row and row["status"] != "backlog":   # removed for good = new again (Brian, 2026-11-23)
            out[h] = {"result": "known", **known_result(conn, row["id"])}
    return JSONResponse({"known": out})


@router.post("/capture/upload")
def capture_upload(request: Request, file: list[UploadFile] = File(...), ai: str = Form(""), conn: sqlite3.Connection = Conn):
    """Audio uploads (and in-app voice notes) go to data/uploads/, which the M2 ingest watches alongside the phone folder.
    Not into inbound/: that is Syncthing's receive-only folder, where local additions would be flagged.
    Several at once (e.g. Apple Voice Memos dragged out of the app, 2026-11-20), and a .txt with the same name as a
    recording is its transcript for the Inbox (replaces one with the same name).
    The app's uploader sends one file at a time and asks for JSON (Brian, 2026-11-23: 23 memos showed a stuck screen and
    no result), so it can show "3 of 23", then what happened to each file, duplicates included."""
    if public_demo():   # D-11: nothing is saved; the uploader shows "Not added (demo)" on each file
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"results": [{"name": f.filename, "result": "demo"} for f in file if f.filename]})
        return demo_stop(request, "upload", "/capture")
    up, results = request.app.state.cfg.data_dir / "uploads", []
    for f in file:
        if not f.filename:
            continue
        if Path(f.filename).suffix.lower() in TRANSCRIPT_TYPES:
            name = re.sub(r"[^\w .()-]", "_", Path(f.filename).name).strip() or "transcript.txt"
            up.mkdir(parents=True, exist_ok=True)
            (up / name).write_bytes(f.file.read(2_000_000))
            results.append({"name": f.filename, "result": "transcript"})
            continue
        saved = save_upload(up, f, UPLOAD_TYPES + TRANSCRIPT_TYPES)
        res = audio_result(request, conn, saved)
        if res["result"] == "audio" and ai == "1":   # G2 (Brian, 2026-11-23): "Transcribe + AI notes" chosen on the upload card
            ingest.want_ai(conn, ingest.sha256(saved))
        results.append({"name": f.filename, **res})
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"results": results})
    said = [r["result"] for r in results]
    audio = [x for x in said if x != "transcript"]
    return RedirectResponse(f"/capture?saved={audio[-1] if audio else 'transcript' if said else 'none'}"
                            f"&n={len(audio)}", status_code=303)


@router.post("/capture/share")
def capture_share(request: Request, title: str = Form(""), text: str = Form(""), url: str = Form(""),
                  files: list[UploadFile] = File(default=[]), conn: sqlite3.Connection = Conn):
    """PWA share target (Android share sheet). Audio (Recorder → Share → Scribe Bot) goes to uploads/, the folder the
    pipeline watches, so it's processed like a synced recording (waits in Review; an identical file is skipped).
    Links, text and screenshots go to the Capture inbox (processed in Phase 3)."""
    if any(f.filename and Path(f.filename).suffix.lower() in UPLOAD_TYPES for f in files) and (
            stop := demo_stop(request, "upload", "/capture")):
        return stop
    audio: list[Path] = []
    body = "\n".join(x for x in (title.strip(), text.strip()) if x)
    with db.transaction(conn):
        if url.strip() or re.match(r"https?://\S+$", text.strip()):
            cid = conn.execute("INSERT INTO capture (kind, text, created_at) VALUES ('link', ?, ?)",
                               ("\n".join(x for x in (url.strip(), body) if x), db.now())).lastrowid
            index(conn, "capture", cid, None, "\n".join(x for x in (url.strip(), body) if x))
        elif body:
            cid = conn.execute("INSERT INTO capture (kind, text, created_at) VALUES ('text', ?, ?)", (body, db.now())).lastrowid
            index(conn, "capture", cid, None, body)
        for f in files:
            if not f.filename:
                continue
            if Path(f.filename).suffix.lower() in UPLOAD_TYPES:
                audio.append(save_upload(request.app.state.cfg.data_dir / "uploads", f, UPLOAD_TYPES))
                continue
            p = save_upload(request.app.state.cfg.data_dir / "captures", f)
            kind = "image" if (f.content_type or "").startswith("image/") else "voice" if p.suffix.lower() in UPLOAD_TYPES else "text"
            conn.execute("INSERT INTO capture (kind, payload_path, text, created_at) VALUES (?, ?, ?, ?)",
                         (kind, str(p), body or None, db.now()))
    shared = [received_audio(request, conn, p) for p in audio]   # after the transaction: moving one to Review opens its own
    return RedirectResponse("/capture?saved=" + (shared[-1] if shared else "share"), status_code=303)


def received_audio(request: Request, conn: sqlite3.Connection, path: Path) -> str:
    """What the Capture page should say about a shared/uploaded recording (see audio_result)."""
    r = audio_result(request, conn, path)["result"]
    return "known" if r == "pending" else r


def audio_result(request: Request, conn: sqlite3.Connection, path: Path) -> dict:
    """A shared/uploaded recording Scribe Bot may already have (e.g. an older one that synced earlier, or the same memo
    uploaded twice). 'audio' = new; 'older' = an unprocessed older recording, now in the Inbox; 'known' = already in
    Scribe Bot (with its talk, if there is one); 'pending' = the same file was just uploaded and is still being added.
    Our extra copy of a file we already have is removed, so nothing is ever added twice."""
    digest = ingest.sha256(path)
    row = conn.execute("SELECT id, status FROM recording WHERE sha256 = ? AND audio_deleted_at IS NULL", (digest,)).fetchone()
    if row is None:   # never seen, or removed for good from Trash: a new talk (Brian, 2026-11-23)
        size = path.stat().st_size
        twin = any(p != path and p.is_file() and p.stat().st_size == size and ingest.sha256(p) == digest
                   for p in path.parent.iterdir())
        if not twin:
            return {"result": "audio"}
        path.unlink(missing_ok=True)
        return {"result": "pending"}
    path.unlink(missing_ok=True)
    if row["status"] == "backlog" and ingest.backlog_to_review(conn, row["id"], request.app.state.cfg):
        return {"result": "older"}
    return known_result(conn, row["id"])


def known_result(conn: sqlite3.Connection, rec_id: int) -> dict:
    """What to show for a file Scribe Bot already has: its talk (or Trash, or the Inbox)."""
    row = {"id": rec_id}
    joined = conn.execute("SELECT recording_id FROM recording_part WHERE source_id = ? ORDER BY id DESC LIMIT 1",
                          (row["id"],)).fetchone()
    talk = conn.execute("SELECT id, deleted_at, coalesce(short_title, title) AS title FROM conversation WHERE recording_id = ? "
                        "AND purge_at IS NULL ORDER BY deleted_at IS NOT NULL, id DESC LIMIT 1",
                        (joined["recording_id"] if joined else row["id"],)).fetchone()
    if talk and talk["deleted_at"]:
        return {"result": "known", "title": talk["title"], "href": "/conversations?trash=1", "trash": True}
    if talk:
        return {"result": "known", "title": talk["title"], "href": f"/conversations/{talk['id']}"}
    return {"result": "known", "href": "/inbox"}


# ---------- Settings ----------

PROVIDERS = [("anthropic", "Anthropic (Claude)"), ("google", "Google (Gemini)"), ("openai", "OpenAI")]
ACCOUNT_TYPES = sorted({t for _, t in accounts.POLICIES})


def accounts_by_provider(request: Request, conn: sqlite3.Connection) -> list[dict]:
    """Accounts & Keys: every provider is listed, even with no account yet. Shows only whether a key exists, never the key."""
    groups = {p: {"key": p, "label": label, "arg": p, "accounts": []} for p, label in PROVIDERS}
    for a in accounts.list_accounts(conn, request.app.state.secrets):
        groups.setdefault(a["provider"], {"key": a["provider"], "label": a["provider"].capitalize(), "arg": a["provider"],
                                          "accounts": []})["accounts"].append(a)
    return list(groups.values())


# Settings › AI, R1 (Brian, 2026-11-20): one card per model; tapping a card picks the profile that starts with it.
# Only models from a company with a stored key are shown. "If it fails, try another company" appears with 2+ companies.
AI_CARDS = [("all_claude", ai_router.SONNET, "Best notes", 0.036), ("budget_mix", ai_router.HAIKU, "Cheapest · a bit less detail", 0.002),
            ("all_gemini", ai_router.ARGON, "Google's best", 0.03), ("all_openai", ai_router.SOL, "OpenAI's best", 0.03)]
SHORT_PROVIDER = {"anthropic": "Claude", "google": "Gemini", "openai": "ChatGPT"}
FALLBACK_OF = {"all_claude": "quality_mix", "quality_mix": "all_claude"}


def ai_cards(conn: sqlite3.Connection, providers: list[dict], current: str) -> dict:
    connected = {p["key"] for p in providers
                 if any(a["enabled"] and a.get("key_present") is not False for a in p["accounts"])}
    avg = {r[0]: r[1] for r in conn.execute(
        "SELECT model, sum(cost_usd) / count(DISTINCT conversation_id) FROM ledger_entry "
        "WHERE task = 'extract' AND status = 'ok' AND conversation_id IS NOT NULL GROUP BY model")}
    first = ai_router.PROFILES.get(current, {}).get("chains", {}).get("extract", [[None, None]])[0][1]
    cards = []
    for key, (prov, model), note, guess in AI_CARDS:
        if prov not in connected and key != current:
            continue
        usd = avg.get(model, guess)
        cards.append({"key": key, "name": ai_router.MODEL_NAMES.get(model, model), "note": note,
                      "cost": f"about ${usd:.3f}".rstrip("0") if usd < 0.01 else f"about ${usd:.2f}",
                      "on": model == first, "missing": prov not in connected})
    return {"cards": cards, "connected": len(connected), "fallback_on": current in ("quality_mix", "budget_mix"),
            "fallback_key": FALLBACK_OF.get(current), "missing": [SHORT_PROVIDER[k] for k, _ in PROVIDERS if k not in connected]}


# Plain-English name and time for each schedule (times match the Schedule(...) definitions).
SCHEDULE_INFO = {
    "export": ("Readable export", "daily 1:30 AM"), "trash-purge": ("Empty old trash", "daily 1:45 AM"),
    "backup": ("Encrypted backup", "daily 2:00 AM"), "reconcile": ("Check for missed recordings", "daily 2:30 AM"),
    "original_cleanup": ("Remove processed phone originals", "daily 3:00 AM"),
    "restore-test": ("Restore test", "monthly 3:00 AM"), "briefing": ("Morning briefing", "daily 7:00 AM"),
    "task-cleanup": ("Tidy old tasks", "Sundays 5:30 PM"), "weekly-review": ("Weekly review", "Sundays 6:00 PM"),
}


SETTINGS_GROUPS = {"appearance": ("Appearance", "sun"), "ai": ("AI & processing", "cloud"), "keys": ("Accounts & keys", "key"),
                   "privacy": ("Privacy & Data Ledger", "shield"), "glossary": ("Glossary", "book"),
                   "templates": ("Summary templates", "note"), "backups": ("Backups & schedules", "archive")}


def settings_home(request: Request, conn: sqlite3.Connection, ctx: dict) -> list[dict]:
    """One row per group with what it's set to now."""
    theme = request.cookies.get("theme") if request.cookies.get("theme") in ("light", "dark") else "system"
    last = conn.execute("SELECT status, finished_at FROM backup_run WHERE kind = 'backup' ORDER BY id DESC LIMIT 1").fetchone()
    n_accounts = conn.execute("SELECT count(*) FROM account").fetchone()[0]
    density = request.cookies.get("density") if request.cookies.get("density") in ("comfortable", "compact") else "default"
    subs = {"appearance": f"Theme: {theme.capitalize()} · Density: {density.capitalize()}",
            "ai": f"{'Paused' if ctx['paused'] else 'Running'} · AI spend ${ctx['month']:.2f} of ${ctx['cap']:.0f} this month",
            "keys": f"{n_accounts} account{'s' if n_accounts != 1 else ''}",
            "privacy": f"{ctx['left_today']} call{'s' if ctx['left_today'] != 1 else ''} left the M14 today",
            "glossary": f"{len(ctx['glossary'])} term{'s' if len(ctx['glossary']) != 1 else ''}",
            "templates": "What the AI is told for each meeting type",
            "backups": ("No backup yet" if not last else
                        f"Last backup {'OK' if last['status'] == 'ok' else 'failed'} · {local_datetime(last['finished_at'])}")}
    return [{"href": f"/settings/{k}", "title": t, "icon": ic, "sub": subs[k]} for k, (t, ic) in SETTINGS_GROUPS.items()]


@router.get("/settings", response_class=HTMLResponse)
@router.get("/settings/{group}", response_class=HTMLResponse)
def settings_page(request: Request, group: str = "home", tested: int | None = None, ok: int = 0, msg: str = "",
                  conn: sqlite3.Connection = Conn):
    """A short home of groups (/settings), each group on its own page (/settings/ai, /settings/keys, ...)."""
    if group == "status":   # old links from Discord
        return RedirectResponse("/status", status_code=303)
    if group != "home" and group not in SETTINGS_GROUPS:
        raise HTTPException(404, "No such settings page")
    ms = month_start()
    today0 = local_midnight()
    cap = float(setting(conn, "global_cap_usd", str(DEFAULT_CAP_USD)))
    month = spend(conn, ms)
    ledger = conn.execute("SELECT l.*, a.nickname, coalesce(nullif(c.short_title, ''), c.title) AS conv_title FROM ledger_entry l "   # console matches sent nothing
                          "LEFT JOIN account a ON a.id = l.account_id LEFT JOIN conversation c ON c.id = l.conversation_id "
                          "WHERE l.status != 'adjustment' ORDER BY l.time DESC LIMIT 50").fetchall()
    left_today = conn.execute("SELECT count(*) FROM ledger_entry WHERE time >= ? AND status = 'ok'", (today0,)).fetchone()[0]
    glossary = [{**dict(g), "variant_list": json.loads(g["variants"] or "[]")}
                for g in conn.execute("SELECT * FROM glossary_term ORDER BY approved, term")]
    added = request.query_params.get("added")
    templates_ = [{"kind": k, "label": label, "text": setting(conn, f"template:{k}", prompts.TEMPLATES[k]),
                   "custom": setting(conn, f"template:{k}") is not None} for k, label in prompts.MEETING_TYPES.items()]
    ctx = {"paused": setting(conn, "pipeline_paused", "0") == "1", "month": month, "all_time": spend(conn, "0000"), "cap": cap, "left_today": left_today, "glossary": glossary}
    return render(request, "settings.html", "settings", conn, group=group, group_title=SETTINGS_GROUPS.get(group, ("Settings",))[0],
                  settings_groups=settings_home(request, conn, ctx),
                  providers=(provs := accounts_by_provider(request, conn)), summary_templates=templates_,
                  ai=ai_cards(conn, provs, ai_router.profile(conn)) if group == "ai" else None,
                  account_types=ACCOUNT_TYPES, profile=ai_router.profile(conn),
                  paused=setting(conn, "pipeline_paused", "0") == "1",
                  quick_titles=setting(conn, "quick_titles", "1") != "0",
                  added={"ok": bool(ok), "message": msg} if added else None,
                  tested={"ok": bool(ok), "message": msg or ("Key works." if ok else "Key test failed.")} if tested else None,
                  cap=cap, month=month, all_time=ctx["all_time"], ledger=ledger, left_today=left_today,   # F15: was never passed, so it read $0.00
                  offline=ai_router.offline(conn),
                  backups=conn.execute("SELECT * FROM backup_run ORDER BY started_at DESC LIMIT 20").fetchall(),
                  schedules=sorted(conn.execute("SELECT * FROM schedule_run").fetchall(),
                                   key=lambda r: list(SCHEDULE_INFO).index(r["job_name"]) if r["job_name"] in SCHEDULE_INFO else 99),
                  schedule_info=SCHEDULE_INFO,
                  glossary=glossary)


CONSOLE_USAGE_URL = "https://console.anthropic.com/settings/usage"


def cost_days(conn: sqlite3.Connection, days: int = 30) -> dict:
    """B19 (Brian picked ⭐, 2026-11-25): the last 30 days of AI spend, one bar a day (the M14's own days), split by
    account. Spend matched to the Anthropic console and key tests (no account) count as "Other"."""
    end = date.today()
    start = end - timedelta(days=days - 1)
    since = datetime.combine(start, time()).astimezone(timezone.utc).isoformat(timespec="seconds")
    rows = conn.execute("SELECT l.time, l.cost_usd, a.nickname FROM ledger_entry l LEFT JOIN account a ON a.id = l.account_id "
                        "WHERE l.time >= ? AND l.cost_usd > 0", (since,)).fetchall()
    per: dict[date, dict[str, float]] = {}
    names: dict[str, float] = {}
    for t, usd, nick in rows:
        try:
            d = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone().date()
        except ValueError:
            continue
        if d < start or d > end:
            continue
        who = nick or "Other"
        per.setdefault(d, {})[who] = per.get(d, {}).get(who, 0) + usd
        names[who] = names.get(who, 0) + usd
    order = sorted(names, key=lambda n: (n == "Other", -names[n]))[:5]
    top = max((sum(v.values()) for v in per.values()), default=0)
    bars = []
    for i in range(days):
        d = start + timedelta(days=i)
        v = per.get(d, {})
        y, parts = 0.0, []
        for k, n in enumerate(order):
            usd = v.get(n, 0)
            if usd and top:
                h = usd / top * 60
                parts.append({"k": k, "y": round(70 - y - h, 2), "h": round(max(h, .8), 2)})
                y += h
        bars.append({"x": i * 10 + 1, "day": d, "usd": sum(v.values()), "parts": parts})
    total = sum(names.values())
    peak = max(bars, key=lambda b: b["usd"]) if total else None
    return {"bars": bars, "names": order, "totals": [names[n] for n in order], "top": top, "total": total, "peak": peak,
            "start": start, "end": end}


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, conn: sqlite3.Connection = Conn):
    """Every AI call with its cost (from the Data Ledger), grouped by conversation, plus today / month / all-time totals."""
    today0 = local_midnight()
    cap = float(setting(conn, "global_cap_usd", str(DEFAULT_CAP_USD)))
    rows = conn.execute(
        "SELECT l.*, coalesce(nullif(c.short_title, ''), c.title) AS conv_title FROM ledger_entry l LEFT JOIN conversation c ON c.id = l.conversation_id "
        "ORDER BY l.time DESC LIMIT 500").fetchall()
    by_conv = conn.execute(
        "SELECT l.conversation_id, coalesce(nullif(c.short_title, ''), c.title) AS conv_title, max(l.time) AS last, count(*) AS calls, sum(coalesce(l.tokens_in, 0)) AS tin, "
        "sum(coalesce(l.tokens_out, 0)) AS tout, round(sum(l.cost_usd), 4) AS usd, group_concat(DISTINCT l.model) AS models "
        "FROM ledger_entry l LEFT JOIN conversation c ON c.id = l.conversation_id GROUP BY l.conversation_id "
        "ORDER BY last DESC LIMIT 200").fetchall()
    return render(request, "costs.html", "costs", conn, rows=rows, by_conv=by_conv, cap=cap, today_spend=spend(conn, today0),
                  month=spend(conn, month_start()), all_time=spend(conn, "0000"), console_url=CONSOLE_USAGE_URL,
                  model_label=model_label, credits=costs.credits_left(conn), check=costs.console_check(conn),
                  matched=request.query_params.get("matched"), graph=cost_days(conn))


@router.post("/costs/console")
def costs_match_console(spent: str = Form(...), credits: str = Form(""), conn: sqlite3.Connection = Conn):
    """Brian typed what the Anthropic console shows; this month's spend now matches it (see costs.match_console)."""
    try:
        amount = float(spent.strip().lstrip("$"))
        left = float(credits.strip().lstrip("$")) if credits.strip() else None
    except ValueError:
        return RedirectResponse("/costs?matched=bad#console", status_code=303)
    gap = costs.match_console(conn, amount, left)
    return RedirectResponse(f"/costs?matched={gap:+.2f}#console", status_code=303)


@router.post("/settings/accounts")
def add_account(request: Request, provider: str = Form(...), nickname: str = Form(...), owner: str = Form("Brian"), type: str = Form("paid"),
                monthly_cap_usd: str = Form(""), conn: sqlite3.Connection = Conn):
    """Add an account row. The key is never entered here; Brian stores it with set_api_key.sh on the M14."""
    if stop := demo_stop(request, "keys", "/settings/keys"):
        return stop
    try:
        cap = float(monthly_cap_usd) if monthly_cap_usd.strip() else None
        name = nickname.strip() or provider
        accounts.add_account(conn, provider, name, owner.strip() or "Brian", type, cap)
        msg, ok = (f"Added {name}. If its key isn't stored yet, run bash ~/scribe-bot/scripts/setup/set_api_key.sh "
                   f"{provider} in Terminal on the M14."), 1
    except ValueError as e:
        msg, ok = f"Couldn't add that account: {e}", 0
    return RedirectResponse("/settings/keys?" + urlencode({"added": 1, "ok": ok, "msg": msg[:300]}), status_code=303)


@router.get("/settings/accounts/{aid}/test/confirm", response_class=HTMLResponse)
def test_account_confirm(request: Request, aid: int, conn: sqlite3.Connection = Conn):
    a = conn.execute("SELECT * FROM account WHERE id = ?", (aid,)).fetchone()
    if a is None:
        raise HTTPException(404, "No such account")
    from scribe_bot.ai import prices
    model = ai_router.PING_MODELS[a["provider"]]
    return confirm_page(request, conn, "settings", heading=f"Test the {a['nickname']} key?", cost=prices.cost(model, 20, 5),
                        model=model, tokens=25, detail="Sends the word 'ping' and expects 'OK' back. Logged in the Data Ledger.",
                        action=f"/settings/accounts/{aid}/test", cancel="/settings/keys")


@router.post("/settings/accounts/{aid}/test")
def test_account(request: Request, aid: int, confirm: str = Form(""), conn: sqlite3.Connection = Conn):
    """Test button: a tiny call (fractions of a cent, logged in the Data Ledger). The key itself never reaches the page."""
    if not conn.execute("SELECT 1 FROM account WHERE id = ?", (aid,)).fetchone():
        raise HTTPException(404, "No such account")
    if not confirmed(confirm):
        return RedirectResponse(f"/settings/accounts/{aid}/test/confirm", status_code=303)
    if stop := demo_stop(request, "keys", "/settings/keys"):
        return stop
    r = accounts.test_key(conn, aid)
    ok = bool(r.get("ok"))
    msg = f"Key works (test cost ${r.get('cost_usd', 0):.5f})." if ok else f"Key test failed: {r.get('error') or 'unexpected reply'}"
    return RedirectResponse("/settings/keys?" + urlencode({"tested": aid, "ok": int(ok), "msg": msg[:200]}), status_code=303)


@router.post("/settings/profile")
def set_profile(request: Request, profile: str = Form(...), conn: sqlite3.Connection = Conn):
    """Router profile (SPEC §8.2): which provider and models each AI task uses."""
    try:
        ai_router.set_profile(conn, profile)
    except ValueError:
        raise HTTPException(400, "Unknown profile")
    return back(request, "/settings/ai")


@router.post("/settings/offline")
def set_offline(request: Request, on: int = Form(...), conn: sqlite3.Connection = Conn):
    """Offline switch: the AI layer refuses every outbound call while it is on (SPEC §8.3)."""
    ai_router.set_offline(conn, bool(on))
    return back(request, "/settings/privacy")


@router.post("/settings/pause")
def set_pause(request: Request, on: int = Form(...), conn: sqlite3.Connection = Conn):
    """Pause/resume processing: while paused, new recordings wait (nothing is transcribed or sent); schedules still run.
    F125: the same pause as Processing's, so a Pause here also drops an earlier "Resume at 1 AM" (it used to keep it,
    and processing restarted at 1 AM on its own)."""
    control.pause(conn) if on else control.resume(conn, "now")
    return back(request, "/status")


@router.post("/processing/pause")
def processing_pause(request: Request, conn: sqlite3.Connection = Conn):
    """Pause (Brian, 2026-11-21): the worker stops at the next safe point; finished pieces are kept."""
    control.pause(conn)
    return back(request, "/")


@router.post("/processing/resume")
def processing_resume(request: Request, when: str = Form("now"), conn: sqlite3.Connection = Conn):
    control.resume(conn, "tonight" if when == "tonight" else "now")
    return back(request, "/")


@router.post("/processing/speed")
def processing_speed(request: Request, mode: str = Form("auto"), conn: sqlite3.Connection = Conn):
    """Auto / Full / Gentle / Night only (Brian, 2026-11-21; pipeline/pace.py)."""
    pace.set_mode(conn, mode)
    return back(request, "/processing#now")


@router.post("/processing/{job_id}/retry")
def processing_retry(request: Request, job_id: int, conn: sqlite3.Connection = Conn):
    """Try again from the failed step; finished steps (like the transcript) are kept."""
    if stop := demo_stop(request, "run", "/processing"):
        return stop
    if not queue.retry(conn, job_id):
        raise HTTPException(409, "Only a failed recording can be tried again.")
    return back(request, "/processing")


@router.post("/processing/{job_id}/cancel")
def processing_cancel(request: Request, job_id: int, conn: sqlite3.Connection = Conn):
    """Back to the Inbox, half-done files deleted (only while paused and before the transcript is saved)."""
    rec_id = control.cancel(conn, job_id, request.app.state.cfg.jobs_dir)
    if rec_id is None:
        raise HTTPException(409, "That one can't be cancelled now (it's running or nearly done). Pause it first.")
    return RedirectResponse(f"/inbox#rec-{rec_id}", status_code=303)


@router.post("/settings/quick-titles")
def set_quick_titles(request: Request, on: int = Form(...), conn: sqlite3.Connection = Conn):
    """Quick titles on/off (Brian, 2026-11-20): off stops the 2-minute speech work for new Inbox recordings."""
    set_setting(conn, "quick_titles", "1" if on else "0")
    return back(request, "/settings/ai#quick-titles")


@router.post("/settings/glossary")
def add_glossary(request: Request, term: str = Form(...), variants: str = Form(""), conn: sqlite3.Connection = Conn):
    v = [x.strip() for x in variants.split(",") if x.strip()]
    if not term.strip():
        raise HTTPException(400, "Term needed")
    conn.execute("INSERT INTO glossary_term (term, variants, approved, created_at) VALUES (?, ?, 1, ?) "
                 "ON CONFLICT (term) DO UPDATE SET variants = excluded.variants, approved = 1",
                 (term.strip(), json.dumps(v), db.now()))
    return back(request, "/settings/glossary")


@router.post("/settings/glossary/{gid}/delete")
def delete_glossary(request: Request, gid: int, conn: sqlite3.Connection = Conn):
    with db.transaction(conn):   # U12 (Brian, 2026-11-25): every delete can be undone
        if conn.execute("SELECT 1 FROM glossary_term WHERE id = ?", (gid,)).fetchone():
            undo.removed(conn, undo.save(conn, "Word removed", []), "glossary_term", "id = ?", (gid,))
        conn.execute("DELETE FROM glossary_term WHERE id = ?", (gid,))
    return back(request, "/settings/glossary")
