"""Accounts & Keys helpers for the web app (SPEC §8.4, A19). Keys are never read here except by test_key."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from scribe_bot.ai import ledger, prices, providers, router
from scribe_bot.core.db import now
from scribe_bot.core.secrets import KeychainStore

# Training policy per provider and account type. Gemini's free tier (no billing on the Google project) uses
# prompts to improve Google products, so it is may_train: Demo mode on synthetic data only (CLAUDE.md rule 2).
POLICIES = {
    ("anthropic", "paid"): ("API: no training on content by default", 0),
    ("anthropic", "credits"): ("API: no training on content by default", 0),
    ("google", "paid"): ("Gemini API paid tier: not used to improve Google products", 0),
    ("google", "free"): ("Gemini API free tier: MAY TRAIN (used to improve Google products)", 1),
    ("openai", "paid"): ("API: not used for training by default", 0),
    ("openai", "credits"): ("API: not used for training by default", 0),
}


def add_account(conn: sqlite3.Connection, provider: str, nickname: str, owner: str = "Brian", type: str = "paid",
                monthly_cap_usd: float | None = None) -> int:
    """Create the account row once Brian has stored its key (scripts/setup/set_api_key.sh <provider>).

    The key itself stays in the Keychain; secret_ref names the service (scribe-bot.<provider>, account brian-personal,
    as set_api_key.sh stores it). Unknown provider/type pairs are refused.
    """
    if (provider, type) not in POLICIES:
        raise ValueError(f"unsupported account: {provider} ({type})")
    policy, may_train = POLICIES[(provider, type)]
    secret_ref = f"scribe-bot.{provider}"
    cur = conn.execute(
        "INSERT INTO account (provider, nickname, owner, type, monthly_cap_usd, training_policy, may_train, secret_ref,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (provider, nickname, owner, type, monthly_cap_usd, policy, may_train, secret_ref, now()))
    return cur.lastrowid


def list_accounts(conn: sqlite3.Connection, store: KeychainStore | None = None) -> list[dict]:
    store = store or KeychainStore()
    out = []
    for a in conn.execute("SELECT * FROM account ORDER BY provider, id"):
        d = dict(a)
        d["key_present"] = store.exists(a["secret_ref"].removeprefix("scribe-bot."))
        d["month_spend"] = ledger.spend(conn, account_id=a["id"])
        out.append(d)
    return out


def test_key(conn: sqlite3.Connection, account_id: int, factory: router.ProviderFactory = router.default_factory) -> dict:
    """The Test button: a tiny ping (fractions of a cent), written to the ledger."""
    a = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    model = router.PING_MODELS[a["provider"]]
    try:
        res = factory(a).call(model, "Reply with the single word OK.", "ping", 5)
    except (providers.ProviderError, LookupError) as e:
        ledger.record(conn, provider=a["provider"], model=model, task="ping", account_id=a["id"],
                      sent_summary="the word 'ping' (key test)", training_policy=a["training_policy"], status="failed")
        return {"ok": False, "error": str(e)}
    cost = prices.cost(model, res.tokens_in, res.tokens_out)
    ledger.record(conn, provider=a["provider"], model=model, task="ping", account_id=a["id"],
                  sent_summary="the word 'ping' (key test)", tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                  cost_usd=cost, training_policy=a["training_policy"])
    return {"ok": res.text.strip().upper().startswith("OK"), "cost_usd": cost}


def health(conn: sqlite3.Connection, at: datetime | None = None) -> list[str]:
    """Account health check: keys unused 90+ days, expiring within 14 days, recent failed calls."""
    at = at or datetime.now(timezone.utc)
    warnings = []
    for a in conn.execute("SELECT * FROM account WHERE enabled = 1"):
        last = conn.execute("SELECT max(time) FROM ledger_entry WHERE account_id = ? AND status = 'ok'",
                            (a["id"],)).fetchone()[0]
        if last is None or datetime.fromisoformat(last) < at - timedelta(days=90):
            warnings.append(f"{a['nickname']}: not used in 90+ days")
        if a["expires_at"] and datetime.fromisoformat(a["expires_at"]).replace(tzinfo=timezone.utc) < at + timedelta(days=14):
            warnings.append(f"{a['nickname']}: expires {a['expires_at'][:10]}")
        fails = conn.execute("SELECT count(*) FROM ledger_entry WHERE account_id = ? AND status = 'failed' AND time >= ?",
                             (a["id"], (at - timedelta(days=1)).isoformat(timespec="seconds"))).fetchone()[0]
        if fails >= 3:
            warnings.append(f"{a['nickname']}: {fails} failed calls in the last day")
    return warnings
