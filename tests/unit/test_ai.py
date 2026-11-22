"""M3 AI layer on synthetic data only. No network: providers are faked; no Keychain: the store is faked."""
import io
import json
import subprocess
import urllib.error
from datetime import datetime, timezone

import pytest

from scribe_bot.ai import accounts, costs, extract, ledger, prices, prompts, providers, router
from scribe_bot.core.db import now
from scribe_bot.core.secrets import KeychainStore

GOOD = {
    "summary": "Alex and Brian planned the spring fair. Brian will book the hall; Alex will send the budget.",
    "decisions": [{"text": "Hold the fair on a Saturday", "line": 1}],
    "tasks": [{"title": "Book the hall", "owner": "Brian", "due": "next week", "confidence": 0.9, "line": 2},
              {"title": "Check parking", "owner": "Brian", "due": None, "confidence": 0.4, "line": 2}],
    "waiting_on": [{"title": "Send the budget", "owner": "Alex", "due": "Friday", "confidence": 0.8, "line": 3}],
    "people_facts": [{"person": "Alex", "fact": "Has a daughter starting school", "line": 0}],
}


class FakeProvider:
    def __init__(self, replies, calls):
        self.replies, self.calls = replies, calls

    def call(self, model, system, user, max_tokens=16000):
        self.calls.append({"model": model, "system": system, "user": user})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        text = r if isinstance(r, str) else json.dumps(r)
        return providers.Result(text, 1000, 200, "end_turn", 1.0, model)


def factory(replies, calls=None):
    calls = [] if calls is None else calls
    return lambda account: FakeProvider(replies, calls)


def make_conversation(conn, private=0, area="work"):
    t = now()
    cid = conn.execute("INSERT INTO conversation (title, private, area, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                       ("Synthetic planning call", private, area, t, t)).lastrowid
    alex = conn.execute("INSERT INTO person (name, created_at) VALUES ('Alex Rivera', ?)", (t,)).lastrowid
    s_alex = conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id) VALUES (?, 0, ?)", (cid, alex)).lastrowid
    s_me = conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id) VALUES (?, 1, 1)", (cid,)).lastrowid
    lines = [(s_alex, "My daughter starts school next month."), (s_me, "Let's do Saturday for the fair."),
             (s_me, "I'll book the hall next week."), (s_alex, "I'll send the budget by Friday, frontier comments said so.")]
    for i, (spk, text) in enumerate(lines):
        conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) VALUES (?, ?, ?, ?, ?, ?)",
                     (cid, i, i * 5.0, i * 5.0 + 4, spk, text))
    return cid, alex


def ledger_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM ledger_entry ORDER BY id")]


# --- prices, costs, ledger ---

def test_prices_verified_table():
    assert prices.PRICES["claude-sonnet-5-5"] == (2.00, 10.00)
    assert prices.cost("claude-sonnet-5-5", 14136, 1687) == pytest.approx(0.045142)
    with pytest.raises(KeyError):
        prices.cost("unknown-model", 1, 1)


def test_estimate_uses_chars_over_four():
    est = costs.estimate("claude-sonnet-5-5", "x" * 400, "y" * 4000, output_tokens=1000)
    assert est["tokens_in"] == 1100 and est["cost_usd"] == pytest.approx(0.0122)


def test_forecast_straight_line():
    assert ledger.forecast(1.0, datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)) == pytest.approx(3.1)


def test_alerts_fire_once_per_threshold(conn):
    acct = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    ledger.record(conn, provider="anthropic", model="m", task="extract", sent_summary="x", account_id=1, cost_usd=5.5)
    assert costs.alerts(conn, acct) == [50]
    assert costs.alerts(conn, acct) == []
    ledger.record(conn, provider="anthropic", model="m", task="extract", sent_summary="x", account_id=1, cost_usd=4.6)
    assert costs.alerts(conn, acct) == [80, 100]
    titles = [r[0] for r in conn.execute("SELECT title FROM notification WHERE channel = 'alerts'")]
    assert len(titles) == 6  # 3 global + 3 for the account (same $10 cap)
    assert all("$" in t for t in titles)



def test_match_console_adds_outside_spend_once_and_counts_toward_cap(conn):
    """Brian's console reading becomes this month's total; later calls add on top; credits count down from the reading."""
    ledger.record(conn, provider="anthropic", model="claude-sonnet-5-5", task="extract", sent_summary="x", account_id=1, cost_usd=0.055)
    at = now()
    assert costs.match_console(conn, 0.21, 4.80, at=at) == pytest.approx(0.155)
    assert ledger.spend(conn) == pytest.approx(0.21)
    assert costs.credits_left(conn) == pytest.approx(4.80)
    ledger.record(conn, provider="anthropic", model="claude-sonnet-5-5", task="extract", sent_summary="x", account_id=1, cost_usd=0.03)
    assert ledger.spend(conn) == pytest.approx(0.24) and costs.credits_left(conn) == pytest.approx(4.77)
    assert costs.match_console(conn, 0.24) == 0      # already matching: no new row
    assert conn.execute("SELECT count(*) FROM ledger_entry WHERE status = 'adjustment'").fetchone()[0] == 1
    conn.execute("INSERT INTO setting (key, value) VALUES ('global_cap_usd', '0.25')")
    with pytest.raises(costs.CapExceeded):
        costs.check(conn, conn.execute("SELECT * FROM account WHERE id = 1").fetchone(), 0.02)


# --- router ---

def test_offline_switch_blocks_and_ledgers(conn):
    router.set_offline(conn, True)
    calls = []
    with pytest.raises(router.Blocked) as e:
        router.run(conn, "extract", "s", "u", sent_summary="test", factory=factory([GOOD], calls))
    assert e.value.kind == "offline" and calls == []
    assert ledger_rows(conn)[0]["status"] == "blocked"


def test_stage1_needs_approval_then_runs(conn):
    with pytest.raises(router.NeedsApproval) as e:
        router.run(conn, "weekly_review", "s", "u", sent_summary="t", factory=factory(["ok"]))
    assert e.value.estimate["cost_usd"] > 0
    call = router.run(conn, "weekly_review", "s", "u", sent_summary="t", approved=True, factory=factory(["ok"]))
    assert call.text == "ok"


def test_stage2_rule_auto_approves_cheap_runs(conn):
    conn.execute("INSERT INTO setting VALUES ('ai_task:weekly_review', '{\"stage\": 2}')")
    conn.execute("INSERT INTO setting VALUES ('ai_rules', ?)", (json.dumps([{"task": "weekly_review", "max_usd": 0.05}]),))
    assert router.run(conn, "weekly_review", "s", "u", sent_summary="t", factory=factory(["ok"])).text == "ok"
    with pytest.raises(router.NeedsApproval):
        router.run(conn, "weekly_review", "s", "u" * 80000, sent_summary="t", factory=factory(["ok"]))


def test_may_train_account_blocked_for_real_data(conn):
    conn.execute("UPDATE account SET enabled = 0 WHERE id = 1")
    conn.execute("INSERT INTO account (provider, nickname, owner, type, training_policy, may_train, secret_ref, created_at) "
                 "VALUES ('anthropic', 'Free test lane', 'Brian', 'free', 'may train', 1, 'scribe-bot.free', ?)", (now(),))
    calls = []
    with pytest.raises(router.Blocked) as e:
        router.run(conn, "extract", "s", "u", sent_summary="real", factory=factory([GOOD], calls))
    assert e.value.kind == "may_train" and calls == []
    # Demo mode (synthetic data) may use it.
    assert router.run(conn, "extract", "s", "u", sent_summary="demo", real_data=False, factory=factory(["ok"])).text == "ok"


def test_over_cap_run_is_blocked(conn):
    ledger.record(conn, provider="anthropic", model="m", task="extract", sent_summary="x", account_id=1, cost_usd=9.999)
    calls = []
    with pytest.raises(router.Blocked) as e:
        router.run(conn, "extract", "s", "u" * 4000, sent_summary="t", factory=factory([GOOD], calls))
    assert e.value.kind == "cap" and calls == []


def test_free_accounts_tried_first_and_fallback_on_retryable_error(conn):
    conn.execute("INSERT INTO account (provider, nickname, owner, type, training_policy, secret_ref, created_at) "
                 "VALUES ('anthropic', 'Credits', 'Family', 'credits', 'no training', 'scribe-bot.credits', ?)", (now(),))
    used = []

    def f(account):
        used.append(account["nickname"])
        return FakeProvider([providers.ProviderError("overloaded", 529)] if account["type"] == "credits" else ["ok"], [])

    call = router.run(conn, "extract", "s", "u", sent_summary="t", factory=f)
    assert used == ["Credits", "Brian personal"] and call.account_nickname == "Brian personal"
    assert [r["status"] for r in ledger_rows(conn)] == ["failed", "ok"]


def test_non_retryable_error_stops(conn):
    with pytest.raises(router.CallFailed):
        router.run(conn, "extract", "s", "u", sent_summary="t",
                   factory=factory([providers.ProviderError("bad request", 400)]))


def test_successful_call_is_ledgered_with_cost(conn):
    router.run(conn, "extract", "s", "u", sent_summary="conversation #1 transcript", conversation_id=None,
               factory=factory(["ok"]))
    row = ledger_rows(conn)[0]
    assert row["status"] == "ok" and row["tokens_in"] == 1000 and row["cost_usd"] == pytest.approx(0.004)
    assert row["training_policy"] and row["sent_summary"] == "conversation #1 transcript"


# --- provider adapter (HTTP faked) ---

def fake_store(key="sk-ant-FAKEFAKEFAKEFAKE0000"):
    return KeychainStore(lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=key + "\n", stderr=""))


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_anthropic_adapter_request_and_parse():
    seen = {}

    def opener(req, timeout):
        seen["headers"] = dict(req.header_items())
        seen["body"] = json.loads(req.data)
        return FakeResp(json.dumps({"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "hi"}],
                                    "usage": {"input_tokens": 12, "output_tokens": 3}, "stop_reason": "end_turn",
                                    "model": "claude-sonnet-5-5"}).encode())

    res = providers.Anthropic("scribe-bot.anthropic", fake_store(), opener).call("claude-sonnet-5-5", "sys", "hello", 100)
    assert res.text == "hi" and res.tokens_in == 12 and res.stop_reason == "end_turn"
    assert seen["body"]["fallbacks"] == "default" and seen["body"]["system"] == "sys"
    assert seen["headers"]["Anthropic-beta"] == providers.FALLBACK_BETA


def test_anthropic_adapter_error_never_contains_key():
    key = "sk-ant-FAKEFAKEFAKEFAKE9999"

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(f"bad key {key}".encode()))

    with pytest.raises(providers.ProviderError) as e:
        providers.Anthropic("scribe-bot.anthropic", fake_store(key), opener).call("claude-sonnet-5-5", "s", "u")
    assert key not in str(e.value) and e.value.status == 401 and not e.value.retryable


# --- prompts ---

def test_parse_extract_accepts_good_and_rejects_bad():
    assert prompts.parse_extract("Here you go:\n" + json.dumps(GOOD))["tasks"][0]["title"] == "Book the hall"
    with pytest.raises(prompts.InvalidOutput):
        prompts.parse_extract("no json here")
    with pytest.raises(prompts.InvalidOutput):
        prompts.parse_extract(json.dumps({**GOOD, "tasks": "none"}))


# --- extract_conversation ---

def test_private_conversation_makes_no_call(conn):
    cid, _ = make_conversation(conn, private=1)
    calls = []
    assert extract.extract_conversation(conn, cid, factory=factory([GOOD], calls))["status"] == "skipped_private"
    assert calls == [] and ledger_rows(conn) == []


def test_extract_writes_rows(conn):
    cid, alex = make_conversation(conn)
    conn.execute("INSERT INTO glossary_term (term, variants, approved, created_at) VALUES "
                 "('Frontier Commons', '[\"frontier comments\"]', 1, ?)", (now(),))
    calls = []
    out = extract.extract_conversation(conn, cid, factory=factory([GOOD], calls))
    assert out["status"] == "done" and out["cost_usd"] == pytest.approx(0.004)
    assert "[0] Alex Rivera: My daughter" in calls[0]["user"] and "Frontier Commons said so" in calls[0]["user"]
    assert "frontier comments" not in calls[0]["user"]

    conv = conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone()
    assert conv["status"] == "extracted" and conv["processed_by"] == "Brian personal:claude-sonnet-5-5"
    tasks = {r["title"]: r for r in conn.execute("SELECT * FROM task WHERE conversation_id = ?", (cid,))}
    assert tasks["Book the hall"]["owner_person_id"] == 1
    assert tasks["Send the budget"]["owner_person_id"] == alex          # "Alex" matched "Alex Rivera"
    assert tasks["Check parking"]["inbox"] == 1                          # confidence 0.4
    line2 = conn.execute("SELECT id FROM segment WHERE conversation_id = ? AND idx = 2", (cid,)).fetchone()[0]
    assert tasks["Book the hall"]["source_segment_id"] == line2
    fact = conn.execute("SELECT * FROM person_fact").fetchone()
    assert fact["person_id"] == alex and fact["person_name"] == "Alex"
    assert conn.execute("SELECT count(*) FROM review_item WHERE kind = 'inbox'").fetchone()[0] == 1
    hits = conn.execute("SELECT kind FROM search_index WHERE search_index MATCH 'budget'").fetchall()
    assert {h[0] for h in hits} == {"summary", "task"}


def test_unknown_owner_creates_person(conn):
    cid, _ = make_conversation(conn)
    data = {**GOOD, "waiting_on": [{"title": "Share slides", "owner": "Jordan", "due": None, "confidence": 0.9, "line": 3}]}
    extract.extract_conversation(conn, cid, factory=factory([data]))
    jordan = conn.execute("SELECT id FROM person WHERE name = 'Jordan'").fetchone()
    assert jordan and conn.execute("SELECT owner_person_id FROM task WHERE title = 'Share slides'").fetchone()[0] == jordan[0]


def test_rerun_replaces_ai_rows_but_keeps_brians_edits(conn):
    cid, _ = make_conversation(conn)
    extract.extract_conversation(conn, cid, factory=factory([GOOD]))
    conn.execute("UPDATE task SET status = 'done' WHERE title = 'Book the hall'")
    extract.extract_conversation(conn, cid, factory=factory([GOOD]))
    titles = sorted(r[0] for r in conn.execute("SELECT title FROM task WHERE conversation_id = ?", (cid,)))
    assert titles == ["Book the hall", "Book the hall", "Check parking", "Send the budget"]
    assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM search_index WHERE kind = 'summary'").fetchone()[0] == 1


def test_invalid_output_retries_once_then_inbox(conn):
    cid, _ = make_conversation(conn)
    out = extract.extract_conversation(conn, cid, factory=factory(["not json", "still not json"]))
    assert out["status"] == "inbox" and out["cost_usd"] == pytest.approx(0.008)
    assert len(ledger_rows(conn)) == 2
    assert conn.execute("SELECT count(*) FROM review_item WHERE kind = 'inbox'").fetchone()[0] == 1


def test_invalid_then_valid(conn):
    cid, _ = make_conversation(conn)
    assert extract.extract_conversation(conn, cid, factory=factory(["oops", GOOD]))["status"] == "done"


def test_over_cap_extract_reports_blocked_cap(conn):
    cid, _ = make_conversation(conn)
    ledger.record(conn, provider="anthropic", model="m", task="extract", sent_summary="x", account_id=1, cost_usd=10)
    assert extract.extract_conversation(conn, cid, factory=factory([GOOD]))["status"] == "blocked_cap"


def test_estimate_cost_preview(conn):
    cid, _ = make_conversation(conn)
    est = extract.estimate_cost(conn, cid)
    assert est["model"] == "claude-sonnet-5-5" and est["account"] == "Brian personal" and est["blocked"] is None
    assert est["cost_usd_est"] > 0 and est["lines"] == 4
    pid, _ = make_conversation(conn, private=1)
    assert extract.estimate_cost(conn, pid)["blocked"].startswith("private")


# --- accounts ---

def test_list_accounts_and_test_key(conn):
    present = KeychainStore(lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    rows = accounts.list_accounts(conn, present)
    assert rows[0]["key_present"] and "sk-" not in json.dumps(rows, default=str)
    out = accounts.test_key(conn, 1, factory=factory(["OK"]))
    assert out["ok"] and ledger_rows(conn)[0]["task"] == "ping"


def test_account_health_flags_unused_and_expiring(conn):
    conn.execute("UPDATE account SET expires_at = '2026-10-25' WHERE id = 1")
    w = accounts.health(conn, datetime(2026, 10, 19, tzinfo=timezone.utc))
    assert any("90+ days" in x for x in w) and any("expires" in x for x in w)


def test_learning_type_has_a_template_and_the_database_accepts_it(conn):
    from scribe_bot.ai import prompts
    text = prompts.extract_system("Brian", "learning")
    assert "Meeting type: Learning" in text and "never turn advice to the audience into tasks" in text
    conn.execute("INSERT INTO conversation (title, type, created_at, updated_at) VALUES ('Talk', 'learning', 'x', 'x')")


def test_ai_can_switch_an_unpicked_conversation_to_learning(conn):
    from scribe_bot.ai import extract
    for title, chosen in (("Auto", 0), ("Picked", 1)):
        cid = conn.execute("INSERT INTO conversation (title, type, type_chosen, created_at, updated_at) "
                           "VALUES (?, 'multi', ?, 'x', 'x')", (title, chosen)).lastrowid
        conv = conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone()
        extract._write(conn, conv, {"summary": "Key ideas.", "learning": True, "tasks": [], "decisions": [],
                                    "waiting_on": [], "people_facts": []}, "test")
    assert [r[0] for r in conn.execute("SELECT type FROM conversation WHERE title IN ('Auto', 'Picked') ORDER BY id")] == \
        ["learning", "multi"]


def test_compare_keeps_both_until_brian_picks(conn):
    # Brian 2026-11-19: run the other model on the same transcript, see both, keep one; no re-transcribing
    cid, _ = make_conversation(conn)
    extract.extract_conversation(conn, cid, factory=factory([GOOD]))                 # Sonnet made the current notes
    assert extract.other_model(conn, cid) == router.HAIKU
    alt = {**GOOD, "summary": "Haiku's take.", "tasks": [], "waiting_on": [{"title": "Budget", "owner": "Alex", "line": 3}]}
    calls = []
    out = extract.extract_conversation(conn, cid, factory=factory([alt], calls), approved=True,
                                       chain=[extract.other_model(conn, cid)], keep=False)
    assert out["status"] == "compared" and calls[0]["model"] == "claude-haiku-5-5"
    conv = conn.execute("SELECT summary, processed_by FROM conversation WHERE id = ?", (cid,)).fetchone()
    assert conv["summary"].startswith("Alex and Brian") and "sonnet" in conv["processed_by"]   # current untouched
    assert extract.compared(conn, cid)["data"]["summary"] == "Haiku's take."
    extract.keep_compared(conn, cid, "other")
    conv = conn.execute("SELECT summary, processed_by FROM conversation WHERE id = ?", (cid,)).fetchone()
    assert conv["summary"] == "Haiku's take." and "haiku" in conv["processed_by"] and extract.compared(conn, cid) is None
    assert [r[0] for r in conn.execute("SELECT title FROM task WHERE conversation_id = ?", (cid,))] == ["Budget"]
    assert extract.other_model(conn, cid) == router.SONNET                          # next compare flips back
