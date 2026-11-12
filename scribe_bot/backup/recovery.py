"""Recovery sheet (SPEC §3.11): the backup password plus restore steps, given to Brian once to keep in his password
manager or print. Without the password, backups can't be restored.

The password is shown only when explicitly asked for (--with-password) and only on a real terminal, so it can't end
up in a log file, a launchd log, or a pipe. It is read from the Keychain at that moment and never stored elsewhere.
"""
from __future__ import annotations

from collections.abc import Callable

from scribe_bot.backup.restic import PASSWORD_COMMAND, REPO_FOLDER


def sheet(dests: list[dict], password: str | None = None) -> str:
    lines = [
        "SCRIBE BOT RECOVERY SHEET",
        "Keep this in your password manager or print it. Anyone with it can read the backups.",
        "",
        "Backup password: " + (password if password else "(not shown; run with --with-password in Terminal on the M14)"),
        f"  Stored on the M14 in the Keychain: {PASSWORD_COMMAND}",
        "",
        "Backup locations (each is a full, separate copy, encrypted before upload):",
    ]
    for d in dests:
        lines.append(f"  - {d['nickname']}: Google Drive account {d['email']}, folder '{REPO_FOLDER}' (rclone remote '{d['remote']}')")
    if not dests:
        lines.append("  (none set up yet)")
    lines += [
        "",
        "To restore on any Mac:",
        "  1. Install the tools:            brew install restic rclone",
        "  2. Connect the Drive account:    rclone config   (new remote, type 'drive', sign in with that account)",
        "  3. See what's in the backup:     restic -r rclone:<remote>:" + REPO_FOLDER + " snapshots",
        "     (it asks for the backup password above)",
        "  4. Restore everything:           restic -r rclone:<remote>:" + REPO_FOLDER + " restore latest --target ~/restored",
        "  5. The database is in backup-staging/scribe-bot.db inside the restored folder; copy it to",
        "     ~/scribe-bot-data/scribe-bot.db along with the rest of the restored scribe-bot-data folder.",
        "  6. Secrets (API keys, Discord webhook) are NOT in backups. Re-enter them with",
        "     scripts/setup/set_api_key.sh on the new machine.",
    ]
    return "\n".join(lines)


def print_sheet(dests: list[dict], with_password: bool, get_password: Callable[[], str], isatty: bool,
                write: Callable[[str], object] = print) -> int:
    if with_password and not isatty:
        write("Refusing to print the backup password outside a real Terminal window (it could end up in a file or log).")
        return 2
    write(sheet(dests, get_password() if with_password else None))
    return 0
