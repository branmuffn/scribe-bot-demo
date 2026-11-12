"""Cost estimates, hard caps, and 50/80/100% alerts (CLAUDE.md rule 7).

Estimates are made before every call (tokens ≈ characters / 4). A call is blocked if the estimate would push the
month past the global cap ($10 default, setting 'global_cap_usd') or the account's monthly_cap_usd.
Alerts go to #scribe-alerts once per threshold, per scope, per month.
"""
from __future__ import annotations

import json
import sqlite3

from scribe_bot.ai import ledger, prices
from scribe_bot.core.db import now
from scribe_bot.discord_bot import notify

DEFAULT_GLOBAL_CAP = 10.0
OUTPUT_TOKENS_GUESS = 4000   # extraction used ~1.7k output tokens on a 42-min call; err high
THRESHOLDS = (50, 80, 100)


class CapExceeded(Exception):
    pass


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate(model: str, system: str, user: str, output_tokens: int = OUTPUT_TOKENS_GUESS) -> dict:
    tokens_in = estimate_tokens(system) + estimate_tokens(user)
    return {"model": model, "tokens_in": tokens_in, "tokens_out": output_tokens,
            "cost_usd": prices.cost(model, tokens_in, output_tokens)}


def global_cap(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM setting WHERE key = 'global_cap_usd'").fetchone()
    return float(row[0]) if row else DEFAULT_GLOBAL_CAP


def check(conn: sqlite3.Connection, account: sqlite3.Row, est_usd: float) -> None:
    """Raise CapExceeded if this call could take the month over the global or the account cap."""
    month_total = ledger.spend(conn)
    cap = global_cap(conn)
    if month_total + est_usd > cap:
        raise CapExceeded(f"global cap: ${month_total:.2f} spent + ~${est_usd:.2f} would pass ${cap:.2f}")
    if account["monthly_cap_usd"] is not None:
        acct_total = ledger.spend(conn, account_id=account["id"])
        if acct_total + est_usd > account["monthly_cap_usd"]:
            raise CapExceeded(f"{account['nickname']} cap: ${acct_total:.2f} spent + ~${est_usd:.2f} would pass "
                              f"${account['monthly_cap_usd']:.2f}")


def _alert_once(conn: sqlite3.Connection, scope: str, label: str, spent: float, cap: float) -> list[int]:
    sent = []
    month = ledger.month_key()
    for pct in THRESHOLDS:
        if cap <= 0 or spent < cap * pct / 100:
            continue
        key = f"cap_alert:{scope}:{month}:{pct}"
        if conn.execute("SELECT 1 FROM setting WHERE key = ?", (key,)).fetchone():
            continue
        conn.execute("INSERT INTO setting (key, value) VALUES (?, '1')", (key,))
        icon = "🔴" if pct >= 100 else "⚠️"
        notify.enqueue(conn, "alerts", f"{icon} AI spend {label}: {pct}% of the ${cap:.2f} monthly cap (${spent:.2f})", "/costs")
        sent.append(pct)
    return sent


def alerts(conn: sqlite3.Connection, account: sqlite3.Row | None = None) -> list[int]:
    """Call after any recorded cost. Returns the thresholds newly alerted (global scope)."""
    fired = _alert_once(conn, "global", "this month", ledger.spend(conn), global_cap(conn))
    if account is not None and account["monthly_cap_usd"]:
        _alert_once(conn, f"account{account['id']}", f"on {account['nickname']}",
                    ledger.spend(conn, account_id=account["id"]), account["monthly_cap_usd"])
    return fired


CONSOLE_TASK = "console_match"
CONSOLE_KEY = "console_check"   # setting: JSON {time, spent, credits} from Brian's last look at the Anthropic console


def match_console(conn: sqlite3.Connection, spent: float, credits: float | None = None, at: str | None = None) -> float:
    """Bring this month's spend up (or down) to what the Anthropic console shows. Returns the gap recorded.

    The gap (setup tests, other use of the key) becomes one ledger row with status 'adjustment' and no tokens, so every
    total (Costs, Today, briefing, caps) includes it. Nothing is sent anywhere. Later calls add on top as usual.
    """
    when = at or now()
    so_far = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM ledger_entry WHERE substr(time, 1, 7) = ? AND time <= ?",
                          (when[:7], when)).fetchone()[0]
    gap = round(spent - so_far, 4)
    if abs(gap) >= 0.005:
        conn.execute(
            "INSERT INTO ledger_entry (time, account_id, provider, model, task, sent_summary, cost_usd, status) "
            "VALUES (?, 1, 'anthropic', 'console', ?, ?, ?, 'adjustment')",
            (when, CONSOLE_TASK, f"Matched to the Anthropic console (${spent:.2f} this month); "
                                 "spend outside Scribe Bot, nothing was sent", gap))
    conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (CONSOLE_KEY, json.dumps({"time": when, "spent": spent, "credits": credits,
                                           "last_id": conn.execute("SELECT coalesce(max(id), 0) FROM ledger_entry").fetchone()[0]})))
    return gap


def console_check(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (CONSOLE_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def credits_left(conn: sqlite3.Connection) -> float | None:
    """Console credits at the last check, minus Anthropic spend Scribe Bot recorded since. None if never entered."""
    c = console_check(conn)
    if not c or c.get("credits") is None:
        return None
    since = conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM ledger_entry WHERE provider = 'anthropic' "
                         "AND status != 'adjustment' AND id > ?", (c.get("last_id", 0),)).fetchone()[0]
    return round(c["credits"] - since, 4)


def main(argv: list[str] | None = None) -> int:
    """python -m scribe_bot.ai.costs match-console SPENT [--credits N] [--at ISO]: the same as the form on Costs."""
    import argparse

    from scribe_bot.core import db, settings
    ap = argparse.ArgumentParser(prog="python -m scribe_bot.ai.costs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mc = sub.add_parser("match-console")
    mc.add_argument("spent", type=float)
    mc.add_argument("--credits", type=float)
    mc.add_argument("--at", help="when the console was read (UTC ISO); default now")
    a = ap.parse_args(argv)
    conn = db.connect(settings.load().db_path)
    with conn:
        gap = match_console(conn, a.spent, a.credits, a.at)
    print(json.dumps({"gap_usd": gap, "month_usd": ledger.spend(conn), "credits_left": credits_left(conn)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
