"""Web app <-> AI layer: re-run AI, approvals that actually run, Accounts & Keys, profile, pause switch. Synthetic data only."""
import json
import logging

from test_web_pages import env, one  # noqa: F401  (env is a fixture)

from scribe_bot import worker
from scribe_bot.ai import jobs as ai_jobs
from scribe_bot.core import queue


def jobs(conn):
    return [(r["kind"], json.loads(r["payload"]), r["status"]) for r in conn.execute("SELECT * FROM job ORDER BY id")]


def test_conversation_page_shows_rerun_with_cost_preview(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert "Re-run AI" in page and "/conversations/1/rerun" in page and "~$0.0" in page


def test_rerun_queues_one_approved_job(env):
    client, conn, _ = env
    r = client.post("/conversations/1/rerun", follow_redirects=False)    # first tap only shows the cost
    assert r.status_code == 303 and r.headers["location"] == "/conversations/1/rerun/confirm"
    assert jobs(conn) == []
    panel = client.get("/conversations/1/rerun/confirm", headers={"HX-Request": "true"}).text
    assert "This will cost about" in panel and "Claude Sonnet" in panel and "<html" not in panel   # inline fragment
    r = client.post("/conversations/1/rerun", data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/conversations/1?rerun=queued"
    client.post("/conversations/1/rerun", data={"confirm": "1"})  # double click: still one job
    assert jobs(conn) == [("ai_rerun", {"conversation_id": 1, "approved": True}, "pending")]
    page = client.get("/conversations/1?rerun=queued").text
    assert "AI re-run queued" in page and 'action="/conversations/1/rerun"' not in page


def test_transcript_only_talk_gets_ai_from_the_confirm_step(env):
    """Round 7 (Brian, 2026-11-21): no Private. A talk without notes has Make AI notes, through the usual confirm step."""
    client, conn, _ = env
    conn.execute("UPDATE conversation SET summary = NULL WHERE id = 1")
    assert client.post("/conversations/1/private").status_code in (404, 405)
    page = client.get("/conversations/1").text
    assert 'cv-sum' in page and "/conversations/1/rerun/confirm" in page and 'action="/conversations/1/rerun"' not in page
    assert "Summarize · ~$" not in page   # one Summarize per page: the button on top, not also in ⋯ (bug list)
    assert "Summarize this talk?" in client.get("/conversations/1/rerun/confirm").text
    r = client.post("/conversations/1/rerun", data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert jobs(conn) == [("ai_rerun", {"conversation_id": 1, "approved": True}, "pending")]


def test_rerun_job_calls_extract_with_approval(env, monkeypatch, tmp_path):
    client, conn, cfg = env
    calls = []
    monkeypatch.setattr(ai_jobs, "extract_conversation",
                        lambda c, cid, approved=False: calls.append((cid, approved)) or {"status": "done", "cost_usd": 0.04})
    ai_jobs.queue_rerun(conn, 1, approved=True)
    queue.run_all(conn, {ai_jobs.KIND: ai_jobs.PIPELINE}, tmp_path / "jobs")
    assert calls == [(1, True)]
    assert jobs(conn)[0][2] == "done"


def test_worker_registers_ai_rerun():
    assert worker.PIPELINES[ai_jobs.KIND] is ai_jobs.PIPELINE


def test_unapproved_run_creates_one_approval_item(env, monkeypatch, tmp_path):
    client, conn, _ = env
    monkeypatch.setattr(ai_jobs, "extract_conversation", lambda c, cid, approved=False: {
        "status": "pending_approval", "cost_usd": 0, "estimate": {"cost_usd": 0.05, "tokens_in": 1000}})
    for _ in range(2):
        ai_jobs.queue_rerun(conn, 1)
        queue.run_all(conn, {ai_jobs.KIND: ai_jobs.PIPELINE}, tmp_path / "jobs")
    assert one(conn, "SELECT count(*) FROM review_item WHERE kind = 'approval' AND status = 'open'") == 1
    assert "$0.05" in client.get("/review").text


def test_approving_cloud_request_queues_approved_run(env):
    client, conn, _ = env
    conn.execute("INSERT INTO review_item (kind, conversation_id, payload, created_at) VALUES ('approval', 1, ?, '2026-10-19')",
                 (json.dumps({"estimate": {"cost_usd": 0.05}}),))
    rid = one(conn, "SELECT id FROM review_item WHERE kind = 'approval'")
    assert "This will cost about" in client.post(f"/review/{rid}/approve").text   # confirm step first
    assert jobs(conn) == []
    client.post(f"/review/{rid}/approve", data={"confirm": "1"})
    assert one(conn, "SELECT status FROM review_item WHERE id = ?", rid) == "done"
    assert jobs(conn) == [("ai_rerun", {"conversation_id": 1, "approved": True}, "pending")]


def test_dismissing_cloud_request_runs_nothing(env):
    client, conn, _ = env
    conn.execute("INSERT INTO review_item (kind, conversation_id, created_at) VALUES ('approval', 1, '2026-10-19')")
    rid = one(conn, "SELECT id FROM review_item WHERE kind = 'approval'")
    client.post(f"/review/{rid}/dismiss")
    assert jobs(conn) == []


def test_add_account_never_takes_a_key(env):
    client, conn, _ = env
    page = client.post("/settings/accounts", data={"provider": "openai", "nickname": "Brian OpenAI", "owner": "Brian",
                                                   "type": "paid", "monthly_cap_usd": "5"}).text
    row = conn.execute("SELECT * FROM account WHERE nickname = 'Brian OpenAI'").fetchone()
    assert row["provider"] == "openai" and row["monthly_cap_usd"] == 5 and row["secret_ref"] == "scribe-bot.openai"
    assert "set_api_key.sh openai" in page
    bad = client.post("/settings/accounts", data={"provider": "google", "nickname": "x", "type": "nonsense"}).text
    assert "Couldn&#39;t add that account" in bad or "Couldn't add that account" in bad


def test_free_gemini_account_is_marked_may_train(env):
    client, conn, _ = env
    client.post("/settings/accounts", data={"provider": "google", "nickname": "Free Gemini", "type": "free"})
    assert one(conn, "SELECT may_train FROM account WHERE nickname = 'Free Gemini'") == 1


def test_profile_picker(env):
    client, conn, _ = env
    page = client.get("/settings/ai").text
    # R1 cards: only companies with a key are listed (the test env has none, so just the current model's card shows)
    assert 'value="all_claude"' in page and "Claude Sonnet" in page and "per meeting" in page
    assert "Only models with a connected key are listed" in page
    client.post("/settings/profile", data={"profile": "budget_mix"})
    assert one(conn, "SELECT value FROM setting WHERE key = 'ai_profile'") == "budget_mix"
    assert client.post("/settings/profile", data={"profile": "nope"}).status_code == 400


def test_offline_switch_uses_router(env):
    client, conn, _ = env
    client.post("/settings/offline", data={"on": 1})
    from scribe_bot.ai import router
    assert router.offline(conn)


def test_pause_toggle_banner_and_worker(env, monkeypatch, tmp_path):
    client, conn, cfg = env
    assert "Processing is paused" not in client.get("/").text
    client.post("/settings/pause", data={"on": 1})
    assert one(conn, "SELECT value FROM setting WHERE key = 'pipeline_paused'") == "1"
    assert "Processing is paused" in client.get("/").text
    assert "Resume" in client.get("/status").text and "Paused" in client.get("/settings/ai").text
    ran = []
    monkeypatch.setattr(worker, "PIPELINES", {"demo": [("only", lambda ctx: ran.append(1))]})
    monkeypatch.setattr(worker.sender, "send_pending", lambda c: {})  # never reach Discord/Keychain from a test
    queue.enqueue(conn, "demo")
    worker.tick(conn, cfg, logging.getLogger("test"))
    assert ran == []
    client.post("/settings/pause", data={"on": 0})
    assert "Processing is paused" not in client.get("/").text
    worker.tick(conn, cfg, logging.getLogger("test"))
    assert ran == [1]


def test_compare_button_confirm_queue_and_pick(env, monkeypatch):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert "Compare with Claude" in page and "/conversations/1/compare/confirm" in page
    r = client.post("/conversations/1/compare", follow_redirects=False)              # first tap only shows the cost
    assert r.headers["location"] == "/conversations/1/compare/confirm" and jobs(conn) == []
    assert "This will cost about" in client.get("/conversations/1/compare/confirm").text
    client.post("/conversations/1/compare", data={"confirm": "1"})
    client.post("/conversations/1/compare", data={"confirm": "1"})                    # double click: one job
    assert jobs(conn) == [("ai_compare", {"conversation_id": 1}, "pending")]
    assert "Its version appears here" in client.get("/conversations/1").text
    assert worker.PIPELINES[ai_jobs.COMPARE_KIND] is ai_jobs.COMPARE_PIPELINE
    # a finished compare: both shown side by side; keeping the current one throws the other away
    conn.execute("DELETE FROM job")
    data = {"title": None, "area": None, "area_confidence": None, "summary": "Other model summary", "decisions": [],
            "tasks": [{"title": "Other task", "owner": "Brian", "line": 0}], "waiting_on": [], "people_facts": []}
    conn.execute("INSERT INTO setting (key, value) VALUES ('compare:1', ?)",
                 (json.dumps({"by": "Brian personal:claude-haiku-5-5", "data": data, "cost_usd": 0.002, "at": "x"}),))
    assert "Compare side by side" in client.get("/conversations/1").text
    cmp = client.get("/conversations/1/compare").text
    assert "Other model summary" in cmp and "Other task" in cmp and "Claude Haiku" in cmp
    client.post("/conversations/1/compare/keep", data={"which": "current"})
    assert one(conn, "SELECT count(*) FROM setting WHERE key = 'compare:1'") == 0
    assert client.get("/conversations/1/compare", follow_redirects=False).status_code == 303
