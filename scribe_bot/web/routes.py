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
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from scribe_bot.ai import accounts, costs, extract, jobs as ai_jobs, prompts, router as ai_router
from scribe_bot.backup.trash import TRASH_DAYS
from scribe_bot.core import cleanup, db, machine, people as people_svc, progress, talk
from scribe_bot.discord_bot import notify, sender
from scribe_bot.pipeline import identify, ingest
from scribe_bot.web import export, undo
from scribe_bot.web.app import area_of, local_datetime, templates, worker_status

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
    n = conn.execute(f"SELECT count(*) FROM {REVIEW_FROM} WHERE r.status = 'open' AND {AWAKE} AND {scope}",
                     [db.now(), *args]).fetchone()[0]
    a, targs = area_clause(area, "t.area")
    n += bool(m14) and not put_off(conn, "m14")
    n += not put_off(conn, "older") and bool(conn.execute("SELECT 1 FROM recording WHERE status = 'backlog' LIMIT 1").fetchone())
    n += bool(conn.execute(f"SELECT 1 FROM task t WHERE {cleanup.STALE} AND {LIVE_TASK}{a} LIMIT 1", targs).fetchone())
    n += conn.execute(f"SELECT count(*) FROM task t WHERE {REMIND_DUE} AND {LIVE_TASK}{a}", [db.now(), *targs]).fetchone()[0]
    return n


REMIND_DUE = "t.status = 'open' AND t.remind_at <= ?"   # a task reminder whose time has come (Needs you)


def render(request: Request, name: str, nav: str, conn: sqlite3.Connection | None = None, **ctx) -> HTMLResponse:
    area = area_of(request)
    m14 = machine.needs_attention(conn) if conn else []
    if conn:
        notify.tidy(conn)   # done things leave the bell before its count is read
    return templates.TemplateResponse(request, name, {"nav": nav, "area": area, "today": date.today().isoformat(),
                                                      "undo": undo.latest(conn) if conn else None,
                                                      "today_label": date.today().strftime("%A, %B %-d"),
                                                      "needs_count": needs_total(conn, area, m14) if conn else 0,
                                                      "head_alert": bool(m14), "now_iso": db.now(),
                                                      "n_trash": conn.execute("SELECT count(*) FROM conversation WHERE deleted_at "
                                                                              "IS NOT NULL").fetchone()[0] if conn else 0,
                                                      "n_older": conn.execute("SELECT count(*) FROM recording WHERE status = "
                                                                              "'backlog'").fetchone()[0] if conn else 0,
                                                      "bell_new": bell_new(conn) if conn else 0, **ctx})


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
    return RedirectResponse(n["link"] if n and n["link"] and n["link"].startswith("/") else "/notifications", status_code=303)


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
def notifications_test(conn: sqlite3.Connection = Conn):
    """Brian's check that pushes reach his phone: one harmless line to each Discord channel (the worker sends within
    ~5 s; quiet hours hold the #scribe-bot one until 7 AM). No content, like every notification."""
    with db.transaction(conn):
        for channel, name in (("bot", "#scribe-bot"), ("alerts", "#scribe-alerts")):
            notify.enqueue(conn, channel, f"🔔 Test notification from Scribe Bot ({name})", "/notifications")
    return RedirectResponse("/notifications?sent=1", status_code=303)


def as_dict(row) -> dict:
    return dict(row) if not isinstance(row, dict) else row


def back(request: Request, default: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


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
    return conn.execute("SELECT id, name FROM person ORDER BY id = 1 DESC, name").fetchall()


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
    return [{**dict(r), "name": r["person_name"] or f"Speaker {nums[r['id']]}", "seconds": secs.get(r["id"], 0),
             "share": secs.get(r["id"], 0) / total, "turns": stats.get(r["id"], {}).get("turns", 0)}
            for r in rows]


# ---------- Today ----------

def nudge(t) -> str:
    """A ready-to-paste reminder for a late promise: a plain local template (no AI). Brian copies it; nothing is sent."""
    first = (t["owner_name"] or "").split()[0] if t["owner_name"] else "there"
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
            "done": conn.execute(f"SELECT count(*) FROM task t WHERE t.status = 'done' AND t.owner_person_id = {ME} "
                                 f"AND t.updated_at >= ? AND {LIVE_TASK}{ta}", [since, *targs]).fetchone()[0]}


@router.get("/speaking", response_class=HTMLResponse)
def speaking_page(request: Request, conn: sqlite3.Connection = Conn):
    """Your speaking: pace, longest stretch, questions and fillers over the last 7 and 30 days (no AI)."""
    return render(request, "speaking.html", "more", conn, speaking={d: talk.speaking_trend(conn, d) for d in (7, 30)})


@router.get("/", response_class=HTMLResponse)
def today_page(request: Request, conn: sqlite3.Connection = Conn):
    area = area_of(request)
    a, args = area_clause(area, "t.area")
    today = date.today().isoformat()
    due = conn.execute(
        "SELECT t.*, c.title AS conv_title FROM task t LEFT JOIN conversation c ON c.id = t.conversation_id "
        f"WHERE t.status = 'open' AND t.inbox = 0 AND t.owner_person_id = 1{a} "
        f"AND {LIVE_TASK} ORDER BY t.due IS NULL, t.due, t.id LIMIT 50", args).fetchall()
    late = [t for t in due if t["due"] and t["due"] <= today]
    due = late + [t for t in due if t not in late][:max(0, 3 - len(late))]   # Next up: everything due, topped up to 3
    overdue_theirs = conn.execute(   # promises to Brian that are now late: worth a nudge (drafted by Brian, never sent for him)
        "SELECT t.*, p.name AS owner_name, c.title AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) "
        "AS line FROM task t JOIN person p ON p.id = t.owner_person_id LEFT JOIN conversation c ON c.id = t.conversation_id "
        f"WHERE t.status = 'open' AND t.inbox = 0 AND t.owner_person_id != 1 AND t.due IS NOT NULL AND t.due < ?{a} "
        f"AND {LIVE_TASK} ORDER BY t.due", [today, *args]).fetchall()
    overdue_theirs = [{**dict(t), "nudge": nudge(t)} for t in overdue_theirs]
    cap = float(setting(conn, "global_cap_usd", str(DEFAULT_CAP_USD)))
    month = spend(conn, month_start())
    awaiting = conn.execute("SELECT count(*) FROM recording WHERE status = 'awaiting_approval'").fetchone()[0]
    return render(request, "today.html", "today", conn, due=due, overdue_theirs=overdue_theirs, **needs_you(conn, area), week=week(request, conn),
                  on_today=True, people=people(conn),
                  jobs=progress.active_jobs(conn), awaiting=awaiting, month_spend=month, cap=cap,
                  spend_alert=cap and month / cap >= 0.8, worker=worker_status(request.app.state.cfg),
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
    if put_off(conn, "older"):
        if review["older"]:
            snoozed.append({"what": "Older recordings", "title": f"{len(review['older'])} older recording"
                            f"{'s' if len(review['older']) != 1 else ''} not processed yet", "until": setting(conn, "later:older"),
                            "now": "/needs/older/now"})
        review["older"] = []
    a, args = area_clause(area, "t.area")
    stale = conn.execute(f"SELECT count(*) FROM task t WHERE {cleanup.STALE} AND {LIVE_TASK}{a}", args).fetchone()[0]
    now, reminders = db.now(), []
    for t in conn.execute(   # task reminders: due ones under Needs you, later ones in Snoozed
            "SELECT t.*, p.name AS owner_name, c.title AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) "
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
            raise HTTPException(404, "No such open task")
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
    return render(request, "help.html", "help", conn, demo_id=demo[0] if demo else None, tour_conv=tour_conversation(conn))


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
    awaiting = conn.execute("SELECT count(*) FROM recording WHERE status = 'awaiting_approval'").fetchone()[0]
    return templates.TemplateResponse(request, "_progress.html", {"jobs": progress.active_jobs(conn), "awaiting": awaiting})


@router.get("/more", response_class=HTMLResponse)
def more_page(request: Request, conn: sqlite3.Connection = Conn):
    return render(request, "more.html", "more", conn)


# ---------- Conversations ----------

def conv_where(request: Request, person, type, start, end, unsorted) -> tuple[list[str], list]:
    """The Conversations filters as SQL (the list and its export use the same ones)."""
    area = area_of(request)
    where, args = ["c.deleted_at IS NULL"], []
    if unsorted:   # finished but not filed as Work or Home yet: their own list, one tap to file each
        where.append(UNSORTED)
    elif area != "all":   # still being processed: under both until it's filed
        where.append("(c.area = ? OR (c.area IS NULL AND c.status != 'done'))"); args.append(area)
    if person:
        where.append("EXISTS (SELECT 1 FROM speaker s WHERE s.conversation_id = c.id AND s.person_id = ?)"); args.append(person)
    if type:
        where.append("c.type = ?"); args.append(type)
    if start:
        where.append("substr(coalesce(c.started_at, c.created_at), 1, 10) >= ?"); args.append(start)
    if end:
        where.append("substr(coalesce(c.started_at, c.created_at), 1, 10) <= ?"); args.append(end)
    return where, args


@router.get("/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, person: int | None = None, type: str | None = None,
                       start: str | None = None, end: str | None = None, trash: int = 0, unsorted: int = 0, n: int = PAGE,
                       order: str = "newest", conn: sqlite3.Connection = Conn):
    if trash:
        rows = [{**dict(r), "gone_on": gone_on(r["deleted_at"])} for r in conn.execute(
            "SELECT * FROM conversation WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC")]
        return render(request, "conversations.html", "conversations", conn, rows=rows, trash=True, trash_days=TRASH_DAYS, f={})
    where, args = conv_where(request, person, type, start, end, unsorted)
    rows, total = paged(conn,
        "SELECT c.*, (SELECT group_concat(p.name, ', ') FROM speaker s JOIN person p ON p.id = s.person_id "
        "             WHERE s.conversation_id = c.id AND p.id != 1) AS people "
        f"FROM conversation c WHERE {' AND '.join(where)} ORDER BY {CONV_ORDERS.get(order, CONV_ORDERS['newest'])[1]}", args, n)
    n_unsorted = 0 if unsorted else conn.execute(f"SELECT count(*) FROM conversation c WHERE c.deleted_at IS NULL AND {UNSORTED}").fetchone()[0]
    return render(request, "conversations.html", "conversations", conn, rows=rows, total=total, people=people(conn),
                  unsorted=unsorted, n_unsorted=n_unsorted,
                  orders=CONV_ORDERS, f={"person": person, "type": type or "", "start": start or "", "end": end or "",
                                         "order": order if order in CONV_ORDERS else "newest"})


CONV_ORDERS = {   # Conversations › Sort: key -> (label, ORDER BY); SQL comes from here only, never from the request
    "newest": ("Newest", "coalesce(c.started_at, c.created_at) DESC"),
    "oldest": ("Oldest", "coalesce(c.started_at, c.created_at)"),
    "longest": ("Longest", "coalesce(c.duration_s, 0) DESC, coalesce(c.started_at, c.created_at) DESC"),
    "title": ("A–Z", "c.title COLLATE NOCASE, coalesce(c.started_at, c.created_at) DESC"),
}
UNSORTED = "c.area IS NULL AND c.status = 'done'"   # finished, not filed as Work or Home yet ("Not sorted")


def gone_on(deleted_at: str | None) -> str | None:
    """When a conversation in Trash is removed for good (the nightly purge after TRASH_DAYS)."""
    return (datetime.fromisoformat(deleted_at) + timedelta(days=TRASH_DAYS)).isoformat() if deleted_at else None


def get_conversation(conn: sqlite3.Connection, cid: int) -> sqlite3.Row:
    c = conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone()
    if c is None:
        raise HTTPException(404, "No such conversation")
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


@router.get("/conversations/export.{fmt}")
def conversations_export(request: Request, fmt: str, person: int | None = None, type: str | None = None, start: str | None = None,
                         end: str | None = None, order: str = "newest", transcript: int = 0, conn: sqlite3.Connection = Conn):
    """The Conversations list as filtered now, newest first by default; summaries only unless transcript=1."""
    where, args = conv_where(request, person, type, start, end, 0)
    ids = [r[0] for r in conn.execute(f"SELECT c.id FROM conversation c WHERE {' AND '.join(where)} ORDER BY "
                                      f"{CONV_ORDERS.get(order, CONV_ORDERS['newest'])[1]} LIMIT {EXPORT_MAX}", args)]
    body = "\n\n---\n\n".join(export.conversation_md(conn, i, transcript=bool(transcript)) for i in ids)
    return _md(f"# Scribe Bot export · {len(ids)} conversations\n\n" + body, f"Scribe Bot {date.today().isoformat()}", _fmt(fmt))


@router.get("/conversations/{cid}/export.{fmt}")
def conversation_export(cid: int, fmt: str, transcript: int = 1, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    return _md(export.conversation_md(conn, cid, transcript=bool(transcript)), c["title"], _fmt(fmt))


@router.get("/people/{pid}/export.{fmt}")
def person_export(pid: int, fmt: str, conn: sqlite3.Connection = Conn):
    p = conn.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise HTTPException(404, "No such person")
    return _md(export.person_md(conn, pid), p["name"], _fmt(fmt))


@router.get("/conversations/{cid}", response_class=HTMLResponse)
def conversation_page(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    if c["status"] == "done" and (c["opened_at"] is None or c["opened_at"] < c["updated_at"]):
        # its "New" badge goes and its Ready ping tidies itself (notify.tidy); no write on ordinary re-visits
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
    rec = conn.execute("SELECT coalesce(archive_path, vault_path), title FROM recording WHERE id = ?", (c["recording_id"],)).fetchone()
    has_audio = bool(rec and rec[0])
    mine = [t for t in tasks if t["owner_person_id"] == ME]
    theirs = [t for t in tasks if t["owner_person_id"] != ME]
    check = {"mine": len(mine), "theirs": len(theirs), "notes": len(facts),
             "unsure": sum(1 for t in tasks if t["status"] == "open"
                           and (t["inbox"] or (t["confidence"] is not None and t["confidence"] < 0.6))),
             "unnamed": sum(1 for s in speakers if s["person_id"] is None)}
    preview = None if not segments else extract.estimate_cost(conn, cid, ending_private=bool(c["private"]))
    processing = progress.latest_for_recording(conn, c["recording_id"])
    ai_calls = conn.execute(
        "SELECT model, count(*) AS calls, coalesce(sum(cost_usd), 0) AS cost FROM ledger_entry "
        "WHERE conversation_id = ? AND status = 'ok' GROUP BY model", (cid,)).fetchall()
    pace = talk.pace_by_speaker(conn, cid)
    return render(request, "conversation.html", "conversations", conn, c=c, short_speaker_s=SHORT_SPEAKER_SECONDS, person_colors=PERSON_COLORS, gone_on=gone_on(c["deleted_at"]),
                  speakers=speakers, segments=segments,
                  mine=mine, theirs=theirs, check=check, recording_title=rec["title"] if rec else None,
                  decisions=decisions, facts=grouped, n_facts=len(facts), takeaways=json.loads(c["takeaways"] or "[]"),
                  people=people(conn), has_audio=has_audio, preview=preview, rerun_pending=ai_jobs.pending_for(conn, cid),
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
    return re.sub(r"\b(\d{1,2})_(\d{2})\b", r"\1:\2", name or "")


templates.env.filters["model_label"] = model_label
templates.env.filters["ai_label"] = ai_label
templates.env.filters["recorder_title"] = recorder_title
templates.env.filters["hours"] = talk.hours_label
templates.env.globals["speaking_levels"] = talk.levels
templates.env.filters["area_label"] = lambda a: {"work": "Work", "personal": "Home"}.get(a, a or "")   # Q2: "Home" everywhere
templates.env.globals["chart_keys"] = {c[0] for c in machine.CHARTS}   # M14 tiles that open their history chart


def fragment_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true" and request.headers.get("HX-Boosted") != "true"


def confirm_page(request: Request, conn: sqlite3.Connection, nav: str, *, heading: str, cost: float, model: str | None,
                 tokens: int | None, detail: str, action: str, cancel: str, fields: dict | None = None,
                 blocked: str | None = None, type_pick: str | None = None) -> HTMLResponse:
    frag = fragment_request(request)
    return render(request, "confirm.html", nav, conn, layout="_bare.html" if frag else "base.html", fragment=frag,
                  heading=heading, cost=cost or 0.0, model=model_label(model), tokens=f"{tokens:,}" if tokens else None,
                  detail=detail, action=action, cancel=cancel, fields=fields or {}, blocked=blocked,
                  type_pick=type_pick, meeting_types=prompts.MEETING_TYPES)


def confirmed(confirm: str) -> bool:
    return confirm == "1"


@router.get("/conversations/{cid}/rerun/confirm", response_class=HTMLResponse)
def rerun_confirm(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    c = get_conversation(conn, cid)
    est = extract.estimate_cost(conn, cid, ending_private=bool(c["private"]))
    if c["private"]:   # "Transcript only" now, AI later (DECISIONS 2026-10-20): only from this step, and it ends Private
        return confirm_page(request, conn, "conversations", heading="Run the AI on this Private conversation?",
                            cost=est["cost_usd_est"], model=est["model"], tokens=est["tokens_in_est"], blocked=est["blocked"],
                            detail="So far it has never left the M14. This sends the transcript text (never the audio) "
                                   "for a summary, tasks and decisions, and the 🔒 Private mark comes off. The Data Ledger records it.",
                            action=f"/conversations/{cid}/rerun", cancel=f"/conversations/{cid}", fields={"end_private": "1"})
    return confirm_page(request, conn, "conversations", heading="Re-run the AI on this conversation?", cost=est["cost_usd_est"],
                        model=est["model"], tokens=est["tokens_in_est"], blocked=est["blocked"],
                        detail="Sends the transcript text (never the audio). Edits you already made are kept.",
                        action=f"/conversations/{cid}/rerun", cancel=f"/conversations/{cid}")


@router.post("/conversations/{cid}/rerun")
def rerun_ai(cid: int, confirm: str = Form(""), end_private: str = Form(""), conn: sqlite3.Connection = Conn):
    """Re-run AI, only from the confirm step (Brian saw the cost), so the run counts as approved. A Private conversation
    runs only from its own confirm step, which ends Private first (the AI never runs on a Private one)."""
    c = get_conversation(conn, cid)
    if not confirmed(confirm):
        return RedirectResponse(f"/conversations/{cid}/rerun/confirm", status_code=303)
    if c["private"]:
        if end_private != "1":
            raise HTTPException(400, "Private conversations go to the AI only after you end Private on the confirm step")
        with db.transaction(conn):
            conn.execute("UPDATE conversation SET private = 0, updated_at = ? WHERE id = ?", (db.now(), cid))
            if c["recording_id"]:
                conn.execute("UPDATE recording SET private = 0 WHERE id = ?", (c["recording_id"],))
    ai_jobs.queue_rerun(conn, cid, approved=True)
    return RedirectResponse(f"/conversations/{cid}?rerun=queued", status_code=303)


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
    label = "Renamed the conversation" if title is not None and summary is None and takeaways is None else "Edited the summary"
    with db.transaction(conn):
        undo.save(conn, label, [("conversation", cid)])
        conn.execute("UPDATE conversation SET title = ?, summary = ?, takeaways = ?, updated_at = ? WHERE id = ?",
                     (title.strip() if title is not None else c["title"],
                      (summary.strip() or None) if summary is not None else c["summary"],
                      json.dumps(points) if points is not None else c["takeaways"], db.now(), cid))
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
    if area not in ("work", "personal"):
        raise HTTPException(400, "area must be work or personal")
    get_conversation(conn, cid)
    with db.transaction(conn):
        conn.execute("UPDATE conversation SET area = ?, area_confidence = 1, updated_at = ? WHERE id = ?", (area, db.now(), cid))
        conn.execute("UPDATE task SET area = ? WHERE conversation_id = ?", (area, cid))
    return back(request, f"/conversations/{cid}")


@router.post("/conversations/{cid}/private")
def mark_private(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    """Private from now on (Brian DECISION 2026-10-22, replaces E's delete): AI notes already made are kept; nothing
    about this conversation goes to the AI again (re-runs blocked, pending approvals dropped)."""
    c = get_conversation(conn, cid)
    with db.transaction(conn):
        conn.execute("DELETE FROM review_item WHERE conversation_id = ? AND kind = 'approval' AND status = 'open'", (cid,))
        conn.execute("UPDATE conversation SET private = 1, updated_at = ? WHERE id = ?", (db.now(), cid))
        if c["recording_id"]:
            conn.execute("UPDATE recording SET private = 1 WHERE id = ?", (c["recording_id"],))
    return back(request, f"/conversations/{cid}")


@router.post("/conversations/{cid}/delete")
def trash_conversation(cid: int, conn: sqlite3.Connection = Conn):
    """Trash: the conversation and everything from it (tasks, notes, review items) are hidden until restored, or
    removed for good after TRASH_DAYS. Undo works like any other change."""
    c = get_conversation(conn, cid)
    with db.transaction(conn):
        undo.save(conn, f"Moved “{c['title']}” to Trash", [("conversation", cid)])
        conn.execute("UPDATE conversation SET deleted_at = ?, updated_at = ? WHERE id = ?", (db.now(), db.now(), cid))
    return RedirectResponse("/conversations", status_code=303)


@router.post("/conversations/{cid}/restore")
def restore_conversation(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    get_conversation(conn, cid)
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
    return FileResponse(p, media_type=AUDIO_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        headers={"Cache-Control": "private, no-store"})


@router.get("/audio/conversation/{cid}")
def conversation_audio(request: Request, cid: int, conn: sqlite3.Connection = Conn):
    """Plays the original while it's kept (30 days, D8), then the archive copy. Only formats browsers can play."""
    row = conn.execute("SELECT r.vault_path, r.archive_path FROM conversation c JOIN recording r ON r.id = c.recording_id "
                       "WHERE c.id = ?", (cid,)).fetchone()
    playable = [p for p in (row or []) if p and Path(p).suffix.lower() in AUDIO_TYPES and Path(p).is_file()]
    return audio_file(request, playable[0] if playable else None)


@router.get("/audio/speaker/{sid}")
def speaker_clip(request: Request, sid: int, conn: sqlite3.Connection = Conn):
    row = conn.execute("SELECT clip_path FROM speaker WHERE id = ?", (sid,)).fetchone()
    return audio_file(request, row[0] if row else None)


@router.post("/speakers/{sid}/name")
def name_speaker(request: Request, sid: int, person_id: int | None = Form(None), new_name: str = Form(""),
                 conn: sqlite3.Connection = Conn):
    """Name a speaker once; the M2 pipeline then learns their voiceprint so they're recognised next time."""
    if not conn.execute("SELECT 1 FROM speaker WHERE id = ?", (sid,)).fetchone():
        raise HTTPException(404, "No such speaker")
    with db.transaction(conn):
        person_id = person_from_form(conn, person_id, new_name)
        conn.execute("UPDATE speaker SET person_id = ?, confirmed = 1 WHERE id = ?", (person_id, sid))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'speaker' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), sid))
    identify.enroll(conn, sid, person_id, cfg=request.app.state.cfg)  # adds their voice to the voiceprint
    extract.relabel_speakers(conn, conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (sid,)).fetchone()[0])
    return back(request, "/review")


@router.post("/speakers/{sid}/unassign")
def unassign_speaker(request: Request, sid: int, conn: sqlite3.Connection = Conn):
    """"This isn't X": the speaker goes back to unnamed (and back into Review) so Brian can name them again."""
    sp = conn.execute("SELECT * FROM speaker WHERE id = ?", (sid,)).fetchone()
    if sp is None:
        raise HTTPException(404, "No such speaker")
    with db.transaction(conn):
        conn.execute("UPDATE speaker SET person_id = NULL, confirmed = 0, match_score = NULL WHERE id = ?", (sid,))
        if not conn.execute("SELECT 1 FROM review_item WHERE kind = 'speaker' AND ref_id = ? AND status = 'open'",
                            (sid,)).fetchone():
            conn.execute("INSERT INTO review_item (kind, conversation_id, ref_id, payload, created_at) VALUES ('speaker', ?, ?, ?, ?)",
                         (sp["conversation_id"], sid, json.dumps({"clip": sp["clip_path"]}), db.now()))
    return back(request, f"/conversations/{sp['conversation_id']}")


SHORT_SPEAKER_SECONDS = 30   # less talk than this: probably a split-off piece of another speaker (F7, 2026-10-21)


@router.post("/speakers/{sid}/fold")
def fold_speaker(request: Request, sid: int, into: int = Form(...), conn: sqlite3.Connection = Conn):
    """"Part of someone else": all of this speaker's lines (and talk time) move to another speaker in the same
    conversation. Voiceprints are untouched (a short piece is too little audio to learn from). Undo restores it."""
    sp = conn.execute("SELECT * FROM speaker WHERE id = ?", (sid,)).fetchone()
    if sp is None or into == sid or not conn.execute("SELECT 1 FROM speaker WHERE id = ? AND conversation_id = ?",
                                                     (into, sp["conversation_id"])).fetchone():
        raise HTTPException(400, "Pick another speaker from this conversation")
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
        raise HTTPException(404, "No such line")
    return render(request, "_line_edit.html", "conversations", conn, s=seg, speakers=speakers_of(conn, seg["conversation_id"]))


@router.post("/segments/{seg_id}/speaker")
def line_speaker(request: Request, seg_id: int, speaker_id: int | None = Form(None), new_name: str = Form(""),
                 conn: sqlite3.Connection = Conn):
    """Fix who said one line. Only that line moves (and its talk time); voiceprints are untouched: one line is too little audio."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "No such line")
    cid = seg["conversation_id"]
    with db.transaction(conn):
        uid = undo.save(conn, f"Changed who said line {seg['idx']}",
                        [("segment", seg_id), *(("speaker", r[0]) for r in conn.execute(
                            "SELECT id FROM speaker WHERE conversation_id = ?", (cid,)))])
        if new_name.strip():
            pid = people_svc.find_or_create(conn, new_name)
            row = conn.execute("SELECT id FROM speaker WHERE conversation_id = ? AND person_id = ? ORDER BY id LIMIT 1",
                               (cid, pid)).fetchone()
            if row:
                speaker_id = row["id"]
            else:
                speaker_id = conn.execute(
                    "INSERT INTO speaker (conversation_id, cluster, person_id, confirmed) "
                    "VALUES (?, (SELECT coalesce(max(cluster), -1) + 1 FROM speaker WHERE conversation_id = ?), ?, 1)",
                    (cid, cid, pid)).lastrowid
                undo.created(conn, uid, "speaker", speaker_id)
        elif not speaker_id or not conn.execute("SELECT 1 FROM speaker WHERE id = ? AND conversation_id = ?",
                                                (speaker_id, cid)).fetchone():
            raise HTTPException(400, "Pick a speaker or type a name")
        if speaker_id != seg["speaker_id"]:
            secs = max(0.0, seg["end_s"] - seg["start_s"])
            conn.execute("UPDATE speaker SET talk_seconds = max(0, talk_seconds - ?) WHERE id = ?", (secs, seg["speaker_id"]))
            conn.execute("UPDATE speaker SET talk_seconds = talk_seconds + ? WHERE id = ?", (secs, speaker_id))
            conn.execute("UPDATE segment SET speaker_id = ? WHERE id = ?", (speaker_id, seg_id))
    return back(request, f"/conversations/{cid}#L{seg['idx']}")


@router.post("/segments/{seg_id}/text")
def line_text(request: Request, seg_id: int, text: str = Form(...), heard: str = Form(""), correct: str = Form(""),
              conn: sqlite3.Connection = Conn):
    """Fix the words of one line (search follows). Optionally teach the glossary a mis-hearing for future AI runs."""
    seg = conn.execute("SELECT * FROM segment WHERE id = ?", (seg_id,)).fetchone()
    if seg is None:
        raise HTTPException(404, "No such line")
    if not text.strip():
        raise HTTPException(400, "A line can't be empty")
    with db.transaction(conn):
        undo.save(conn, f"Edited line {seg['idx']}", [("segment", seg_id)])
        conn.execute("UPDATE segment SET text = ? WHERE id = ?", (text.strip(), seg_id))
        index(conn, "segment", seg_id, seg["conversation_id"], text.strip())
        if heard.strip() and correct.strip():
            add_glossary_variant(conn, correct.strip(), heard.strip())
    return back(request, f"/conversations/{seg['conversation_id']}#L{seg['idx']}")


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
    if not undo.undo(conn, uid):
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
               conn: sqlite3.Connection = Conn):
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
    archived = request.query_params.get("archived") == "1"
    base, base_args = where, list(args)   # this tab, area and person, before the Open / Done / Old choice
    if show == "old":
        where += f" AND {cleanup.STALE}"
    elif show == "done":
        where += " AND t.status = 'done'" + ("" if archived else " AND t.archived_at IS NULL")
    else:
        where += " AND t.status = 'open'"
    rows, total = paged(conn,
        "SELECT t.*, p.name AS owner_name, c.title AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) AS line "
        "FROM task t JOIN person p ON p.id = t.owner_person_id LEFT JOIN conversation c ON c.id = t.conversation_id "
        f"WHERE {where} ORDER BY t.due IS NULL, t.due, t.created_at DESC", args, n, TASKS_PER_PAGE)
    counts = task_counts(request, conn)
    n_archived = conn.execute(f"SELECT count(*) FROM task t WHERE {base} AND t.status = 'done' AND t.archived_at IS NOT NULL",
                              base_args).fetchone()[0] if show == "done" and not archived else 0
    return render(request, "tasks.html", "tasks", conn, rows=rows, tab=tab, counts=counts, people=people(conn), person=person, show=show,
                  n_archived=n_archived, total=total)


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
                           (title.strip(), owner, due or None, area if area in ("work", "personal") else None, t, t)).lastrowid
        index(conn, "task", tid, None, title.strip())
    return back(request, "/tasks")


def task_or_404(conn: sqlite3.Connection, tid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM task WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such task")
    return row


@router.post("/tasks/{tid}/done")
def task_done(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    t = task_or_404(conn, tid)
    with db.transaction(conn):
        undo.save(conn, f"Marked “{t['title']}” done", [("task", tid)])
        conn.execute("UPDATE task SET status = 'done', rated = coalesce(rated, 'right'), updated_at = ? WHERE id = ?", (db.now(), tid))
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
    conn.execute("UPDATE task SET status = 'open', updated_at = ? WHERE id = ?", (db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/edit")
def task_edit(request: Request, tid: int, title: str = Form(...), owner: int = Form(ME), due: str = Form(""),
              conn: sqlite3.Connection = Conn):
    t = task_or_404(conn, tid)
    changed = (title.strip(), owner, due or None) != (t["title"], t["owner_person_id"], t["due"])
    with db.transaction(conn):
        undo.save(conn, f"Edited “{t['title']}”", [("task", tid)])
        conn.execute("UPDATE task SET title = ?, owner_person_id = ?, due = ?, rated = ?, updated_at = ? WHERE id = ?",
                     (title.strip(), owner, due or None, "wrong" if changed and t["conversation_id"] else t["rated"],
                      db.now(), tid))
        index(conn, "task", tid, t["conversation_id"], title.strip())
    return back(request, "/tasks")


@router.post("/tasks/{tid}/owner")
def task_owner(request: Request, tid: int, person_id: int | None = Form(None), new_name: str = Form(""),
               conn: sqlite3.Connection = Conn):
    """Change person: moves the task between You owe and They owe you. An AI task given to the wrong person counts as wrong."""
    t = task_or_404(conn, tid)
    with db.transaction(conn):
        owner = person_from_form(conn, person_id, new_name)
        if owner != t["owner_person_id"]:
            name = conn.execute("SELECT name FROM person WHERE id = ?", (owner,)).fetchone()[0]
            undo.save(conn, f"Moved “{t['title']}” to {'you' if owner == ME else name}", [("task", tid)])
            conn.execute("UPDATE task SET owner_person_id = ?, rated = ?, updated_at = ? WHERE id = ?",
                         (owner, "wrong" if t["conversation_id"] else t["rated"], db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/delete")
def task_delete(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    t = task_or_404(conn, tid)
    with db.transaction(conn):
        undo.save(conn, f"Removed “{t['title']}”", [("task", tid), *(("review_item", r[0]) for r in conn.execute(   # Undo reopens it
            "SELECT id FROM review_item WHERE kind = 'inbox' AND ref_id = ? AND status = 'open'", (tid,)))])
        conn.execute("UPDATE task SET status = 'dropped', rated = 'wrong', inbox = 0, updated_at = ? WHERE id = ?", (db.now(), tid))
        unindex(conn, "task", tid)
        conn.execute("UPDATE review_item SET status = 'dismissed', resolved_at = ? WHERE kind = 'inbox' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), tid))
    return back(request, "/tasks")


@router.post("/tasks/{tid}/sure")
def task_sure(request: Request, tid: int, conn: sqlite3.Connection = Conn):
    """"Unsure" → Looks right: the tag goes and the task leaves the Inbox."""
    t = task_or_404(conn, tid)
    with db.transaction(conn):
        undo.save(conn, f"“{t['title']}” marked as right", [("task", tid)])
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
                     (area if area in ("work", "personal") else None, db.now(), tid))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'inbox' AND ref_id = ? "
                     "AND status = 'open'", (db.now(), tid))
    return back(request, "/tasks?tab=inbox")


# ---------- Decisions ----------

def decision_rows(request: Request, conn: sqlite3.Connection, n: int = TASKS_PER_PAGE) -> tuple[list, int]:
    a, args = area_clause(area_of(request), "c.area")
    return paged(conn,
        "SELECT d.*, c.title AS conv_title, coalesce(c.started_at, c.created_at) AS at, "
        "(SELECT idx FROM segment g WHERE g.id = d.source_segment_id) AS line "
        "FROM decision d JOIN conversation c ON c.id = d.conversation_id "
        f"WHERE c.deleted_at IS NULL AND coalesce(d.rated, '') != 'wrong'{a} ORDER BY at DESC", args, n, TASKS_PER_PAGE)


@router.get("/decisions")
def decisions_page():
    return RedirectResponse("/tasks?tab=decisions", status_code=302)


@router.post("/decisions/{did}/wrong")
def decision_wrong(request: Request, did: int, conn: sqlite3.Connection = Conn):
    with db.transaction(conn):
        undo.save(conn, "Removed a decision", [("decision", did)])
        conn.execute("UPDATE decision SET rated = 'wrong' WHERE id = ?", (did,))
        unindex(conn, "decision", did)
    return back(request, "/decisions")


@router.post("/decisions/{did}/edit")
def decision_edit(request: Request, did: int, text: str = Form(...), conn: sqlite3.Connection = Conn):
    """Fix a decision's wording. Saved as rated 'right' (Brian's version), so a later AI re-run keeps it."""
    d = conn.execute("SELECT * FROM decision WHERE id = ?", (did,)).fetchone()
    if d is None:
        raise HTTPException(404, "No such decision")
    if not text.strip():
        raise HTTPException(400, "A decision can't be empty")
    with db.transaction(conn):
        undo.save(conn, "Edited a decision", [("decision", did)])
        conn.execute("UPDATE decision SET text = ?, rated = 'right' WHERE id = ?", (text.strip(), did))
        index(conn, "decision", did, d["conversation_id"], text.strip())
    return back(request, "/decisions")


@router.post("/decisions/{did}/right")
def decision_right(request: Request, did: int, conn: sqlite3.Connection = Conn):
    conn.execute("UPDATE decision SET rated = 'right' WHERE id = ?", (did,))
    return back(request, "/decisions")


# ---------- People ----------

# "You owe X": Brian's open tasks from conversations X spoke in (tasks don't record who they're for).
I_OWE_THEM = "EXISTS (SELECT 1 FROM speaker s WHERE s.conversation_id = t.conversation_id AND s.person_id = ?)"


PEOPLE_SORTS = {"recent": ("Last spoke", "p.last_spoken_at IS NULL, p.last_spoken_at DESC, p.name COLLATE NOCASE"),
                "talked": ("Most talked", "talked DESC, p.name COLLATE NOCASE"), "name": ("Name", "p.name COLLATE NOCASE")}
TALKED = "(SELECT coalesce(sum(s.talk_seconds), 0) FROM speaker s WHERE s.person_id = p.id)"


@router.get("/people", response_class=HTMLResponse)
def people_page(request: Request, q: str = "", sort: str = "recent", n: int = PAGE, conn: sqlite3.Connection = Conn):
    """Filter by name or alias, sort by last spoke / most talked / name, a page at a time. The counts are worked out
    only for the people shown, so a long list stays quick."""
    area = area_of(request)
    sort = sort if sort in PEOPLE_SORTS else "recent"
    where, args = ["1"], []
    if area != "all":
        where.append("p.area IN (?, 'both')"); args.append(area)
    if q.strip():
        where.append("(p.name LIKE ? OR p.aliases LIKE ?)"); args += [f"%{q.strip()}%"] * 2
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
                  sorts=PEOPLE_SORTS, duplicates=dups[:10], more_duplicates=len(dups) - 10)


@router.post("/people")
def add_person(request: Request, name: str = Form(...), area: str = Form("both"), conn: sqlite3.Connection = Conn):
    if not name.strip():
        raise HTTPException(400, "Name needed")
    with db.transaction(conn):
        pid = conn.execute("INSERT INTO person (name, area, created_at) VALUES (?, ?, ?)",
                           (name.strip(), area if area in ("work", "personal", "both") else "both", db.now())).lastrowid
        index(conn, "person", pid, None, name.strip())
    return RedirectResponse(f"/people/{pid}", status_code=303)


@router.get("/people/{pid}", response_class=HTMLResponse)
def person_page(request: Request, pid: int, n: int = PAGE, conn: sqlite3.Connection = Conn):
    p = conn.execute("SELECT * FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise HTTPException(404, "No such person")
    facts = conn.execute(
        "SELECT f.*, c.title AS conv_title FROM person_fact f LEFT JOIN conversation c ON c.id = f.conversation_id "
        f"WHERE f.person_id = ? AND coalesce(f.confirmed, 1) != 0 AND {live('f')} ORDER BY coalesce(f.date, f.created_at) DESC",
        (pid,)).fetchall()
    ta, targs = area_clause(area_of(request), "t.area")
    ca, cargs = area_clause(area_of(request), "c.area")
    open_tasks = ("SELECT t.*, c.title AS conv_title, (SELECT idx FROM segment g WHERE g.id = t.source_segment_id) AS line "
                  f"FROM task t LEFT JOIN conversation c ON c.id = t.conversation_id WHERE t.status = 'open' AND {LIVE_TASK} AND {{}}" + ta +
                  " ORDER BY t.due IS NULL, t.due, t.id")
    they_owe = conn.execute(open_tasks.format("t.owner_person_id = ?"), (pid, *targs)).fetchall()
    i_owe = conn.execute(open_tasks.format(f"t.owner_person_id = {ME} AND {I_OWE_THEM}"),
                         (pid, *targs)).fetchall() if pid != ME else []
    convs, n_convs = paged(conn,
        "SELECT DISTINCT c.* FROM conversation c JOIN speaker s ON s.conversation_id = c.id WHERE s.person_id = ? "
        f"AND c.deleted_at IS NULL{ca} ORDER BY coalesce(c.started_at, c.created_at) DESC", (pid, *cargs), n)
    voice = conn.execute("SELECT sum(sample_count) FROM voiceprint WHERE person_id = ?", (pid,)).fetchone()[0]
    return render(request, "person.html", "people", conn, p=p, facts=facts, they_owe=they_owe, i_owe=i_owe, convs=convs, n_convs=n_convs,
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
        raise HTTPException(404, "No such person")
    return render(request, "merge.html", "people", conn, pv=pv, from_id=pid, into_id=into)


@router.post("/people/{pid}/merge")
def merge_people(pid: int, into: int = Form(...), conn: sqlite3.Connection = Conn):
    try:
        people_svc.merge(conn, pid, into)
    except LookupError:
        raise HTTPException(404, "No such person")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/people/{into}", status_code=303)


@router.post("/people/{pid}/delete")
def delete_person(pid: int, conn: sqlite3.Connection = Conn):
    """Delete a person (people.delete says what happens to their lines and tasks). Undo works for 10 minutes."""
    p = conn.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()
    if p is None:
        raise HTTPException(404, "No such person")
    if pid == ME:
        raise HTTPException(400, "You can't delete yourself")
    with db.transaction(conn):
        uid = undo.save(conn, f"Deleted {p['name']}", people_svc.undo_rows(conn, pid))
        undo.removed(conn, uid, "person", "id = ?", (pid,))
        undo.removed(conn, uid, "voiceprint", "person_id = ?", (pid,))
        people_svc.delete(conn, pid)
    return RedirectResponse("/people", status_code=303)


# The same 8 colors the talk-time bar uses automatically (app.css .talk), so a picked color never clashes.
PERSON_COLORS = {"#2563eb": "Blue", "#ea580c": "Orange", "#16a34a": "Green", "#9333ea": "Purple", "#db2777": "Pink",
                 "#0891b2": "Teal", "#ca8a04": "Gold", "#dc2626": "Red"}


@router.post("/speakers/{sid}/color")
def speaker_color(request: Request, sid: int, color: str = Form(""), conn: sqlite3.Connection = Conn):
    """A speaker's color in this one conversation (talk bar + chip). Empty = automatic."""
    row = conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such speaker")
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
        raise HTTPException(404, "No such person")
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
                     (name.strip(), json.dumps(alias_list), area if area in ("work", "personal", "both") else "both",
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
        raise HTTPException(404, "No such note")
    if not value.strip():
        raise HTTPException(400, "A note can't be empty")
    with db.transaction(conn):
        undo.save(conn, "Edited a note", [("person_fact", fid)])
        conn.execute("UPDATE person_fact SET value = ?, confirmed = 1 WHERE id = ?", (value.strip(), fid))
        index(conn, "fact", fid, f["conversation_id"], f"{f['person_name'] or ''}: {value.strip()}")
    return back(request)


@router.post("/facts/{fid}/person")
def fact_person(request: Request, fid: int, person_id: int | None = Form(None), new_name: str = Form(""),
                conn: sqlite3.Connection = Conn):
    """Change person on a people note (the AI attached it to the wrong person). The note stays kept."""
    if not conn.execute("SELECT 1 FROM person_fact WHERE id = ?", (fid,)).fetchone():
        raise HTTPException(404, "No such note")
    with db.transaction(conn):
        pid = person_from_form(conn, person_id, new_name)
        name = conn.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()[0]
        undo.save(conn, f"Moved a note to {name}", [("person_fact", fid)])
        conn.execute("UPDATE person_fact SET person_id = ?, person_name = ? WHERE id = ?", (pid, name, fid))
    return back(request)


# ---------- Review ----------

def review_data(conn: sqlite3.Connection, area: str = "all", snoozed: bool = False) -> dict:
    """Open review items grouped by kind, ready to act on: the awake ones (Needs you), or the snoozed ones."""
    scope, args = review_scope(area)
    items = conn.execute(
        f"SELECT r.*, c.title AS conv_title FROM {REVIEW_FROM} WHERE r.status = 'open' AND {scope} "
        f"AND {'NOT ' if snoozed else ''}{AWAKE} ORDER BY r.created_at", [*args, db.now()]).fetchall()
    speakers, inbox, approvals, glossary, recordings = [], [], [], [], []
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
            dur = conn.execute("SELECT duration_s FROM recording WHERE id = ?", (payload["recording_id"],)).fetchone()
            d = dur[0] if dur and dur[0] else (payload.get("minutes") or 0) * 60
            recordings.append({"r": r, "payload": payload, "est": {**progress.choices(conn, [d])[0],
                                                                   "cost": payload.get("ai_cost") or ai_cost(d)},
                               "type_guess": "learning" if LEARNING_WORDS.search(payload.get("title") or "") else ""})
        elif r["kind"] == "approval":
            est = payload.get("estimate") or {}
            approvals.append({"r": r, "payload": payload, "cost": est.get("cost_usd", estimate(conn, r["conversation_id"]))})
        else:
            g = conn.execute("SELECT * FROM glossary_term WHERE id = ?", (r["ref_id"],)).fetchone()
            glossary.append({"r": r, "g": g, "payload": payload, "variants": json.loads(g["variants"] or "[]") if g else []})
    older = [] if snoozed else [dict(r) for r in conn.execute(
        "SELECT id, title, duration_s, received_at FROM recording WHERE status = 'backlog' ORDER BY received_at DESC, id DESC")]
    for r, e in zip(older, progress.choices(conn, [r["duration_s"] for r in older])):
        r["est"] = {**e, "cost": ai_cost(r["duration_s"])}
    return {"speakers": speakers, "inbox": inbox, "approvals": approvals, "glossary": glossary, "recordings": recordings,
            "older": older, "recordings_total": totals(recordings), "older_total": totals(older),
            "m14_wait": progress.queue_wait_s(conn) if recordings or older else 0,
            "ai_step_s": progress.rates(conn)["ai_extract"][1] if recordings or older else 0}


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
    rows = conn.execute(f"SELECT r.*, c.title AS conv_title FROM {REVIEW_FROM} WHERE r.status = 'open' AND NOT {AWAKE} "
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
    rows, total = paged(conn, f"SELECT r.*, c.title AS conv_title FROM {REVIEW_FROM} WHERE r.status = ? AND {scope} "
                        "ORDER BY coalesce(r.resolved_at, r.created_at) DESC", [tab, *args], n)
    return render(request, "review.html", "review", conn, **ctx, log=[log_row(conn, r) for r in rows], total=total)


REVIEW_TABS = {"open": "Open", "snoozed": "Snoozed", "done": "Done", "dismissed": "Dismissed"}


def log_row(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    """One resolved review item for the log: what, which, what Brian did and when, and where it led."""
    what, title = review_label(conn, r)
    payload = json.loads(r["payload"] or "{}")
    recording = r["kind"] == "approval" and payload.get("recording_id")
    did = {"speaker": ("Named", "Skipped"), "inbox": ("Kept", "Removed"), "glossary": ("Added", "Dismissed"),
           "approval": ("Approved", "Skipped" if recording else "Declined")}[r["kind"]][r["status"] == "dismissed"]
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
        raise HTTPException(404, "No such review item")
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
    with db.transaction(conn):
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
    if not spends_money(r, "1"):
        raise HTTPException(400, "Nothing to confirm")
    if r["conversation_id"]:
        est = extract.estimate_cost(conn, r["conversation_id"])
        return confirm_page(request, conn, "review", heading="Run the AI on this conversation?", cost=est["cost_usd_est"],
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
    if spends_money(r, ai) and not confirmed(confirm):
        return RedirectResponse(f"/review/{rid}/confirm", status_code=303)
    with db.transaction(conn):
        if r["kind"] == "approval" and not r["conversation_id"] and r["ref_id"]:
            from scribe_bot.pipeline import ingest
            if ai == "0":  # "Transcript Only (No AI)" locks it as Private; the pipeline copies recording.private over
                conn.execute("UPDATE recording SET private = 1 WHERE id = ?", (r["ref_id"],))
            ingest.approve(conn, r["ref_id"], ai=ai != "0",
                           meeting_type=meeting_type if meeting_type in prompts.MEETING_TYPES else None)
        if r["kind"] == "glossary" and r["ref_id"]:
            conn.execute("UPDATE glossary_term SET approved = 1 WHERE id = ?", (r["ref_id"],))
        if r["kind"] == "approval" and r["conversation_id"]:
            ai_jobs.queue_rerun(conn, r["conversation_id"], approved=True)
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE id = ?", (db.now(), rid))
    return back(request, "/review")


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
HIT_SPEAKER = ("(SELECT p.name FROM segment g JOIN speaker sp ON sp.id = g.speaker_id JOIN person p ON p.id = sp.person_id "
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
        hits = conn.execute(f"SELECT s.kind, s.ref_id, s.conversation_id AS cid, c.title AS conv_title, {SNIPPET} AS snip, "
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
        for r in conn.execute("SELECT s.conversation_id AS cid, group_concat(p.name, ', ') AS names FROM speaker s "
                              f"JOIN person p ON p.id = s.person_id WHERE p.id != 1 AND s.conversation_id IN ({marks}) "
                              "GROUP BY s.conversation_id", ids):
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


@router.post("/capture/upload")
def capture_upload(request: Request, file: UploadFile = File(...), conn: sqlite3.Connection = Conn):
    """Audio uploads (and in-app voice notes) go to data/uploads/, which the M2 ingest watches alongside the phone folder.
    Not into inbound/: that is Syncthing's receive-only folder, where local additions would be flagged."""
    saved = save_upload(request.app.state.cfg.data_dir / "uploads", file, UPLOAD_TYPES)
    return RedirectResponse(f"/capture?saved={received_audio(request, conn, saved)}", status_code=303)


@router.post("/capture/share")
def capture_share(request: Request, title: str = Form(""), text: str = Form(""), url: str = Form(""),
                  files: list[UploadFile] = File(default=[]), conn: sqlite3.Connection = Conn):
    """PWA share target (Android share sheet). Audio (Recorder → Share → Scribe Bot) goes to uploads/, the folder the
    pipeline watches, so it's processed like a synced recording (waits in Review; an identical file is skipped).
    Links, text and screenshots go to the Capture inbox (processed in Phase 3)."""
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
    """A shared/uploaded recording Scribe Bot may already have (e.g. an older one that synced earlier). Returns what
    the Capture page should say: 'audio' (new), 'older' (an unprocessed older recording, now in Review) or 'known'."""
    row = conn.execute("SELECT id, status FROM recording WHERE sha256 = ?", (ingest.sha256(path),)).fetchone()
    if row is None:
        return "audio"
    path.unlink(missing_ok=True)   # our extra copy of a file we already have
    if row["status"] == "backlog" and ingest.backlog_to_review(conn, row["id"], request.app.state.cfg):
        return "older"
    return "known"


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


def cost_breakdown(conn: sqlite3.Connection, since: str) -> dict[str, list[dict]]:
    """Spend by provider / task / model this month, straight from the Data Ledger."""
    return {k: [{"name": r[0], "usd": r[1], "n": r[2]} for r in conn.execute(
        f"SELECT {k}, round(sum(cost_usd), 4), count(*) FROM ledger_entry WHERE time >= ? GROUP BY {k} ORDER BY 2 DESC", (since,))]
        for k in ("provider", "task", "model")}


# Plain-English name and time for each schedule (times match the Schedule(...) definitions).
SCHEDULE_INFO = {
    "export": ("Readable export", "daily 1:30 AM"), "trash-purge": ("Empty old trash", "daily 1:45 AM"),
    "backup": ("Encrypted backup", "daily 2:00 AM"), "reconcile": ("Check for missed recordings", "daily 2:30 AM"),
    "original_cleanup": ("Remove processed phone originals", "daily 3:00 AM"),
    "restore-test": ("Restore test", "monthly, 3:00 AM"), "briefing": ("Morning briefing", "daily 7:00 AM"),
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
    subs = {"appearance": f"Theme: {theme.capitalize()}",
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
    ledger = conn.execute("SELECT l.*, a.nickname, c.title AS conv_title FROM ledger_entry l "   # console matches sent nothing
                          "LEFT JOIN account a ON a.id = l.account_id LEFT JOIN conversation c ON c.id = l.conversation_id "
                          "WHERE l.status != 'adjustment' ORDER BY l.time DESC LIMIT 50").fetchall()
    left_today = conn.execute("SELECT count(*) FROM ledger_entry WHERE time >= ? AND status = 'ok'", (today0,)).fetchone()[0]
    glossary = [{**dict(g), "variant_list": json.loads(g["variants"] or "[]")}
                for g in conn.execute("SELECT * FROM glossary_term ORDER BY approved, term")]
    added = request.query_params.get("added")
    templates_ = [{"kind": k, "label": label, "text": setting(conn, f"template:{k}", prompts.TEMPLATES[k]),
                   "custom": setting(conn, f"template:{k}") is not None} for k, label in prompts.MEETING_TYPES.items()]
    ctx = {"paused": setting(conn, "pipeline_paused", "0") == "1", "month": month, "cap": cap, "left_today": left_today, "glossary": glossary}
    return render(request, "settings.html", "settings", conn, group=group, group_title=SETTINGS_GROUPS.get(group, ("Settings",))[0],
                  settings_groups=settings_home(request, conn, ctx),
                  providers=accounts_by_provider(request, conn), summary_templates=templates_,
                  account_types=ACCOUNT_TYPES, profiles=ai_router.PROFILES, profile=ai_router.profile(conn),
                  paused=setting(conn, "pipeline_paused", "0") == "1",
                  added={"ok": bool(ok), "message": msg} if added else None,
                  tested={"ok": bool(ok), "message": msg or ("Key works." if ok else "Key test failed.")} if tested else None,
                  cap=cap, month=month, ledger=ledger, left_today=left_today,
                  offline=ai_router.offline(conn),
                  backups=conn.execute("SELECT * FROM backup_run ORDER BY started_at DESC LIMIT 20").fetchall(),
                  schedules=sorted(conn.execute("SELECT * FROM schedule_run").fetchall(),
                                   key=lambda r: list(SCHEDULE_INFO).index(r["job_name"]) if r["job_name"] in SCHEDULE_INFO else 99),
                  schedule_info=SCHEDULE_INFO,
                  glossary=glossary)


CONSOLE_USAGE_URL = "https://console.anthropic.com/settings/usage"


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, conn: sqlite3.Connection = Conn):
    """Every AI call with its cost (from the Data Ledger), grouped by conversation, plus today / month / all-time totals."""
    today0 = local_midnight()
    cap = float(setting(conn, "global_cap_usd", str(DEFAULT_CAP_USD)))
    rows = conn.execute(
        "SELECT l.*, c.title AS conv_title FROM ledger_entry l LEFT JOIN conversation c ON c.id = l.conversation_id "
        "ORDER BY l.time DESC LIMIT 500").fetchall()
    by_conv = conn.execute(
        "SELECT l.conversation_id, c.title AS conv_title, max(l.time) AS last, count(*) AS calls, sum(coalesce(l.tokens_in, 0)) AS tin, "
        "sum(coalesce(l.tokens_out, 0)) AS tout, round(sum(l.cost_usd), 4) AS usd, group_concat(DISTINCT l.model) AS models "
        "FROM ledger_entry l LEFT JOIN conversation c ON c.id = l.conversation_id GROUP BY l.conversation_id "
        "ORDER BY last DESC LIMIT 200").fetchall()
    return render(request, "costs.html", "costs", conn, rows=rows, by_conv=by_conv, cap=cap, today_spend=spend(conn, today0),
                  month=spend(conn, month_start()), all_time=spend(conn, "0000"), console_url=CONSOLE_USAGE_URL,
                  model_label=model_label, credits=costs.credits_left(conn), check=costs.console_check(conn),
                  matched=request.query_params.get("matched"))


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
def add_account(provider: str = Form(...), nickname: str = Form(...), owner: str = Form("Brian"), type: str = Form("paid"),
                monthly_cap_usd: str = Form(""), conn: sqlite3.Connection = Conn):
    """Add an account row. The key is never entered here; Brian stores it with set_api_key.sh on the M14."""
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
def test_account(aid: int, confirm: str = Form(""), conn: sqlite3.Connection = Conn):
    """Test button: a tiny call (fractions of a cent, logged in the Data Ledger). The key itself never reaches the page."""
    if not conn.execute("SELECT 1 FROM account WHERE id = ?", (aid,)).fetchone():
        raise HTTPException(404, "No such account")
    if not confirmed(confirm):
        return RedirectResponse(f"/settings/accounts/{aid}/test/confirm", status_code=303)
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
    """Pause/resume processing: while paused, new recordings wait (nothing is transcribed or sent); schedules still run."""
    set_setting(conn, "pipeline_paused", "1" if on else "0")
    return back(request, "/status")


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
    conn.execute("DELETE FROM glossary_term WHERE id = ?", (gid,))
    return back(request, "/settings/glossary")
