"""Encrypted backups of ~/scribe-bot-data with restic, over rclone, to each named Google Drive account (SPEC §3.11).

- Destinations live in the setting 'backup_destinations' (JSON list of {nickname, remote, email}); each one is an
  rclone remote Brian signs in to once (scripts/setup/m14_backup.sh). restic encrypts before upload.
- The restic password is in the Keychain (service scribe-bot.restic). restic reads it itself through
  RESTIC_PASSWORD_COMMAND, so it never passes through this process, the logs, or the database.
- The live database is not copied directly: a consistent snapshot is taken with SQLite's backup API first.
- Every run and restore test is a backup_run row. restic/rclone are called through an injectable runner (tests fake it).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scribe_bot.core.db import now as utc_now
from scribe_bot.core.settings import Settings

Runner = Callable[..., subprocess.CompletedProcess]
REPO_FOLDER = "scribe-bot-backup"
PASSWORD_COMMAND = "security find-generic-password -a brian-personal -s scribe-bot.restic -w"
STAGING = "backup-staging"
EXCLUDES = ["scribe-bot.db", "scribe-bot.db-wal", "scribe-bot.db-shm", "tmp", "*.wav"]
KEEP = ["--keep-daily", "7", "--keep-weekly", "5", "--keep-monthly", "24"]
RESTORE_EVERY_DAYS = 28
BACKUP_STALE_HOURS = 36


class BackupError(RuntimeError):
    pass


# --- Destinations -------------------------------------------------------------------------------------------------
def destinations(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute("SELECT value FROM setting WHERE key = 'backup_destinations'").fetchone()
    return json.loads(row[0]) if row else []


def _save(conn: sqlite3.Connection, dests: list[dict]) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES ('backup_destinations', ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(dests),))


def add_destination(conn: sqlite3.Connection, nickname: str, remote: str, email: str) -> None:
    dests = [d for d in destinations(conn) if d["nickname"] != nickname]
    _save(conn, dests + [{"nickname": nickname, "remote": remote.rstrip(":"), "email": email}])


def remove_destination(conn: sqlite3.Connection, nickname: str) -> None:
    _save(conn, [d for d in destinations(conn) if d["nickname"] != nickname])


# --- restic ---------------------------------------------------------------------------------------------------------
def _env(dest: dict) -> dict:
    # launchd's PATH has no Homebrew folders, so restic (and the rclone it calls) weren't found from the worker
    path = os.pathsep.join(["/usr/local/bin", "/opt/homebrew/bin", os.environ.get("PATH", "/usr/bin:/bin")])
    return {**os.environ, "PATH": path, "RESTIC_REPOSITORY": f"rclone:{dest['remote']}:{REPO_FOLDER}",
            "RESTIC_PASSWORD_COMMAND": PASSWORD_COMMAND}


def _restic(run: Runner, dest: dict, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        p = run(["restic", *args], env=_env(dest), capture_output=True, text=True)
    except OSError as e:
        raise BackupError(f"restic not available ({type(e).__name__})") from e
    if check and p.returncode != 0:
        raise BackupError(f"restic {args[0]} failed (exit {p.returncode})")  # stderr may hold paths; not recorded
    return p


def snapshot_db(cfg: Settings) -> Path:
    """Consistent copy of the live database, safe while the worker is writing."""
    out = cfg.data_dir / STAGING / "scribe-bot.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    src = sqlite3.connect(cfg.db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    tmp.replace(out)
    return out


def _start(conn: sqlite3.Connection, dest: str, kind: str) -> int:
    return conn.execute("INSERT INTO backup_run (destination, kind, started_at) VALUES (?, ?, ?)",
                        (dest, kind, utc_now())).lastrowid


def _finish(conn: sqlite3.Connection, run_id: int, status: str, nbytes: int | None = None) -> None:
    conn.execute("UPDATE backup_run SET finished_at = ?, status = ?, bytes = ? WHERE id = ?",
                 (utc_now(), status, nbytes, run_id))


def _summary_bytes(stdout: str) -> int | None:
    for line in reversed(stdout.splitlines()):
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("message_type") == "summary":
            return msg.get("total_bytes_processed")
    return None


def run_backup(conn: sqlite3.Connection, cfg: Settings, run: Runner = subprocess.run,
               at: datetime | None = None) -> list[dict]:
    """Back up to every destination. One destination failing doesn't stop the others."""
    at = at or datetime.now().astimezone()
    dests = destinations(conn)
    if not dests:
        return []
    snapshot_db(cfg)
    results = []
    for d in dests:
        run_id = _start(conn, d["nickname"], "backup")
        try:
            if _restic(run, d, "cat", "config", check=False).returncode != 0:
                _restic(run, d, "init")
            excludes = [a for e in EXCLUDES for a in ("--exclude", str(cfg.data_dir / e) if "*" not in e else e)]
            p = _restic(run, d, "backup", "--json", "--tag", "scribe-bot", *excludes, str(cfg.data_dir))
            nbytes = _summary_bytes(p.stdout)
            _restic(run, d, "forget", *KEEP, *(["--prune"] if at.weekday() == 6 else []))
        except BackupError as e:
            _finish(conn, run_id, f"failed: {e}")
            results.append({"destination": d["nickname"], "ok": False, "error": str(e)})
            continue
        _finish(conn, run_id, "ok", nbytes)
        results.append({"destination": d["nickname"], "ok": True, "bytes": nbytes})
    return results


def restore_test(conn: sqlite3.Connection, cfg: Settings, run: Runner = subprocess.run) -> list[dict]:
    """Restore the database snapshot from each destination into a scratch folder and check that it opens cleanly."""
    staged = cfg.data_dir / STAGING / "scribe-bot.db"
    results = []
    for d in destinations(conn):
        run_id = _start(conn, d["nickname"], "restore_test")
        (cfg.data_dir / "tmp").mkdir(parents=True, exist_ok=True)  # tmp/ is excluded from backups
        scratch = Path(tempfile.mkdtemp(prefix="restore-test-", dir=cfg.data_dir / "tmp"))
        try:
            _restic(run, d, "restore", "latest", "--target", str(scratch), "--include", str(staged))
            restored = scratch / staged.relative_to(staged.anchor)
            if not restored.exists():
                raise BackupError("restored database not found")
            c = sqlite3.connect(restored)
            try:
                ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                c.execute("SELECT count(*) FROM conversation").fetchone()
            except sqlite3.Error as e:
                raise BackupError(f"restored database unreadable ({type(e).__name__})") from e
            finally:
                c.close()
            if not ok:
                raise BackupError("restored database failed its integrity check")
            nbytes = restored.stat().st_size
        except BackupError as e:
            _finish(conn, run_id, f"failed: {e}")
            results.append({"destination": d["nickname"], "ok": False, "error": str(e)})
            continue
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        _finish(conn, run_id, "ok", nbytes)
        results.append({"destination": d["nickname"], "ok": True, "bytes": nbytes})
    return results


def restore_test_due(conn: sqlite3.Connection, at: datetime) -> bool:
    last = conn.execute("SELECT max(finished_at) FROM backup_run WHERE kind = 'restore_test' AND status = 'ok'").fetchone()[0]
    return last is None or datetime.fromisoformat(last) < at.astimezone(timezone.utc) - timedelta(days=RESTORE_EVERY_DAYS)


def status(conn: sqlite3.Connection, at: datetime | None = None) -> dict:
    """For the Settings > Backups page and the briefing: last backup and restore test per destination."""
    at = (at or datetime.now().astimezone()).astimezone(timezone.utc)
    out = []
    for d in destinations(conn):
        def last(kind: str) -> dict | None:
            r = conn.execute("SELECT finished_at, status, bytes FROM backup_run WHERE destination = ? AND kind = ? "
                             "AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", (d["nickname"], kind)).fetchone()
            return dict(r) if r else None
        b, t = last("backup"), last("restore_test")
        backup_ok = bool(b and b["status"] == "ok"
                         and datetime.fromisoformat(b["finished_at"]) > at - timedelta(hours=BACKUP_STALE_HOURS))
        test_ok = bool(t and t["status"] == "ok"
                       and datetime.fromisoformat(t["finished_at"]) > at - timedelta(days=RESTORE_EVERY_DAYS + 7))
        out.append({**d, "last_backup": b, "last_restore_test": t, "ok": backup_ok and test_ok})
    return {"destinations": out, "ok": bool(out) and all(x["ok"] for x in out)}
