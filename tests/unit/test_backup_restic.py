import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scribe_bot.backup import jobs, restic
from scribe_bot.core import db
from scribe_bot.core.settings import Settings


class FakeRestic:
    """Pretends to be restic: remembers which repos exist and what was backed up; restore copies the staged DB back."""

    def __init__(self, fail_repo=None, corrupt_restore=False):
        self.repos, self.calls, self.fail_repo, self.corrupt = {}, [], fail_repo, corrupt_restore

    def __call__(self, args, env=None, **kw):
        repo = env["RESTIC_REPOSITORY"]
        assert env["RESTIC_PASSWORD_COMMAND"].startswith("security find-generic-password")
        assert "RESTIC_PASSWORD" not in env  # the password itself never passes through Scribe Bot
        cmd = args[1]
        self.calls.append((repo, cmd, args))
        if repo == self.fail_repo and cmd == "backup":
            return subprocess.CompletedProcess(args, 1, "", "network down")
        if cmd == "cat":
            return subprocess.CompletedProcess(args, 0 if repo in self.repos else 10, "", "")
        if cmd == "init":
            self.repos[repo] = None
        elif cmd == "backup":
            data_dir = Path(args[-1])
            self.repos[repo] = (data_dir / "backup-staging" / "scribe-bot.db").read_bytes()
            out = json.dumps({"message_type": "summary", "total_bytes_processed": 4096})
            return subprocess.CompletedProcess(args, 0, '{"message_type":"status"}\n' + out + "\n", "")
        elif cmd == "restore":
            target, include = Path(args[args.index("--target") + 1]), Path(args[args.index("--include") + 1])
            dest = target / include.relative_to(include.anchor)
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"not a database" if self.corrupt else self.repos[repo])
        return subprocess.CompletedProcess(args, 0, "", "")


def setup(tmp_path, n_dest=2):
    cfg = Settings(tmp_path / "data")
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    conn.execute("INSERT INTO conversation (title, created_at, updated_at) VALUES ('Synthetic', 'x', 'x')")
    for i in range(n_dest):
        restic.add_destination(conn, f"Drive {i}", f"gdrive-{i}:", f"test{i}@example.com")
    return cfg, conn


def runs(conn):
    return [(r["destination"], r["kind"], r["status"]) for r in conn.execute("SELECT * FROM backup_run ORDER BY id")]


def test_destinations_round_trip(conn):
    restic.add_destination(conn, "Brian Drive", "gdrive-brian:", "a@example.com")
    restic.add_destination(conn, "Brian Drive", "gdrive-brian2", "a@example.com")  # same nickname replaces
    assert restic.destinations(conn) == [{"nickname": "Brian Drive", "remote": "gdrive-brian2", "email": "a@example.com"}]
    restic.remove_destination(conn, "Brian Drive")
    assert restic.destinations(conn) == []


def test_backup_snapshots_db_inits_repo_and_records_runs(tmp_path):
    cfg, conn = setup(tmp_path)
    fake = FakeRestic()
    monday = datetime(2026, 10, 19, 2, tzinfo=timezone.utc)
    results = restic.run_backup(conn, cfg, fake, monday)
    assert [r["ok"] for r in results] == [True, True] and results[0]["bytes"] == 4096
    assert runs(conn) == [("Drive 0", "backup", "ok"), ("Drive 1", "backup", "ok")]
    assert set(fake.repos) == {"rclone:gdrive-0:scribe-bot-backup", "rclone:gdrive-1:scribe-bot-backup"}
    backup_args = next(a for _, c, a in fake.calls if c == "backup")
    assert str(cfg.db_path) in backup_args  # live DB excluded; the consistent snapshot is backed up instead
    assert "--prune" not in next(a for _, c, a in fake.calls if c == "forget")  # prune only on Sundays
    snap = sqlite3.connect(cfg.data_dir / "backup-staging" / "scribe-bot.db")
    assert snap.execute("SELECT title FROM conversation").fetchone()[0] == "Synthetic"

    restic.run_backup(conn, cfg, fake, monday)
    assert [c for _, c, _ in fake.calls].count("init") == 2  # repos are only created once


def test_one_destination_failing_does_not_stop_others(tmp_path):
    cfg, conn = setup(tmp_path)
    results = restic.run_backup(conn, cfg, FakeRestic(fail_repo="rclone:gdrive-0:scribe-bot-backup"))
    assert [r["ok"] for r in results] == [False, True]
    assert runs(conn)[0][2] == "failed: restic backup failed (exit 1)"
    assert "network down" not in runs(conn)[0][2]


def test_missing_restic_is_reported_not_crashed(tmp_path):
    cfg, conn = setup(tmp_path, n_dest=1)

    def no_restic(args, **kw):
        raise FileNotFoundError("restic")

    assert restic.run_backup(conn, cfg, no_restic)[0]["error"].startswith("restic not available")


# M6 "done when": a restore from each Drive destination into a scratch folder passes.
def test_restore_test_from_each_destination(tmp_path):
    cfg, conn = setup(tmp_path)
    fake = FakeRestic()
    restic.run_backup(conn, cfg, fake)
    results = restic.restore_test(conn, cfg, fake)
    assert [r["ok"] for r in results] == [True, True]
    assert runs(conn)[-2:] == [("Drive 0", "restore_test", "ok"), ("Drive 1", "restore_test", "ok")]
    assert list((cfg.data_dir / "tmp").iterdir()) == []  # scratch folders cleaned up


def test_corrupt_restore_fails(tmp_path):
    cfg, conn = setup(tmp_path, n_dest=1)
    fake = FakeRestic(corrupt_restore=True)
    restic.run_backup(conn, cfg, fake)
    assert not restic.restore_test(conn, cfg, fake)[0]["ok"]


def test_status_and_monthly_due(tmp_path):
    cfg, conn = setup(tmp_path, n_dest=1)
    now = datetime.now(timezone.utc)
    assert restic.status(conn)["ok"] is False and restic.restore_test_due(conn, now)
    fake = FakeRestic()
    restic.run_backup(conn, cfg, fake)
    restic.restore_test(conn, cfg, fake)
    s = restic.status(conn)
    assert s["ok"] and s["destinations"][0]["last_backup"]["bytes"] == 4096
    assert not restic.restore_test_due(conn, now)
    assert restic.restore_test_due(conn, now + timedelta(days=29))
    assert not restic.status(conn, now + timedelta(days=2))["ok"]  # backup older than 36 h


def test_nightly_job_alerts_on_failure_and_no_destinations(tmp_path):
    cfg, conn = setup(tmp_path, n_dest=0)
    jobs.nightly_backup(conn, cfg, FakeRestic())
    restic.add_destination(conn, "Drive 0", "gdrive-0", "t@example.com")
    jobs.nightly_backup(conn, cfg, FakeRestic(fail_repo="rclone:gdrive-0:scribe-bot-backup"))
    titles = [r[0] for r in conn.execute("SELECT title FROM notification WHERE channel = 'alerts' ORDER BY id")]
    assert titles == ["⚠️ No backup destinations set up yet (Settings > Backups)"]   # one failed night: no alert yet
    jobs.nightly_backup(conn, cfg, FakeRestic(fail_repo="rclone:gdrive-0:scribe-bot-backup"))
    titles = [r[0] for r in conn.execute("SELECT title FROM notification WHERE channel = 'alerts' ORDER BY id")]
    assert titles[1:] == ["🔴 Backup to Drive 0 failed two nights in a row (restic backup failed (exit 1))"]


def test_schedules_exposed():
    assert [(s.name, s.hour, s.minute) for s in jobs.SCHEDULES] == [
        ("export", 1, 30), ("trash-purge", 1, 45), ("backup", 2, 0), ("restore-test", 3, 0)]
