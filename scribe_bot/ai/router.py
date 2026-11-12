"""AI router: task -> (account, model), with stages, the offline switch, caps, and a fallback chain (SPEC §8).

Stages (per task; setting 'ai_task:<task>' JSON overrides TASKS):
  0 local only  -> blocked (there is no local LLM, DECISIONS 2026-10-19)
  1 ask always  -> NeedsApproval unless the caller passes approved=True
  2 rules       -> auto when a rule in setting 'ai_rules' matches (task, optional area, max_usd); else NeedsApproval
  3 cloud default -> auto (meeting extraction)
Models come from the profile in setting 'ai_profile' (SPEC §8.2; default all_claude, i.e. Claude Sonnet for extract);
each task's chain is tried in order, so the mixes fall back across providers when an account is missing or failing.
Every attempt, including blocked and failed ones, is written to the Data Ledger.
Accounts marked may_train are never used for real data (real_data=True); only Demo mode on synthetic data.
Account order within a provider: free, then credits, then paid.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from scribe_bot.ai import costs, ledger, prices, providers

TASKS: dict[str, dict] = {  # stage per task; the chain comes from the profile
    "extract": {"stage": 3},
    "weekly_review": {"stage": 1},
    "ping": {"stage": 3},
}
SONNET, HAIKU = ["anthropic", "claude-sonnet-5-5"], ["anthropic", "claude-haiku-5-5"]
ARGON, FLASH = ["google", "gemini-4-argon"], ["google", "gemini-3.8-flash"]
SOL, LUNA = ["openai", "gpt-6.1-sol"], ["openai", "gpt-6-luna"]
PROFILES: dict[str, dict] = {
    "all_claude": {"label": "All Claude", "chains": {"extract": [SONNET], "weekly_review": [SONNET], "ping": [HAIKU]}},
    "all_gemini": {"label": "All Gemini", "chains": {"extract": [ARGON], "weekly_review": [ARGON], "ping": [FLASH]}},
    "all_openai": {"label": "All OpenAI", "chains": {"extract": [SOL], "weekly_review": [SOL], "ping": [LUNA]}},
    "budget_mix": {"label": "Budget Mix",
                   "chains": {"extract": [HAIKU, FLASH, LUNA], "weekly_review": [HAIKU, FLASH, LUNA], "ping": [HAIKU]}},
    "quality_mix": {"label": "Quality Mix",
                    "chains": {"extract": [SONNET, ARGON, SOL], "weekly_review": [SONNET, ARGON, SOL], "ping": [HAIKU]}},
}
DEFAULT_PROFILE = "all_claude"
# Cheapest model per provider, for the Accounts & Keys "Test" button.
PING_MODELS = {"anthropic": "claude-haiku-5-5", "google": "gemini-3.5-flash-lite", "openai": "gpt-6-luna"}
TYPE_ORDER = {"free": 0, "credits": 1, "paid": 2}


class Blocked(Exception):
    """No call was made: offline switch, stage 0, cap, may-train block, or no usable account."""

    def __init__(self, reason: str, kind: str):
        super().__init__(reason)
        self.kind = kind   # offline, stage0, cap, may_train, no_account


class NeedsApproval(Exception):
    def __init__(self, estimate: dict):
        super().__init__(f"needs approval (~${estimate['cost_usd']:.3f})")
        self.estimate = estimate


class CallFailed(Exception):
    pass


@dataclass
class Call:
    text: str
    cost_usd: float
    account_id: int
    account_nickname: str
    model: str
    tokens_in: int
    tokens_out: int
    stop_reason: str | None


ProviderFactory = Callable[[sqlite3.Row], object]


def default_factory(account: sqlite3.Row):
    return providers.make(account["provider"], account["secret_ref"])


def _setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def offline(conn: sqlite3.Connection) -> bool:
    return _setting(conn, "ai_offline") == "1"


def set_offline(conn: sqlite3.Connection, on: bool) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES ('ai_offline', ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", ("1" if on else "0",))


def profile(conn: sqlite3.Connection) -> str:
    name = _setting(conn, "ai_profile")
    return name if name in PROFILES else DEFAULT_PROFILE


def set_profile(conn: sqlite3.Connection, name: str) -> None:
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}")
    conn.execute("INSERT INTO setting (key, value) VALUES ('ai_profile', ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (name,))


def task_config(conn: sqlite3.Connection, task: str) -> dict:
    """Stage from TASKS, chain from the profile; setting 'ai_task:<task>' (JSON) overrides either (per-task override)."""
    chains = PROFILES[profile(conn)]["chains"]
    cfg = {"stage": TASKS.get(task, {"stage": 1})["stage"], "chain": chains.get(task, chains["extract"])}
    if override := _setting(conn, f"ai_task:{task}"):
        cfg.update(json.loads(override))
    return cfg


def _rule_allows(conn: sqlite3.Connection, task: str, area: str | None, est_usd: float) -> bool:
    for r in json.loads(_setting(conn, "ai_rules") or "[]"):
        if r.get("task") == task and r.get("area") in (None, area) and est_usd <= float(r.get("max_usd", 0)):
            return True
    return False


def accounts_for(conn: sqlite3.Connection, provider: str) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM account WHERE provider = ? AND enabled = 1", (provider,)).fetchall()
    return sorted(rows, key=lambda a: (TYPE_ORDER.get(a["type"], 9), a["id"]))


def run(conn: sqlite3.Connection, task: str, system: str, user: str, *, sent_summary: str,
        conversation_id: int | None = None, area: str | None = None, real_data: bool = True, approved: bool = False,
        max_tokens: int = 16000, factory: ProviderFactory = default_factory) -> Call:
    cfg = task_config(conn, task)
    first_model = cfg["chain"][0][1]

    def blocked(reason: str, kind: str, provider: str = cfg["chain"][0][0], model: str = first_model,
                account_id: int | None = None):
        ledger.record(conn, provider=provider, model=model, task=task, sent_summary=f"BLOCKED, not sent: {sent_summary}",
                      account_id=account_id, conversation_id=conversation_id, status="blocked")
        return Blocked(reason, kind)

    if offline(conn):
        raise blocked("offline switch is on", "offline")
    if cfg["stage"] == 0:
        raise blocked(f"{task} is set to local only, and there is no local model", "stage0")
    est = costs.estimate(first_model, system, user)
    if cfg["stage"] == 1 and not approved:
        raise NeedsApproval(est)
    if cfg["stage"] == 2 and not approved and not _rule_allows(conn, task, area, est["cost_usd"]):
        raise NeedsApproval(est)

    last_block: Blocked | None = None
    errors: list[str] = []
    for provider, model in cfg["chain"]:
        est = costs.estimate(model, system, user)
        for account in accounts_for(conn, provider):
            if account["may_train"] and real_data:
                last_block = blocked(f"{account['nickname']} may train on data; real data is never sent there",
                                     "may_train", provider, model, account["id"])
                continue
            try:
                costs.check(conn, account, est["cost_usd"])
            except costs.CapExceeded as e:
                last_block = blocked(str(e), "cap", provider, model, account["id"])
                continue
            try:
                res = factory(account).call(model, system, user, max_tokens)
            except (providers.ProviderError, LookupError) as e:
                ledger.record(conn, provider=provider, model=model, task=task, sent_summary=sent_summary,
                              account_id=account["id"], conversation_id=conversation_id,
                              training_policy=account["training_policy"], status="failed")
                errors.append(f"{account['nickname']}/{model}: {e}")
                if isinstance(e, providers.ProviderError) and not e.retryable:
                    raise CallFailed("; ".join(errors))
                continue
            cost = prices.cost(model, res.tokens_in, res.tokens_out)
            ledger.record(conn, provider=provider, model=model, task=task, sent_summary=sent_summary,
                          account_id=account["id"], conversation_id=conversation_id, tokens_in=res.tokens_in,
                          tokens_out=res.tokens_out, cost_usd=cost, training_policy=account["training_policy"])
            costs.alerts(conn, account)
            return Call(res.text, cost, account["id"], account["nickname"], model, res.tokens_in, res.tokens_out,
                        res.stop_reason)
    if errors:
        raise CallFailed("; ".join(errors))
    if last_block:
        raise last_block
    raise blocked(f"no enabled account for {task}", "no_account")
