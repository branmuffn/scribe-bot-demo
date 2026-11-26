"""Morning briefing (7:00), weekly review (Sunday 18:00), and the "while I was down" report (M5, SPEC §3.7, §3.10).

Each is ONE notification row: counts above zero and a link only, never content. Missed briefings become one
combined catch-up briefing labeled with the offline window (scheduler.Missed). The downtime report is sent once per
outage, detected from a gap in the worker heartbeat (setting 'worker_heartbeat'); call downtime_report() before the
worker writes its next heartbeat.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from scribe_bot.core import db, health, settings
from scribe_bot.core.scheduler import Missed, Schedule
from scribe_bot.discord_bot.notify import enqueue
from scribe_bot.discord_bot.sender import TITLE_MAX

DOWNTIME_MINUTES = 10
AGING_DAYS = 14
NUDGE_TYPES = ("birthday", "family", "event")
DEFAULT_CAP_USD = 10.0
ACT_ON = ("due today", "overdue", "to review", "needs attention", "💵")  # the briefing is sent only if one is in it
HealthFn = Callable[[], dict]
# Tasks, notes and review items from a conversation in Trash don't count (the web app hides them too).
LIVE = "NOT EXISTS (SELECT 1 FROM conversation z WHERE z.id = conversation_id AND z.deleted_at IS NOT NULL)"


def _utc(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> float:
    return conn.execute(sql, args).fetchone()[0] or 0


def _n(count: float, word: str) -> str:
    return f"{count:g} {word}" + ("" if count == 1 else "s")


def _when(at: datetime) -> str:
    return at.strftime("%a %b %-d %H:%M")


def month_spend(conn: sqlite3.Connection, at: datetime) -> float:
    start = at.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _one(conn, "SELECT sum(cost_usd) FROM ledger_entry WHERE time >= ?", (_utc(start),))


def _one_line(head: str, parts: list[str], empty: str) -> str:
    """One ping per briefing (Brian 2026-10-21: 8 rows a morning was too many): head + the non-zero parts, kept under
    the Discord title limit by dropping the last parts (the app shows everything)."""
    parts = [p for p in parts if p] or [empty]
    while len(parts) > 1 and len(f"{head}: {' · '.join(parts)}") > TITLE_MAX:
        parts.pop()
    return f"{head}: {' · '.join(parts)}"[:TITLE_MAX]


def briefing_line(conn: sqlite3.Connection, at: datetime, missed: Missed | None, health_fn: HealthFn) -> str:
    """Counts above zero only. All-clear facts (system healthy, normal spend) stay on Status and Costs, not in pings."""
    today = at.date().isoformat()
    if missed and missed.catching_up:
        head = f"☀️ Catch-up · offline {_when(missed.since.astimezone(at.tzinfo))} → {_when(at)}"
        since, until, since_label = missed.since, at, "since then"
    else:
        head = f"☀️ {at.strftime('%a %b %-d')}"
        until = at.replace(hour=0, minute=0, second=0, microsecond=0)
        since, since_label = until - timedelta(days=1), "yesterday"

    due_today = _one(conn, f"SELECT count(*) FROM task WHERE status = 'open' AND owner_person_id = 1 AND due = ? AND {LIVE}", (today,))
    overdue = _one(conn, f"SELECT count(*) FROM task WHERE status = 'open' AND owner_person_id = 1 AND due < ? AND {LIVE}", (today,))
    nudges = _one(conn, f"SELECT count(*) FROM person_fact WHERE type IN ({','.join('?' * len(NUDGE_TYPES))}) "
                        f"AND coalesce(confirmed, 1) = 1 AND created_at >= ? AND {LIVE}", (*NUDGE_TYPES, _utc(at - timedelta(days=7))))
    review = _one(conn, f"SELECT count(*) FROM review_item WHERE status = 'open' AND {LIVE}")
    convs = _one(conn, "SELECT count(*) FROM conversation WHERE deleted_at IS NULL "
                       "AND coalesce(started_at, created_at) >= ? AND coalesce(started_at, created_at) < ?",
                 (_utc(since), _utc(until)))
    cap = float(_setting(conn, "global_cap_usd") or DEFAULT_CAP_USD)
    spend = month_spend(conn, at)
    h = health_fn()
    problems = [] if h.get("ok") else [k for k in ("db", "disk") if not h.get(k, {}).get("ok", True)] or ["see Status"]
    failed = {r[0] for r in conn.execute("SELECT kind FROM backup_run WHERE finished_at >= ? AND status NOT IN ('ok', 'running')",
                                         (_utc(at - timedelta(days=1)),))}   # last night's backup / restore test
    problems += [label for kind, label in (("backup", "backup failed"), ("restore_test", "restore test failed")) if kind in failed]
    if h.get("power", {}).get("source") == "battery":
        problems.append(f"on battery {h['power'].get('battery_pct')}%")
    return _one_line(head, [
        f"🩺 needs attention ({', '.join(problems)})" if problems else "",
        " · ".join(x for x in (f"{due_today:g} due today" if due_today else "", f"{overdue:g} overdue" if overdue else "") if x),
        f"{review:g} to review" if review else "",
        f"{_n(convs, 'conversation')} {since_label}" if convs else "",
        f"{_n(nudges, 'people note')} (birthdays, family)" if nudges else "",
        f"💵 ${spend:.2f} of ${cap:.2f}" if spend >= cap * 0.5 else "",
    ], "nothing needs you")


def weekly_line(conn: sqlite3.Connection, at: datetime) -> str:
    week_ago = _utc(at - timedelta(days=7))
    today = at.date()
    done = _one(conn, f"SELECT count(*) FROM task WHERE status = 'done' AND closed_as IS NULL AND updated_at >= ? AND {LIVE}", (week_ago,))
    open_ = _one(conn, f"SELECT count(*) FROM task WHERE status = 'open' AND {LIVE}")
    aging = _one(conn, f"SELECT count(*) FROM task WHERE status = 'open' AND created_at < ? AND {LIVE}",
                 (_utc(at - timedelta(days=AGING_DAYS)),))
    loose = _one(conn, f"SELECT count(*) FROM task WHERE status = 'open' AND owner_person_id != 1 AND updated_at < ? AND {LIVE}",
                 (week_ago,))   # promises to Brian with no update for a week: worth a nudge
    next_week = _one(conn, f"SELECT count(*) FROM task WHERE status = 'open' AND due > ? AND due <= ? AND {LIVE}",
                     (today.isoformat(), (today + timedelta(days=7)).isoformat()))
    return _one_line(f"🗓️ Week ending {at.strftime('%a %b %-d')}", [
        f"{done:g} done, {open_:g} open",
        f"{_n(loose, 'loose end')} (promises to you, no update 7+ days)" if loose else "",
        f"{next_week:g} due next 7 days" if next_week else "",
        f"{aging:g} open {AGING_DAYS}+ days" if aging else "",
    ], "quiet week")


def _post(conn: sqlite3.Connection, line: str, path: str) -> list[int]:
    return [enqueue(conn, "bot", line, path, discord=False)]   # in the app only (Brian 2026-10-22: no pings)


def send_briefing(conn: sqlite3.Connection, missed: Missed | None = None, at: datetime | None = None,
                  health_fn: HealthFn | None = None) -> list[int]:
    at = at or datetime.now().astimezone()
    health_fn = health_fn or (lambda: health.check(settings.load()))
    line = briefing_line(conn, at, missed, health_fn)
    if not any(k in line for k in ACT_ON):   # Brian 2026-10-22: a "check the app" ping on quiet days isn't worth it
        return []
    return _post(conn, line, "/")


def send_weekly(conn: sqlite3.Connection, missed: Missed | None = None, at: datetime | None = None) -> list[int]:
    return _post(conn, weekly_line(conn, at or datetime.now().astimezone()), "/tasks")


def _span(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return (f"{d} d " if d else "") + (f"{h} h " if h or d else "") + f"{m} min"


def downtime_report(conn: sqlite3.Connection, at: datetime | None = None) -> int | None:
    """If the worker's last heartbeat is more than DOWNTIME_MINUTES old, send one report for that outage."""
    at = at or datetime.now().astimezone()
    beat = _setting(conn, "worker_heartbeat")
    if not beat or _setting(conn, "downtime_reported_for") == beat:
        return None
    last = datetime.fromisoformat(beat)
    gap = at - last
    if gap < timedelta(minutes=DOWNTIME_MINUTES):
        return None
    arrived = _one(conn, "SELECT count(*) FROM recording WHERE received_at >= ?", (_utc(last),))
    missed = len(BRIEFING.times_between(last, at))
    _set(conn, "downtime_reported_for", beat)
    title = (f"🔁 While I was down: offline {_when(last.astimezone(at.tzinfo))} → {_when(at)} ({_span(gap)}) · "
             f"{_n(arrived, 'recording')} arrived · {_n(missed, 'briefing')} missed")
    return enqueue(conn, "alerts", title, "/status")


def _with_db(fn: Callable[[sqlite3.Connection, Missed], object]) -> Callable[[Missed], None]:
    def run(missed: Missed) -> None:
        conn = db.connect(settings.load().db_path)
        try:
            fn(conn, missed)
        finally:
            conn.close()
    return run


BRIEFING = Schedule("briefing", hour=7, run=_with_db(lambda c, m: send_briefing(c, m)))
WEEKLY = Schedule("weekly-review", hour=18, weekday=6, run=_with_db(lambda c, m: send_weekly(c, m)))
SCHEDULES = [BRIEFING, WEEKLY]
