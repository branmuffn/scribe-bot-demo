"""Read secrets from the macOS Keychain (CLAUDE.md rule 4). Values are never written to the DB, logs, or git.

Keys are stored by scripts/setup/set_api_key.sh (service "scribe-bot.<provider>", account "brian-personal").
Reading needs the M14's logged-in session (scripts/m14_gui_run.sh), since a plain SSH session can't unlock it.
Every value read is registered with the log redactor, so it is scrubbed if it ever reaches a log line.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

from scribe_bot.core import logs

DEFAULT_ACCOUNT = "brian-personal"
Runner = Callable[..., subprocess.CompletedProcess]


class SecretMissing(LookupError):
    pass


class KeychainStore:
    def __init__(self, run: Runner = subprocess.run):
        self._run = run

    def get(self, provider: str, account: str = DEFAULT_ACCOUNT) -> str:
        if _public_demo():   # D-11: the public demo never reads a key, so no AI, Discord or backup call can start there
            raise SecretMissing(f"the public demo has no keys (scribe-bot.{provider})")
        try:
            p = self._run(["security", "find-generic-password", "-a", account, "-s", f"scribe-bot.{provider}", "-w"],
                          capture_output=True, text=True)
        except FileNotFoundError:   # no macOS Keychain on this machine (Linux, the public demo on Vercel): no keys
            raise SecretMissing(f"no Keychain on this machine for scribe-bot.{provider}") from None
        if p.returncode != 0 or not p.stdout.strip():
            # Don't pass on security's stderr: keep the message predictable and value-free.
            raise SecretMissing(f"no Keychain entry for scribe-bot.{provider} ({account}), or the Keychain is locked")
        value = p.stdout.strip()
        logs.register_secret(value)
        return value

    def exists(self, provider: str, account: str = DEFAULT_ACCOUNT) -> bool:
        if _public_demo():
            return False
        try:
            p = self._run(["security", "find-generic-password", "-a", account, "-s", f"scribe-bot.{provider}"],
                          capture_output=True, text=True)
        except FileNotFoundError:   # no macOS Keychain on this machine: nothing is stored
            return False
        return p.returncode == 0


def _public_demo() -> bool:
    return os.environ.get("SCRIBE_BOT_PUBLIC_DEMO") == "1"


def mask(value: str) -> str:
    """How a key is shown in the UI: its type prefix and last 4 characters only."""
    if len(value) <= 12:
        return "••••"
    prefix = value.split("-")[0] + "-" if "-" in value[:8] else ""
    return f"{prefix}••••{value[-4:]}"
