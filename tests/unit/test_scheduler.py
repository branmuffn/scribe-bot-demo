from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scribe_bot.core import db, scheduler
from scribe_bot.core.scheduler import Schedule

TZ = ZoneInfo("America/New_York")


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def briefing(calls):
    return Schedule("briefing", hour=7, run=calls.append)


def test_new_schedule_does_not_fire_on_install(conn):
    calls = []
    assert scheduler.run_due(conn, [briefing(calls)], at(19, 15)) == []
    assert calls == []


def test_runs_once_on_time(conn):
    calls = []
    s = [briefing(calls)]
    scheduler.run_due(conn, s, at(19, 15))       # installed Monday afternoon
    scheduler.run_due(conn, s, at(20, 6, 59))    # not yet
    ran = scheduler.run_due(conn, s, at(20, 7))
    assert [n for n, _ in ran] == ["briefing"]
    assert calls[0].count == 1 and not calls[0].catching_up
    assert scheduler.run_due(conn, s, at(20, 7, 1)) == []  # doesn't repeat


# M1 "done when": a missed scheduled job runs once on restart.
def test_missed_runs_once_on_restart_with_catch_up_info(tmp_path):
    calls = []
    path = tmp_path / "x.db"
    c = db.connect(path)
    db.migrate(c)
    scheduler.run_due(c, [briefing(calls)], at(19, 15))
    c.close()                                    # M14 goes down Monday afternoon

    c = db.connect(path)                         # restart Friday 10:30
    ran = scheduler.run_due(c, [briefing(calls)], at(23, 10, 30))
    assert len(ran) == 1 and len(calls) == 1     # one combined catch-up, not four briefings
    assert calls[0].count == 4 and calls[0].catching_up  # missed Oct 20, 21, 22, 23
    assert calls[0].since == at(19, 15)
    assert scheduler.run_due(c, [briefing(calls)], at(23, 10, 31)) == []


def test_failed_run_stays_due_and_retries(conn):
    attempts = []

    def flaky(missed):
        attempts.append(missed)
        if len(attempts) == 1:
            raise RuntimeError("Discord unreachable")

    s = [Schedule("briefing", hour=7, run=flaky)]
    scheduler.run_due(conn, s, at(19, 15))
    assert scheduler.run_due(conn, s, at(20, 7)) == []
    assert conn.execute("SELECT status FROM schedule_run").fetchone()[0].startswith("failed")
    assert [n for n, _ in scheduler.run_due(conn, s, at(20, 7, 1))] == ["briefing"]


def test_weekly_only_counts_its_weekday(conn):
    calls = []
    s = [Schedule("weekly-review", hour=18, weekday=6, run=calls.append)]  # Sundays
    scheduler.run_due(conn, s, at(19, 9))       # Monday
    assert scheduler.run_due(conn, s, at(24, 23)) == []  # Saturday: not due
    scheduler.run_due(conn, s, at(25, 18))      # Sunday 6 pm
    assert len(calls) == 1 and calls[0].count == 1


def test_times_between_handles_dst_change():
    s = Schedule("briefing", hour=7)
    times = s.times_between(datetime(2026, 10, 31, 8, tzinfo=TZ), datetime(2026, 11, 2, 8, tzinfo=TZ))
    assert [(t.day, t.hour) for t in times] == [(1, 7), (2, 7)]  # still 7:00 local after clocks change on Nov 1
    assert times[0].utcoffset() == timedelta(hours=-5)
