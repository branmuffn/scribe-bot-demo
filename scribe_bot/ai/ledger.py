"""Data Ledger (CLAUDE.md rule 2): one row for every outbound call, including blocked and failed ones.

sent_summary describes what was sent ("conversation #12 transcript, 240 lines") and never contains the content.
Months are calendar months in UTC, matching the ledger's UTC timestamps.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timezone

from scribe_bot.core.db import now


def record(conn: sqlite3.Connection, *, provider: str, model: str, task: str, sent_summary: str,
           account_id: int | None = None, conversation_id: int | None = None, tokens_in: int | None = None,
           tokens_out: int | None = None, cost_usd: float = 0.0, training_policy: str | None = None,
           status: str = "ok") -> int:
    cur = conn.execute(
        "INSERT INTO ledger_entry (time, account_id, provider, model, task, conversation_id, sent_summary, tokens_in, "
        "tokens_out, cost_usd, training_policy, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now(), account_id, provider, model, task, conversation_id, sent_summary, tokens_in, tokens_out, cost_usd,
         training_policy, status),
    )
    return cur.lastrowid


def month_key(at: datetime | None = None) -> str:
    return (at or datetime.now(timezone.utc)).strftime("%Y-%m")


def spend(conn: sqlite3.Connection, month: str | None = None, account_id: int | None = None) -> float:
    q = "SELECT coalesce(sum(cost_usd), 0) FROM ledger_entry WHERE substr(time, 1, 7) = ?"
    args: list = [month or month_key()]
    if account_id is not None:
        q += " AND account_id = ?"
        args.append(account_id)
    return round(conn.execute(q, args).fetchone()[0], 6)


def breakdown(conn: sqlite3.Connection, month: str | None = None) -> dict:
    """For the Costs page: today, month, by provider, account, and task, plus a month-end forecast."""
    m = month or month_key()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def by(col: str) -> dict:
        return {r[0]: round(r[1], 6) for r in conn.execute(
            f"SELECT {col}, sum(cost_usd) FROM ledger_entry WHERE substr(time, 1, 7) = ? GROUP BY {col}", (m,))}

    total = spend(conn, m)
    return {
        "today": round(conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM ledger_entry WHERE substr(time, 1, 10) = ?",
                                    (today,)).fetchone()[0], 6),
        "month": total,
        "by_provider": by("provider"), "by_account": by("account_id"), "by_task": by("task"),
        "forecast": forecast(total, datetime.now(timezone.utc)) if m == month_key() else total,
    }


def forecast(month_spend: float, at: datetime) -> float:
    """Straight-line month-end estimate from spend so far."""
    days = calendar.monthrange(at.year, at.month)[1]
    elapsed = at.day - 1 + (at.hour * 3600 + at.minute * 60) / 86400
    return round(month_spend * days / max(elapsed, 1), 4)
