"""`python -m scribe_bot.backup <command>` on the M14 (from ~/scribe-bot, via `~/.local/bin/uv run`).

  status                                   last backup and restore test per destination (JSON, no content)
  add-destination NICK REMOTE EMAIL        register an rclone Google Drive remote as a backup destination
  remove-destination NICK
  backup-now | restore-test-now | export-now
  recovery-sheet [--with-password]         restore steps (+ the password, only in a real Terminal window)
Commands that read the Keychain (backup, restore test, recovery sheet) need the logged-in session: run them in
Terminal on the M14 or through scripts/m14_gui_run.sh.
"""
import argparse
import json
import sys

from scribe_bot.backup import export, recovery, restic
from scribe_bot.core import db, secrets, settings


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m scribe_bot.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("add-destination")
    a.add_argument("nickname")
    a.add_argument("remote")
    a.add_argument("email")
    sub.add_parser("remove-destination").add_argument("nickname")
    for name in ("backup-now", "restore-test-now", "export-now"):
        sub.add_parser(name)
    sub.add_parser("recovery-sheet").add_argument("--with-password", action="store_true")
    args = ap.parse_args()

    cfg = settings.load()
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    if args.cmd == "status":
        print(json.dumps(restic.status(conn), indent=1))
    elif args.cmd == "add-destination":
        restic.add_destination(conn, args.nickname, args.remote, args.email)
        print(f"added {args.nickname}")
    elif args.cmd == "remove-destination":
        restic.remove_destination(conn, args.nickname)
        print(f"removed {args.nickname}")
    elif args.cmd == "backup-now":
        print(json.dumps(restic.run_backup(conn, cfg)))
    elif args.cmd == "restore-test-now":
        print(json.dumps(restic.restore_test(conn, cfg)))
    elif args.cmd == "export-now":
        print(json.dumps(export.export_all(conn, cfg)))
    else:
        return recovery.print_sheet(restic.destinations(conn), args.with_password,
                                    lambda: secrets.KeychainStore().get("restic"), sys.stdout.isatty())
    return 0


if __name__ == "__main__":
    sys.exit(main())
