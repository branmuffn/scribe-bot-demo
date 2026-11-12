"""Deliver queued notification rows to Discord (M5). Titles, counts, and links only; never content (SPEC §3.9).

Uses Discord webhooks (stdlib urllib), not a bot token: the existing M14 setup already stores a webhook link in the
Keychain (scripts/setup/set_api_key.sh discord). #scribe-alerts uses "scribe-bot.discord"; #scribe-bot uses
"scribe-bot.discord-bot" if present, otherwise the same webhook.

- Only rows enqueued for Discord are posted: alerts, and on #scribe-bot "waiting for your OK" and "Ready" (Brian 2026-10-22).
- Batching: all deliverable rows of a channel go out together, as few messages as Discord's size limit allows.
- Quiet hours (11 PM-7 AM local): #scribe-bot rows wait until 7 AM. Alerts still go out.
- Discord unreachable: rows stay queued/held and are retried on the next call.
- Content guard: a title over TITLE_MAX characters or with line breaks is never sent (it may contain content).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime

from scribe_bot.core import secrets
from scribe_bot.core.db import now as utc_now
from scribe_bot.discord_bot.notify import DISCORD_CHANNELS

TITLE_MAX = 150
MESSAGE_MAX = 1900               # Discord's limit is 2000 characters
QUIET_START, QUIET_END = 23, 7   # local hours
QUIET_CHANNELS = {"bot"}
WEBHOOK_SERVICES = {"alerts": ["discord"], "bot": ["discord-bot", "discord"]}

log = logging.getLogger("scribe_bot.discord")
Poster = Callable[[str, str], None]          # (webhook url, message text); raises on failure
WebhookLookup = Callable[[str], str | None]  # channel -> webhook url


def title_ok(title: str) -> bool:
    return 0 < len(title) <= TITLE_MAX and "\n" not in title and "\r" not in title


def in_quiet_hours(at: datetime) -> bool:
    return at.hour >= QUIET_START or at.hour < QUIET_END


def post_webhook(url: str, text: str) -> None:
    body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "ScribeBot (private, 0.1)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status >= 300:
            raise urllib.error.HTTPError(url, r.status, "unexpected status", r.headers, None)


def keychain_webhook(channel: str) -> str | None:
    store = secrets.KeychainStore()
    for service in WEBHOOK_SERVICES[channel]:
        try:
            return store.get(service)
        except secrets.SecretMissing:
            continue
    return None


def _line(row: sqlite3.Row, base: str = "") -> str:
    """The title and its link. An app path (/conversations/12) becomes a full address Discord can open; without the
    app's address (setting web_base_url) the path is left out rather than sent as dead text."""
    link = row["link"]
    if link and link.startswith("/"):
        link = base.rstrip("/") + link if base else None
    return f"{row['title']} <{link}>" if link else row["title"]


def _chunks(rows: list[sqlite3.Row], base: str = "") -> list[list[sqlite3.Row]]:
    out: list[list[sqlite3.Row]] = []
    size = 0
    for r in rows:
        n = len(_line(r, base)) + 1
        if not out or size + n > MESSAGE_MAX:
            out.append([])
            size = 0
        out[-1].append(r)
        size += n
    return out


def send_pending(conn: sqlite3.Connection, at: datetime | None = None, post: Poster = post_webhook,
                 webhook: WebhookLookup = keychain_webhook) -> dict:
    """Send what can be sent now. Returns counts: sent, held, blocked, waiting (Discord unreachable / no webhook)."""
    at = at or datetime.now().astimezone()
    result = {"sent": 0, "held": 0, "blocked": 0, "waiting": 0}
    # Rows from a channel that no longer goes to Discord (e.g. held overnight before DECISION 2026-10-22) stay in the app
    conn.execute(f"UPDATE notification SET status = 'sent' WHERE status IN ('queued', 'held') AND channel NOT IN "
                 f"({','.join('?' * len(DISCORD_CHANNELS))})", DISCORD_CHANNELS)
    rows = conn.execute("SELECT * FROM notification WHERE status IN ('queued', 'held') ORDER BY id").fetchall()
    base = (conn.execute("SELECT value FROM setting WHERE key = 'web_base_url'").fetchone() or [""])[0]
    for channel in DISCORD_CHANNELS:
        ready = []
        for r in (r for r in rows if r["channel"] == channel):
            if not title_ok(r["title"]):
                result["blocked"] += 1
                if r["status"] != "held":
                    conn.execute("UPDATE notification SET status = 'held' WHERE id = ?", (r["id"],))
                    log.warning("notification %d blocked by the content guard (title too long or multi-line)", r["id"])
                continue
            ready.append(r)
        if not ready:
            continue
        if channel in QUIET_CHANNELS and in_quiet_hours(at):
            conn.execute(f"UPDATE notification SET status = 'held' WHERE id IN ({','.join('?' * len(ready))})",
                         [r["id"] for r in ready])
            result["held"] += len(ready)
            continue
        url = webhook(channel)
        if not url:
            result["waiting"] += len(ready)
            log.warning("no Discord webhook for %s; %d notifications waiting", channel, len(ready))
            continue
        chunks = _chunks(ready, base)
        for i, chunk in enumerate(chunks):
            try:
                post(url, "\n".join(_line(r, base) for r in chunk))
            except (OSError, urllib.error.URLError) as e:
                waiting = sum(len(c) for c in chunks[i:])
                result["waiting"] += waiting
                log.warning("Discord unreachable for %s (%s); %d notifications will retry", channel, type(e).__name__, waiting)
                break
            conn.execute(f"UPDATE notification SET status = 'sent', sent_at = ? WHERE id IN ({','.join('?' * len(chunk))})",
                         [utc_now(), *[r["id"] for r in chunk]])
            result["sent"] += len(chunk)
    return result
