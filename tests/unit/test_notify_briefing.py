from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scribe_bot.core import scheduler
from scribe_bot.core.scheduler import Missed, Schedule
from scribe_bot.discord_bot import briefing, sender

TZ = ZoneInfo("America/New_York")
HEALTHY = {"ok": True, "db": {"ok": True}, "disk": {"ok": True}, "power": {"source": "ac", "battery_pct": 90}}


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def seed(conn):
    """Synthetic data only."""
    now = utc(at(20, 6))
    conn.execute("INSERT INTO person (id, name, created_at) VALUES (2, 'Alex Example', ?)", (now,))
    tasks = [  # (title, owner, due, status, created)
        ("Send the deck", 1, "2026-10-20", "open", at(19, 9)),
        ("Book the room", 1, "2026-10-18", "open", at(17, 9)),
        ("Share the numbers", 2, None, "open", at(10, 9)),
        ("Reply to intro", 2, None, "open", at(19, 9)),
        ("Old done thing", 1, None, "done", at(15, 9)),
    ]
    for title, owner, due, status, created in tasks:
        conn.execute("INSERT INTO task (title, owner_person_id, due, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (title, owner, due, status, utc(created), utc(created)))
    for i, started in enumerate([at(19, 10), at(19, 15), at(18, 10)]):
        conn.execute("INSERT INTO conversation (title, started_at, created_at, updated_at) VALUES (?, ?, ?, ?)",
                     (f"Synthetic call {i}", utc(started), now, now))
    conn.execute("INSERT INTO person_fact (person_id, type, value, created_at) VALUES (2, 'birthday', 'synthetic', ?)", (now,))
    conn.execute("INSERT INTO review_item (kind, created_at) VALUES ('speaker', ?)", (now,))
    conn.execute("INSERT INTO ledger_entry (time, provider, model, task, sent_summary, cost_usd) "
                 "VALUES (?, 'anthropic', 'm', 'extract', 'synthetic', 0.07)", (utc(at(5, 9)),))
    conn.execute("INSERT INTO ledger_entry (time, provider, model, task, sent_summary, cost_usd) "
                 "VALUES ('2026-09-30T12:00:00+00:00', 'anthropic', 'm', 'extract', 'synthetic', 5)")


def test_briefing_is_one_line_with_only_what_needs_brian(conn):
    seed(conn)
    line = briefing.briefing_line(conn, at(20, 7), Missed(at(19, 7), 1), lambda: HEALTHY)
    assert line == ("☀️ Tue Oct 20: 1 due today · 1 overdue · 1 to review · 2 conversations yesterday · "
                    "1 people note (birthdays, family)")
    assert sender.title_ok(line) and "healthy" not in line and "$" not in line   # all-clear facts stay in the app


def test_briefing_all_clear_and_high_spend(conn):
    assert briefing.briefing_line(conn, at(20, 7), None, lambda: HEALTHY) == "☀️ Tue Oct 20: nothing needs you"
    conn.execute("INSERT INTO ledger_entry (time, provider, model, task, sent_summary, cost_usd) "
                 "VALUES (?, 'anthropic', 'm', 'extract', 'synthetic', 6)", (utc(at(5, 9)),))
    assert "💵 $6.00 of $10.00" in briefing.briefing_line(conn, at(20, 7), None, lambda: HEALTHY)


def test_briefing_reports_a_failed_backup_from_last_night(conn):
    seed(conn)
    conn.execute("INSERT INTO backup_run (destination, kind, started_at, finished_at, status) VALUES "
                 "('Synthetic Drive', 'backup', ?, ?, 'failed: restic not available')", (utc(at(20, 2)), utc(at(20, 2))))
    line = briefing.briefing_line(conn, at(20, 7), Missed(at(19, 7), 1), lambda: HEALTHY)
    assert line.startswith("☀️ Tue Oct 20: 🩺 needs attention (backup failed) · ")


def test_briefing_flags_problems(conn):
    sick = {"ok": False, "db": {"ok": True}, "disk": {"ok": False}, "power": {"source": "battery", "battery_pct": 41}}
    assert briefing.briefing_line(conn, at(20, 7), None, lambda: sick) == \
        "☀️ Tue Oct 20: 🩺 needs attention (disk, on battery 41%)"


def test_briefing_is_one_row_with_link(conn):
    seed(conn)
    ids = briefing.send_briefing(conn, None, at(20, 7), lambda: HEALTHY)
    rows = conn.execute("SELECT channel, link FROM notification ORDER BY id").fetchall()
    assert len(ids) == 1 and rows[0]["channel"] == "bot" and rows[0]["link"] == "/"   # the sender adds the address for Discord


# M5 "done when": a simulated outage produces one combined briefing plus a downtime report.
def test_outage_gives_one_combined_briefing_and_one_downtime_report(conn):
    seed(conn)
    sent = []
    s = Schedule("briefing", hour=7, run=lambda missed: sent.append(
        briefing.send_briefing(conn, missed, at(22, 10, 30), lambda: HEALTHY)))
    scheduler.run_due(conn, [s], at(19, 15))                # installed; worker last alive Mon 15:00
    conn.execute("INSERT INTO setting VALUES ('worker_heartbeat', ?)", (utc(at(19, 15)),))

    restart = at(22, 10, 30)                                # back Thu 10:30: missed Tue, Wed, Thu
    report = briefing.downtime_report(conn, restart)
    scheduler.run_due(conn, [s], restart)
    assert len(sent) == 1                                   # one briefing, not three
    head = conn.execute("SELECT title FROM notification WHERE id = ?", (sent[0][0],)).fetchone()[0]
    assert head.startswith("☀️ Catch-up · offline Mon Oct 19 15:00 → Thu Oct 22 10:30: ")
    assert "1 conversation since then" in head

    title, channel = conn.execute("SELECT title, channel FROM notification WHERE id = ?", (report,)).fetchone()
    assert channel == "alerts" and sender.title_ok(title)
    assert title.startswith("🔁 While I was down: offline Mon Oct 19 15:00 → Thu Oct 22 10:30 (2 d 19 h 30 min)")
    assert "3 briefings missed" in title
    assert briefing.downtime_report(conn, restart + timedelta(minutes=1)) is None  # only once per outage


def test_no_downtime_report_for_short_gap_or_first_start(conn):
    assert briefing.downtime_report(conn, at(20, 9)) is None  # no heartbeat yet
    conn.execute("INSERT INTO setting VALUES ('worker_heartbeat', ?)", (utc(at(20, 8, 55)),))
    assert briefing.downtime_report(conn, at(20, 9)) is None


def test_weekly_review(conn):
    seed(conn)
    conn.execute("UPDATE task SET status = 'done', updated_at = ? WHERE title = 'Send the deck'", (utc(at(24, 9)),))
    conn.execute("INSERT INTO task (title, due, created_at, updated_at) VALUES ('Next week', '2026-10-28', ?, ?)",
                 (utc(at(24, 9)), utc(at(24, 9))))
    assert briefing.weekly_line(conn, at(25, 18)) == (
        "🗓️ Week ending Sun Oct 25: 1 done, 4 open · 1 loose end (promises to you, no update 7+ days) · "
        "1 due next 7 days · 1 open 14+ days")


def test_schedules_exposed():
    assert [(s.name, s.hour, s.weekday) for s in briefing.SCHEDULES] == [("briefing", 7, None), ("weekly-review", 18, 6)]


def test_no_briefing_ping_on_a_quiet_day(conn):
    assert briefing.send_briefing(conn, None, at(20, 7), lambda: HEALTHY) == []
    assert conn.execute("SELECT count(*) FROM notification").fetchone()[0] == 0


def test_briefing_is_in_the_app_only(conn):
    seed(conn)
    briefing.send_briefing(conn, None, at(20, 7), lambda: HEALTHY)
    assert conn.execute("SELECT status FROM notification").fetchone()[0] == "sent"   # the Discord sender skips it
