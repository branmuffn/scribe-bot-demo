"""Nightly M6 schedules (local time): export 1:30, Trash purge 1:45, backup 2:00 (so tonight's export is in it),
restore-test check 3:00 (runs the test when the last good one is 28+ days old, i.e. monthly).
Failures go to #scribe-alerts as titles only."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from scribe_bot.backup import export, restic, trash
from scribe_bot.core import db, settings
from scribe_bot.core.scheduler import Missed, Schedule
from scribe_bot.discord_bot.notify import enqueue


def nightly_backup(conn: sqlite3.Connection, cfg: settings.Settings, run=None) -> list[dict]:
    results = restic.run_backup(conn, cfg, *([run] if run else []))
    for r in results:
        # Brian, 2026-11-21: only when it failed two nights in a row (a one-off, like Nov 20, fixes itself the next night)
        if not r["ok"] and _failed_twice(conn, r["destination"]):
            enqueue(conn, "alerts", f"🔴 Backup to {r['destination']} failed two nights in a row ({r['error']})"[:150], "/settings/backups")
    if not results:
        enqueue(conn, "alerts", "⚠️ No backup destinations set up yet (Settings > Backups)", "/settings/backups")
    return results


def _failed_twice(conn: sqlite3.Connection, destination: str) -> bool:
    last = [r[0] for r in conn.execute("SELECT status FROM backup_run WHERE destination = ? AND kind = 'backup' "
                                       "ORDER BY id DESC LIMIT 2", (destination,))]
    return len(last) == 2 and all(s != "ok" for s in last)


def monthly_restore_test(conn: sqlite3.Connection, cfg: settings.Settings, run=None, at: datetime | None = None) -> list[dict]:
    if not restic.restore_test_due(conn, at or datetime.now().astimezone()):
        return []
    results = restic.restore_test(conn, cfg, *([run] if run else []))
    for r in results:  # a pass is shown on Status only (no ping, Brian 2026-10-21); a failure alerts
        if not r["ok"]:
            enqueue(conn, "alerts", f"🔴 Monthly restore test from {r['destination']} failed ({r['error']})"[:150], "/settings/backups")
    return results


def _job(fn: Callable[[sqlite3.Connection, settings.Settings], object]) -> Callable[[Missed], None]:
    def run(missed: Missed) -> None:
        cfg = settings.load()
        conn = db.connect(cfg.db_path)
        try:
            fn(conn, cfg)
        finally:
            conn.close()
    return run


SCHEDULES = [
    Schedule("export", hour=1, minute=30, run=_job(export.export_all)),
    Schedule("trash-purge", hour=1, minute=45, run=_job(lambda c, cfg: trash.purge(c, cfg=cfg))),
    Schedule("backup", hour=2, run=_job(nightly_backup)),
    Schedule("restore-test", hour=3, run=_job(monthly_restore_test)),
]
