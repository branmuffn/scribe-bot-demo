"""Scheduled jobs with last-success tracking and catch-up (SPEC §3.10, CLAUDE.md rule 5).

Each schedule records its last success in schedule_run. When the scheduler checks (every minute, and at startup),
a job whose most recent scheduled time is after its last success runs once, however many times it was missed.
The job is told how many runs it missed and since when, so e.g. the briefing can send one combined catch-up.
A brand-new schedule starts counting from when it is first seen (installing it doesn't fire a "missed" run).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Missed:
    since: datetime          # last success
    count: int               # scheduled times missed (1 = on time)

    @property
    def catching_up(self) -> bool:
        return self.count > 1


@dataclass(frozen=True)
class Schedule:
    name: str
    hour: int
    minute: int = 0
    weekday: int | None = None   # None = every day; 0 = Monday ... 6 = Sunday
    run: Callable[[Missed], None] = lambda missed: None

    def times_between(self, start: datetime, end: datetime) -> list[datetime]:
        """Scheduled times t with start < t <= end, in end's time zone."""
        start = start.astimezone(end.tzinfo)
        day = start.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        out = []
        while day <= end:
            if day > start and (self.weekday is None or day.weekday() == self.weekday):
                out.append(day)
            day += timedelta(days=1)
        return out


def _row(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM schedule_run WHERE job_name = ?", (name,)).fetchone()


def run_due(conn: sqlite3.Connection, schedules: list[Schedule], now: datetime) -> list[tuple[str, Missed]]:
    """Run each schedule that is due, once. Returns what ran. A failed run stays due and is retried next check."""
    ran = []
    for s in schedules:
        row = _row(conn, s.name)
        if row is None:
            conn.execute("INSERT INTO schedule_run (job_name, last_success_at, status) VALUES (?, ?, 'new')",
                         (s.name, now.isoformat()))
            continue
        since = datetime.fromisoformat(row["last_success_at"])
        missed = s.times_between(since, now)
        if not missed:
            continue
        info = Missed(since=since, count=len(missed))
        conn.execute("UPDATE schedule_run SET last_attempt_at = ?, status = 'running' WHERE job_name = ?",
                     (now.isoformat(), s.name))
        try:
            s.run(info)
        except Exception as e:
            conn.execute("UPDATE schedule_run SET status = ? WHERE job_name = ?", (f"failed: {e}"[:500], s.name))
            continue
        conn.execute("UPDATE schedule_run SET last_success_at = ?, status = 'ok' WHERE job_name = ?",
                     (now.isoformat(), s.name))
        ran.append((s.name, info))
    return ran
