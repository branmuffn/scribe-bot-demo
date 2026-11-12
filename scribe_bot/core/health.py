"""Health check for the deploy script and the Status page (M1): numbers and states only, never content.

Unhealthy (non-zero exit) when the database can't be opened or has unapplied migrations, or the disk is over 85% used.
Running on battery is reported but isn't a deploy failure (the on-battery alert handles that).
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

from scribe_bot.core import db
from scribe_bot.core.settings import Settings

DISK_ALERT_PCT = 85
Runner = Callable[..., subprocess.CompletedProcess]


def parse_pmset(text: str) -> dict:
    """`pmset -g batt` -> {"source": "ac" | "battery" | "unknown", "battery_pct": int | None, "state": str | None}."""
    source = "unknown"
    if m := re.search(r"Now drawing from '([^']+)'", text):
        source = {"AC Power": "ac", "Battery Power": "battery"}.get(m.group(1), "unknown")
    pct = state = None
    if m := re.search(r"(\d{1,3})%;\s*([^;]+);", text):
        pct, state = int(m.group(1)), m.group(2).strip()
    return {"source": source, "battery_pct": pct, "state": state}


def power(run: Runner = subprocess.run) -> dict:
    try:
        p = run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {"source": "unknown", "battery_pct": None, "state": None}
    return parse_pmset(p.stdout)


def disk(path: Path) -> dict:
    u = shutil.disk_usage(path)
    used = round(100 * u.used / u.total, 1)
    return {"used_pct": used, "free_gb": round(u.free / 1e9, 1), "ok": used <= DISK_ALERT_PCT}


def database(path: Path) -> dict:
    try:
        conn = db.connect(path)
        try:
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            expected = {int(f.name[:4]) for f in db.MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")}
            queue = {r[0]: r[1] for r in conn.execute("SELECT status, count(*) FROM job GROUP BY status")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"ok": False, "error": type(e).__name__}
    missing = sorted(expected - applied)
    return {"ok": not missing, "missing_migrations": missing, "queue": queue}


def check(settings: Settings, run: Runner = subprocess.run) -> dict:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    result = {"db": database(settings.db_path), "disk": disk(settings.data_dir), "power": power(run)}
    result["ok"] = result["db"]["ok"] and result["disk"]["ok"]
    return result
